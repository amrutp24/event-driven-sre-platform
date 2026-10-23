"""
alert_ingest: the Lambda behind POST /webhook.

Accepts an Alertmanager webhook payload or a Datadog webhook payload (see
docs/API.md), validates it, normalizes every alert into one format, enriches it
with the account, region and receive time, and records it.

Each alert gets a stable incident_id, derived from Alertmanager's fingerprint and
start time or Datadog's alert cycle key. A firing alert and its later resolved
notification therefore share an incident_id, and a re-sent firing alert doesn't
start a second remediation run.
"""
import base64
import json
import logging
import os
import time
import uuid
from datetime import datetime, timezone

import boto3
from botocore.exceptions import BotoCoreError, ClientError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

eventbridge = boto3.client("events")
ddb = boto3.resource("dynamodb")
sns = boto3.client("sns")
sfn = boto3.client("stepfunctions")

BUS = os.environ["EVENT_BUS_NAME"]
TABLE = os.environ["INCIDENT_TABLE"]
RUNBOOK_ARN = os.environ["RUNBOOK_ARN"]
TOPIC = os.environ["SNS_TOPIC_ARN"]
RUNBOOK_ACTION_ARN = os.environ.get("RUNBOOK_ACTION_ARN", "")

SEVERITIES = ("critical", "warning", "info")
STATUSES = ("firing", "resolved")

# Fixed namespace so the same alert always maps to the same incident_id.
INCIDENT_NAMESPACE = uuid.UUID("6f0c8f52-2d1e-4b7e-9c4a-5b8f3e1d2a90")

# Datadog transitions that close an incident. Everything else (Triggered,
# Re-Triggered, Warn, No Data, Renotify) is treated as firing.
DATADOG_RESOLVED = {"recovered"}
DATADOG_PRIORITY_SEVERITY = {"P1": "critical", "P2": "critical", "P3": "warning", "P4": "info", "P5": "info"}

SNS_SUBJECT_MAX = 100


class PayloadError(Exception):
    """A request we reject with 400."""

    def __init__(self, error_code, message, field=None, expected=None, received=None):
        super().__init__(message)
        self.error_code = error_code
        self.message = message
        self.details = {k: v for k, v in
                        {"field": field, "expected": expected, "received": received}.items()
                        if v is not None}


class EventBridgeError(Exception):
    """put_events failed or rejected an entry."""


# ---------------------------------------------------------------- responses

def _response(status_code, body):
    return {
        "statusCode": status_code,
        "headers": {"content-type": "application/json"},
        "body": json.dumps(body),
    }


def _error(status_code, error_code, message, details=None):
    return _response(status_code, {
        "status": "error",
        "message": message,
        "error_code": error_code,
        "details": details or {},
    })


# ---------------------------------------------------------------- parsing

def _type_name(value):
    if value is None:
        return "null"
    return {dict: "object", list: "array", str: "string", bool: "boolean",
            int: "number", float: "number"}.get(type(value), type(value).__name__)


def _parse_body(event):
    body = event.get("body")
    if not body:
        raise PayloadError("INVALID_PAYLOAD", "Request body is empty",
                           field="body", expected="JSON object", received="empty")
    if event.get("isBase64Encoded"):
        body = base64.b64decode(body).decode("utf-8")
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise PayloadError("INVALID_PAYLOAD", f"Request body is not valid JSON: {exc.msg}",
                           field="body", expected="JSON object", received="invalid JSON")
    if not isinstance(payload, dict):
        raise PayloadError("INVALID_PAYLOAD", "Request body must be a JSON object",
                           field="body", expected="object", received=_type_name(payload))
    return payload


def _iso_now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _incident_id(*parts):
    if all(parts):
        return str(uuid.uuid5(INCIDENT_NAMESPACE, ":".join(str(p) for p in parts)))
    # Nothing stable to correlate on (a hand-written test alert, for example).
    return str(uuid.uuid4())


def _check_severity(value, field):
    if value not in SEVERITIES:
        raise PayloadError("INVALID_SEVERITY",
                           f"Severity must be one of {', '.join(SEVERITIES)}",
                           field=field, expected=" | ".join(SEVERITIES), received=str(value))
    return value


