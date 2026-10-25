"""
Scenario tests for the runbook state machine, run through the simulator with
mocked DynamoDB, Lambda and EventBridge results.
"""
import pytest
from asl_sim import Simulator, TaskError, is_expression, load
from conftest import BUS, RUNBOOK_ACTION_ARN, RUNBOOK_VARS, TABLE, incident

ATTEMPTS = RUNBOOK_VARS["verify_attempts"]

CLAIMED = TaskError("DynamoDB.ConditionalCheckFailedException", "The conditional request failed")
STILL_FIRING = {"Item": {"status": {"S": "remediating"}, "first_seen_at": {"S": "2026-10-22T10:00:00Z"}}}


def resolved(first_seen="2026-10-22T10:00:00Z", resolved_at="2026-10-22T10:04:30Z"):
    item = {"status": {"S": "resolved"}, "resolved_at": {"S": resolved_at}}
    if first_seen:
        item["first_seen_at"] = {"S": first_seen}
    return {"Item": item}


def lambda_ok(arguments, _call):
    payload = arguments["Payload"]
    return {"StatusCode": 200, "Payload": {
        "incident_id": payload["incident_id"],
        "action": payload["action"],
        "alertname": payload["alertname"],
        "severity": payload["severity"],
        "namespace": payload["namespace"],
        "deployment": payload["deployment"],
        "degraded": True,
    }}


def run(detail, definition=None, **tasks):
    tasks.setdefault("ExecuteAction", lambda_ok)
    tasks.setdefault("VerifyResolution", [STILL_FIRING, resolved()])
    result = Simulator(definition or load("runbook.asl.json", RUNBOOK_VARS), tasks).run(detail)
    _check_dynamodb_types(result)
    return result


def _check_dynamodb_types(result):
    # S and N values have to be strings once the expressions are evaluated
    for call in result.calls:
        if ":dynamodb:" not in call.resource:
            continue
        assert call.arguments["TableName"] == TABLE
        for section in ("Key", "ExpressionAttributeValues"):
            for placeholder, typed in call.arguments.get(section, {}).items():
                for type_key, value in typed.items():
                    if type_key in ("S", "N"):
                        assert isinstance(value, str), f"{call.state} {placeholder} {type_key}={value!r}"


def payload(result):
    calls = result.calls_to("ExecuteAction")
    assert len(calls) == 1
    assert calls[0].arguments["FunctionName"] == RUNBOOK_ACTION_ARN
    return calls[0].arguments["Payload"]


def detail_of(result, detail_type):
    entries = result.events(detail_type)
    assert len(entries) == 1, f"expected one {detail_type}, got {len(entries)}"
    entry = entries[0]
    assert entry["EventBusName"] == BUS
    assert entry["Source"] == "sre.runbook"
    return entry["Detail"]


# ---------------------------------------------------------------- happy path

def test_routed_alert_is_remediated_and_auto_resolves():
    result = run(incident())

    assert result.status == "SUCCEEDED"
    assert result.end_state == "AutoResolved"
    assert result.path == [
        "EvaluateAlert", "ShouldRemediate", "LogIncident", "ChooseAction", "RunbookAvailable",
        "ExecuteAction", "ReportActionCompleted", "WaitForStabilization",
        "VerifyResolution", "IsResolved", "WaitBeforeRecheck",
        "VerifyResolution", "IsResolved", "MarkResolved", "PublishAutoResolved", "AutoResolved",
    ]
    assert result.waits == [("WaitForStabilization", 60), ("WaitBeforeRecheck", 30)]


def test_claim_marks_the_incident_remediating():
    result = run(incident())
    claim = result.calls_to("LogIncident")[0].arguments
    assert claim["Key"] == {"incident_id": {"S": incident()["incident_id"]}}
    assert claim["ExpressionAttributeValues"][":remediating"] == {"S": "remediating"}
    assert claim["ExpressionAttributeValues"][":execution"] == {"S": Simulator.EXECUTION_ARN}
    assert claim["ExpressionAttributeValues"][":now"]["S"].startswith("2026-10-22T10:00:")
    assert "attribute_not_exists(remediation_started_at)" in claim["ConditionExpression"]


def test_lambda_gets_the_action_and_target():
    assert payload(run(incident())) == {
        "incident_id": incident()["incident_id"],
        "alertname": "CheckoutHighErrorRate",
        "severity": "critical",
        "action": "degrade_or_scale",
        "namespace": "apps",
        "deployment": "checkout",
        "replicas": 4,
        "node": "",
    }


