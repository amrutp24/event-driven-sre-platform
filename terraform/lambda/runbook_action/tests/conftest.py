"""
Test configuration and fixtures for runbook_action Lambda
"""
import os
import sys

import pytest

# Add the parent directory to import the handlers
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture(autouse=True)
def lambda_env(monkeypatch):
    """The Lambda's environment as Terraform sets it. Tests override pieces with monkeypatch."""
    monkeypatch.setenv("REGION", "us-east-1")
    monkeypatch.setenv("CLUSTER_NAME", "test-cluster")
    monkeypatch.setenv("ALLOWED_NAMESPACES", "apps")
    monkeypatch.setenv("DEGRADED_DEPLOYMENT", "checkout")
    monkeypatch.setenv("DEGRADED_PARAM", "/checkout/degraded_mode")
    monkeypatch.setenv("DRAINABLE_NODE_LABEL", "nodegroup=apps")
    monkeypatch.setenv("MIN_SCHEDULABLE_NODES", "1")
    monkeypatch.delenv("MAX_REPLICAS", raising=False)


@pytest.fixture
def runbook_payload():
    """What the runbook state machine's ExecuteAction step sends"""
    return {
        "incident_id": "test-incident-123",
        "alertname": "CheckoutHighErrorRate",
        "severity": "critical",
        "action": "degrade_or_scale",
        "namespace": "apps",
        "deployment": "checkout",
        "replicas": 4,
        "node": "",
    }


@pytest.fixture
def drain_payload():
    """A NodeUnderPressure alert comes from kube-state-metrics, so its labels
    carry the exporter's namespace and the 'cluster' service"""
    return {
        "incident_id": "test-incident-456",
        "alertname": "NodeUnderPressure",
        "severity": "critical",
        "action": "drain_node",
        "namespace": "monitoring",
        "deployment": "cluster",
        "replicas": 4,
        "node": "ip-10-0-1-23.ec2.internal",
    }
