"""
Integration tests for the Event-Driven SRE Platform

This module provides end-to-end testing of the complete alert pipeline:
- Alertmanager webhook → API Gateway
- alert_ingest Lambda → EventBridge → Step Functions → DynamoDB → SNS
- runbook_action Lambda → Kubernetes API
- Complete incident response flow

Integration tests use local infrastructure mocks (LocalStack, k3s/kind) to simulate
the full AWS and Kubernetes environment without requiring actual cloud resources.
"""
import pytest
import json
import time
import boto3
import requests
import os
import tempfile
import subprocess
from unittest.mock import Mock, patch, MagicMock
from typing import Dict, Any, List


@pytest.fixture(scope="session")
def localstack_endpoint():
    """LocalStack endpoint for AWS services"""
    return "http://localhost:4566"


@pytest.fixture(scope="session")
def aws_clients(localstack_endpoint):
    """AWS clients configured for LocalStack"""
    clients = {
        'events': boto3.client('events', endpoint_url=localstack_endpoint, region_name='us-east-1'),
        'lambda': boto3.client('lambda', endpoint_url=localstack_endpoint, region_name='us-east-1'),
        'stepfunctions': boto3.client('stepfunctions', endpoint_url=localstack_endpoint, region_name='us-east-1'),
        'dynamodb': boto3.client('dynamodb', endpoint_url=localstack_endpoint, region_name='us-east-1'),
        'sns': boto3.client('sns', endpoint_url=localstack_endpoint, region_name='us-east-1'),
        'apigateway': boto3.client('apigateway', endpoint_url=localstack_endpoint, region_name='us-east-1'),
        'iam': boto3.client('iam', endpoint_url=localstack_endpoint, region_name='us-east-1')
    }
    return clients


@pytest.fixture
def sample_alertmanager_payload():
    """Sample Alertmanager webhook payload"""
    return {
        "alerts": [
            {
                "labels": {
                    "alertname": "CheckoutHighErrorRate",
                    "severity": "critical",
                    "service": "checkout"
                },
                "annotations": {
                    "description": "High error rate detected in checkout service",
                    "runbook": "https://runbook.example.com/checkout-error-rate"
                },
                "status": "firing",
                "startsAt": "2026-02-07T10:00:00.000Z"
            }
        ],
        "groupLabels": {
            "alertname": "CheckoutHighErrorRate",
            "service": "checkout"
        },
        "commonLabels": {
            "alertname": "CheckoutHighErrorRate",
            "service": "checkout"
        },
        "commonAnnotations": {
            "description": "High error rate detected in checkout service"
        },
        "externalURL": "http://alertmanager.example.com",
        "version": "4",
        "groupKey": "{}:{alertname=\"CheckoutHighErrorRate\", service=\"checkout\"}",
        "truncatedAlerts": 0,
        "status": "firing",
        "receiver": "sre-webhook"
    }


@pytest.fixture
def sample_alertmanager_multiple_payload():
    """Sample Alertmanager payload with multiple alerts"""
    return {
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
        ],
        "status": "firing"
    }


@pytest.fixture
def test_infrastructure(aws_clients):
    """Set up test infrastructure for integration tests"""
    # Create test resources
    event_bus_name = "test-sre-events"
    incident_table = "test-sre-incidents"
    sns_topic_arn = "arn:aws:sns:us-east-1:123456789012:test-sre-notifications"
    runbook_arn = "arn:aws:states:us-east-1:123456789012:stateMachine:test-sre-runbook"
    
    # Create EventBridge event bus
    aws_clients['events'].create_event_bus(Name=event_bus_name)
    
    # Create DynamoDB table
    aws_clients['dynamodb'].create_table(
        TableName=incident_table,
        KeySchema=[
            {'AttributeName': 'incident_id', 'KeyType': 'HASH'},
            {'AttributeName': 'ts', 'KeyType': 'RANGE'}
        ],
        AttributeDefinitions=[
            {'AttributeName': 'incident_id', 'AttributeType': 'S'},
            {'AttributeName': 'ts', 'AttributeType': 'N'}
        ],
        BillingMode='PAY_PER_REQUEST'
    )
    
    # Create SNS topic
    aws_clients['sns'].create_topic(Name="test-sre-notifications")
    
    yield {
        'event_bus_name': event_bus_name,
        'incident_table': incident_table,
        'sns_topic_arn': sns_topic_arn,
        'runbook_arn': runbook_arn,
        'aws_clients': aws_clients
    }
    
    # Cleanup
    try:
        aws_clients['events'].delete_event_bus(Name=event_bus_name)
        aws_clients['dynamodb'].delete_table(TableName=incident_table)
        aws_clients['sns'].delete_topic(TopicArn=sns_topic_arn)
    except Exception:
        pass


@pytest.fixture
def mock_kubernetes_cluster():
    """Mock Kubernetes cluster for testing"""
    mock_cluster = {
        'endpoint': 'https://kubernetes.default.svc.cluster.local',
        'ca_cert': b'mock-ca-certificate-data',
        'namespace': 'test-namespace',
        'deployment': 'test-deployment'
    }
    return mock_cluster


class MockK8sResponse:
    """Mock Kubernetes API responses"""
    
    @staticmethod
    def deployment_response(name='test-deployment', namespace='test-namespace', replicas=3):
        return {
            'apiVersion': 'apps/v1',
            'kind': 'Deployment',
            'metadata': {
                'name': name,
                'namespace': namespace,
                'annotations': {}
            },
            'spec': {
                'replicas': replicas,
                'template': {
                    'spec': {
                        'containers': [{
                            'name': name,
                            'env': []
                        }]
                    }
                }
            }
        }
    
    @staticmethod
    def pods_response(deployment='test-deployment', namespace='test-namespace', count=3):
        return {
            'apiVersion': 'v1',
            'kind': 'PodList',
            'items': [
                {
                    'metadata': {
                        'name': f'{deployment}-{i}',
                        'namespace': namespace,
                        'labels': {'app': deployment}
                    },
                    'status': {
                        'phase': 'Running'
                    }
                } for i in range(count)
            ]
        }


def create_lambda_package(lambda_dir: str) -> bytes:
    """Create ZIP package for Lambda deployment"""
    import zipfile
    import io
    
    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, 'w') as zip_file:
        # Add handler.py
        handler_path = os.path.join(lambda_dir, 'handler.py')
        if os.path.exists(handler_path):
            zip_file.write(handler_path, 'handler.py')
        
        # Add any dependencies
        requirements_path = os.path.join(lambda_dir, 'requirements.txt')
        if os.path.exists(requirements_path):
            zip_file.writestr('requirements.txt', open(requirements_path).read())
    
    return zip_buffer.getvalue()


def wait_for_lambda_ready(lambda_client, function_name: str, timeout: int = 30):
    """Wait for Lambda function to be ready"""
    start_time = time.time()
    while time.time() - start_time < timeout:
        try:
            response = lambda_client.get_function(FunctionName=function_name)
            if response['Configuration']['State'] == 'Active':
                return True
        except Exception:
            pass
        time.sleep(1)
    return False