"""
Unit tests for runbook_action Lambda handler

Tests cover:
- EKS authentication and token generation
- Kubernetes API requests (patch, restart, scale)
- Each action, with the payload the runbook state machine sends
- Input validation (InvalidRequest) before any AWS or cluster call
- Environment variables: ALLOWED_NAMESPACES, MAX_REPLICAS, DEGRADED_PARAM, region
"""
import base64
import datetime
import os
import sys
import urllib.parse
from types import SimpleNamespace
from unittest.mock import Mock, patch

import botocore.session
import pytest

# Add the parent directory to import the handler
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# boto3 clients are created at import time, so mock them for the import
test_env_vars = {
    'AWS_DEFAULT_REGION': 'us-east-1',
    'REGION': 'us-east-1',
    'CLUSTER_NAME': 'test-cluster',
    'ALLOWED_NAMESPACES': 'apps',
    'DEGRADED_PARAM': '/checkout/degraded_mode'
}

with patch.dict(os.environ, test_env_vars), patch('boto3.client'):
    import handler

ENDPOINT = 'https://test-cluster.us-east-1.eks.amazonaws.com'


@pytest.fixture
def k8s():
    """Mocks the cluster connection, token, Kubernetes calls and SSM"""
    with patch('handler._cluster_conn', return_value=(ENDPOINT, b'test-ca')) as cluster, \
            patch('handler._eks_bearer_token', return_value='test-token') as token, \
            patch('handler._patch_deployment_env') as patch_env, \
            patch('handler._restart_deployment') as restart, \
            patch('handler._scale_deployment') as scale, \
            patch('handler.SSM') as ssm:
        yield SimpleNamespace(cluster=cluster, token=token, patch_env=patch_env,
                              restart=restart, scale=scale, ssm=ssm)


def assert_untouched(k8s):
    k8s.cluster.assert_not_called()
    k8s.token.assert_not_called()
    k8s.patch_env.assert_not_called()
    k8s.restart.assert_not_called()
    k8s.scale.assert_not_called()
    k8s.ssm.put_parameter.assert_not_called()


class TestEKSAuthentication:
    """Test EKS authentication and token generation"""

    def test_eks_bearer_token_generation(self):
        """Token is a presigned STS GetCallerIdentity URL, base64url without padding"""
        session = botocore.session.get_session()
        session.set_credentials('AKIDEXAMPLE', 'wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY')

        with patch('handler.botocore.session.get_session', return_value=session):
            token = handler._eks_bearer_token('test-cluster', 'us-east-1')

        assert token.startswith('k8s-aws-v1.')
        assert not token.endswith('=')

        encoded = token[len('k8s-aws-v1.'):]
        url = base64.urlsafe_b64decode(encoded + '=' * (-len(encoded) % 4)).decode('utf-8')
        query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)

        assert url.startswith('https://sts.amazonaws.com/')
        assert query['Action'] == ['GetCallerIdentity']
        assert query['X-Amz-Expires'] == ['60']
        assert 'x-k8s-aws-id' in query['X-Amz-SignedHeaders'][0]
        assert query['X-Amz-Credential'][0].startswith('AKIDEXAMPLE/')

    def test_cluster_connection_info(self):
        """Endpoint comes back as-is and the CA is base64-decoded"""
        with patch.object(handler, 'EKS') as eks:
            eks.describe_cluster.return_value = {
                'cluster': {
                    'endpoint': ENDPOINT,
                    'certificateAuthority': {'data': base64.b64encode(b'test-ca').decode()},
                }
            }
            endpoint, ca = handler._cluster_conn('test-cluster')

        eks.describe_cluster.assert_called_once_with(name='test-cluster')
        assert endpoint == ENDPOINT
        assert ca == b'test-ca'


