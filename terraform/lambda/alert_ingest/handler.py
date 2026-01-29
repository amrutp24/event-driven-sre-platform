import json, os, time, uuid
import boto3

eventbridge = boto3.client("events")
ddb = boto3.resource("dynamodb")
sns = boto3.client("sns")
sfn = boto3.client("stepfunctions")

BUS = os.environ["EVENT_BUS_NAME"]
TABLE = os.environ["INCIDENT_TABLE"]
RUNBOOK_ARN = os.environ["RUNBOOK_ARN"]
TOPIC = os.environ["SNS_TOPIC_ARN"]
RUNBOOK_ACTION_ARN = os.environ.get("RUNBOOK_ACTION_ARN","")

def lambda_handler(event, context):
    body = event.get("body", "{}")
    if event.get("isBase64Encoded"):
        import base64
        body = base64.b64decode(body).decode("utf-8")

    payload = json.loads(body or "{}")
    alerts = payload.get("alerts", [])
    now = int(time.time())

    results = []
    for a in alerts:
        incident_id = str(uuid.uuid4())
        labels = a.get("labels", {})
        annotations = a.get("annotations", {})
        severity = labels.get("severity", "ticket")
        service = labels.get("service", "unknown")
        alertname = labels.get("alertname", "UnknownAlert")
        status = a.get("status", "firing")

        normalized = {
            "incident_id": incident_id,
            "timestamp": now,
            "status": status,
            "severity": severity,
            "service": service,
            "alertname": alertname,
            "labels": labels,
            "annotations": annotations,
            "runbook_action_arn": RUNBOOK_ACTION_ARN,
        }

        ddb.Table(TABLE).put_item(Item={
            "incident_id": incident_id,
            "ts": now,
            "service": service,
            "alertname": alertname,
            "severity": severity,
            "status": status,
            "payload": normalized
        })

        eventbridge.put_events(Entries=[{
            "EventBusName": BUS,
            "Source": "prometheus.alertmanager",
            "DetailType": "SREAlert",
            "Detail": json.dumps(normalized),
        }])

        sns.publish(
            TopicArn=TOPIC,
            Subject=f"[{severity.upper()}] {service} - {alertname} ({status})",
            Message=json.dumps(normalized, indent=2)[:240000]
        )

        sfn.start_execution(
            stateMachineArn=RUNBOOK_ARN,
            input=json.dumps(normalized)
        )

        results.append({"incident_id": incident_id, "alertname": alertname})

    return {
        "statusCode": 200,
        "headers": {"content-type": "application/json"},
        "body": json.dumps({"ok": True, "processed": results})
    }
