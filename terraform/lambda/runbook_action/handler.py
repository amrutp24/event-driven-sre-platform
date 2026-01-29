import base64
import datetime
import json
import os
import urllib.parse

import boto3
import botocore
import requests

EKS = boto3.client("eks")
SSM = boto3.client("ssm")

# Env
REGION = os.environ.get("REGION")
CLUSTER_NAME = os.environ.get("CLUSTER_NAME")
TARGET_NAMESPACE = os.environ.get("TARGET_NAMESPACE", "apps")
TARGET_DEPLOYMENT = os.environ.get("TARGET_DEPLOYMENT", "checkout")
DEGRADED_PARAM = os.environ.get("DEGRADED_PARAM", "/checkout/degraded_mode")

def _eks_bearer_token(cluster_name: str, region: str) -> str:
    """Generate an EKS authentication token (k8s-aws-v1) using a presigned STS GetCallerIdentity URL."""
    session = botocore.session.get_session()
    sts = session.create_client("sts", region_name=region)
    request_signer = botocore.signers.RequestSigner(
        sts.meta.service_model.service_name,
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
    # Strategic merge patch to set env var (updates container env)
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
    restarted_at = datetime.datetime.utcnow().isoformat() + "Z"
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

def lambda_handler(event, context):
    """
    Input is the normalized alert from alert_ingest (incident_id, alertname, severity, annotations, labels).
    Actions are derived from alertname + severity by default; can be overridden with annotations.runbook_action.
    """
    region = REGION or os.environ.get("AWS_REGION") or "us-east-1"
    cluster = CLUSTER_NAME
    if not cluster:
        raise RuntimeError("CLUSTER_NAME env var is required")

    endpoint, ca = _cluster_conn(cluster)
    token = _eks_bearer_token(cluster, region)

    alertname = event.get("alertname", "UnknownAlert")
    severity = event.get("severity", "ticket")
    annotations = event.get("annotations", {}) or {}
    explicit = annotations.get("runbook_action")

    # default routing
    action = explicit or (
        "degrade_or_scale" if alertname in ("CheckoutHighLatencyP95", "CheckoutHighErrorRate", "CheckoutSLOBurnFast") else
        "restart" if alertname in ("CheckoutDown",) else
        "notify_only"
    )

    result = {"action": action, "alertname": alertname, "severity": severity}

    if action == "notify_only":
        return result

    if action == "degrade":
        # also store state in SSM for audit/visibility
        SSM.put_parameter(Name=DEGRADED_PARAM, Value="true", Type="String", Overwrite=True)
        _patch_deployment_env(endpoint, token, ca, TARGET_NAMESPACE, TARGET_DEPLOYMENT, "DEGRADED_MODE", "true")
        _restart_deployment(endpoint, token, ca, TARGET_NAMESPACE, TARGET_DEPLOYMENT)
        result["degraded"] = True
        return result

    if action == "restart":
        _restart_deployment(endpoint, token, ca, TARGET_NAMESPACE, TARGET_DEPLOYMENT)
        return result

    if action == "scale":
        # scale to 4 by default, or derive from labels/annotations
        replicas = int(annotations.get("desired_replicas", "4"))
        _scale_deployment(endpoint, token, ca, TARGET_NAMESPACE, TARGET_DEPLOYMENT, replicas)
        return result

    if action == "degrade_or_scale":
        # degrade first for customer relief, then scale up
        SSM.put_parameter(Name=DEGRADED_PARAM, Value="true", Type="String", Overwrite=True)
        _patch_deployment_env(endpoint, token, ca, TARGET_NAMESPACE, TARGET_DEPLOYMENT, "DEGRADED_MODE", "true")
        _restart_deployment(endpoint, token, ca, TARGET_NAMESPACE, TARGET_DEPLOYMENT)
        _scale_deployment(endpoint, token, ca, TARGET_NAMESPACE, TARGET_DEPLOYMENT, int(annotations.get("desired_replicas", "4")))
        result["degraded"] = True
        result["scaled_to"] = int(annotations.get("desired_replicas", "4"))
        return result

    raise RuntimeError(f"Unknown action: {action}")