class TestKubernetesAPIRequests:
    """Test Kubernetes API request functionality"""

    @patch('handler.requests.request')
    def test_k8s_request_success(self, mock_request):
        """Test successful Kubernetes API request"""
        # Mock successful response
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"status": "success"}
        mock_response.text = '{"status": "success"}'
        mock_request.return_value = mock_response

        # Execute
        result = handler._k8s_request(
            "GET",
            "https://test-cluster.us-east-1.eks.amazonaws.com/api/v1/namespaces/default/pods",
            "test-token",
            b"test-ca"
        )

        # Verify result
        assert result == {"status": "success"}
        mock_request.assert_called_once()

    @patch('handler.requests.request')
    def test_k8s_request_failure(self, mock_request):
        """Test Kubernetes API request failure"""
        # Mock failed response
        mock_response = Mock()
        mock_response.status_code = 403
        mock_response.text = "Forbidden"
        mock_request.return_value = mock_response

        # Execute and verify exception
        with pytest.raises(RuntimeError, match="K8s API GET .* failed: 403 Forbidden"):
            handler._k8s_request(
                "GET",
                "https://test-cluster.us-east-1.eks.amazonaws.com/api/v1/namespaces/default/pods",
                "test-token",
                b"test-ca"
            )

    @patch('handler.requests.request')
    def test_k8s_request_empty_response(self, mock_request):
        """Test Kubernetes API request with empty response"""
        # Mock empty response
        mock_response = Mock()
        mock_response.status_code = 204
        mock_response.text = ""
        mock_request.return_value = mock_response

        # Execute
        result = handler._k8s_request(
            "DELETE",
            "https://test-cluster.us-east-1.eks.amazonaws.com/api/v1/namespaces/default/pods/test-pod",
            "test-token",
            b"test-ca"
        )

        # Verify empty result
        assert result == {}


class TestDeploymentActions:
    """Test deployment-specific actions"""

    @patch('handler._k8s_request')
    def test_patch_deployment_env(self, mock_k8s_request):
        """Test patching deployment environment variables"""
        # Execute
        handler._patch_deployment_env(
            "https://test-cluster.us-east-1.eks.amazonaws.com",
            "test-token",
            b"test-ca",
            "test-namespace",
            "test-deployment",
            "TEST_VAR",
            "test-value"
        )

        # Verify K8s API call
        mock_k8s_request.assert_called_once()
        call_args = mock_k8s_request.call_args

        assert call_args[0][0] == "PATCH"
        assert "test-namespace" in call_args[0][1]
        assert "test-deployment" in call_args[0][1]

        # Verify patch body
        patch_body = call_args[1]['json_body']
        assert patch_body['spec']['template']['spec']['containers'][0]['name'] == 'test-deployment'
        assert patch_body['spec']['template']['spec']['containers'][0]['env'][0]['name'] == 'TEST_VAR'
        assert patch_body['spec']['template']['spec']['containers'][0]['env'][0]['value'] == 'test-value'

        # Verify headers
        headers = call_args[1]['headers']
        assert headers['Content-Type'] == 'application/strategic-merge-patch+json'

    @patch('handler._k8s_request')
    def test_restart_deployment(self, mock_k8s_request):
        """Test restarting deployment"""
        # Execute
        handler._restart_deployment(
            "https://test-cluster.us-east-1.eks.amazonaws.com",
            "test-token",
            b"test-ca",
            "test-namespace",
            "test-deployment"
        )

        # Verify K8s API call
        mock_k8s_request.assert_called_once()
        call_args = mock_k8s_request.call_args

        assert call_args[0][0] == "PATCH"

        # Verify patch body contains restart annotation
        patch_body = call_args[1]['json_body']
        assert 'kubectl.kubernetes.io/restartedAt' in patch_body['spec']['template']['metadata']['annotations']

        # Verify headers
        headers = call_args[1]['headers']
        assert headers['Content-Type'] == 'application/strategic-merge-patch+json'

    @patch('handler._k8s_request')
    def test_restart_annotation_is_utc_with_z(self, mock_k8s_request):
        """restartedAt is a UTC timestamp ending in Z, like kubectl writes"""
        handler._restart_deployment(ENDPOINT, "test-token", b"test-ca", "apps", "checkout")

        patch_body = mock_k8s_request.call_args[1]['json_body']
        restarted_at = patch_body['spec']['template']['metadata']['annotations']['kubectl.kubernetes.io/restartedAt']

        assert restarted_at.endswith('Z')
        assert '+00:00' not in restarted_at
        parsed = datetime.datetime.fromisoformat(restarted_at)
        age = datetime.datetime.now(datetime.UTC) - parsed
        assert datetime.timedelta(0) <= age < datetime.timedelta(minutes=1)

    @patch('handler._k8s_request')
    def test_scale_deployment(self, mock_k8s_request):
        """Test scaling deployment"""
        # Execute
        handler._scale_deployment(
            "https://test-cluster.us-east-1.eks.amazonaws.com",
            "test-token",
            b"test-ca",
            "test-namespace",
            "test-deployment",
            5
        )

        # Verify K8s API call
        mock_k8s_request.assert_called_once()
        call_args = mock_k8s_request.call_args

        assert call_args[0][0] == "PATCH"

        # Verify patch body
        patch_body = call_args[1]['json_body']
        assert patch_body['spec']['replicas'] == 5

        # Verify headers
        headers = call_args[1]['headers']
        assert headers['Content-Type'] == 'application/merge-patch+json'


