"""
Test configuration and fixtures for runbook_action Lambda
"""
import pytest
import json
from unittest.mock import Mock, patch, MagicMock
import sys
import os

# Add the parent directory to import the handlers
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


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