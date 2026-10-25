"""
The pieces have to agree on field names: alert_ingest's event detail is the
runbook's input, and the runbook's ExecuteAction payload is runbook_action's
input. These run the real handlers on both ends.
"""
import pytest
from asl_sim import Simulator, TaskError, load
from conftest import RUNBOOK_VARS
from test_runbook import STILL_FIRING, lambda_ok, resolved


def alertmanager(status="firing", alertname="CheckoutHighErrorRate", labels=None, annotations=None):
    return {
        "version": "4",
        "status": status,
        "receiver": "aws-webhook",
        "alerts": [{
            "status": status,
            "labels": {"alertname": alertname, "severity": "critical", "service": "checkout", **(labels or {})},
            "annotations": {"summary": "Checkout 5xx rate > 5% (5m)", **(annotations or {})},
            "startsAt": "2026-10-22T09:55:00Z",
            "endsAt": "2026-10-22T10:04:00Z" if status == "resolved" else "0001-01-01T00:00:00Z",
            "fingerprint": "a1b2c3d4e5f60718",
        }],
    }


def datadog(transition="Triggered", tags="service:checkout,alertname:CheckoutHighLatencyP95"):
    return {
        "body": "p95 latency over 1s on checkout",
        "title": "CheckoutHighLatencyP95 on checkout",
        "priority": "P1",
        "tags": tags,
        "date_happened": 1792663100,
        "alert_id": "12345678",
        "alert_transition": transition,
        "alert_cycle_key": "987654321",
        "event_url": "https://app.datadoghq.com/monitors/12345678",
    }


def validating(runbook_action):
    """A Lambda mock that runs runbook_action's own input validation."""
    def invoke(arguments, call):
        try:
            runbook_action._validate(arguments["Payload"])
        except runbook_action.InvalidRequest as exc:
            raise TaskError("InvalidRequest", str(exc)) from exc
        return lambda_ok(arguments, call)
    return invoke


def run_runbook(detail, runbook_action, variables=RUNBOOK_VARS, **tasks):
    tasks.setdefault("ExecuteAction", validating(runbook_action))
    tasks.setdefault("VerifyResolution", [STILL_FIRING, resolved()])
    return Simulator(load("runbook.asl.json", variables), tasks).run(detail)


def test_alertmanager_alert_runs_end_to_end(ingest, runbook_action):
    [(detail_type, source, detail)] = ingest(alertmanager())
    assert (detail_type, source) == ("incident.created", "sre.alertmanager")

    result = run_runbook(detail, runbook_action)
    assert result.end_state == "AutoResolved"
    assert result.calls_to("LogIncident")[0].arguments["Key"]["incident_id"]["S"] == detail["incident_id"]


def test_datadog_alert_with_kube_tags_runs_end_to_end(ingest, runbook_action):
    tags = "service:checkout,alertname:CheckoutHighLatencyP95,kube_namespace:apps,kube_deployment:checkout"
    [(_, source, detail)] = ingest(datadog(tags=tags))
    assert source == "sre.datadog"

    result = run_runbook(detail, runbook_action)
    assert result.end_state == "AutoResolved"
    sent = result.calls_to("ExecuteAction")[0].arguments["Payload"]
    assert (sent["namespace"], sent["deployment"]) == ("apps", "checkout")


def test_desired_replicas_annotation_passes_validation(ingest, runbook_action):
    [(_, _, detail)] = ingest(alertmanager(annotations={"desired_replicas": "6"}))
    result = run_runbook(detail, runbook_action)
    assert result.calls_to("ExecuteAction")[0].arguments["Payload"]["replicas"] == "6"
    assert result.end_state == "AutoResolved"


@pytest.mark.parametrize("labels, annotations, why", [
    ({"namespace": "monitoring"}, {}, "monitoring"),
    ({"deployment": "Checkout_API"}, {}, "Checkout_API"),
    ({}, {"desired_replicas": "500"}, "500"),
])
def test_targets_the_lambda_rejects_escalate_without_retries(ingest, runbook_action, labels, annotations, why):
    # e.g. CheckoutDown fires on `up == 0`, which carries whatever namespace the target is in
    [(_, _, detail)] = ingest(alertmanager(labels=labels, annotations=annotations))
    result = run_runbook(detail, runbook_action)

    call = result.calls_to("ExecuteAction")[0]
    assert (call.error, call.retries) == ("InvalidRequest", 0)
    assert result.end_state == "Escalated"
    failed = result.events("action.completed")[0]["Detail"]
    assert why in failed["cause"]


