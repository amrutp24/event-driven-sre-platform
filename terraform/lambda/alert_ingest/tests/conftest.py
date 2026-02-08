"""
Test configuration and fixtures for Lambda function unit tests
"""
import pytest
import json
from unittest.mock import Mock, patch
import sys
import os

# Add the parent directory to the path so we can import the handlers
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture
def mock_env_vars():
    """Mock environment variables for Lambda functions"""
    return {
        'EVENT_BUS_NAME': 'test-event-bus',
        'INCIDENT_TABLE': 'test-incidents',
        'RUNBOOK_ARN': 'arn:aws:states:us-east-1:123456789012:stateMachine:test-runbook',
        'SNS_TOPIC_ARN': 'arn:aws:sns:us-east-1:123456789012:test-topic',
        'RUNBOOK_ACTION_ARN': 'arn:aws:lambda:us-east-1:123456789012:function:test-action',
        'REGION': 'us-east-1',
        'CLUSTER_NAME': 'test-cluster',
        'TARGET_NAMESPACE': 'test-namespace',
        'TARGET_DEPLOYMENT': 'test-deployment',
        'DEGRADED_PARAM': '/test/degraded_mode'
    }


@pytest.fixture
def sample_alertmanager_event():
    """Sample Alertmanager webhook event"""
    return {
        "body": json.dumps({
            "alerts": [
                {
                    "labels": {
                        "alertname": "CheckoutHighErrorRate",
                        "severity": "critical",
                        "service": "checkout"
                    },
                    "annotations": {
                        "description": "High error rate detected"
                    },
                    "status": "firing"
                }
            ]
        })
    }


@pytest.fixture
def sample_alertmanager_event_multiple():
    """Sample Alertmanager event with multiple alerts"""
    return {
        "body": json.dumps({
            "alerts": [
                {
                    "labels": {
                        "alertname": "CheckoutHighErrorRate",
                        "severity": "critical",
                        "service": "checkout"
                    },
                    "annotations": {
                        "description": "High error rate detected"
                    },
                    "status": "firing"
                },
                {
                    "labels": {
                        "alertname": "CheckoutHighLatencyP95",
                        "severity": "warning",
                        "service": "checkout"
                    },
                    "annotations": {
                        "description": "High latency detected"
                    },
                    "status": "firing"
                }
            ]
        })
    }


@pytest.fixture
def sample_base64_event():
    """Sample base64 encoded event"""
    import base64
    body = json.dumps({
        "alerts": [
            {
                "labels": {
                    "alertname": "CheckoutDown",
                    "severity": "critical",
                    "service": "checkout"
                },
                "status": "firing"
            }
        ]
    })
    return {
        "body": base64.b64encode(body.encode()).decode(),
        "isBase64Encoded": True
    }


@pytest.fixture
def mock_boto_clients():
    """Mock all AWS service clients"""
    return {
        'events': Mock(),
        'dynamodb': Mock(),
        'sns': Mock(),
        'stepfunctions': Mock(),
        'eks': Mock(),
        'ssm': Mock()
    }


@pytest.fixture
def mock_eks_cluster():
    """Mock EKS cluster description"""
    return {
        'cluster': {
            'endpoint': 'https://test-cluster.us-east-1.eks.amazonaws.com',
            'certificateAuthority': {
                'data': 'dGVzdC1jZXJ0aWZpY2F0ZS1hdXRo'  # base64 encoded 'test-certificate-auth'
            }
        }
    }


@pytest.fixture
def sample_normalized_alert():
    """Sample normalized alert event (input to runbook_action)"""
    return {
        "incident_id": "test-incident-123",
        "timestamp": 1704067200,
        "status": "firing",
        "severity": "critical",
        "service": "checkout",
        "alertname": "CheckoutHighErrorRate",
        "labels": {"service": "checkout"},
        "annotations": {"description": "High error rate"},
        "runbook_action_arn": "arn:aws:lambda:us-east-1:123456789012:function:test-action"
    }