import base64
import datetime
import functools
import json
import os
import re
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request

import boto3
import botocore.session
import botocore.signers

EKS = boto3.client("eks")
SSM = boto3.client("ssm")

ACTIONS = ("degrade", "restart", "scale", "degrade_or_scale", "drain_node")
DEGRADE_ACTIONS = ("degrade", "degrade_or_scale")
SCALING_ACTIONS = ("scale", "degrade_or_scale")

# Kubernetes DNS-1123 label: lowercase alphanumerics and '-', 63 chars max
DNS_1123_LABEL = re.compile(r"^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$")
# DNS-1123 subdomain (node names): dot-separated labels, 253 chars max
DNS_1123_SUBDOMAIN = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?(\.[a-z0-9]([-a-z0-9]*[a-z0-9])?)*$")

# drain_node retries evictions a PodDisruptionBudget blocks, for up to a minute
EVICTION_RETRY_SECONDS = 5
EVICTION_RETRY_LIMIT_SECONDS = 60
MERGE_PATCH = {"Content-Type": "application/merge-patch+json"}


class InvalidRequest(Exception):
    """Bad input from the state machine. Step Functions sees the class name as
    the error, and the runbook doesn't retry it."""


# Env defaults
def get_env(name, default=None):
    return os.environ.get(name, default)

def _eks_bearer_token(cluster_name: str, region: str) -> str:
    """Generate an EKS authentication token (k8s-aws-v1) using a presigned STS GetCallerIdentity URL."""
    session = botocore.session.get_session()
    sts = session.create_client("sts", region_name=region)
    request_signer = botocore.signers.RequestSigner(
        sts.meta.service_model.service_id,
        region,
        "sts",
        "v4",
        session.get_credentials(),
        session.get_component("event_emitter"),
    )

    params = {
        "method": "GET",
        "url": "https://sts.amazonaws.com/?Action=GetCallerIdentity&Version=2011-06-15",
        "body": {},
        "headers": {"x-k8s-aws-id": cluster_name},
        "context": {},
    }
    presigned_url = request_signer.generate_presigned_url(
        request_dict=params, expires_in=60, operation_name=""
    )
    token = "k8s-aws-v1." + base64.urlsafe_b64encode(presigned_url.encode("utf-8")).decode("utf-8").rstrip("=")
    return token

def _cluster_conn(cluster_name: str):
    desc = EKS.describe_cluster(name=cluster_name)["cluster"]
    endpoint = desc["endpoint"]
    ca = base64.b64decode(desc["certificateAuthority"]["data"])
    return endpoint, ca

class K8sResponse:
    """What _k8s_send returns: the status code and the body as text."""

    def __init__(self, status_code, text):
        self.status_code = status_code
        self.text = text


@functools.lru_cache(maxsize=4)
def _ssl_context(ca_bytes):
    """Trusts only the cluster's CA. The CA from describe_cluster is PEM."""
    return ssl.create_default_context(cadata=ca_bytes.decode("ascii"))