NODE = "ip-10-0-1-23.ec2.internal"

# kube_node_status_condition comes from kube-state-metrics, so the series
# carries its namespace and service, not the node's
KSM_LABELS = {"condition": "MemoryPressure", "status": "true", "namespace": "monitoring",
              "service": "mon-kube-state-metrics", "job": "kube-state-metrics"}


def test_node_under_pressure_from_alertmanager_drains_the_node(ingest, runbook_action):
    [(_, _, detail)] = ingest(alertmanager(alertname="NodeUnderPressure", labels={"node": NODE, **KSM_LABELS}))
    result = run_runbook(detail, runbook_action)

    sent = result.calls_to("ExecuteAction")[0].arguments["Payload"]
    assert (sent["action"], sent["node"]) == ("drain_node", NODE)
    assert result.calls_to("ExecuteAction")[0].error is None
    assert result.end_state == "AutoResolved"


def test_node_under_pressure_from_datadog_uses_the_host_tag(ingest, runbook_action):
    [(_, _, detail)] = ingest(datadog(tags=f"alertname:NodeUnderPressure,host:{NODE},kube_cluster_name:sre"))
    result = run_runbook(detail, runbook_action)

    assert result.calls_to("ExecuteAction")[0].arguments["Payload"]["node"] == NODE
    assert result.end_state == "AutoResolved"


def test_drain_without_a_node_is_rejected_and_escalated(ingest, runbook_action):
    [(_, _, detail)] = ingest(alertmanager(alertname="NodeUnderPressure", labels=KSM_LABELS))
    result = run_runbook(detail, runbook_action)

    call = result.calls_to("ExecuteAction")[0]
    assert call.arguments["Payload"]["node"] == ""
    assert (call.error, call.retries) == ("InvalidRequest", 0)
    assert result.cause == "drain_node failed: InvalidRequest"
    cause = result.events("action.completed")[0]["Detail"]["cause"].lower()
    # Rejected for the missing node, not because the Lambda doesn't know the action
    assert "node" in cause and "unknown action" not in cause
    assert result.end_state == "Escalated"


def test_resolved_alerts_dont_start_remediation(ingest, runbook_action):
    # The incident.resolved rule doesn't target the runbook, but the input
    # would be ignored anyway
    [(detail_type, _, detail)] = ingest(alertmanager(status="resolved"))
    assert detail_type == "incident.resolved"
    assert run_runbook(detail, runbook_action).end_state == "NotAutoRemediated"


def test_runbook_payload_has_every_field_the_lambda_reads(runbook_action):
    from conftest import incident
    result = run_runbook(incident(), runbook_action)
    sent = result.calls_to("ExecuteAction")[0].arguments["Payload"]
    assert set(sent) == {"incident_id", "alertname", "severity", "action", "namespace", "deployment",
                         "replicas", "node"}
    runbook_action._validate(sent)  # raises InvalidRequest if anything's off


# ---------------------------------------------------------------- module defaults

# No routes, no default deployment, and runbook_action without degraded mode or drain_node
MODULE_DEFAULTS = {**RUNBOOK_VARS, "routes": "{}", "default_deployment": ""}


@pytest.fixture
def module_defaults_env(monkeypatch):
    for name in ("DEGRADED_DEPLOYMENT", "DEGRADED_PARAM", "DRAINABLE_NODE_LABEL"):
        monkeypatch.delenv(name)


@pytest.mark.parametrize("labels, annotations, why", [
    ({"service": "unknown"}, {"runbook_action": "restart"}, "namespace and deployment are required"),
    ({}, {"runbook_action": "degrade"}, "degraded mode isn't configured"),
    ({"node": NODE}, {"runbook_action": "drain_node"}, "drain_node isn't configured"),
])
def test_what_the_defaults_leave_off_escalates(ingest, runbook_action, module_defaults_env, labels, annotations, why):
    [(_, _, detail)] = ingest(alertmanager(alertname="AppDown", labels=labels, annotations=annotations))
    result = run_runbook(detail, runbook_action, variables=MODULE_DEFAULTS)

    call = result.calls_to("ExecuteAction")[0]
    assert (call.error, call.retries) == ("InvalidRequest", 0)
    assert why in result.events("action.completed")[0]["Detail"]["cause"]
    assert result.end_state == "Escalated"