def _from_alertmanager(payload):
    alerts = payload.get("alerts")
    if not isinstance(alerts, list) or not alerts:
        received = _type_name(alerts) if not isinstance(alerts, list) else "empty array"
        raise PayloadError("INVALID_PAYLOAD",
                           "Invalid alert payload: 'alerts' must be a non-empty array",
                           field="alerts", expected="non-empty array", received=received)

    normalized = []
    for i, alert in enumerate(alerts):
        where = f"alerts[{i}]"
        if not isinstance(alert, dict):
            raise PayloadError("INVALID_PAYLOAD", f"{where} must be an object",
                               field=where, expected="object", received=_type_name(alert))

        labels = alert.get("labels")
        if not isinstance(labels, dict):
            raise PayloadError("INVALID_PAYLOAD", f"{where}.labels must be an object",
                               field=f"{where}.labels", expected="object", received=_type_name(labels))

        alertname = labels.get("alertname")
        if not isinstance(alertname, str) or not alertname.strip():
            raise PayloadError("MISSING_ALERT_NAME", f"{where} is missing the 'alertname' label",
                               field=f"{where}.labels.alertname", expected="non-empty string",
                               received=_type_name(alertname))

        severity = _check_severity(labels.get("severity"), f"{where}.labels.severity")

        status = alert.get("status")
        if status not in STATUSES:
            raise PayloadError("INVALID_PAYLOAD", f"{where}.status must be 'firing' or 'resolved'",
                               field=f"{where}.status", expected="firing | resolved",
                               received=str(status))

        annotations = alert.get("annotations") or {}
        fingerprint = alert.get("fingerprint")
        starts_at = alert.get("startsAt")

        normalized.append({
            "incident_id": _incident_id("alertmanager", fingerprint, starts_at),
            "source": "alertmanager",
            "status": status,
            "severity": severity,
            "service": labels.get("service", "unknown"),
            "alertname": alertname,
            "labels": labels,
            "annotations": annotations,
            "fingerprint": fingerprint,
            "starts_at": starts_at,
            "ends_at": alert.get("endsAt") if status == "resolved" else None,
        })
    return normalized


def _datadog_tags(raw):
    """Datadog's $TAGS renders as "a:b,c:d"; also accept a JSON array of tags."""
    if isinstance(raw, list):
        tags = [str(t).strip() for t in raw]
    elif isinstance(raw, str):
        tags = [t.strip() for t in raw.split(",")]
    else:
        tags = []
    parsed = {}
    for tag in filter(None, tags):
        key, sep, value = tag.partition(":")
        parsed[key] = value if sep else ""
    return parsed


