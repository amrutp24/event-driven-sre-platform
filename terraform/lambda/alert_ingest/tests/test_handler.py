"""
Unit tests for the alert_ingest Lambda (POST /webhook).

Covers:
- Alertmanager and Datadog payloads, normalized to one format
- 202 response format and enrichment (account, region, correlation_id)
- Payload validation and the 400 error codes from docs/API.md
- Firing vs resolved: storage, EventBridge detail type, remediation
- Stable incident ids and de-duplicated remediation runs
- EventBridge and other AWS failures (502 / 500)
"""
import json
import os
import sys
from unittest.mock import Mock, patch

import pytest
from botocore.exceptions import ClientError

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

test_env_vars = {
    'AWS_DEFAULT_REGION': 'us-east-1',
    'AWS_REGION': 'us-east-1',
    'EVENT_BUS_NAME': 'test-event-bus',
    'INCIDENT_TABLE': 'test-incidents',
    'RUNBOOK_ARN': 'arn:aws:states:us-east-1:123456789012:stateMachine:test-runbook',
    'SNS_TOPIC_ARN': 'arn:aws:sns:us-east-1:123456789012:test-topic',
    'RUNBOOK_ACTION_ARN': 'arn:aws:lambda:us-east-1:123456789012:function:test-action'
}

with patch.dict(os.environ, test_env_vars):
    with patch('boto3.client'):
        with patch('boto3.resource'):
            import handler


class FakeContext:
    invoked_function_arn = 'arn:aws:lambda:us-east-1:123456789012:function:test-alert-ingest'


@pytest.fixture
def aws(monkeypatch):
    """Patch the module's AWS clients and return them, with healthy defaults."""
    monkeypatch.setenv('AWS_REGION', 'us-east-1')
    eventbridge, ddb, sns, sfn, table = Mock(), Mock(), Mock(), Mock(), Mock()
    ddb.Table.return_value = table
    eventbridge.put_events.return_value = {'FailedEntryCount': 0, 'Entries': [{'EventId': 'e1'}]}
    sns.publish.return_value = {'MessageId': 'm1'}
    sfn.start_execution.return_value = {'executionArn': 'x1'}
    with patch.object(handler, 'eventbridge', eventbridge), \
         patch.object(handler, 'ddb', ddb), \
         patch.object(handler, 'sns', sns), \
         patch.object(handler, 'sfn', sfn):
        yield type('AWS', (), dict(eventbridge=eventbridge, ddb=ddb, sns=sns, sfn=sfn, table=table))


def am_alert(status='firing', alertname='CheckoutHighErrorRate', severity='critical',
             service='checkout', fingerprint='a1b2c3d4e5f6', starts_at='2026-10-20T10:30:00Z', **extra):
    labels = {'alertname': alertname, 'severity': severity, 'service': service}
    alert = {'status': status, 'labels': labels, 'annotations': {'summary': 'High error rate'},
             'fingerprint': fingerprint, 'startsAt': starts_at}
    alert.update(extra)
    return alert


def request(payload):
    return {'body': json.dumps(payload)}


def body_of(result):
    return json.loads(result['body'])


# ---------------------------------------------------------------- Alertmanager

class TestAlertIngestion:

    def test_single_alert_processing(self, aws, sample_alertmanager_event):
        result = handler.lambda_handler(sample_alertmanager_event, FakeContext())

        assert result['statusCode'] == 202
        body = body_of(result)
        assert body['status'] == 'accepted'
        assert body['message'] == 'Alert received and queued for processing'
        assert body['alert_count'] == 1
        assert body['correlation_id']
        assert body['incidents'][0]['alertname'] == 'CheckoutHighErrorRate'
        assert body['incidents'][0]['remediation_started'] is True

        aws.table.put_item.assert_called_once()
        aws.eventbridge.put_events.assert_called_once()
        aws.sns.publish.assert_called_once()
        aws.sfn.start_execution.assert_called_once()

    def test_multiple_alert_processing(self, aws, sample_alertmanager_event_multiple):
        result = handler.lambda_handler(sample_alertmanager_event_multiple, FakeContext())

        assert result['statusCode'] == 202
        body = body_of(result)
        assert body['alert_count'] == 2
        assert [i['alertname'] for i in body['incidents']] == ['CheckoutHighErrorRate', 'CheckoutHighLatencyP95']
        assert aws.table.put_item.call_count == 2
        assert aws.eventbridge.put_events.call_count == 2
        assert aws.sns.publish.call_count == 2
        assert aws.sfn.start_execution.call_count == 2

    def test_base64_encoded_payload(self, aws, sample_base64_event):
        result = handler.lambda_handler(sample_base64_event, FakeContext())

        assert result['statusCode'] == 202
        assert body_of(result)['incidents'][0]['alertname'] == 'CheckoutDown'

    def test_enrichment(self, aws):
        handler.lambda_handler(request({'alerts': [am_alert()]}), FakeContext())

        detail = json.loads(aws.eventbridge.put_events.call_args[1]['Entries'][0]['Detail'])
        assert detail['account_id'] == '123456789012'
        assert detail['region'] == 'us-east-1'
        assert detail['received_at'].endswith('Z')
        assert isinstance(detail['timestamp'], int)
        assert detail['source'] == 'alertmanager'
        assert detail['runbook_action_arn'].endswith(':function:test-action')

    def test_correlation_id_shared_across_one_request(self, aws):
        payload = {'alerts': [am_alert(), am_alert(alertname='CheckoutDown', fingerprint='ffff')]}
        result = handler.lambda_handler(request(payload), FakeContext())

        details = [json.loads(c[1]['Entries'][0]['Detail']) for c in aws.eventbridge.put_events.call_args_list]
        assert {d['correlation_id'] for d in details} == {body_of(result)['correlation_id']}


