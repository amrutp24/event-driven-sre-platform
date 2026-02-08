"""
Unit tests for alert_ingest Lambda handler

Tests cover:
- Alert ingestion from Alertmanager webhook
- Base64 encoded payload handling
- Multiple alert processing
- EventBridge event publishing
- DynamoDB incident storage
- SNS notification publishing
- Step Functions execution triggering
- Error handling for malformed payloads
- Environment variable validation
"""
import json
import pytest
from unittest.mock import Mock, patch, MagicMock
import sys
import os

# Add the parent directory to import the handler
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Mock boto3 and set environment variables before importing handler to avoid region errors
test_env_vars = {
    'AWS_DEFAULT_REGION': 'us-east-1',
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


class TestAlertIngestion:
    """Test alert ingestion and processing"""

    @patch('handler.eventbridge')
    @patch('handler.ddb')
    @patch('handler.sns')
    @patch('handler.sfn')
    @patch.dict(os.environ, {
        'EVENT_BUS_NAME': 'test-event-bus',
        'INCIDENT_TABLE': 'test-incidents',
        'RUNBOOK_ARN': 'arn:aws:states:us-east-1:123456789012:stateMachine:test-runbook',
        'SNS_TOPIC_ARN': 'arn:aws:sns:us-east-1:123456789012:test-topic',
        'RUNBOOK_ACTION_ARN': 'arn:aws:lambda:us-east-1:123456789012:function:test-action'
    })
    def test_single_alert_processing(self, mock_sfn, mock_sns, mock_ddb, mock_eventbridge, sample_alertmanager_event):
        """Test processing of a single alert"""
        # Setup mocks
        mock_table = Mock()
        mock_ddb.Table.return_value = mock_table
        mock_eventbridge.put_events.return_value = {'FailedEntryCount': 0}
        mock_sns.publish.return_value = {'MessageId': 'test-message-id'}
        mock_sfn.start_execution.return_value = {'executionArn': 'test-execution-arn'}

        # Execute
        result = handler.lambda_handler(sample_alertmanager_event, {})

        # Verify response
        assert result['statusCode'] == 200
        response_body = json.loads(result['body'])
        assert response_body['ok'] is True
        assert len(response_body['processed']) == 1
        assert 'incident_id' in response_body['processed'][0]
        assert response_body['processed'][0]['alertname'] == 'CheckoutHighErrorRate'

        # Verify AWS service calls
        mock_table.put_item.assert_called_once()
        mock_eventbridge.put_events.assert_called_once()
        mock_sns.publish.assert_called_once()
        mock_sfn.start_execution.assert_called_once()

    @patch('handler.eventbridge')
    @patch('handler.ddb')
    @patch('handler.sns')
    @patch('handler.sfn')
    @patch.dict(os.environ, {
        'EVENT_BUS_NAME': 'test-event-bus',
        'INCIDENT_TABLE': 'test-incidents',
        'RUNBOOK_ARN': 'arn:aws:states:us-east-1:123456789012:stateMachine:test-runbook',
        'SNS_TOPIC_ARN': 'arn:aws:sns:us-east-1:123456789012:test-topic'
    })
    def test_multiple_alert_processing(self, mock_sfn, mock_sns, mock_ddb, mock_eventbridge, sample_alertmanager_event_multiple):
        """Test processing of multiple alerts in one event"""
        # Setup mocks
        mock_table = Mock()
        mock_ddb.Table.return_value = mock_table
        mock_eventbridge.put_events.return_value = {'FailedEntryCount': 0}
        mock_sns.publish.return_value = {'MessageId': 'test-message-id'}
        mock_sfn.start_execution.return_value = {'executionArn': 'test-execution-arn'}

        # Execute
        result = handler.lambda_handler(sample_alertmanager_event_multiple, {})

        # Verify response
        assert result['statusCode'] == 200
        response_body = json.loads(result['body'])
        assert response_body['ok'] is True
        assert len(response_body['processed']) == 2

        # Verify each alert was processed
        processed_alerts = response_body['processed']
        alertnames = [alert['alertname'] for alert in processed_alerts]
        assert 'CheckoutHighErrorRate' in alertnames
        assert 'CheckoutHighLatencyP95' in alertnames

        # Verify AWS service calls (should be called twice for each service)
        assert mock_table.put_item.call_count == 2
        assert mock_eventbridge.put_events.call_count == 2
        assert mock_sns.publish.call_count == 2
        assert mock_sfn.start_execution.call_count == 2

    @patch('handler.eventbridge')
    @patch('handler.ddb')
    @patch('handler.sns')
    @patch('handler.sfn')
    @patch.dict(os.environ, {
        'EVENT_BUS_NAME': 'test-event-bus',
        'INCIDENT_TABLE': 'test-incidents',
        'RUNBOOK_ARN': 'arn:aws:states:us-east-1:123456789012:stateMachine:test-runbook',
        'SNS_TOPIC_ARN': 'arn:aws:sns:us-east-1:123456789012:test-topic'
    })
    def test_base64_encoded_payload(self, mock_sfn, mock_sns, mock_ddb, mock_eventbridge, sample_base64_event):
        """Test handling of base64 encoded payloads"""
        # Setup mocks
        mock_table = Mock()
        mock_ddb.Table.return_value = mock_table
        mock_eventbridge.put_events.return_value = {'FailedEntryCount': 0}
        mock_sns.publish.return_value = {'MessageId': 'test-message-id'}
        mock_sfn.start_execution.return_value = {'executionArn': 'test-execution-arn'}

        # Execute
        result = handler.lambda_handler(sample_base64_event, {})

        # Verify response
        assert result['statusCode'] == 200
        response_body = json.loads(result['body'])
        assert response_body['ok'] is True
        assert len(response_body['processed']) == 1

        # Verify AWS service calls
        mock_table.put_item.assert_called_once()
        mock_eventbridge.put_events.assert_called_once()
        mock_sns.publish.assert_called_once()
        mock_sfn.start_execution.assert_called_once()


class TestAlertNormalization:
    """Test alert normalization and data transformation"""

    @patch('handler.eventbridge')
    @patch('handler.ddb')
    @patch('handler.sns')
    @patch('handler.sfn')
    @patch.dict(os.environ, {
        'EVENT_BUS_NAME': 'test-event-bus',
        'INCIDENT_TABLE': 'test-incidents',
        'RUNBOOK_ARN': 'arn:aws:states:us-east-1:123456789012:stateMachine:test-runbook',
        'SNS_TOPIC_ARN': 'arn:aws:sns:us-east-1:123456789012:test-topic'
    })
    def test_alert_with_missing_fields(self, mock_sfn, mock_sns, mock_ddb, mock_eventbridge):
        """Test handling of alerts with missing optional fields"""
        # Create event with minimal alert data
        minimal_event = {
            "body": json.dumps({
                "alerts": [
                    {
                        "labels": {},
                        "annotations": {}
                    }
                ]
            })
        }

        # Setup mocks
        mock_table = Mock()
        mock_ddb.Table.return_value = mock_table
        mock_eventbridge.put_events.return_value = {'FailedEntryCount': 0}
        mock_sns.publish.return_value = {'MessageId': 'test-message-id'}
        mock_sfn.start_execution.return_value = {'executionArn': 'test-execution-arn'}

        # Execute
        result = handler.lambda_handler(minimal_event, {})

        # Verify response
        assert result['statusCode'] == 200
        response_body = json.loads(result['body'])
        assert response_body['ok'] is True

        # Verify default values were applied
        mock_table.put_item.assert_called_once()
        call_args = mock_table.put_item.call_args[1]['Item']
        assert call_args['severity'] == 'ticket'  # default severity
        assert call_args['service'] == 'unknown'  # default service
        assert call_args['alertname'] == 'UnknownAlert'  # default alertname
        assert call_args['status'] == 'firing'  # default status

    @patch('handler.eventbridge')
    @patch('handler.ddb')
    @patch('handler.sns')
    @patch('handler.sfn')
    @patch.dict(os.environ, {
        'EVENT_BUS_NAME': 'test-event-bus',
        'INCIDENT_TABLE': 'test-incidents',
        'RUNBOOK_ARN': 'arn:aws:states:us-east-1:123456789012:stateMachine:test-runbook',
        'SNS_TOPIC_ARN': 'arn:aws:sns:us-east-1:123456789012:test-topic'
    })
    def test_alert_with_all_fields(self, mock_sfn, mock_sns, mock_ddb, mock_eventbridge):
        """Test handling of alerts with all possible fields"""
        complete_event = {
            "body": json.dumps({
                "alerts": [
                    {
                        "labels": {
                            "alertname": "CustomAlert",
                            "severity": "warning",
                            "service": "payment",
                            "team": "backend"
                        },
                        "annotations": {
                            "description": "Custom alert description",
                            "runbook": "https://runbook.url"
                        },
                        "status": "resolved"
                    }
                ]
            })
        }

        # Setup mocks
        mock_table = Mock()
        mock_ddb.Table.return_value = mock_table
        mock_eventbridge.put_events.return_value = {'FailedEntryCount': 0}
        mock_sns.publish.return_value = {'MessageId': 'test-message-id'}
        mock_sfn.start_execution.return_value = {'executionArn': 'test-execution-arn'}

        # Execute
        result = handler.lambda_handler(complete_event, {})

        # Verify response
        assert result['statusCode'] == 200
        response_body = json.loads(result['body'])
        assert response_body['ok'] is True

        # Verify all fields were properly stored
        mock_table.put_item.assert_called_once()
        call_args = mock_table.put_item.call_args[1]['Item']
        assert call_args['severity'] == 'warning'
        assert call_args['service'] == 'payment'
        assert call_args['alertname'] == 'CustomAlert'
        assert call_args['status'] == 'resolved'


class TestErrorHandling:
    """Test error handling and edge cases"""

    def test_empty_body(self):
        """Test handling of event with empty body"""
        empty_event = {"body": ""}
        
        result = handler.lambda_handler(empty_event, {})
        
        assert result['statusCode'] == 200
        response_body = json.loads(result['body'])
        assert response_body['ok'] is True
        assert len(response_body['processed']) == 0

    def test_malformed_json_body(self):
        """Test handling of malformed JSON in body"""
        malformed_event = {"body": "invalid json"}
        
        # Should raise json.JSONDecodeError
        with pytest.raises(json.JSONDecodeError):
            handler.lambda_handler(malformed_event, {})

    def test_no_alerts_in_payload(self):
        """Test handling of payload with no alerts array"""
        no_alerts_event = {
            "body": json.dumps({
                "some_other_field": "value"
            })
        }
        
        result = handler.lambda_handler(no_alerts_event, {})
        
        assert result['statusCode'] == 200
        response_body = json.loads(result['body'])
        assert response_body['ok'] is True
        assert len(response_body['processed']) == 0

    def test_empty_alerts_array(self):
        """Test handling of payload with empty alerts array"""
        empty_alerts_event = {
            "body": json.dumps({
                "alerts": []
            })
        }
        
        result = handler.lambda_handler(empty_alerts_event, {})
        
        assert result['statusCode'] == 200
        response_body = json.loads(result['body'])
        assert response_body['ok'] is True
        assert len(response_body['processed']) == 0

    def test_missing_body_key(self):
        """Test handling of event with no body key"""
        no_body_event = {}
        
        result = handler.lambda_handler(no_body_event, {})
        
        assert result['statusCode'] == 200
        response_body = json.loads(result['body'])
        assert response_body['ok'] is True
        assert len(response_body['processed']) == 0


class TestAWSServiceIntegration:
    """Test AWS service integration details"""

    @patch('handler.eventbridge')
    @patch('handler.ddb')
    @patch('handler.sns')
    @patch('handler.sfn')
    @patch.dict(os.environ, {
        'EVENT_BUS_NAME': 'test-event-bus',
        'INCIDENT_TABLE': 'test-incidents',
        'RUNBOOK_ARN': 'arn:aws:states:us-east-1:123456789012:stateMachine:test-runbook',
        'SNS_TOPIC_ARN': 'arn:aws:sns:us-east-1:123456789012:test-topic'
    })
    def test_eventbridge_event_format(self, mock_sfn, mock_sns, mock_ddb, mock_eventbridge, sample_alertmanager_event):
        """Test EventBridge event is formatted correctly"""
        mock_table = Mock()
        mock_ddb.Table.return_value = mock_table
        mock_eventbridge.put_events.return_value = {'FailedEntryCount': 0}
        mock_sns.publish.return_value = {'MessageId': 'test-message-id'}
        mock_sfn.start_execution.return_value = {'executionArn': 'test-execution-arn'}

        handler.lambda_handler(sample_alertmanager_event, {})

        # Verify EventBridge event format
        mock_eventbridge.put_events.assert_called_once()
        call_args = mock_eventbridge.put_events.call_args[1]['Entries'][0]
        assert call_args['EventBusName'] == 'test-event-bus'
        assert call_args['Source'] == 'prometheus.alertmanager'
        assert call_args['DetailType'] == 'SREAlert'
        
        # Verify Detail contains normalized alert data
        detail = json.loads(call_args['Detail'])
        assert 'incident_id' in detail
        assert 'timestamp' in detail
        assert detail['alertname'] == 'CheckoutHighErrorRate'
        assert detail['severity'] == 'critical'

    @patch('handler.eventbridge')
    @patch('handler.ddb')
    @patch('handler.sns')
    @patch('handler.sfn')
    @patch.dict(os.environ, {
        'EVENT_BUS_NAME': 'test-event-bus',
        'INCIDENT_TABLE': 'test-incidents',
        'RUNBOOK_ARN': 'arn:aws:states:us-east-1:123456789012:stateMachine:test-runbook',
        'SNS_TOPIC_ARN': 'arn:aws:sns:us-east-1:123456789012:test-topic'
    })
    def test_sns_notification_format(self, mock_sfn, mock_sns, mock_ddb, mock_eventbridge, sample_alertmanager_event):
        """Test SNS notification is formatted correctly"""
        mock_table = Mock()
        mock_ddb.Table.return_value = mock_table
        mock_eventbridge.put_events.return_value = {'FailedEntryCount': 0}
        mock_sns.publish.return_value = {'MessageId': 'test-message-id'}
        mock_sfn.start_execution.return_value = {'executionArn': 'test-execution-arn'}

        handler.lambda_handler(sample_alertmanager_event, {})

        # Verify SNS notification format
        mock_sns.publish.assert_called_once()
        call_args = mock_sns.publish.call_args
        assert call_args[1]['TopicArn'] == 'arn:aws:sns:us-east-1:123456789012:test-topic'
        assert '[CRITICAL] checkout - CheckoutHighErrorRate (firing)' == call_args[1]['Subject']
        
        # Verify message contains incident details
        message = call_args[1]['Message']
        incident_data = json.loads(message)
        assert incident_data['alertname'] == 'CheckoutHighErrorRate'
        assert incident_data['severity'] == 'critical'

    @patch('handler.eventbridge')
    @patch('handler.ddb')
    @patch('handler.sns')
    @patch('handler.sfn')
    @patch.dict(os.environ, {
        'EVENT_BUS_NAME': 'test-event-bus',
        'INCIDENT_TABLE': 'test-incidents',
        'RUNBOOK_ARN': 'arn:aws:states:us-east-1:123456789012:stateMachine:test-runbook',
        'SNS_TOPIC_ARN': 'arn:aws:sns:us-east-1:123456789012:test-topic'
    })
    def test_step_functions_input_format(self, mock_sfn, mock_sns, mock_ddb, mock_eventbridge, sample_alertmanager_event):
        """Test Step Functions execution input is formatted correctly"""
        mock_table = Mock()
        mock_ddb.Table.return_value = mock_table
        mock_eventbridge.put_events.return_value = {'FailedEntryCount': 0}
        mock_sns.publish.return_value = {'MessageId': 'test-message-id'}
        mock_sfn.start_execution.return_value = {'executionArn': 'test-execution-arn'}

        handler.lambda_handler(sample_alertmanager_event, {})

        # Verify Step Functions execution call
        mock_sfn.start_execution.assert_called_once()
        call_args = mock_sfn.start_execution.call_args
        assert call_args[1]['stateMachineArn'] == 'arn:aws:states:us-east-1:123456789012:stateMachine:test-runbook'
        
        # Verify input contains normalized alert data
        input_data = json.loads(call_args[1]['input'])
        assert 'incident_id' in input_data
        assert input_data['alertname'] == 'CheckoutHighErrorRate'
        assert input_data['severity'] == 'critical'