class TestActions:
    """Each action, run with the payload the state machine sends"""

    def test_degrade(self, k8s, runbook_payload):
        runbook_payload.update(action='degrade')
        del runbook_payload['replicas']

        result = handler.lambda_handler(runbook_payload, {})

        k8s.ssm.put_parameter.assert_called_once_with(
            Name='/checkout/degraded_mode', Value='true', Type='String', Overwrite=True
        )
        k8s.patch_env.assert_called_once_with(
            ENDPOINT, 'test-token', b'test-ca', 'apps', 'checkout', 'DEGRADED_MODE', 'true'
        )
        k8s.restart.assert_called_once_with(ENDPOINT, 'test-token', b'test-ca', 'apps', 'checkout')
        k8s.scale.assert_not_called()
        assert result['degraded'] is True
        assert 'scaled_to' not in result

    def test_restart(self, k8s, runbook_payload):
        runbook_payload.update(action='restart', alertname='CheckoutDown')

        result = handler.lambda_handler(runbook_payload, {})

        k8s.restart.assert_called_once_with(ENDPOINT, 'test-token', b'test-ca', 'apps', 'checkout')
        k8s.patch_env.assert_not_called()
        k8s.scale.assert_not_called()
        k8s.ssm.put_parameter.assert_not_called()
        assert 'degraded' not in result
        assert 'scaled_to' not in result

    def test_scale(self, k8s, runbook_payload):
        runbook_payload.update(action='scale', replicas=6)

        result = handler.lambda_handler(runbook_payload, {})

        k8s.scale.assert_called_once_with(ENDPOINT, 'test-token', b'test-ca', 'apps', 'checkout', 6)
        k8s.restart.assert_not_called()
        k8s.patch_env.assert_not_called()
        k8s.ssm.put_parameter.assert_not_called()
        assert result['scaled_to'] == 6
        assert 'degraded' not in result

    def test_degrade_or_scale(self, k8s, runbook_payload):
        result = handler.lambda_handler(runbook_payload, {})

        k8s.ssm.put_parameter.assert_called_once()
        k8s.patch_env.assert_called_once_with(
            ENDPOINT, 'test-token', b'test-ca', 'apps', 'checkout', 'DEGRADED_MODE', 'true'
        )
        k8s.restart.assert_called_once()
        k8s.scale.assert_called_once_with(ENDPOINT, 'test-token', b'test-ca', 'apps', 'checkout', 4)
        assert result['degraded'] is True
        assert result['scaled_to'] == 4

    def test_target_comes_from_payload(self, k8s, runbook_payload, monkeypatch):
        monkeypatch.setenv('ALLOWED_NAMESPACES', 'apps,payments')
        runbook_payload.update(action='restart', namespace='payments', deployment='payments-api')

        result = handler.lambda_handler(runbook_payload, {})

        k8s.restart.assert_called_once_with(ENDPOINT, 'test-token', b'test-ca', 'payments', 'payments-api')
        assert result['namespace'] == 'payments'
        assert result['deployment'] == 'payments-api'

    def test_return_shape(self, k8s, runbook_payload):
        runbook_payload.update(action='restart')

        result = handler.lambda_handler(runbook_payload, {})

        assert result == {
            'incident_id': 'test-incident-123',
            'action': 'restart',
            'alertname': 'CheckoutHighErrorRate',
            'severity': 'critical',
            'namespace': 'apps',
            'deployment': 'checkout',
        }

    @pytest.mark.parametrize('replicas', [6, '6', ' 6 ', 6.0])
    def test_replicas_number_or_string(self, k8s, runbook_payload, replicas):
        runbook_payload.update(action='scale', replicas=replicas)

        result = handler.lambda_handler(runbook_payload, {})

        assert result['scaled_to'] == 6
        k8s.scale.assert_called_once_with(ENDPOINT, 'test-token', b'test-ca', 'apps', 'checkout', 6)

    def test_restart_ignores_replicas(self, k8s, runbook_payload):
        """replicas only matters for the scaling actions"""
        runbook_payload.update(action='restart', replicas='not-a-number')

        handler.lambda_handler(runbook_payload, {})

        k8s.restart.assert_called_once()