class TestResolvedAlerts:

    def test_resolved_alert_updates_record_and_skips_remediation(self, aws):
        result = handler.lambda_handler(request({'alerts': [am_alert(status='resolved')]}), FakeContext())

        assert result['statusCode'] == 202
        assert body_of(result)['incidents'][0]['remediation_started'] is False
        aws.table.put_item.assert_not_called()
        aws.table.update_item.assert_called_once()
        values = aws.table.update_item.call_args[1]['ExpressionAttributeValues']
        assert values[':status'] == 'resolved'
        aws.sfn.start_execution.assert_not_called()
        entry = aws.eventbridge.put_events.call_args[1]['Entries'][0]
        assert entry['DetailType'] == 'incident.resolved'
        assert '(resolved)' in aws.sns.publish.call_args[1]['Subject']

    def test_firing_and_resolved_share_incident_id(self, aws):
        handler.lambda_handler(request({'alerts': [am_alert(status='firing')]}), FakeContext())
        handler.lambda_handler(request({'alerts': [am_alert(status='resolved')]}), FakeContext())

        fired_id = aws.table.put_item.call_args[1]['Item']['incident_id']
        resolved_id = aws.table.update_item.call_args[1]['Key']['incident_id']
        assert fired_id == resolved_id

    def test_different_start_times_are_different_incidents(self, aws):
        handler.lambda_handler(request({'alerts': [am_alert(starts_at='2026-10-20T10:00:00Z')]}), FakeContext())
        handler.lambda_handler(request({'alerts': [am_alert(starts_at='2026-10-21T10:00:00Z')]}), FakeContext())

        ids = {c[1]['Item']['incident_id'] for c in aws.table.put_item.call_args_list}
        assert len(ids) == 2


class TestRemediationDeduplication:

    def test_execution_named_after_incident(self, aws):
        handler.lambda_handler(request({'alerts': [am_alert()]}), FakeContext())

        kwargs = aws.sfn.start_execution.call_args[1]
        assert kwargs['name'] == aws.table.put_item.call_args[1]['Item']['incident_id']

    def test_resent_alert_does_not_fail_or_restart(self, aws):
        aws.sfn.start_execution.side_effect = ClientError(
            {'Error': {'Code': 'ExecutionAlreadyExists', 'Message': 'exists'}}, 'StartExecution')

        result = handler.lambda_handler(request({'alerts': [am_alert()]}), FakeContext())

        assert result['statusCode'] == 202
        assert body_of(result)['incidents'][0]['remediation_started'] is False


# ---------------------------------------------------------------- Datadog