@pytest.mark.parametrize("alertname, action", [
    ("CheckoutHighErrorRate", "degrade_or_scale"),
    ("CheckoutHighLatencyP95", "degrade_or_scale"),
    ("CheckoutSLOBurnFast", "degrade_or_scale"),
    ("CheckoutDown", "restart"),
    ("NodeUnderPressure", "drain_node"),
])
def test_route_table(alertname, action):
    assert payload(run(incident(alertname=alertname)))["action"] == action


def test_mark_resolved_records_mttr():
    result = run(incident(), VerifyResolution=[resolved("2026-10-22T10:00:00Z", "2026-10-22T10:04:30Z")])
    values = result.calls_to("MarkResolved")[0].arguments["ExpressionAttributeValues"]
    assert values == {
        ":result": {"S": "auto_resolved"},
        ":action": {"S": "degrade_or_scale"},
        ":mttr": {"N": "270"},
    }


def test_auto_resolved_event():
    result = run(incident())
    assert detail_of(result, "incident.auto_resolved") == {
        "incident_id": incident()["incident_id"],
        "alertname": "CheckoutHighErrorRate",
        "service": "checkout",
        "severity": "critical",
        "action": "degrade_or_scale",
        "mttr_seconds": 270,
        "execution": Simulator.EXECUTION_ARN,
    }
    assert not result.events("incident.escalated")


def test_action_completed_event_carries_the_lambda_output():
    result = run(incident())
    detail = detail_of(result, "action.completed")
    assert detail["result"] == "success"
    assert detail["action"] == "degrade_or_scale"
    assert detail["output"]["degraded"] is True
    assert detail["execution"] == Simulator.EXECUTION_ARN


def test_mttr_is_zero_when_first_seen_is_missing():
    # The recorder may not have written the created event yet
    result = run(incident(), VerifyResolution=[resolved(first_seen=None)])
    assert result.end_state == "AutoResolved"
    assert detail_of(result, "incident.auto_resolved")["mttr_seconds"] == 0


def test_more_severities_can_be_auto_remediated():
    definition = load("runbook.asl.json", dict(RUNBOOK_VARS, auto_remediate_severities="'critical', 'warning'"))
    result = run(incident(severity="warning"), definition=definition)
    assert result.end_state == "AutoResolved"


# ---------------------------------------------------------------- not remediated

@pytest.mark.parametrize("detail", [
    pytest.param(incident(severity="warning"), id="warning"),
    pytest.param(incident(severity="info"), id="info"),
    pytest.param(incident(status="resolved"), id="resolved"),
    pytest.param(incident(annotations={"runbook_action": "notify_only"}), id="notify_only"),
])
def test_only_notifies(detail):
    result = run(detail)
    assert result.status == "SUCCEEDED"
    assert result.end_state == "NotAutoRemediated"
    assert result.calls == []


def test_already_claimed_incident_stops():
    result = run(incident(), LogIncident=CLAIMED)
    assert result.status == "SUCCEEDED"
    assert result.end_state == "AlreadyHandled"
    assert [c.state for c in result.calls] == ["LogIncident"]
    # A failed condition is final, not something to retry
    assert result.calls[0].retries == 0


# ---------------------------------------------------------------- choosing the action and target

def test_annotation_overrides_the_route():
    detail = incident(annotations={"runbook_action": "restart"})
    assert payload(run(detail))["action"] == "restart"


def test_target_comes_from_labels():
    detail = incident(
        labels={"alertname": "CheckoutHighErrorRate", "severity": "critical",
                "namespace": "payments", "deployment": "payments-api"},
        annotations={"desired_replicas": "6"},
    )
    sent = payload(run(detail))
    assert (sent["namespace"], sent["deployment"], sent["replicas"]) == ("payments", "payments-api", "6")


NODE = "ip-10-0-1-23.ec2.internal"


def node_pressure(**labels):
    return incident(
        alertname="NodeUnderPressure",
        service="unknown",
        labels={"alertname": "NodeUnderPressure", "severity": "critical", **labels},
        annotations={"summary": f"{labels.get('node', 'A node')} has MemoryPressure"},
    )


def test_node_under_pressure_drains_the_node_from_its_label():
    detail = node_pressure(node=NODE, condition="MemoryPressure", namespace="monitoring")
    result = run(detail)
    sent = payload(result)
    assert (sent["action"], sent["node"]) == ("drain_node", NODE)
    assert result.end_state == "AutoResolved"