class TestInvalidRequest:
    """Bad input raises InvalidRequest before anything touches AWS or the cluster"""

    def test_error_name_is_the_class_name(self):
        # Step Functions matches on this name to skip retries
        assert handler.InvalidRequest.__name__ == 'InvalidRequest'
        assert issubclass(handler.InvalidRequest, Exception)

    @pytest.mark.parametrize('action', [None, '', 'notify_only', 'drain_node', 'DEGRADE', 'unknown_action'])
    def test_unknown_or_missing_action(self, k8s, runbook_payload, action):
        if action is None:
            del runbook_payload['action']
        else:
            runbook_payload['action'] = action

        with pytest.raises(handler.InvalidRequest, match='Unknown action'):
            handler.lambda_handler(runbook_payload, {})
        assert_untouched(k8s)

    @pytest.mark.parametrize('field', ['namespace', 'deployment'])
    def test_missing_target(self, k8s, runbook_payload, field):
        del runbook_payload[field]

        with pytest.raises(handler.InvalidRequest, match='namespace and deployment are required'):
            handler.lambda_handler(runbook_payload, {})
        assert_untouched(k8s)

    @pytest.mark.parametrize('namespace', ['kube-system', 'monitoring', 'Apps'])
    def test_namespace_not_allowed(self, k8s, runbook_payload, namespace):
        runbook_payload['namespace'] = namespace

        with pytest.raises(handler.InvalidRequest, match='ALLOWED_NAMESPACES'):
            handler.lambda_handler(runbook_payload, {})
        assert_untouched(k8s)

    @pytest.mark.parametrize('deployment', [
        'Checkout', 'check_out', '-checkout', 'checkout-', 'checkout.v2', 'a' * 64, 'apps/checkout', 42,
    ])
    def test_bad_deployment_name(self, k8s, runbook_payload, deployment):
        runbook_payload['deployment'] = deployment

        with pytest.raises(handler.InvalidRequest, match="isn't a valid Kubernetes name"):
            handler.lambda_handler(runbook_payload, {})
        assert_untouched(k8s)

    def test_longest_deployment_name_is_fine(self, k8s, runbook_payload):
        runbook_payload.update(action='restart', deployment='a' * 63)

        handler.lambda_handler(runbook_payload, {})

        k8s.restart.assert_called_once()

    @pytest.mark.parametrize('action', ['scale', 'degrade_or_scale'])
    @pytest.mark.parametrize('replicas', [None, 'four', '', '-1', '2.5', 2.5, True, [4], 0, '0', 11, '11'])
    def test_bad_replicas(self, k8s, runbook_payload, action, replicas):
        runbook_payload['action'] = action
        if replicas is None:
            del runbook_payload['replicas']
        else:
            runbook_payload['replicas'] = replicas

        with pytest.raises(handler.InvalidRequest, match='replicas must be a whole number from 1 to 10'):
            handler.lambda_handler(runbook_payload, {})
        assert_untouched(k8s)