def _k8s_send(method, url, token, ca_bytes, json_body=None, headers=None):
    """Sends one request and returns the response, whatever its status."""
    headers = dict(headers or {})
    headers.update({
        "Authorization": f"Bearer {token}",
        "Accept": "application/json"
    })
    data = None
    if json_body is not None:
        data = json.dumps(json_body).encode("utf-8")
        headers.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10, context=_ssl_context(ca_bytes)) as resp:
            return K8sResponse(resp.status, resp.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as err:
        # 4xx and 5xx land here; the callers decide what each one means
        with err:
            return K8sResponse(err.code, err.read().decode("utf-8", errors="replace"))

def _k8s_body(resp, method, url):
    if resp.status_code >= 300:
        raise RuntimeError(f"K8s API {method} {url} failed: {resp.status_code} {resp.text[:500]}")
    if resp.text:
        return json.loads(resp.text)
    return {}

def _k8s_request(method, url, token, ca_bytes, json_body=None, headers=None):
    resp = _k8s_send(method, url, token, ca_bytes, json_body=json_body, headers=headers)
    return _k8s_body(resp, method, url)

def _k8s_get_if_exists(url, token, ca_bytes):
    """GET that returns None on 404 instead of raising."""
    resp = _k8s_send("GET", url, token, ca_bytes)
    if resp.status_code == 404:
        return None
    return _k8s_body(resp, "GET", url)

def _restart_deployment(endpoint, token, ca, namespace, deployment):
    url = f"{endpoint}/apis/apps/v1/namespaces/{namespace}/deployments/{deployment}"
    # Changing this annotation is what makes Kubernetes roll the pods, like kubectl rollout restart
    restarted_at = datetime.datetime.now(datetime.UTC).isoformat().replace("+00:00", "Z")
    patch = {
        "spec": {
            "template": {
                "metadata": {
                    "annotations": {
                        "kubectl.kubernetes.io/restartedAt": restarted_at
                    }
                }
            }
        }
    }
    headers = {"Content-Type": "application/strategic-merge-patch+json"}
    return _k8s_request("PATCH", url, token, ca, json_body=patch, headers=headers)

def _scale_deployment(endpoint, token, ca, namespace, deployment, replicas):
    url = f"{endpoint}/apis/apps/v1/namespaces/{namespace}/deployments/{deployment}"
    patch = {"spec": {"replicas": int(replicas)}}
    return _k8s_request("PATCH", url, token, ca, json_body=patch, headers=MERGE_PATCH)

def _scale(endpoint, token, ca, namespace, deployment, replicas):
    """An HPA would undo a replica patch within seconds, so when the deployment
    has one, raise its floor instead. Returns how it scaled: "hpa" or "deployment"."""
    url = f"{endpoint}/apis/autoscaling/v2/namespaces/{namespace}/horizontalpodautoscalers/{deployment}"
    hpa = _k8s_get_if_exists(url, token, ca)
    if hpa is None:
        _scale_deployment(endpoint, token, ca, namespace, deployment, replicas)
        return "deployment"

    spec = hpa.get("spec", {})
    # minReplicas defaults to 1 when the HPA doesn't set it; never lower it
    patch = {"minReplicas": max(spec.get("minReplicas", 1), replicas)}
    if replicas > spec.get("maxReplicas", 0):
        patch["maxReplicas"] = replicas
    _k8s_request("PATCH", url, token, ca, json_body={"spec": patch}, headers=MERGE_PATCH)
    return "hpa"

def _drainable_label():
    """DRAINABLE_NODE_LABEL is key=value, or just a key that has to be present."""
    key, sep, value = get_env("DRAINABLE_NODE_LABEL", "nodegroup=apps").partition("=")
    return key, (value if sep else None)

def _ready_and_schedulable(node):
    if node.get("spec", {}).get("unschedulable"):
        return False
    conditions = node.get("status", {}).get("conditions", [])
    return any(c.get("type") == "Ready" and c.get("status") == "True" for c in conditions)

def _skip_on_drain(pod):
    """The pods kubectl drain leaves alone."""
    metadata = pod.get("metadata", {})
    if any(ref.get("kind") == "DaemonSet" for ref in metadata.get("ownerReferences", [])):
        return True
    if "kubernetes.io/config.mirror" in metadata.get("annotations", {}):
        return True
    if metadata.get("deletionTimestamp"):
        return True
    return pod.get("status", {}).get("phase") in ("Succeeded", "Failed")

def _evict_pod(endpoint, token, ca, namespace, name):
    """Returns "evicted", "gone" (404) or "blocked" (429, a PodDisruptionBudget said no)."""
    url = f"{endpoint}/api/v1/namespaces/{namespace}/pods/{name}/eviction"
    eviction = {
        "apiVersion": "policy/v1",
        "kind": "Eviction",
        "metadata": {"name": name, "namespace": namespace},
    }
    resp = _k8s_send("POST", url, token, ca, json_body=eviction, headers={"Content-Type": "application/json"})
    if resp.status_code == 404:
        return "gone"
    if resp.status_code == 429:
        return "blocked"
    _k8s_body(resp, "POST", url)
    return "evicted"

def _drain_node(endpoint, token, ca, node, sleep=None):
    """Cordons the node and evicts its pods like kubectl drain. The node stays
    cordoned; replacing or uncordoning it is a person's job."""
    sleep = sleep or time.sleep
    node_url = f"{endpoint}/api/v1/nodes/{node}"
    found = _k8s_get_if_exists(node_url, token, ca)
    if found is None:
        raise InvalidRequest(f"Node {node!r} doesn't exist")

    # Only app nodes. Anything else, system nodes included, needs a person.
    key, value = _drainable_label()
    selector = key if value is None else f"{key}={value}"
    labels = found.get("metadata", {}).get("labels", {})
    if key not in labels or (value is not None and labels[key] != value):
        raise InvalidRequest(f"Node {node} doesn't have the {selector} label, so it isn't drained automatically")

    # Somewhere for the pods to go
    query = urllib.parse.urlencode({"labelSelector": selector})
    nodes = _k8s_request("GET", f"{endpoint}/api/v1/nodes?{query}", token, ca)
    others = [n for n in nodes.get("items", [])
              if n.get("metadata", {}).get("name") != node and _ready_and_schedulable(n)]
    minimum = int(get_env("MIN_SCHEDULABLE_NODES", "1"))
    if len(others) < minimum:
        raise InvalidRequest(f"Only {len(others)} other Ready, schedulable {selector} node(s); "
                             f"draining {node} needs {minimum}")

    _k8s_request("PATCH", node_url, token, ca, json_body={"spec": {"unschedulable": True}}, headers=MERGE_PATCH)

    query = urllib.parse.urlencode({"fieldSelector": f"spec.nodeName={node}"})
    pods = _k8s_request("GET", f"{endpoint}/api/v1/pods?{query}", token, ca).get("items", [])
    to_move = [p for p in pods if not _skip_on_drain(p)]
    skipped = len(pods) - len(to_move)
    # A pod with no owner wouldn't be recreated anywhere, so it stays put and
    # gets reported, like kubectl drain without --force
    unmanaged = [f"{p['metadata']['namespace']}/{p['metadata']['name']}"
                 for p in to_move if not p["metadata"].get("ownerReferences")]
    pending = [(p["metadata"]["namespace"], p["metadata"]["name"])
               for p in to_move if p["metadata"].get("ownerReferences")]

    # A pod that's already gone (404) counts as evicted: it's off the node either way
    evicted = 0
    waited = 0
    while True:
        blocked = []
        for namespace, name in pending:
            if _evict_pod(endpoint, token, ca, namespace, name) == "blocked":
                blocked.append((namespace, name))
            else:
                evicted += 1
        pending = blocked
        if not pending or waited >= EVICTION_RETRY_LIMIT_SECONDS:
            break
        sleep(EVICTION_RETRY_SECONDS)
        waited += EVICTION_RETRY_SECONDS

    # Pods still blocked don't fail the action; the runbook's verify step
    # decides whether the alert cleared
    return {
        "cordoned": True,
        "evicted": evicted,
        "skipped": skipped,
        "unmanaged": unmanaged,
        "still_blocked": [f"{namespace}/{name}" for namespace, name in pending],
    }

def _replicas(raw):
    """replicas arrives as a number, or as a string when it comes from an alert annotation."""
    max_replicas = int(get_env("MAX_REPLICAS", "10"))
    count = None
    if isinstance(raw, int) and not isinstance(raw, bool):
        count = raw
    elif isinstance(raw, float) and raw.is_integer():
        count = int(raw)
    elif isinstance(raw, str) and raw.strip().isascii() and raw.strip().isdigit():
        count = int(raw.strip())
    if count is None or not 1 <= count <= max_replicas:
        raise InvalidRequest(f"replicas must be a whole number from 1 to {max_replicas}, got {raw!r}")
    return count

def _validate(event):
    """Checks the request before anything touches AWS or the cluster.
    Returns (action, namespace, deployment, replicas); drain_node only uses
    the event's node, so the other three are None for it."""
    action = event.get("action")
    if action not in ACTIONS:
        raise InvalidRequest(f"Unknown action {action!r}; expected one of {', '.join(ACTIONS)}")

    if action == "drain_node":
        # namespace and deployment are ignored here: a node alert from
        # kube-state-metrics carries the exporter's namespace, not a target
        node = event.get("node")
        if not node:
            raise InvalidRequest("node is required for drain_node")
        if not isinstance(node, str) or len(node) > 253 or not DNS_1123_SUBDOMAIN.match(node):
            raise InvalidRequest(f"Node {node!r} isn't a valid Kubernetes node name")
        return action, None, None, None

    namespace = event.get("namespace")
    deployment = event.get("deployment")
    if not namespace or not deployment:
        raise InvalidRequest("namespace and deployment are required")

    allowed = [n.strip() for n in get_env("ALLOWED_NAMESPACES", "apps").split(",") if n.strip()]
    if namespace not in allowed:
        raise InvalidRequest(f"Namespace {namespace!r} isn't in ALLOWED_NAMESPACES ({', '.join(allowed)})")

    if not isinstance(deployment, str) or not DNS_1123_LABEL.match(deployment):
        raise InvalidRequest(f"Deployment {deployment!r} isn't a valid Kubernetes name")

    degraded_deployment = get_env("DEGRADED_DEPLOYMENT", "checkout")
    if action in DEGRADE_ACTIONS and deployment != degraded_deployment:
        raise InvalidRequest(f"degraded mode is only wired up for {degraded_deployment}")

    replicas = _replicas(event.get("replicas")) if action in SCALING_ACTIONS else None
    return action, namespace, deployment, replicas

def lambda_handler(event, context):
    """
    Input comes from the runbook state machine's ExecuteAction step:
    {incident_id, alertname, severity, action, namespace, deployment, replicas, node}.
    The state machine picks the action and the target; this carries it out.
    """
    # Read environment variables
    region = get_env("REGION") or get_env("AWS_REGION") or "us-east-1"
    cluster = get_env("CLUSTER_NAME")
    if not cluster:
        raise RuntimeError("CLUSTER_NAME env var is required")
    degraded_param = get_env("DEGRADED_PARAM", "/checkout/degraded_mode")

    action, namespace, deployment, replicas = _validate(event)

    result = {
        "incident_id": event.get("incident_id"),
        "action": action,
        "alertname": event.get("alertname"),
        "severity": event.get("severity"),
    }

    if action == "drain_node":
        result["node"] = event["node"]
    else:
        result.update(namespace=namespace, deployment=deployment)

    if action in DEGRADE_ACTIONS:
        # The app reads this on its request path (cached about 15 s), so no restart.
        # Nothing turns it back off when the incident resolves; that's a person's call.
        SSM.put_parameter(Name=degraded_param, Value="true", Type="String", Overwrite=True)
        result["degraded"] = True

    if action == "degrade":
        return result

    endpoint, ca = _cluster_conn(cluster)
    token = _eks_bearer_token(cluster, region)

    if action == "drain_node":
        result.update(_drain_node(endpoint, token, ca, event["node"]))

    if action == "restart":
        _restart_deployment(endpoint, token, ca, namespace, deployment)

    if action in SCALING_ACTIONS:
        result["scaled_via"] = _scale(endpoint, token, ca, namespace, deployment, replicas)
        result["scaled_to"] = replicas

    return result