def test_drain_node_takes_the_host_tag_from_datadog():
    detail = node_pressure(host=NODE, kube_cluster_name="event-driven-sre-eks")
    detail["source"] = "datadog"
    assert payload(run(detail))["node"] == NODE


def test_node_label_wins_over_host():
    assert payload(run(node_pressure(node=NODE, host="something-else")))["node"] == NODE


def test_missing_node_reaches_the_lambda_as_empty():
    # Always defined, so the Lambda can reject it with a clear message
    assert payload(run(node_pressure()))["node"] == ""


def test_target_comes_from_datadog_kube_tags():
    detail = incident(
        source="datadog",
        service="checkout",
        labels={
            "alertname": "CheckoutHighLatencyP95", "severity": "critical", "service": "checkout",
            "env": "prod", "kube_namespace": "apps", "kube_deployment": "checkout-v2",
        },
        annotations={
            "summary": "[P1] Checkout p95 latency high",
            "event_url": "https://app.datadoghq.com/event/event?id=7000000000000000000",
            "datadog_alert_id": "123456789",
            "datadog_transition": "Triggered",
        },
        alertname="CheckoutHighLatencyP95",
        fingerprint="1234567890_1792663100",
    )
    sent = payload(run(detail))
    assert (sent["action"], sent["namespace"], sent["deployment"]) == ("degrade_or_scale", "apps", "checkout-v2")


def test_plain_labels_win_over_kube_tags():
    detail = incident(labels={
        "alertname": "CheckoutDown", "severity": "critical",
        "namespace": "apps", "kube_namespace": "other",
        "deployment": "checkout", "kube_deployment": "other",
    }, alertname="CheckoutDown")
    sent = payload(run(detail))
    assert (sent["namespace"], sent["deployment"]) == ("apps", "checkout")


def test_service_is_the_deployment_when_there_is_no_label():
    detail = incident(service="cart", labels={"alertname": "CheckoutDown", "severity": "critical"},
                      alertname="CheckoutDown")
    assert payload(run(detail))["deployment"] == "cart"


def test_unknown_service_falls_back_to_checkout():
    detail = incident(service="unknown", labels={"alertname": "CheckoutDown", "severity": "critical"},
                      alertname="CheckoutDown")
    assert payload(run(detail))["deployment"] == "checkout"


# The module's own defaults: no routes and no default deployment
MODULE_DEFAULTS = {**RUNBOOK_VARS, "routes": "{}", "default_deployment": ""}


def test_with_no_routes_a_critical_alert_escalates():
    result = run(incident(), definition=load("runbook.asl.json", MODULE_DEFAULTS))
    assert (result.error, result.cause) == ("Escalated", "no runbook for CheckoutHighErrorRate")
    assert result.calls_to("ExecuteAction") == []


def test_with_no_default_deployment_the_lambda_gets_an_empty_one():
    detail = incident(service="unknown", labels={"alertname": "AppDown", "severity": "critical"},
                      alertname="AppDown", annotations={"runbook_action": "restart"})
    assert payload(run(detail, definition=load("runbook.asl.json", MODULE_DEFAULTS)))["deployment"] == ""


# ---------------------------------------------------------------- escalation

def test_unknown_alert_is_claimed_then_escalated():
    result = run(incident(alertname="PaymentsQueueBacklog"))
    assert result.status == "FAILED"
    assert (result.error, result.cause) == ("Escalated", "no runbook for PaymentsQueueBacklog")
    assert [c.state for c in result.calls] == ["LogIncident", "Escalate", "PublishEscalation"]
    detail = detail_of(result, "incident.escalated")
    assert detail["action"] == "none"
    assert detail["reason"] == "no runbook for PaymentsQueueBacklog"


def test_unknown_annotated_action_is_escalated_without_calling_the_lambda():
    result = run(incident(annotations={"runbook_action": "reboot_cluster"}))
    assert result.end_state == "Escalated"
    assert result.cause == "unknown runbook_action reboot_cluster on CheckoutHighErrorRate"
    assert not result.calls_to("ExecuteAction")


