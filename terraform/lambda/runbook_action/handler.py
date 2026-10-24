import base64
import datetime
import os
import re

import boto3
import botocore.session
import botocore.signers
import requests

EKS = boto3.client("eks")
SSM = boto3.client("ssm")

ACTIONS = ("degrade", "restart", "scale", "degrade_or_scale")
SCALING_ACTIONS = ("scale", "degrade_or_scale")

# Kubernetes DNS-1123 label: lowercase alphanumerics and '-', 63 chars max
DNS_1123_LABEL = re.compile(r"^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$")


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

def _k8s_request(method, url, token, ca_bytes, json_body=None, headers=None):
    headers = headers or {}
    headers.update({
        "Authorization": f"Bearer {token}",
        "Accept": "application/json"
    })
    # write CA to temp file
    ca_path = "/tmp/ca.crt"
    with open(ca_path, "wb") as f:
        f.write(ca_bytes)
    resp = requests.request(method, url, headers=headers, json=json_body, verify=ca_path, timeout=10)
    if resp.status_code >= 300:
        raise RuntimeError(f"K8s API {method} {url} failed: {resp.status_code} {resp.text[:500]}")
    if resp.text:
        return resp.json()
    return {}

def _patch_deployment_env(endpoint, token, ca, namespace, deployment, env_name, env_value):
    # Strategic merge patch to set env var (updates container env).
    # Assumes the container is named after the deployment, as in the checkout chart.
    url = f"{endpoint}/apis/apps/v1/namespaces/{namespace}/deployments/{deployment}"
    patch = {
        "spec": {
            "template": {
                "spec": {
                    "containers": [{
                        "name": deployment,
                        "env": [{
                            "name": env_name,
                            "value": str(env_value)
                        }]
                    }]
                }
            }
        }
    }
    headers = {"Content-Type": "application/strategic-merge-patch+json"}
    return _k8s_request("PATCH", url, token, ca, json_body=patch, headers=headers)

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
    headers = {"Content-Type": "application/merge-patch+json"}
    return _k8s_request("PATCH", url, token, ca, json_body=patch, headers=headers)

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
    """Checks the request before anything touches AWS or the cluster."""
    action = event.get("action")
    if action not in ACTIONS:
        raise InvalidRequest(f"Unknown action {action!r}; expected one of {', '.join(ACTIONS)}")

    namespace = event.get("namespace")
    deployment = event.get("deployment")
    if not namespace or not deployment:
        raise InvalidRequest("namespace and deployment are required")

    allowed = [n.strip() for n in get_env("ALLOWED_NAMESPACES", "apps").split(",") if n.strip()]
    if namespace not in allowed:
        raise InvalidRequest(f"Namespace {namespace!r} isn't in ALLOWED_NAMESPACES ({', '.join(allowed)})")

    if not isinstance(deployment, str) or not DNS_1123_LABEL.match(deployment):
        raise InvalidRequest(f"Deployment {deployment!r} isn't a valid Kubernetes name")

    replicas = _replicas(event.get("replicas")) if action in SCALING_ACTIONS else None
    return action, namespace, deployment, replicas

def lambda_handler(event, context):
    """
    Input comes from the runbook state machine's ExecuteAction step:
    {incident_id, alertname, severity, action, namespace, deployment, replicas}.
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
        "namespace": namespace,
        "deployment": deployment,
    }

    endpoint, ca = _cluster_conn(cluster)
    token = _eks_bearer_token(cluster, region)

    if action in ("degrade", "degrade_or_scale"):
        # degrade first for customer relief; SSM also keeps the state for audit/visibility
        SSM.put_parameter(Name=degraded_param, Value="true", Type="String", Overwrite=True)
        _patch_deployment_env(endpoint, token, ca, namespace, deployment, "DEGRADED_MODE", "true")
        _restart_deployment(endpoint, token, ca, namespace, deployment)
        result["degraded"] = True

    if action == "restart":
        _restart_deployment(endpoint, token, ca, namespace, deployment)

    if action in SCALING_ACTIONS:
        _scale_deployment(endpoint, token, ca, namespace, deployment, replicas)
        result["scaled_to"] = replicas

    return result
