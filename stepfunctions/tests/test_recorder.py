"""
Checks for the incident recorder (JSONPath). Its input is the whole
EventBridge event, so these build real events: incidents from alert_ingest and
action.completed from a runbook run.
"""
import re

import pytest
from asl_sim import Simulator, TaskError, load
from conftest import RUNBOOK_VARS, incident
from test_contracts import alertmanager, datadog
from test_runbook import lambda_ok, resolved

PATH = re.compile(r"(?<!\$)\$\.[A-Za-z0-9_\-.]+")


def envelope(detail_type, source, detail):
    return {
        "version": "0",
        "id": "6a7e8feb-b491-4cf7-a9f1-bf3703467718",
        "detail-type": detail_type,
        "source": source,
        "account": "123456789012",
        "time": "2026-10-22T10:00:01Z",
        "region": "us-east-1",
        "resources": [],
        "detail": detail,
    }


def get(event, path):
    value = event
    for part in path[2:].split("."):
        if not isinstance(value, dict) or part not in value:
            raise KeyError(f"{path} isn't in the event")
        value = value[part]
    return value


def route(recorder, event):
    """WhichEvent's Choice, for the rule shapes it uses."""
    def matches(rule):
        if "And" in rule:
            return all(matches(r) for r in rule["And"])
        return get(event, rule["Variable"]) == rule["StringEquals"]

    choice = recorder["States"][recorder["StartAt"]]
    for rule in choice["Choices"]:
        if matches(rule):
            return rule["Next"]
    return choice["Default"]


def dynamic_fields(value, key=""):
    """(key, expression) for every `.$` field in a Parameters block."""
    if isinstance(value, dict):
        for k, v in value.items():
            if k.endswith(".$"):
                yield k, v
            else:
                yield from dynamic_fields(v, k)
    elif isinstance(value, list):
        for v in value:
            yield from dynamic_fields(v, key)


def runbook_events(**tasks):
    tasks.setdefault("ExecuteAction", lambda_ok)
    tasks.setdefault("VerifyResolution", [resolved()])
    result = Simulator(load("runbook.asl.json", RUNBOOK_VARS), tasks).run(incident())
    return [envelope(e["DetailType"], e["Source"], e["Detail"]) for e in result.events()]


@pytest.fixture
def events(ingest):
    found = []
    for payload in (alertmanager(), alertmanager(status="resolved"), datadog(), datadog("Recovered")):
        found += [envelope(dt, src, d) for dt, src, d in ingest(payload)]
    found += runbook_events()
    found += runbook_events(ExecuteAction=TaskError("InvalidRequest", "bad namespace"))
    return found


EXPECTED = {
    "incident.created": "RecordCreated",
    "incident.resolved": "RecordResolved",
    "action.completed": "RecordAction",
    "incident.auto_resolved": "Ignore",
    "incident.escalated": "Ignore",
}


def test_events_cover_every_type(events):
    assert {e["detail-type"] for e in events} == set(EXPECTED)


def test_each_event_type_reaches_the_right_state(recorder, events):
    for event in events:
        assert route(recorder, event) == EXPECTED[event["detail-type"]], event["detail-type"]


def test_unknown_events_are_ignored(recorder):
    assert route(recorder, envelope("something.else", "sre.runbook", {})) == "Ignore"


def test_paths_exist_in_the_events_that_reach_them(recorder, events):
    for event in events:
        state = recorder["States"][route(recorder, event)]
        for key, expression in dynamic_fields(state.get("Parameters", {})):
            for path in PATH.findall(expression):
                value = get(event, path)
                # A plain path into an S attribute has to be a string
                if key == "S.$" and expression == path:
                    assert isinstance(value, str), f"{event['detail-type']}: {path} is {value!r}"
                if "States.MathAdd" in expression:
                    assert isinstance(value, int), f"{path} must be an integer for States.MathAdd"


def test_failed_actions_no_longer_escalate_in_the_recorder(recorder):
    # The runbook decides escalation now; the recorder only logs the action
    action = recorder["States"]["RecordAction"]["Parameters"]
    assert "#status" not in action["UpdateExpression"]


def test_ttl_is_written_on_created_and_resolved(recorder):
    for name in ("RecordCreated", "RecordResolved"):
        params = recorder["States"][name]["Parameters"]
        assert "expires_at = :expires" in params["UpdateExpression"]
        assert params["ExpressionAttributeValues"][":expires"] == {
            "N.$": "States.Format('{}', States.MathAdd($.detail.timestamp, 7776000))"
        }


def test_recorder_doesnt_overwrite_the_runbook_status_on_refires(recorder):
    created = recorder["States"]["RecordCreated"]["Parameters"]["UpdateExpression"]
    assert "#status = if_not_exists(#status, :open)" in created
