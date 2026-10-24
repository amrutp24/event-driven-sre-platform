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
    monkeypatch.setenv("DEGRADED_PARAM", "/checkout/degraded_mode")
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
    }