class TestDatadog:

    def datadog(self, transition='Triggered', **extra):
        payload = {
            'body': 'High error rate detected on checkout service',
            'title': 'CheckoutHighErrorRate on checkout service',
            'priority': 'P2',
            'tags': 'service:checkout,namespace:apps,alertname:CheckoutHighErrorRate',
            'date_happened': 1761000000,
            'alert_id': '12345678',
            'alert_transition': transition,
            'alert_cycle_key': '987654321',
            'event_url': 'https://app.datadoghq.com/monitors/12345678',
        }
        payload.update(extra)
        return request(payload)

    def test_triggered(self, aws):
        result = handler.lambda_handler(self.datadog(), FakeContext())

        assert result['statusCode'] == 202
        entry = aws.eventbridge.put_events.call_args[1]['Entries'][0]
        assert entry['Source'] == 'sre.datadog'
        assert entry['DetailType'] == 'incident.created'
        detail = json.loads(entry['Detail'])
        assert detail['alertname'] == 'CheckoutHighErrorRate'
        assert detail['service'] == 'checkout'
        assert detail['severity'] == 'critical'  # P2
        assert detail['labels']['namespace'] == 'apps'
        assert detail['starts_at'] == '2025-10-20T22:40:00Z'
        aws.sfn.start_execution.assert_called_once()

    def test_recovered_closes_same_incident(self, aws):
        handler.lambda_handler(self.datadog('Triggered'), FakeContext())
        handler.lambda_handler(self.datadog('Recovered'), FakeContext())

        assert (aws.table.put_item.call_args[1]['Item']['incident_id']
                == aws.table.update_item.call_args[1]['Key']['incident_id'])
        assert aws.sfn.start_execution.call_count == 1

    def test_tags_as_array_and_severity_tag(self, aws):
        handler.lambda_handler(self.datadog(tags=['service:payments', 'severity:info']), FakeContext())

        detail = json.loads(aws.eventbridge.put_events.call_args[1]['Entries'][0]['Detail'])
        assert detail['service'] == 'payments'
        assert detail['severity'] == 'info'
        assert detail['alertname'] == 'CheckoutHighErrorRate on checkout service'  # no alertname tag, so the title

    @pytest.mark.parametrize('happened', [1761000000, '1761000000', 1761000000000, '1761000000000'])
    def test_date_in_seconds_or_milliseconds(self, aws, happened):
        result = handler.lambda_handler(self.datadog(date_happened=happened), FakeContext())

        assert result['statusCode'] == 202
        detail = json.loads(aws.eventbridge.put_events.call_args[1]['Entries'][0]['Detail'])
        assert detail['starts_at'] == '2025-10-20T22:40:00Z'

    def test_unknown_priority_defaults_to_warning(self, aws):
        handler.lambda_handler(self.datadog(priority='normal'), FakeContext())

        detail = json.loads(aws.eventbridge.put_events.call_args[1]['Entries'][0]['Detail'])
        assert detail['severity'] == 'warning'

    def test_invalid_severity_tag(self, aws):
        result = handler.lambda_handler(self.datadog(tags='severity:page'), FakeContext())

        assert result['statusCode'] == 400
        assert body_of(result)['error_code'] == 'INVALID_SEVERITY'

    def test_missing_alert_id(self, aws):
        result = handler.lambda_handler(self.datadog(alert_id=None), FakeContext())

        assert result['statusCode'] == 400
        assert body_of(result)['details']['field'] == 'alert_id'


# ---------------------------------------------------------------- validation

class TestErrorHandling:

    @pytest.mark.parametrize('event', [{}, {'body': ''}, {'body': None}])
    def test_empty_body(self, aws, event):
        result = handler.lambda_handler(event, FakeContext())

        assert result['statusCode'] == 400
        body = body_of(result)
        assert body['status'] == 'error'
        assert body['error_code'] == 'INVALID_PAYLOAD'

    def test_malformed_json_body(self, aws):
        result = handler.lambda_handler({'body': 'invalid json'}, FakeContext())

        assert result['statusCode'] == 400
        assert body_of(result)['details']['received'] == 'invalid JSON'

    def test_body_not_an_object(self, aws):
        result = handler.lambda_handler(request([1, 2]), FakeContext())

        assert result['statusCode'] == 400
        assert body_of(result)['details'] == {'field': 'body', 'expected': 'object', 'received': 'array'}

    def test_no_alerts_in_payload(self, aws):
        result = handler.lambda_handler(request({'some_other_field': 'value'}), FakeContext())

        assert result['statusCode'] == 400
        body = body_of(result)
        assert body['message'] == "Invalid alert payload: missing required field 'alerts'"
        assert body['details'] == {'field': 'alerts', 'expected': 'array', 'received': 'null'}

    def test_empty_alerts_array(self, aws):
        result = handler.lambda_handler(request({'alerts': []}), FakeContext())

        assert result['statusCode'] == 400
        assert body_of(result)['details']['received'] == 'empty array'

    def test_missing_alertname(self, aws):
        alert = am_alert()
        del alert['labels']['alertname']
        result = handler.lambda_handler(request({'alerts': [alert]}), FakeContext())

        assert result['statusCode'] == 400
        body = body_of(result)
        assert body['error_code'] == 'MISSING_ALERT_NAME'
        assert body['details']['field'] == 'alerts[0].labels.alertname'

    @pytest.mark.parametrize('severity', [None, 'page', 'CRITICAL', ''])
    def test_invalid_severity(self, aws, severity):
        alert = am_alert(severity=severity)
        result = handler.lambda_handler(request({'alerts': [alert]}), FakeContext())

        assert result['statusCode'] == 400
        assert body_of(result)['error_code'] == 'INVALID_SEVERITY'

    def test_invalid_status(self, aws):
        result = handler.lambda_handler(request({'alerts': [am_alert(status='pending')]}), FakeContext())

        assert result['statusCode'] == 400
        assert body_of(result)['details']['field'] == 'alerts[0].status'

    def test_one_bad_alert_rejects_the_whole_request(self, aws):
        payload = {'alerts': [am_alert(), am_alert(severity='page', fingerprint='ffff')]}
        result = handler.lambda_handler(request(payload), FakeContext())

        assert result['statusCode'] == 400
        aws.table.put_item.assert_not_called()
        aws.eventbridge.put_events.assert_not_called()
        aws.sfn.start_execution.assert_not_called()