def _from_datadog(payload):
    alert_id = payload.get("alert_id")
    if alert_id in (None, ""):
        raise PayloadError("INVALID_PAYLOAD", "Datadog payload is missing 'alert_id'",
                           field="alert_id", expected="string", received=_type_name(alert_id))

    tags = _datadog_tags(payload.get("tags"))
    alertname = tags.get("alertname") or payload.get("title")
    if not isinstance(alertname, str) or not alertname.strip():
        raise PayloadError("MISSING_ALERT_NAME",
                           "Datadog payload needs an 'alertname:' tag or a 'title'",
                           field="title", expected="non-empty string", received=_type_name(alertname))

    if "severity" in tags:
        severity = _check_severity(tags["severity"], "tags.severity")
    else:
        severity = DATADOG_PRIORITY_SEVERITY.get(str(payload.get("priority", "")).upper(), "warning")

    transition = str(payload.get("alert_transition", ""))
    status = "resolved" if transition.lower() in DATADOG_RESOLVED else "firing"

    happened = payload.get("date_happened")
    happened_iso = None
    if isinstance(happened, (int, float)) or (isinstance(happened, str) and happened.isdigit()):
        seconds = int(happened)
        if seconds > 10_000_000_000:  # sent in milliseconds
            seconds //= 1000
        happened_iso = datetime.fromtimestamp(seconds, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    cycle_key = payload.get("alert_cycle_key")
    labels = dict(tags, alertname=alertname, severity=severity)
    labels.setdefault("service", "unknown")

    return [{
        # The cycle key is the same on the trigger and the recovery.
        "incident_id": _incident_id("datadog", cycle_key),
        "source": "datadog",
        "status": status,
        "severity": severity,
        "service": labels["service"],
        "alertname": alertname,
        "labels": labels,
        "annotations": {k: v for k, v in {
            "summary": payload.get("title"),
            "description": payload.get("body"),
            "event_url": payload.get("event_url"),
            "datadog_alert_id": str(alert_id),
            "datadog_transition": transition,
        }.items() if v},
        "fingerprint": cycle_key,
        "starts_at": happened_iso if status == "firing" else None,
        "ends_at": happened_iso if status == "resolved" else None,
    }]


def _normalize(payload):
    if "alerts" in payload:
        return _from_alertmanager(payload)
    if "alert_transition" in payload:
        return _from_datadog(payload)
    raise PayloadError("INVALID_PAYLOAD",
                       "Invalid alert payload: missing required field 'alerts'",
                       field="alerts", expected="array", received="null")


def _account_id(context):
    arn = getattr(context, "invoked_function_arn", "") or ""
    parts = arn.split(":")
    return parts[4] if len(parts) > 4 else None


# ---------------------------------------------------------------- side effects

def _store(alert):
    table = ddb.Table(TABLE)
    if alert["status"] == "firing":
        table.put_item(Item={
            "incident_id": alert["incident_id"],
            "ts": alert["timestamp"],
            "received_at": alert["received_at"],
            "source": alert["source"],
            "correlation_id": alert["correlation_id"],
            "service": alert["service"],
            "alertname": alert["alertname"],
            "severity": alert["severity"],
            "status": "firing",
            "payload": alert,
        })
    else:
        table.update_item(
            Key={"incident_id": alert["incident_id"]},
            UpdateExpression="SET #status = :status, resolved_at = :resolved_at, "
                             "alertname = :alertname, service = :service, severity = :severity",
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues={
                ":status": "resolved",
                ":resolved_at": alert["timestamp"],
                ":alertname": alert["alertname"],
                ":service": alert["service"],
                ":severity": alert["severity"],
            },
        )


def _publish_event(alert):
    try:
        resp = eventbridge.put_events(Entries=[{
            "EventBusName": BUS,
            "Source": f"sre.{alert['source']}",
            "DetailType": "incident.created" if alert["status"] == "firing" else "incident.resolved",
            "Detail": json.dumps(alert),
        }])
    except (ClientError, BotoCoreError) as exc:
        raise EventBridgeError(str(exc)) from exc
    if resp.get("FailedEntryCount"):
        raise EventBridgeError(json.dumps(resp.get("Entries", [])))


def _notify(alert):
    subject = f"[{alert['severity'].upper()}] {alert['service']} - {alert['alertname']} ({alert['status']})"
    sns.publish(
        TopicArn=TOPIC,
        Subject=subject[:SNS_SUBJECT_MAX],
        Message=json.dumps(alert, indent=2)[:240000],
    )


def _start_runbook(alert):
    # Firing alerts only; acting on a resolved notification would undo the recovery.
    # The execution name is the incident_id, so a re-sent alert can't start a
    # second run for the same incident.
    try:
        sfn.start_execution(stateMachineArn=RUNBOOK_ARN, name=alert["incident_id"],
                            input=json.dumps(alert))
        return True
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ExecutionAlreadyExists":
            logger.info("Runbook already started for incident %s", alert["incident_id"])
            return False
        raise


# ---------------------------------------------------------------- handler

def lambda_handler(event, context):
    correlation_id = str(uuid.uuid4())
    try:
        alerts = _normalize(_parse_body(event))
    except PayloadError as exc:
        logger.warning("Rejected payload (%s): %s %s", exc.error_code, exc.message, exc.details)
        return _error(400, exc.error_code, exc.message, exc.details)

    now = int(time.time())
    received_at = _iso_now()
    account_id = _account_id(context)
    region = os.environ.get("AWS_REGION")

    for alert in alerts:
        alert.update({
            "timestamp": now,
            "received_at": received_at,
            "correlation_id": correlation_id,
            "account_id": account_id,
            "region": region,
            "runbook_action_arn": RUNBOOK_ACTION_ARN,
        })

    incidents = []
    try:
        for alert in alerts:
            _store(alert)
            _publish_event(alert)
            _notify(alert)
            started = _start_runbook(alert) if alert["status"] == "firing" else False
            incidents.append({
                "incident_id": alert["incident_id"],
                "alertname": alert["alertname"],
                "status": alert["status"],
                "remediation_started": started,
            })
    except EventBridgeError as exc:
        logger.exception("EventBridge publish failed (correlation_id %s)", correlation_id)
        return _error(502, "EVENTBRIDGE_ERROR", "Failed to publish event to EventBridge",
                      {"correlation_id": correlation_id, "reason": str(exc)[:500]})
    except (ClientError, BotoCoreError):
        logger.exception("Failed to process alert (correlation_id %s)", correlation_id)
        return _error(500, "INTERNAL_ERROR", "Failed to process alert; check CloudWatch Logs",
                      {"correlation_id": correlation_id})

    logger.info("Accepted %d alert(s), correlation_id %s", len(alerts), correlation_id)
    return _response(202, {
        "status": "accepted",
        "message": "Alert received and queued for processing",
        "alert_count": len(alerts),
        "correlation_id": correlation_id,
        "incidents": incidents,
    })