def test_never_resolving_escalates_after_the_verify_window():
    result = run(incident(), VerifyResolution=STILL_FIRING)

    assert result.status == "FAILED"
    assert result.end_state == "Escalated"
    assert len(result.calls_to("VerifyResolution")) == ATTEMPTS
    # verify_timeout_seconds (600) from the end of the action
    assert sum(seconds for _, seconds in result.waits) == 600
    reason = "CheckoutHighErrorRate still firing after degrade_or_scale"
    assert result.cause == reason

    escalate = result.calls_to("Escalate")[0].arguments
    assert escalate["ExpressionAttributeValues"][":reason"] == {"S": reason}
    assert escalate["ExpressionAttributeValues"][":escalated"] == {"S": "escalated"}
    assert "#status <> :resolved" in escalate["ConditionExpression"]

    assert detail_of(result, "incident.escalated") == {
        "incident_id": incident()["incident_id"],
        "alertname": "CheckoutHighErrorRate",
        "service": "checkout",
        "severity": "critical",
        "action": "degrade_or_scale",
        "reason": reason,
        "execution": Simulator.EXECUTION_ARN,
    }
    assert not result.events("incident.auto_resolved")


def test_missing_item_counts_as_not_resolved():
    result = run(incident(), VerifyResolution={})
    assert result.end_state == "Escalated"
    assert len(result.calls_to("VerifyResolution")) == ATTEMPTS


def test_invalid_request_is_not_retried_and_escalates():
    error = TaskError("InvalidRequest", '{"errorMessage": "Namespace \'kube-system\' isn\'t in ALLOWED_NAMESPACES (apps)"}')
    result = run(incident(), ExecuteAction=error)

    assert result.calls_to("ExecuteAction")[0].retries == 0
    detail = detail_of(result, "action.completed")
    assert (detail["result"], detail["error"]) == ("failed", "InvalidRequest")
    assert "kube-system" in detail["cause"]
    assert result.cause == "degrade_or_scale failed: InvalidRequest"
    assert not result.calls_to("VerifyResolution")
    assert result.end_state == "Escalated"


def test_other_lambda_errors_get_retries_first():
    result = run(incident(), ExecuteAction=TaskError("RuntimeError", "K8s API PATCH failed: 503"))
    assert result.calls_to("ExecuteAction")[0].retries == 3
    assert result.cause == "degrade_or_scale failed: RuntimeError"


def test_lambda_timeout_without_a_cause_still_reports():
    result = run(incident(), ExecuteAction=TaskError("States.Timeout"))
    assert detail_of(result, "action.completed")["cause"] == ""
    assert result.end_state == "Escalated"


def test_claim_failure_escalates_instead_of_remediating():
    result = run(incident(), LogIncident=TaskError("DynamoDB.InternalServerErrorException", "boom"))
    assert not result.calls_to("ExecuteAction")
    assert result.cause == "could not record the incident: DynamoDB.InternalServerErrorException"


def test_verify_failure_escalates():
    result = run(incident(), VerifyResolution=TaskError("DynamoDB.ProvisionedThroughputExceededException"))
    assert result.cause == "could not check the incident: DynamoDB.ProvisionedThroughputExceededException"


def test_resolution_during_escalation_wins():
    result = run(incident(), VerifyResolution=STILL_FIRING, Escalate=CLAIMED)
    assert result.status == "SUCCEEDED"
    assert result.end_state == "ResolvedBeforeEscalation"
    assert not result.events("incident.escalated")


def test_escalation_is_published_even_if_the_table_write_fails():
    result = run(incident(), VerifyResolution=STILL_FIRING,
                 Escalate=TaskError("DynamoDB.InternalServerErrorException"))
    assert result.end_state == "Escalated"
    assert detail_of(result, "incident.escalated")["reason"].endswith("still firing after degrade_or_scale")


def test_failed_audit_event_doesnt_stop_verification():
    result = run(incident(), ReportActionCompleted=TaskError("EventBridge.InternalException"))
    assert result.end_state == "AutoResolved"


# ---------------------------------------------------------------- template details

def test_runbook_is_jsonata_throughout(runbook):
    assert runbook["QueryLanguage"] == "JSONata"
    assert all("QueryLanguage" not in s for s in runbook["States"].values())


def test_wait_seconds_come_from_terraform(runbook):
    assert runbook["States"]["WaitForStabilization"]["Seconds"] == "{% 60 %}"
    assert runbook["States"]["WaitBeforeRecheck"]["Seconds"] == "{% 30 %}"
    assert is_expression(runbook["States"]["IsResolved"]["Choices"][1]["Condition"])
    assert f">= {ATTEMPTS}" in runbook["States"]["IsResolved"]["Choices"][1]["Condition"]
