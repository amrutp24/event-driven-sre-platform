# Integration Tests for Event-Driven SRE Platform

This directory contains end-to-end integration tests that verify the complete alert pipeline from webhook ingestion to Kubernetes remediation.

## Overview

Integration tests simulate the full infrastructure using:
- **LocalStack**: Local AWS services (Lambda, EventBridge, Step Functions, DynamoDB, SNS, API Gateway)
- **Kind/K3s**: Local Kubernetes cluster for remediation testing
- **Mock Services**: Controlled testing environment without cloud dependencies

## Test Coverage

### Complete Alert Flow Tests
- ✅ Alertmanager webhook → API Gateway → alert_ingest Lambda
- ✅ alert_ingest Lambda → EventBridge → Step Functions → DynamoDB → SNS
- ✅ Step Functions → runbook_action Lambda
- ✅ runbook_action Lambda → Kubernetes API remediation
- ✅ Complete incident response workflow verification

### Error Scenario Tests
- ✅ Malformed webhook payload handling
- ✅ Lambda timeout scenarios
- ✅ Kubernetes API failure recovery
- ✅ Partial infrastructure failure handling

### Performance Tests
- ✅ High-volume alert processing
- ✅ Concurrent incident response
- ✅ Resource isolation and independence

## Quick Start

### 1. Set up test environment
```bash
chmod +x setup.sh
./setup.sh
```

### 2. Run integration tests
```bash
AWS_DEFAULT_REGION=us-east-1 python -m pytest tests/integration/ -v --cov
```

### 3. Clean up environment
```bash
chmod +x cleanup.sh
./cleanup.sh
```

## File Structure

```
tests/integration/
├── conftest.py              # Test fixtures and shared configuration
├── test_alert_pipeline.py   # Main integration test suite
├── requirements-dev.txt      # Integration test dependencies
├── setup.sh               # Environment setup script
├── cleanup.sh             # Environment cleanup script
└── README.md              # This file
```

## Test Architecture

```
Alertmanager
    ↓ (webhook)
API Gateway (LocalStack)
    ↓
alert_ingest Lambda
    ↓ (EventBridge)
Step Functions
    ↓ (DynamoDB + SNS)
runbook_action Lambda
    ↓ (Kubernetes API)
Kind Cluster (checkout deployment)
```

## Environment Variables

Integration tests use these environment variables:
- `AWS_DEFAULT_REGION`: AWS region (default: us-east-1)
- `LOCALSTACK_HOST`: LocalStack endpoint (default: localhost)
- `TEST_TIMEOUT`: Test timeout in seconds (default: 60)

## Mock Strategy

### AWS Services
- **LocalStack** provides real AWS APIs locally
- **DynamoDB**: Real database operations
- **EventBridge**: Real event routing and rules
- **Step Functions**: Real state machine execution
- **Lambda**: Real function execution with test code

### Kubernetes
- **Kind**: Real Kubernetes cluster
- **Mock EKS**: Simulated EKS authentication
- **Test Deployment**: Actual checkout service deployment

## Performance Benchmarks

Integration tests measure:
- **Alert Processing Time**: < 2 seconds from webhook to Lambda
- **EventBridge Routing**: < 1 second for event delivery
- **Step Functions Execution**: < 5 seconds for state machine completion
- **Remediation Time**: < 10 seconds from alert to K8s remediation
- **Total MTTR**: < 20 seconds end-to-end

## Troubleshooting

### LocalStack Issues
```bash
# Check LocalStack logs
docker logs localstack-sre

# Restart LocalStack
docker restart localstack-sre

# Verify service availability
curl http://localhost:4566/health
```

### Kubernetes Issues
```bash
# Check cluster status
kubectl cluster-info --context kind-sre-integration

# Verify deployment
kubectl get pods -n sre-test

# Check cluster events
kubectl get events -n sre-test
```

### Test Failures
```bash
# Run specific test
python -m pytest tests/integration/test_alert_pipeline.py::TestCompleteAlertFlow::test_complete_error_rate_incident_flow -v

# Run with debugging
python -m pytest tests/integration/ -v -s --tb=short
```

## Integration with CI/CD

Integration tests can be integrated into CI/CD pipelines:

```yaml
# Example GitHub Actions
- name: Run Integration Tests
  run: |
    docker compose -f tests/integration/docker-compose.yml up -d
    sleep 30
    AWS_DEFAULT_REGION=us-east-1 python -m pytest tests/integration/ --cov
    docker compose -f tests/integration/docker-compose.yml down
```

## Contributing

When adding new integration tests:
1. Use existing fixtures from `conftest.py`
2. Test both success and failure scenarios
3. Include performance measurements
4. Update documentation
5. Verify cleanup doesn't leave resources

## Security Considerations

Integration tests use:
- Local-only resources (no cloud credentials required)
- Isolated test environment
- Temporary credentials and resources
- Automated cleanup after each test
- No production data access