class TestEnvironmentVariables:
    """Test environment variable handling"""

    def test_missing_cluster_name(self, k8s, runbook_payload, monkeypatch):
        """A missing CLUSTER_NAME is a config problem, not bad input"""
        monkeypatch.delenv('CLUSTER_NAME')

        with pytest.raises(RuntimeError, match='CLUSTER_NAME env var is required') as exc:
            handler.lambda_handler(runbook_payload, {})
        assert not isinstance(exc.value, handler.InvalidRequest)
        assert_untouched(k8s)

    def test_allowed_namespaces_list(self, k8s, runbook_payload, monkeypatch):
        monkeypatch.setenv('ALLOWED_NAMESPACES', ' apps , payments ,')
        runbook_payload.update(action='restart', namespace='payments')

        handler.lambda_handler(runbook_payload, {})

        k8s.restart.assert_called_once_with(ENDPOINT, 'test-token', b'test-ca', 'payments', 'checkout')

    def test_allowed_namespaces_defaults_to_apps(self, k8s, runbook_payload, monkeypatch):
        monkeypatch.delenv('ALLOWED_NAMESPACES')
        runbook_payload['action'] = 'restart'

        handler.lambda_handler(runbook_payload, {})
        k8s.restart.assert_called_once()

        runbook_payload['namespace'] = 'payments'
        with pytest.raises(handler.InvalidRequest, match=r'ALLOWED_NAMESPACES \(apps\)'):
            handler.lambda_handler(runbook_payload, {})

    def test_max_replicas_override(self, k8s, runbook_payload, monkeypatch):
        monkeypatch.setenv('MAX_REPLICAS', '20')
        runbook_payload.update(action='scale', replicas=20)

        result = handler.lambda_handler(runbook_payload, {})
        assert result['scaled_to'] == 20

        runbook_payload['replicas'] = 21
        with pytest.raises(handler.InvalidRequest, match='from 1 to 20'):
            handler.lambda_handler(runbook_payload, {})

    def test_max_replicas_default_allows_ten(self, k8s, runbook_payload):
        runbook_payload.update(action='scale', replicas=10)

        result = handler.lambda_handler(runbook_payload, {})

        assert result['scaled_to'] == 10

    def test_custom_degraded_param(self, k8s, runbook_payload, monkeypatch):
        monkeypatch.setenv('DEGRADED_PARAM', '/custom/degraded')
        runbook_payload['action'] = 'degrade'

        handler.lambda_handler(runbook_payload, {})

        k8s.ssm.put_parameter.assert_called_once_with(
            Name='/custom/degraded', Value='true', Type='String', Overwrite=True
        )

    def test_region_from_region_env(self, k8s, runbook_payload, monkeypatch):
        monkeypatch.setenv('REGION', 'us-west-2')
        runbook_payload['action'] = 'restart'

        handler.lambda_handler(runbook_payload, {})

        k8s.cluster.assert_called_once_with('test-cluster')
        k8s.token.assert_called_once_with('test-cluster', 'us-west-2')

    def test_region_fallback_to_aws_region(self, k8s, runbook_payload, monkeypatch):
        """Falls back to AWS_REGION when REGION is not set"""
        monkeypatch.delenv('REGION')
        monkeypatch.setenv('AWS_REGION', 'us-gov-west-1')
        runbook_payload['action'] = 'restart'

        handler.lambda_handler(runbook_payload, {})

        k8s.token.assert_called_once_with('test-cluster', 'us-gov-west-1')