class TestAWSFailures:

    def test_eventbridge_rejects_entry(self, aws):
        aws.eventbridge.put_events.return_value = {'FailedEntryCount': 1, 'Entries': [{'ErrorCode': 'InternalFailure'}]}

        result = handler.lambda_handler(request({'alerts': [am_alert()]}), FakeContext())

        assert result['statusCode'] == 502
        body = body_of(result)
        assert body['error_code'] == 'EVENTBRIDGE_ERROR'
        assert body['details']['correlation_id']

    def test_eventbridge_client_error(self, aws):
        aws.eventbridge.put_events.side_effect = ClientError(
            {'Error': {'Code': 'AccessDeniedException', 'Message': 'no'}}, 'PutEvents')

        result = handler.lambda_handler(request({'alerts': [am_alert()]}), FakeContext())

        assert result['statusCode'] == 502

    def test_other_aws_error(self, aws):
        aws.table.put_item.side_effect = ClientError(
            {'Error': {'Code': 'ProvisionedThroughputExceededException', 'Message': 'slow down'}}, 'PutItem')

        result = handler.lambda_handler(request({'alerts': [am_alert()]}), FakeContext())

        assert result['statusCode'] == 500
        assert body_of(result)['error_code'] == 'INTERNAL_ERROR'


# ---------------------------------------------------------------- formats

class TestAWSServiceIntegration:

    def test_eventbridge_event_format(self, aws, sample_alertmanager_event):
        handler.lambda_handler(sample_alertmanager_event, FakeContext())

        entry = aws.eventbridge.put_events.call_args[1]['Entries'][0]
        assert entry['EventBusName'] == 'test-event-bus'
        assert entry['Source'] == 'sre.alertmanager'
        assert entry['DetailType'] == 'incident.created'
        detail = json.loads(entry['Detail'])
        assert 'incident_id' in detail
        assert 'timestamp' in detail
        assert detail['alertname'] == 'CheckoutHighErrorRate'
        assert detail['severity'] == 'critical'

    def test_sns_notification_format(self, aws, sample_alertmanager_event):
        handler.lambda_handler(sample_alertmanager_event, FakeContext())

        kwargs = aws.sns.publish.call_args[1]
        assert kwargs['TopicArn'] == 'arn:aws:sns:us-east-1:123456789012:test-topic'
        assert kwargs['Subject'] == '[CRITICAL] checkout - CheckoutHighErrorRate (firing)'
        message = json.loads(kwargs['Message'])
        assert message['alertname'] == 'CheckoutHighErrorRate'

    def test_sns_subject_capped_at_100_chars(self, aws):
        handler.lambda_handler(request({'alerts': [am_alert(alertname='X' * 200)]}), FakeContext())

        assert len(aws.sns.publish.call_args[1]['Subject']) == 100

    def test_step_functions_input_format(self, aws, sample_alertmanager_event):
        handler.lambda_handler(sample_alertmanager_event, FakeContext())

        kwargs = aws.sfn.start_execution.call_args[1]
        assert kwargs['stateMachineArn'] == 'arn:aws:states:us-east-1:123456789012:stateMachine:test-runbook'
        input_data = json.loads(kwargs['input'])
        assert input_data['alertname'] == 'CheckoutHighErrorRate'
        assert input_data['severity'] == 'critical'
        assert input_data['annotations'] == {'description': 'High error rate detected'}
