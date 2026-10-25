"""
Fixtures for the state machine tests. Template values match what Terraform
passes for examples/complete (the checkout demo), which sets the routes and
targets; the timings are the module's defaults.
"""
import importlib.util
import json
import os
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from asl_sim import MODULE_DIR, load

TABLE = "event-driven-sre-incidents"
BUS = "event-driven-sre-bus"
RUNBOOK_ACTION_ARN = "arn:aws:lambda:us-east-1:123456789012:function:event-driven-sre-runbook-action"

# examples/complete's runbook_routes; Terraform passes it through jsonencode
ROUTES = {
    "CheckoutHighErrorRate": {"action": "degrade_or_scale"},
    "CheckoutHighLatencyP95": {"action": "degrade_or_scale"},
    "CheckoutSLOBurnFast": {"action": "degrade_or_scale"},
    "CheckoutDown": {"action": "restart"},
    "NodeUnderPressure": {"action": "drain_node"},
}

RUNBOOK_VARS = {
    "partition": "aws",
    "table_name": TABLE,
    "runbook_action_arn": RUNBOOK_ACTION_ARN,
    "event_bus_name": BUS,
    "auto_remediate_severities": "'critical'",
    "routes": json.dumps(ROUTES),
    "default_namespace": "apps",
    "default_deployment": "checkout",
    "default_replicas": 4,
    "stabilization_seconds": 60,
    "verify_poll_seconds": 30,
    # floor((600 - 60) / 30) + 1
    "verify_attempts": 19,
}

RECORDER_VARS = {
    "partition": "aws",
    "table_name": TABLE,
    "ttl_seconds": 7776000,
}

TEMPLATES = {
    "runbook.asl.json": RUNBOOK_VARS,
    "incident_recorder.asl.json": RECORDER_VARS,
}


def incident(**overrides):
    """An incident.created detail, shaped like alert_ingest's normalized record."""
    record = {
        "incident_id": "0b6f3c1e-8d2a-5f4b-9c7e-2a1d3e4f5a6b",
        "source": "alertmanager",
        "status": "firing",
        "severity": "critical",
        "service": "checkout",
        "alertname": "CheckoutHighErrorRate",
        "labels": {
            "alertname": "CheckoutHighErrorRate",
            "severity": "critical",
            "service": "checkout",
        },
        "annotations": {
            "summary": "Checkout 5xx rate > 5% (5m)",
            "runbook": "stepfunctions:RunbookCheckoutDegradeOrRollback",
        },
        "fingerprint": "a1b2c3d4e5f60718",
        "starts_at": "2026-10-22T09:55:00Z",
        "ends_at": None,
        "timestamp": 1792663200,
        "received_at": "2026-10-22T10:00:00Z",
        "correlation_id": "5d1c2b3a-4e5f-4a6b-8c7d-9e0f1a2b3c4d",
        "account_id": "123456789012",
        "region": "us-east-1",
    }
    record.update(overrides)
    return record


# runbook_action's environment as Terraform sets it for examples/complete
RUNBOOK_ACTION_ENV = {
    "ALLOWED_NAMESPACES": "apps",
    "DEGRADED_DEPLOYMENT": "checkout",
    "DEGRADED_PARAM": "/checkout/degraded_mode",
    "DRAINABLE_NODE_LABEL": "nodegroup=apps",
}


@pytest.fixture(autouse=True)
def runbook_action_env(monkeypatch):
    for name, value in RUNBOOK_ACTION_ENV.items():
        monkeypatch.setenv(name, value)


@pytest.fixture
def runbook():
    return load("runbook.asl.json", RUNBOOK_VARS)


@pytest.fixture
def recorder():
    return load("incident_recorder.asl.json", RECORDER_VARS)


def _import_lambda(name, env):
    """Imports lambda/<name>/handler.py under its own module name,
    with boto3 clients stubbed, so both handlers can load in one session."""
    path = MODULE_DIR / "lambda" / name / "handler.py"
    spec = importlib.util.spec_from_file_location(f"{name}_handler", path)
    module = importlib.util.module_from_spec(spec)
    with patch.dict(os.environ, env), patch("boto3.client"):
        spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="session")
def alert_ingest():
    return _import_lambda("alert_ingest", {"EVENT_BUS_NAME": BUS, "AWS_DEFAULT_REGION": "us-east-1"})


@pytest.fixture(scope="session")
def runbook_action():
    return _import_lambda("runbook_action", {"AWS_DEFAULT_REGION": "us-east-1"})


@pytest.fixture
def ingest(alert_ingest, monkeypatch):
    """Runs alert_ingest on a webhook payload and returns the event details it
    put on the bus, exactly as EventBridge would pass them on."""
    monkeypatch.setenv("AWS_REGION", "us-east-1")

    def _ingest(payload):
        client = MagicMock()
        client.put_events.return_value = {"FailedEntryCount": 0, "Entries": []}
        context = SimpleNamespace(
            invoked_function_arn="arn:aws:lambda:us-east-1:123456789012:function:event-driven-sre-alert-ingest")
        with patch.object(alert_ingest, "eventbridge", client):
            response = alert_ingest.lambda_handler({"body": json.dumps(payload)}, context)
        assert response["statusCode"] == 202, response
        entries = [e for call in client.put_events.call_args_list for e in call.kwargs["Entries"]]
        return [(e["DetailType"], e["Source"], json.loads(e["Detail"])) for e in entries]

    return _ingest
