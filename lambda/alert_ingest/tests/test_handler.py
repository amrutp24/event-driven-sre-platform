"""
Unit tests for the alert_ingest Lambda (POST /webhook).

alert_ingest only validates, normalizes and publishes to EventBridge; the rules
in event_routing.tf do the rest. Covers:
- Alertmanager and Datadog payloads, normalized to one format
- 202 response format and enrichment (account, region, correlation_id)
- Payload validation and the 400 error codes from docs/API.md
- incident.created vs incident.resolved events and stable incident ids
- put_events batching and failures (502 / 500)
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
}

with patch.dict(os.environ, test_env_vars):
    with patch('boto3.client'):
        import handler


class FakeContext:
    invoked_function_arn = 'arn:aws:lambda:us-east-1:123456789012:function:test-alert-ingest'


@pytest.fixture
def eventbridge(monkeypatch):
    monkeypatch.setenv('AWS_REGION', 'us-east-1')
    client = Mock()
    client.put_events.side_effect = lambda Entries: {
        'FailedEntryCount': 0, 'Entries': [{'EventId': f'e{i}'} for i in range(len(Entries))]}
    with patch.object(handler, 'eventbridge', client):
        yield client


def entries(client):
    """Every entry passed to put_events, across calls."""
    return [e for call in client.put_events.call_args_list for e in call[1]['Entries']]


def details(client):
    return [json.loads(e['Detail']) for e in entries(client)]


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

    def test_single_alert_processing(self, eventbridge, sample_alertmanager_event):
        result = handler.lambda_handler(sample_alertmanager_event, FakeContext())

        assert result['statusCode'] == 202
        body = body_of(result)
        assert body['status'] == 'accepted'
        assert body['message'] == 'Alert received and queued for processing'
        assert body['alert_count'] == 1
        assert body['correlation_id']
        assert body['incidents'][0]['alertname'] == 'CheckoutHighErrorRate'
        assert body['incidents'][0]['status'] == 'firing'
        eventbridge.put_events.assert_called_once()

    def test_multiple_alert_processing(self, eventbridge, sample_alertmanager_event_multiple):
        result = handler.lambda_handler(sample_alertmanager_event_multiple, FakeContext())

        assert result['statusCode'] == 202
        assert body_of(result)['alert_count'] == 2
        assert [d['alertname'] for d in details(eventbridge)] == ['CheckoutHighErrorRate', 'CheckoutHighLatencyP95']
        eventbridge.put_events.assert_called_once()  # one batch

    def test_base64_encoded_payload(self, eventbridge, sample_base64_event):
        result = handler.lambda_handler(sample_base64_event, FakeContext())

        assert result['statusCode'] == 202
        assert body_of(result)['incidents'][0]['alertname'] == 'CheckoutDown'

    def test_enrichment(self, eventbridge):
        handler.lambda_handler(request({'alerts': [am_alert()]}), FakeContext())

        detail = details(eventbridge)[0]
        assert detail['account_id'] == '123456789012'
        assert detail['region'] == 'us-east-1'
        assert detail['received_at'].endswith('Z')
        assert isinstance(detail['timestamp'], int)
        assert detail['source'] == 'alertmanager'

    def test_correlation_id_shared_across_one_request(self, eventbridge):
        payload = {'alerts': [am_alert(), am_alert(alertname='CheckoutDown', fingerprint='ffff')]}
        result = handler.lambda_handler(request(payload), FakeContext())

        assert {d['correlation_id'] for d in details(eventbridge)} == {body_of(result)['correlation_id']}


class TestBatching:

    @pytest.mark.parametrize('count,calls', [(1, 1), (10, 1), (11, 2), (25, 3)])
    def test_put_events_in_batches_of_ten(self, eventbridge, count, calls):
        payload = {'alerts': [am_alert(fingerprint=f'fp{i}') for i in range(count)]}
        result = handler.lambda_handler(request(payload), FakeContext())

        assert result['statusCode'] == 202
        assert eventbridge.put_events.call_count == calls
        assert all(len(c[1]['Entries']) <= 10 for c in eventbridge.put_events.call_args_list)
        assert len(entries(eventbridge)) == count


class TestIncidentEvents:

    def test_firing_is_incident_created(self, eventbridge):
        handler.lambda_handler(request({'alerts': [am_alert()]}), FakeContext())

        entry = entries(eventbridge)[0]
        assert entry['EventBusName'] == 'test-event-bus'
        assert entry['Source'] == 'sre.alertmanager'
        assert entry['DetailType'] == 'incident.created'

    def test_resolved_is_incident_resolved(self, eventbridge):
        result = handler.lambda_handler(request({'alerts': [am_alert(status='resolved', endsAt='2026-10-20T10:40:00Z')]}), FakeContext())

        assert result['statusCode'] == 202
        assert body_of(result)['incidents'][0]['status'] == 'resolved'
        entry = entries(eventbridge)[0]
        assert entry['DetailType'] == 'incident.resolved'
        assert json.loads(entry['Detail'])['ends_at'] == '2026-10-20T10:40:00Z'

    def test_firing_and_resolved_share_incident_id(self, eventbridge):
        handler.lambda_handler(request({'alerts': [am_alert(status='firing')]}), FakeContext())
        handler.lambda_handler(request({'alerts': [am_alert(status='resolved')]}), FakeContext())

        fired, resolved = details(eventbridge)
        assert fired['incident_id'] == resolved['incident_id']

    def test_resent_alert_keeps_incident_id(self, eventbridge):
        handler.lambda_handler(request({'alerts': [am_alert()]}), FakeContext())
        handler.lambda_handler(request({'alerts': [am_alert()]}), FakeContext())

        first, second = details(eventbridge)
        assert first['incident_id'] == second['incident_id']
        assert first['correlation_id'] != second['correlation_id']

    def test_different_start_times_are_different_incidents(self, eventbridge):
        handler.lambda_handler(request({'alerts': [am_alert(starts_at='2026-10-20T10:00:00Z')]}), FakeContext())
        handler.lambda_handler(request({'alerts': [am_alert(starts_at='2026-10-21T10:00:00Z')]}), FakeContext())

        assert len({d['incident_id'] for d in details(eventbridge)}) == 2

    def test_without_fingerprint_each_alert_is_new(self, eventbridge):
        alert = am_alert()
        del alert['fingerprint']
        handler.lambda_handler(request({'alerts': [alert]}), FakeContext())
        handler.lambda_handler(request({'alerts': [alert]}), FakeContext())

        assert len({d['incident_id'] for d in details(eventbridge)}) == 2


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

    def test_triggered(self, eventbridge):
        result = handler.lambda_handler(self.datadog(), FakeContext())

        assert result['statusCode'] == 202
        entry = entries(eventbridge)[0]
        assert entry['Source'] == 'sre.datadog'
        assert entry['DetailType'] == 'incident.created'
        detail = json.loads(entry['Detail'])
        assert detail['alertname'] == 'CheckoutHighErrorRate'
        assert detail['service'] == 'checkout'
        assert detail['severity'] == 'critical'  # P2
        assert detail['labels']['namespace'] == 'apps'
        assert detail['starts_at'] == '2025-10-20T22:40:00Z'

    def test_recovered_closes_same_incident(self, eventbridge):
        handler.lambda_handler(self.datadog('Triggered'), FakeContext())
        handler.lambda_handler(self.datadog('Recovered'), FakeContext())

        triggered, recovered = entries(eventbridge)
        assert recovered['DetailType'] == 'incident.resolved'
        assert json.loads(triggered['Detail'])['incident_id'] == json.loads(recovered['Detail'])['incident_id']

    def test_tags_as_array_and_severity_tag(self, eventbridge):
        handler.lambda_handler(self.datadog(tags=['service:payments', 'severity:info']), FakeContext())

        detail = details(eventbridge)[0]
        assert detail['service'] == 'payments'
        assert detail['severity'] == 'info'
        assert detail['alertname'] == 'CheckoutHighErrorRate on checkout service'  # no alertname tag, so the title

    @pytest.mark.parametrize('happened', [1761000000, '1761000000', 1761000000000, '1761000000000'])
    def test_date_in_seconds_or_milliseconds(self, eventbridge, happened):
        result = handler.lambda_handler(self.datadog(date_happened=happened), FakeContext())

        assert result['statusCode'] == 202
        assert details(eventbridge)[0]['starts_at'] == '2025-10-20T22:40:00Z'

    def test_unknown_priority_defaults_to_warning(self, eventbridge):
        handler.lambda_handler(self.datadog(priority='normal'), FakeContext())

        assert details(eventbridge)[0]['severity'] == 'warning'

    def test_invalid_severity_tag(self, eventbridge):
        result = handler.lambda_handler(self.datadog(tags='severity:page'), FakeContext())

        assert result['statusCode'] == 400
        assert body_of(result)['error_code'] == 'INVALID_SEVERITY'

    def test_missing_alert_id(self, eventbridge):
        result = handler.lambda_handler(self.datadog(alert_id=None), FakeContext())

        assert result['statusCode'] == 400
        assert body_of(result)['details']['field'] == 'alert_id'

    def test_missing_alertname_and_title(self, eventbridge):
        result = handler.lambda_handler(self.datadog(title='', tags='service:checkout'), FakeContext())

        assert result['statusCode'] == 400
        assert body_of(result)['error_code'] == 'MISSING_ALERT_NAME'


# ---------------------------------------------------------------- validation

class TestErrorHandling:

    @pytest.mark.parametrize('event', [{}, {'body': ''}, {'body': None}])
    def test_empty_body(self, eventbridge, event):
        result = handler.lambda_handler(event, FakeContext())

        assert result['statusCode'] == 400
        body = body_of(result)
        assert body['status'] == 'error'
        assert body['error_code'] == 'INVALID_PAYLOAD'

    def test_malformed_json_body(self, eventbridge):
        result = handler.lambda_handler({'body': 'invalid json'}, FakeContext())

        assert result['statusCode'] == 400
        assert body_of(result)['details']['received'] == 'invalid JSON'

    def test_body_not_an_object(self, eventbridge):
        result = handler.lambda_handler(request([1, 2]), FakeContext())

        assert result['statusCode'] == 400
        assert body_of(result)['details'] == {'field': 'body', 'expected': 'object', 'received': 'array'}

    def test_no_alerts_in_payload(self, eventbridge):
        result = handler.lambda_handler(request({'some_other_field': 'value'}), FakeContext())

        assert result['statusCode'] == 400
        body = body_of(result)
        assert body['message'] == "Invalid alert payload: missing required field 'alerts'"
        assert body['details'] == {'field': 'alerts', 'expected': 'array', 'received': 'null'}

    def test_empty_alerts_array(self, eventbridge):
        result = handler.lambda_handler(request({'alerts': []}), FakeContext())

        assert result['statusCode'] == 400
        assert body_of(result)['details']['received'] == 'empty array'

    @pytest.mark.parametrize('alert,field', [
        ('not an object', 'alerts[0]'),
        ({'status': 'firing', 'labels': 'x'}, 'alerts[0].labels'),
    ])
    def test_malformed_alert(self, eventbridge, alert, field):
        result = handler.lambda_handler(request({'alerts': [alert]}), FakeContext())

        assert result['statusCode'] == 400
        assert body_of(result)['details']['field'] == field

    def test_missing_alertname(self, eventbridge):
        alert = am_alert()
        del alert['labels']['alertname']
        result = handler.lambda_handler(request({'alerts': [alert]}), FakeContext())

        assert result['statusCode'] == 400
        body = body_of(result)
        assert body['error_code'] == 'MISSING_ALERT_NAME'
        assert body['details']['field'] == 'alerts[0].labels.alertname'

    @pytest.mark.parametrize('severity', [None, 'page', 'CRITICAL', ''])
    def test_invalid_severity(self, eventbridge, severity):
        result = handler.lambda_handler(request({'alerts': [am_alert(severity=severity)]}), FakeContext())

        assert result['statusCode'] == 400
        assert body_of(result)['error_code'] == 'INVALID_SEVERITY'

    def test_invalid_status(self, eventbridge):
        result = handler.lambda_handler(request({'alerts': [am_alert(status='pending')]}), FakeContext())

        assert result['statusCode'] == 400
        assert body_of(result)['details']['field'] == 'alerts[0].status'

    def test_one_bad_alert_rejects_the_whole_request(self, eventbridge):
        payload = {'alerts': [am_alert(), am_alert(severity='page', fingerprint='ffff')]}
        result = handler.lambda_handler(request(payload), FakeContext())

        assert result['statusCode'] == 400
        eventbridge.put_events.assert_not_called()


class TestAWSFailures:

    def test_eventbridge_rejects_entry(self, eventbridge):
        eventbridge.put_events.side_effect = None
        eventbridge.put_events.return_value = {'FailedEntryCount': 1, 'Entries': [{'ErrorCode': 'InternalFailure'}]}

        result = handler.lambda_handler(request({'alerts': [am_alert()]}), FakeContext())

        assert result['statusCode'] == 502
        body = body_of(result)
        assert body['error_code'] == 'EVENTBRIDGE_ERROR'
        assert body['details']['correlation_id']
        assert 'InternalFailure' in body['details']['reason']

    def test_eventbridge_client_error(self, eventbridge):
        eventbridge.put_events.side_effect = ClientError(
            {'Error': {'Code': 'AccessDeniedException', 'Message': 'no'}}, 'PutEvents')

        result = handler.lambda_handler(request({'alerts': [am_alert()]}), FakeContext())

        assert result['statusCode'] == 502

    def test_unexpected_error(self, eventbridge):
        eventbridge.put_events.side_effect = RuntimeError('boom')

        result = handler.lambda_handler(request({'alerts': [am_alert()]}), FakeContext())

        assert result['statusCode'] == 500
        assert body_of(result)['error_code'] == 'INTERNAL_ERROR'

    def test_second_batch_fails_after_first_published(self, eventbridge):
        calls = []

        def put_events(Entries):
            calls.append(len(Entries))
            if len(calls) == 2:
                return {'FailedEntryCount': 1, 'Entries': [{'ErrorCode': 'ThrottlingException'}]}
            return {'FailedEntryCount': 0, 'Entries': [{} for _ in Entries]}

        eventbridge.put_events.side_effect = put_events
        payload = {'alerts': [am_alert(fingerprint=f'fp{i}') for i in range(12)]}

        result = handler.lambda_handler(request(payload), FakeContext())

        assert result['statusCode'] == 502
        assert calls == [10, 2]
