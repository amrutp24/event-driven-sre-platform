"""
Unit tests for runbook_action Lambda handler

Tests cover:
- EKS authentication and token generation
- Kubernetes API requests (patch, restart, scale)
- Each action, with the payload the runbook state machine sends
- Scaling through the HPA when there is one
- drain_node: checks, cordon, evictions and PodDisruptionBudget retries
- Input validation (InvalidRequest) before any AWS or cluster call
- Environment variables: ALLOWED_NAMESPACES, MAX_REPLICAS, DEGRADED_*, DRAINABLE_NODE_LABEL,
  MIN_SCHEDULABLE_NODES, region
"""
import base64
import datetime
import io
import json
import os
import sys
import urllib.error
import urllib.parse
from types import SimpleNamespace
from unittest.mock import call, patch

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
DEPLOYMENT_PATH = '/apis/apps/v1/namespaces/apps/deployments/checkout'
HPA_PATH = '/apis/autoscaling/v2/namespaces/apps/horizontalpodautoscalers/checkout'
NODE = 'ip-10-0-1-23.ec2.internal'
OTHER_NODE = 'ip-10-0-2-45.ec2.internal'
NODE_PATH = f'/api/v1/nodes/{NODE}'
NODES_PATH = '/api/v1/nodes?labelSelector=nodegroup%3Dapps'
PODS_PATH = f'/api/v1/pods?fieldSelector=spec.nodeName%3D{NODE}'


def response(status, body=None):
    if isinstance(body, str):
        return handler.K8sResponse(status, body)
    return handler.K8sResponse(status, json.dumps(body) if body is not None else '')


class FakeK8s:
    """Stands in for the Kubernetes API at the _k8s_send level. A route answers
    with its responses in order and repeats the last one. Anything unrouted fails the test."""

    def __init__(self):
        self.routes = {}
        self.calls = []

    def on(self, method, path, *responses):
        self.routes[(method, path)] = list(responses)

    def __call__(self, method, url, token, ca_bytes, json_body=None, headers=None):
        assert url.startswith(ENDPOINT)
        assert (token, ca_bytes) == ('test-token', b'test-ca')
        path = url[len(ENDPOINT):]
        self.calls.append(SimpleNamespace(method=method, path=path, body=json_body, headers=headers or {}))
        responses = self.routes.get((method, path))
        if not responses:
            raise AssertionError(f'unexpected request: {method} {path}')
        status, body = responses.pop(0) if len(responses) > 1 else responses[0]
        return response(status, body)

    def sent(self, method, path=None):
        return [c for c in self.calls if c.method == method and (path is None or c.path == path)]


@pytest.fixture
def k8s():
    """Mocks the cluster connection, token, SSM, sleep, and the Kubernetes API (FakeK8s)"""
    api = FakeK8s()
    with patch('handler._cluster_conn', return_value=(ENDPOINT, b'test-ca')) as cluster, \
            patch('handler._eks_bearer_token', return_value='test-token') as token, \
            patch('handler._k8s_send', side_effect=api), \
            patch('handler.SSM') as ssm, \
            patch('handler.time.sleep') as sleep:
        yield SimpleNamespace(cluster=cluster, token=token, api=api, ssm=ssm, sleep=sleep)


def assert_untouched(k8s):
    k8s.cluster.assert_not_called()
    k8s.token.assert_not_called()
    assert k8s.api.calls == []
    k8s.ssm.put_parameter.assert_not_called()


def hpa(min_replicas=3, max_replicas=10):
    spec = {'maxReplicas': max_replicas, 'scaleTargetRef': {'kind': 'Deployment', 'name': 'checkout'}}
    if min_replicas is not None:
        spec['minReplicas'] = min_replicas
    return {'metadata': {'name': 'checkout', 'namespace': 'apps'}, 'spec': spec}


def node(name=NODE, labels=None, ready=True, unschedulable=False, conditions=True):
    return {
        'metadata': {'name': name, 'labels': {'nodegroup': 'apps'} if labels is None else labels},
        'spec': {'unschedulable': True} if unschedulable else {},
        'status': {'conditions': [
            {'type': 'MemoryPressure', 'status': 'False'},
            {'type': 'Ready', 'status': 'True' if ready else 'False'},
        ]} if conditions else {},
    }


def pod(name, namespace='apps', owner='ReplicaSet', phase='Running', annotations=None, terminating=False):
    metadata = {'name': name, 'namespace': namespace}
    if owner:
        metadata['ownerReferences'] = [{'kind': owner, 'name': f'{name}-owner'}]
    if annotations:
        metadata['annotations'] = annotations
    if terminating:
        metadata['deletionTimestamp'] = '2026-10-23T10:00:00Z'
    return {'metadata': metadata, 'status': {'phase': phase}}


def eviction_path(name, namespace='apps'):
    return f'/api/v1/namespaces/{namespace}/pods/{name}/eviction'


def drainable(api, pods, others=None, nodes_path=NODES_PATH, labels=None):
    """Routes for a node that passes the checks, with the given pods on it"""
    api.on('GET', NODE_PATH, (200, node(labels=labels)))
    others = [node(OTHER_NODE, labels=labels)] if others is None else others
    api.on('GET', nodes_path, (200, {'items': [node(labels=labels), *others]}))
    api.on('PATCH', NODE_PATH, (200, node(labels=labels, unschedulable=True)))
    api.on('GET', PODS_PATH, (200, {'items': pods}))


# One of each kind of pod a drain meets: three to evict, five kubectl drain leaves alone
NODE_PODS = (
    pod('web-1'),
    pod('web-2'),
    pod('report-job', owner='Job'),
    pod('fluent-bit', namespace='kube-system', owner='DaemonSet'),
    pod('kube-proxy', namespace='kube-system', owner='Node',
        annotations={'kubernetes.io/config.mirror': 'abc123'}),
    pod('old-web', terminating=True),
    pod('migrate', owner='Job', phase='Succeeded'),
    pod('crashed', phase='Failed'),
)


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

        assert url.startswith('https://sts.us-east-1.amazonaws.com/')
        assert query['Action'] == ['GetCallerIdentity']
        assert query['X-Amz-Expires'] == ['60']
        assert 'x-k8s-aws-id' in query['X-Amz-SignedHeaders'][0]
        assert query['X-Amz-Credential'][0].startswith('AKIDEXAMPLE/')

    @pytest.mark.parametrize('region, host', [
        ('us-east-1', 'sts.us-east-1.amazonaws.com'),
        ('us-west-2', 'sts.us-west-2.amazonaws.com'),
        ('cn-north-1', 'sts.cn-north-1.amazonaws.com.cn'),
    ])
    def test_token_uses_the_regional_sts_endpoint(self, region, host):
        """STS rejects a signature scoped to another region on the global
        endpoint, so the URL and the signature both use the Lambda's region"""
        session = botocore.session.get_session()
        session.set_credentials('AKIDEXAMPLE', 'wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY')

        with patch('handler.botocore.session.get_session', return_value=session):
            token = handler._eks_bearer_token('test-cluster', region)

        encoded = token[len('k8s-aws-v1.'):]
        url = urllib.parse.urlparse(base64.urlsafe_b64decode(encoded + '=' * (-len(encoded) % 4)).decode('utf-8'))
        query = urllib.parse.parse_qs(url.query)

        assert url.scheme == 'https' and url.netloc == host and url.path == '/'
        assert f'/{region}/sts/aws4_request' in query['X-Amz-Credential'][0]

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


class FakeHTTPResponse:
    def __init__(self, status, body):
        self.status = status
        self.body = body

    def read(self):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeUrlopen:
    """Stands in for urllib.request.urlopen with one canned answer. Like the
    real one, it raises HTTPError for 4xx and 5xx."""

    def __init__(self):
        self.status = 200
        self.body = b''
        self.calls = []

    def __call__(self, req, timeout=None, context=None):
        self.calls.append(SimpleNamespace(req=req, timeout=timeout, context=context))
        if self.status >= 400:
            raise urllib.error.HTTPError(req.full_url, self.status, 'error', {}, io.BytesIO(self.body))
        return FakeHTTPResponse(self.status, self.body)

    @property
    def req(self):
        assert len(self.calls) == 1
        return self.calls[0].req


@pytest.fixture
def urlopen():
    """Patches urlopen and the SSL context, so nothing leaves the test"""
    fake = FakeUrlopen()
    with patch('handler.urllib.request.urlopen', side_effect=fake), \
            patch('handler._ssl_context', return_value='cluster-ssl-context') as context:
        fake.ssl_context = context
        yield fake


class TestKubernetesAPIRequests:
    """_k8s_send and the helpers on top of it, down to urlopen"""

    def test_k8s_request_success(self, urlopen):
        urlopen.body = b'{"status": "success"}'

        result = handler._k8s_request('GET', f'{ENDPOINT}/api/v1/namespaces/default/pods', 'test-token', b'test-ca')

        assert result == {'status': 'success'}
        req = urlopen.req
        assert req.get_method() == 'GET'
        assert req.full_url == f'{ENDPOINT}/api/v1/namespaces/default/pods'
        assert req.get_header('Authorization') == 'Bearer test-token'
        assert req.get_header('Accept') == 'application/json'
        assert req.data is None

    def test_timeout_and_cluster_ca(self, urlopen):
        handler._k8s_request('GET', f'{ENDPOINT}{HPA_PATH}', 'test-token', b'test-ca')

        assert urlopen.calls[0].timeout == 10
        assert urlopen.calls[0].context == 'cluster-ssl-context'
        urlopen.ssl_context.assert_called_once_with(b'test-ca')

    def test_json_body_is_sent_with_the_callers_content_type(self, urlopen):
        handler._k8s_request('PATCH', f'{ENDPOINT}{DEPLOYMENT_PATH}', 'test-token', b'test-ca',
                             json_body={'spec': {'replicas': 4}}, headers=handler.MERGE_PATCH)

        req = urlopen.req
        assert req.get_method() == 'PATCH'
        assert json.loads(req.data) == {'spec': {'replicas': 4}}
        assert req.get_header('Content-type') == 'application/merge-patch+json'

    def test_json_body_defaults_to_application_json(self, urlopen):
        handler._k8s_request('POST', f'{ENDPOINT}{eviction_path("web-1")}', 'test-token', b'test-ca',
                             json_body={'kind': 'Eviction'})

        assert urlopen.req.get_header('Content-type') == 'application/json'

    def test_k8s_request_failure(self, urlopen):
        urlopen.status, urlopen.body = 403, b'Forbidden'

        with pytest.raises(RuntimeError, match='K8s API GET .* failed: 403 Forbidden'):
            handler._k8s_request('GET', f'{ENDPOINT}/api/v1/namespaces/default/pods', 'test-token', b'test-ca')

    def test_error_body_is_cut_to_500_characters(self, urlopen):
        urlopen.status, urlopen.body = 500, b'x' * 600

        with pytest.raises(RuntimeError) as raised:
            handler._k8s_request('GET', f'{ENDPOINT}{HPA_PATH}', 'test-token', b'test-ca')

        assert str(raised.value).endswith('failed: 500 ' + 'x' * 500)

    def test_k8s_request_empty_response(self, urlopen):
        urlopen.status = 204

        result = handler._k8s_request('DELETE', f'{ENDPOINT}/api/v1/namespaces/default/pods/test-pod',
                                      'test-token', b'test-ca')

        assert result == {}

    def test_send_returns_error_statuses_instead_of_raising(self, urlopen):
        """drain_node reads 404 and 429 off the eviction response itself"""
        urlopen.status, urlopen.body = 429, b'Cannot evict pod'

        resp = handler._k8s_send('POST', f'{ENDPOINT}{eviction_path("web-1")}', 'test-token', b'test-ca')

        assert (resp.status_code, resp.text) == (429, 'Cannot evict pod')

    def test_k8s_request_leaves_callers_headers_alone(self, urlopen):
        headers = {'Content-Type': 'application/merge-patch+json'}

        handler._k8s_request('PATCH', f'{ENDPOINT}{DEPLOYMENT_PATH}', 'test-token', b'test-ca',
                             json_body={}, headers=headers)

        assert headers == {'Content-Type': 'application/merge-patch+json'}

    def test_get_if_exists_returns_none_on_404(self, urlopen):
        urlopen.status, urlopen.body = 404, b'not found'

        assert handler._k8s_get_if_exists(f'{ENDPOINT}{HPA_PATH}', 'test-token', b'test-ca') is None

    def test_get_if_exists_returns_the_object(self, urlopen):
        urlopen.body = json.dumps(hpa()).encode()

        assert handler._k8s_get_if_exists(f'{ENDPOINT}{HPA_PATH}', 'test-token', b'test-ca') == hpa()

    def test_get_if_exists_raises_on_other_errors(self, urlopen):
        urlopen.status, urlopen.body = 403, b'Forbidden'

        with pytest.raises(RuntimeError, match='failed: 403 Forbidden'):
            handler._k8s_get_if_exists(f'{ENDPOINT}{HPA_PATH}', 'test-token', b'test-ca')

    def test_ssl_context_trusts_the_cluster_ca(self):
        """The CA from describe_cluster is PEM, which cadata takes as text"""
        handler._ssl_context.cache_clear()
        try:
            with patch('handler.ssl.create_default_context', return_value='ctx') as create:
                assert handler._ssl_context(b'-----BEGIN CERTIFICATE-----') == 'ctx'
                assert handler._ssl_context(b'-----BEGIN CERTIFICATE-----') == 'ctx'
            create.assert_called_once_with(cadata='-----BEGIN CERTIFICATE-----')
        finally:
            handler._ssl_context.cache_clear()


class TestDeploymentActions:
    """Test deployment-specific actions"""

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

    def test_degrade_only_sets_the_ssm_flag(self, k8s, runbook_payload):
        """The app reads the flag live, so there's no env patch, no restart, and no cluster call at all"""
        runbook_payload['action'] = 'degrade'
        del runbook_payload['replicas']

        result = handler.lambda_handler(runbook_payload, {})

        k8s.ssm.put_parameter.assert_called_once_with(
            Name='/checkout/degraded_mode', Value='true', Type='String', Overwrite=True
        )
        k8s.cluster.assert_not_called()
        k8s.token.assert_not_called()
        assert k8s.api.calls == []
        assert result == {
            'incident_id': 'test-incident-123',
            'action': 'degrade',
            'alertname': 'CheckoutHighErrorRate',
            'severity': 'critical',
            'namespace': 'apps',
            'deployment': 'checkout',
            'degraded': True,
        }

    def test_restart(self, k8s, runbook_payload):
        runbook_payload.update(action='restart', alertname='CheckoutDown')
        k8s.api.on('PATCH', DEPLOYMENT_PATH, (200, {}))

        result = handler.lambda_handler(runbook_payload, {})

        [restart] = k8s.api.calls
        assert (restart.method, restart.path) == ('PATCH', DEPLOYMENT_PATH)
        assert restart.headers['Content-Type'] == 'application/strategic-merge-patch+json'
        assert 'kubectl.kubernetes.io/restartedAt' in restart.body['spec']['template']['metadata']['annotations']
        k8s.ssm.put_parameter.assert_not_called()
        assert 'degraded' not in result
        assert 'scaled_to' not in result

    def test_scale_without_an_hpa_patches_the_deployment(self, k8s, runbook_payload):
        runbook_payload.update(action='scale', replicas=6)
        k8s.api.on('GET', HPA_PATH, (404, 'not found'))
        k8s.api.on('PATCH', DEPLOYMENT_PATH, (200, {}))

        result = handler.lambda_handler(runbook_payload, {})

        [scale] = k8s.api.sent('PATCH')
        assert scale.path == DEPLOYMENT_PATH
        assert scale.body == {'spec': {'replicas': 6}}
        assert scale.headers['Content-Type'] == 'application/merge-patch+json'
        k8s.ssm.put_parameter.assert_not_called()
        assert (result['scaled_to'], result['scaled_via']) == (6, 'deployment')
        assert 'degraded' not in result

    def test_degrade_or_scale(self, k8s, runbook_payload):
        k8s.api.on('GET', HPA_PATH, (200, hpa()))
        k8s.api.on('PATCH', HPA_PATH, (200, {}))

        result = handler.lambda_handler(runbook_payload, {})

        k8s.ssm.put_parameter.assert_called_once_with(
            Name='/checkout/degraded_mode', Value='true', Type='String', Overwrite=True
        )
        [scale] = k8s.api.sent('PATCH')
        assert scale.path == HPA_PATH
        assert result['degraded'] is True
        assert (result['scaled_to'], result['scaled_via']) == (4, 'hpa')

    def test_target_comes_from_payload(self, k8s, runbook_payload, monkeypatch):
        monkeypatch.setenv('ALLOWED_NAMESPACES', 'apps,payments')
        runbook_payload.update(action='restart', namespace='payments', deployment='payments-api')
        k8s.api.on('PATCH', '/apis/apps/v1/namespaces/payments/deployments/payments-api', (200, {}))

        result = handler.lambda_handler(runbook_payload, {})

        assert len(k8s.api.calls) == 1
        assert result['namespace'] == 'payments'
        assert result['deployment'] == 'payments-api'

    def test_return_shape(self, k8s, runbook_payload):
        runbook_payload.update(action='restart')
        k8s.api.on('PATCH', DEPLOYMENT_PATH, (200, {}))

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
        k8s.api.on('GET', HPA_PATH, (404, 'not found'))
        k8s.api.on('PATCH', DEPLOYMENT_PATH, (200, {}))

        result = handler.lambda_handler(runbook_payload, {})

        assert result['scaled_to'] == 6
        assert k8s.api.sent('PATCH')[0].body == {'spec': {'replicas': 6}}

    def test_restart_ignores_replicas(self, k8s, runbook_payload):
        """replicas only matters for the scaling actions"""
        runbook_payload.update(action='restart', replicas='not-a-number')
        k8s.api.on('PATCH', DEPLOYMENT_PATH, (200, {}))

        handler.lambda_handler(runbook_payload, {})

        assert len(k8s.api.calls) == 1


class TestScaleThroughHPA:
    """With an HPA, a replica patch would be undone within seconds, so scale raises the HPA's floor"""

    def scale(self, k8s, runbook_payload, replicas, existing):
        runbook_payload.update(action='scale', replicas=replicas)
        k8s.api.on('GET', HPA_PATH, (200, existing))
        k8s.api.on('PATCH', HPA_PATH, (200, {}))
        result = handler.lambda_handler(runbook_payload, {})
        [patch_call] = k8s.api.sent('PATCH')
        assert patch_call.path == HPA_PATH
        assert patch_call.headers['Content-Type'] == 'application/merge-patch+json'
        assert (result['scaled_to'], result['scaled_via']) == (replicas, 'hpa')
        return patch_call.body

    def test_raises_min_replicas(self, k8s, runbook_payload):
        assert self.scale(k8s, runbook_payload, 6, hpa(3, 10)) == {'spec': {'minReplicas': 6}}

    def test_never_lowers_min_replicas(self, k8s, runbook_payload):
        assert self.scale(k8s, runbook_payload, 6, hpa(8, 10)) == {'spec': {'minReplicas': 8}}

    def test_raises_max_replicas_when_n_is_above_it(self, k8s, runbook_payload):
        assert self.scale(k8s, runbook_payload, 8, hpa(3, 5)) == {'spec': {'minReplicas': 8, 'maxReplicas': 8}}

    def test_leaves_max_replicas_when_n_equals_it(self, k8s, runbook_payload):
        assert self.scale(k8s, runbook_payload, 10, hpa(3, 10)) == {'spec': {'minReplicas': 10}}

    def test_unset_min_replicas_counts_as_one(self, k8s, runbook_payload):
        assert self.scale(k8s, runbook_payload, 4, hpa(None, 10)) == {'spec': {'minReplicas': 4}}

    def test_deployment_is_left_alone(self, k8s, runbook_payload):
        self.scale(k8s, runbook_payload, 6, hpa())
        assert k8s.api.sent('PATCH', DEPLOYMENT_PATH) == []

    def test_hpa_lookup_error_raises(self, k8s, runbook_payload):
        """A 403 (say, RBAC missing the HPA rule) is retryable, not bad input"""
        runbook_payload.update(action='scale', replicas=6)
        k8s.api.on('GET', HPA_PATH, (403, 'Forbidden'))

        with pytest.raises(RuntimeError, match='failed: 403 Forbidden') as exc:
            handler.lambda_handler(runbook_payload, {})
        assert not isinstance(exc.value, handler.InvalidRequest)
        assert k8s.api.sent('PATCH') == []


class TestDrainNode:
    """drain_node: checks, cordon, then evictions like kubectl drain"""

    def test_drains_and_returns_counts(self, k8s, drain_payload):
        drainable(k8s.api, list(NODE_PODS))
        for name in ('web-1', 'web-2', 'report-job'):
            k8s.api.on('POST', eviction_path(name), (201, {'kind': 'Status', 'status': 'Success'}))

        result = handler.lambda_handler(drain_payload, {})

        assert result == {
            'incident_id': 'test-incident-456',
            'action': 'drain_node',
            'alertname': 'NodeUnderPressure',
            'severity': 'critical',
            'node': NODE,
            'cordoned': True,
            'evicted': 3,
            'skipped': 5,
            'unmanaged': [],
            'still_blocked': [],
        }
        k8s.sleep.assert_not_called()
        k8s.ssm.put_parameter.assert_not_called()

    def test_cordons_before_evicting(self, k8s, drain_payload):
        drainable(k8s.api, [pod('web-1')])
        k8s.api.on('POST', eviction_path('web-1'), (201, {}))

        handler.lambda_handler(drain_payload, {})

        methods = [(c.method, c.path) for c in k8s.api.calls]
        assert methods == [
            ('GET', NODE_PATH),
            ('GET', NODES_PATH),
            ('PATCH', NODE_PATH),
            ('GET', PODS_PATH),
            ('POST', eviction_path('web-1')),
        ]
        [cordon] = k8s.api.sent('PATCH')
        assert cordon.body == {'spec': {'unschedulable': True}}
        assert cordon.headers['Content-Type'] == 'application/merge-patch+json'

    def test_eviction_request(self, k8s, drain_payload):
        drainable(k8s.api, [pod('web-1')])
        k8s.api.on('POST', eviction_path('web-1'), (201, {}))

        handler.lambda_handler(drain_payload, {})

        [eviction] = k8s.api.sent('POST')
        assert eviction.body == {
            'apiVersion': 'policy/v1',
            'kind': 'Eviction',
            'metadata': {'name': 'web-1', 'namespace': 'apps'},
        }

    def test_skips_what_kubectl_drain_skips(self, k8s, drain_payload):
        skipped = [p for p in NODE_PODS if p['metadata']['name'] not in ('web-1', 'web-2', 'report-job')]
        drainable(k8s.api, skipped)

        result = handler.lambda_handler(drain_payload, {})

        assert k8s.api.sent('POST') == []
        assert (result['evicted'], result['skipped']) == (0, 5)

    def test_pod_without_an_owner_is_left_and_reported(self, k8s, drain_payload):
        """Nothing would recreate it elsewhere, so it isn't evicted (kubectl drain needs --force)"""
        drainable(k8s.api, [pod('web-1'), pod('debug-shell', owner=None)])
        k8s.api.on('POST', eviction_path('web-1'), (201, {}))

        result = handler.lambda_handler(drain_payload, {})

        assert [c.path for c in k8s.api.sent('POST')] == [eviction_path('web-1')]
        assert result['evicted'] == 1
        assert result['unmanaged'] == ['apps/debug-shell']
        assert result['still_blocked'] == []

    def test_monitoring_namespace_and_cluster_deployment_still_drains(self, k8s, drain_payload):
        """kube-state-metrics labels aren't a target, so drain_node ignores them"""
        assert (drain_payload['namespace'], drain_payload['deployment']) == ('monitoring', 'cluster')
        drainable(k8s.api, [pod('web-1')])
        k8s.api.on('POST', eviction_path('web-1'), (201, {}))

        result = handler.lambda_handler(drain_payload, {})

        assert result['evicted'] == 1
        assert 'namespace' not in result
        assert 'deployment' not in result

    def test_already_gone_counts_as_evicted(self, k8s, drain_payload):
        drainable(k8s.api, [pod('web-1'), pod('web-2')])
        k8s.api.on('POST', eviction_path('web-1'), (404, 'not found'))
        k8s.api.on('POST', eviction_path('web-2'), (201, {}))

        result = handler.lambda_handler(drain_payload, {})

        assert (result['evicted'], result['still_blocked']) == (2, [])
        k8s.sleep.assert_not_called()

    def test_blocked_pod_is_retried_until_evicted(self, k8s, drain_payload):
        drainable(k8s.api, [pod('web-1'), pod('web-2')])
        k8s.api.on('POST', eviction_path('web-1'), (429, 'disruption budget'), (429, 'disruption budget'), (201, {}))
        k8s.api.on('POST', eviction_path('web-2'), (201, {}))

        result = handler.lambda_handler(drain_payload, {})

        assert (result['evicted'], result['still_blocked']) == (2, [])
        assert len(k8s.api.sent('POST', eviction_path('web-1'))) == 3
        # web-2 went the first time and isn't asked again
        assert len(k8s.api.sent('POST', eviction_path('web-2'))) == 1
        assert k8s.sleep.call_args_list == [call(5), call(5)]

    def test_still_blocked_after_a_minute_is_reported_not_raised(self, k8s, drain_payload):
        drainable(k8s.api, [pod('web-1'), pod('web-2', namespace='apps')])
        k8s.api.on('POST', eviction_path('web-1'), (429, 'disruption budget'))
        k8s.api.on('POST', eviction_path('web-2'), (201, {}))

        result = handler.lambda_handler(drain_payload, {})

        assert result['evicted'] == 1
        assert result['still_blocked'] == ['apps/web-1']
        assert result['cordoned'] is True
        # one try, then a retry every 5 s for 60 s
        assert k8s.sleep.call_args_list == [call(5)] * 12
        assert len(k8s.api.sent('POST', eviction_path('web-1'))) == 13

    def test_sleep_is_injectable(self, k8s):
        drainable(k8s.api, [pod('web-1')])
        k8s.api.on('POST', eviction_path('web-1'), (429, 'disruption budget'), (201, {}))
        slept = []

        result = handler._drain_node(ENDPOINT, 'test-token', b'test-ca', NODE, sleep=slept.append)

        assert slept == [5]
        k8s.sleep.assert_not_called()
        assert result == {'cordoned': True, 'evicted': 1, 'skipped': 0, 'unmanaged': [], 'still_blocked': []}

    def test_eviction_error_raises(self, k8s, drain_payload):
        """Anything but 404/429 fails the action; the runbook retries it and the node is already cordoned"""
        drainable(k8s.api, [pod('web-1')])
        k8s.api.on('POST', eviction_path('web-1'), (500, 'etcd timeout'))

        with pytest.raises(RuntimeError, match='failed: 500 etcd timeout'):
            handler.lambda_handler(drain_payload, {})
        assert len(k8s.api.sent('PATCH', NODE_PATH)) == 1

    def test_node_that_doesnt_exist(self, k8s, drain_payload):
        k8s.api.on('GET', NODE_PATH, (404, 'not found'))

        with pytest.raises(handler.InvalidRequest, match="doesn't exist"):
            handler.lambda_handler(drain_payload, {})
        assert len(k8s.api.calls) == 1

    def test_node_lookup_error_is_not_invalid_request(self, k8s, drain_payload):
        k8s.api.on('GET', NODE_PATH, (500, 'boom'))

        with pytest.raises(RuntimeError, match='failed: 500 boom') as exc:
            handler.lambda_handler(drain_payload, {})
        assert not isinstance(exc.value, handler.InvalidRequest)

    @pytest.mark.parametrize('labels', [{'nodegroup': 'system'}, {'eks.amazonaws.com/nodegroup': 'apps'}, {}])
    def test_node_without_the_drainable_label(self, k8s, drain_payload, labels):
        k8s.api.on('GET', NODE_PATH, (200, node(labels=labels)))

        with pytest.raises(handler.InvalidRequest, match="doesn't have the nodegroup=apps label"):
            handler.lambda_handler(drain_payload, {})
        assert k8s.api.sent('PATCH') == []
        assert len(k8s.api.calls) == 1

    def test_node_with_no_labels_at_all(self, k8s, drain_payload):
        bare = node()
        del bare['metadata']['labels']
        k8s.api.on('GET', NODE_PATH, (200, bare))

        with pytest.raises(handler.InvalidRequest, match="doesn't have the nodegroup=apps label"):
            handler.lambda_handler(drain_payload, {})

    def test_too_few_schedulable_nodes(self, k8s, drain_payload):
        """The node itself, a NotReady node, a cordoned node and one with no status don't count"""
        others = [
            node('ip-10-0-2-1.ec2.internal', ready=False),
            node('ip-10-0-2-2.ec2.internal', unschedulable=True),
            node('ip-10-0-2-3.ec2.internal', conditions=False),
        ]
        drainable(k8s.api, [pod('web-1')], others=others)

        with pytest.raises(handler.InvalidRequest, match='Only 0 other Ready, schedulable nodegroup=apps node'):
            handler.lambda_handler(drain_payload, {})
        assert k8s.api.sent('PATCH') == []
        assert k8s.api.sent('POST') == []

    def test_only_node_in_the_group(self, k8s, drain_payload):
        drainable(k8s.api, [pod('web-1')], others=[])

        with pytest.raises(handler.InvalidRequest, match=f'Only 0 .* draining {NODE} needs 1'):
            handler.lambda_handler(drain_payload, {})

    def test_min_schedulable_nodes_from_env(self, k8s, drain_payload, monkeypatch):
        monkeypatch.setenv('MIN_SCHEDULABLE_NODES', '2')
        drainable(k8s.api, [pod('web-1')])

        with pytest.raises(handler.InvalidRequest, match='Only 1 other .* needs 2'):
            handler.lambda_handler(drain_payload, {})

        k8s.api.on('GET', NODES_PATH, (200, {'items': [node(), node(OTHER_NODE), node('ip-10-0-3-9.ec2.internal')]}))
        k8s.api.on('POST', eviction_path('web-1'), (201, {}))
        assert handler.lambda_handler(drain_payload, {})['evicted'] == 1

    def test_custom_drainable_label(self, k8s, drain_payload, monkeypatch):
        monkeypatch.setenv('DRAINABLE_NODE_LABEL', 'role=batch')
        drainable(k8s.api, [], nodes_path='/api/v1/nodes?labelSelector=role%3Dbatch', labels={'role': 'batch'})

        assert handler.lambda_handler(drain_payload, {})['cordoned'] is True

    def test_key_only_drainable_label(self, k8s, drain_payload, monkeypatch):
        """A label with no '=' only has to be present"""
        monkeypatch.setenv('DRAINABLE_NODE_LABEL', 'workload')
        drainable(k8s.api, [], nodes_path='/api/v1/nodes?labelSelector=workload', labels={'workload': 'anything'})

        assert handler.lambda_handler(drain_payload, {})['cordoned'] is True

    def test_defaults_when_env_is_unset(self, k8s, drain_payload, monkeypatch):
        monkeypatch.delenv('DRAINABLE_NODE_LABEL')
        monkeypatch.delenv('MIN_SCHEDULABLE_NODES')
        drainable(k8s.api, [])

        assert handler.lambda_handler(drain_payload, {})['cordoned'] is True


class TestInvalidRequest:
    """Bad input raises InvalidRequest before anything touches AWS or the cluster"""

    def test_error_name_is_the_class_name(self):
        # Step Functions matches on this name to skip retries
        assert handler.InvalidRequest.__name__ == 'InvalidRequest'
        assert issubclass(handler.InvalidRequest, Exception)

    @pytest.mark.parametrize('action', [None, '', 'notify_only', 'drain', 'DEGRADE', 'unknown_action'])
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
        k8s.api.on('PATCH', f'/apis/apps/v1/namespaces/apps/deployments/{"a" * 63}', (200, {}))

        handler.lambda_handler(runbook_payload, {})

        assert len(k8s.api.calls) == 1

    @pytest.mark.parametrize('action', ['degrade', 'degrade_or_scale'])
    def test_degrade_only_for_the_degraded_deployment(self, k8s, runbook_payload, monkeypatch, action):
        monkeypatch.setenv('ALLOWED_NAMESPACES', 'apps,payments')
        runbook_payload.update(action=action, deployment='payments-api')

        with pytest.raises(handler.InvalidRequest, match='degraded mode is only wired up for checkout'):
            handler.lambda_handler(runbook_payload, {})
        assert_untouched(k8s)

    def test_degraded_deployment_from_env(self, k8s, runbook_payload, monkeypatch):
        monkeypatch.setenv('DEGRADED_DEPLOYMENT', 'payments-api')
        runbook_payload.update(action='degrade', deployment='payments-api')
        assert handler.lambda_handler(runbook_payload, {})['degraded'] is True

        runbook_payload['deployment'] = 'checkout'
        with pytest.raises(handler.InvalidRequest, match='only wired up for payments-api'):
            handler.lambda_handler(runbook_payload, {})

    def test_degraded_deployment_defaults_to_checkout(self, k8s, runbook_payload, monkeypatch):
        monkeypatch.delenv('DEGRADED_DEPLOYMENT')
        runbook_payload['action'] = 'degrade'

        assert handler.lambda_handler(runbook_payload, {})['degraded'] is True

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

    @pytest.mark.parametrize('name', [None, ''])
    def test_drain_needs_a_node(self, k8s, drain_payload, name):
        if name is None:
            del drain_payload['node']
        else:
            drain_payload['node'] = name

        with pytest.raises(handler.InvalidRequest, match='node is required for drain_node'):
            handler.lambda_handler(drain_payload, {})
        assert_untouched(k8s)

    @pytest.mark.parametrize('name', [
        'Node_1', 'ip-10-0-1-23.EC2.internal', '-ip.ec2.internal', 'ip-.ec2.internal', 'ip..internal',
        'ip.internal.', '.internal', 'node/ip-10-0-1-23', 'a' * 254, 42, ['ip-10-0-1-23'],
    ])
    def test_bad_node_name(self, k8s, drain_payload, name):
        drain_payload['node'] = name

        with pytest.raises(handler.InvalidRequest, match="isn't a valid Kubernetes node name"):
            handler.lambda_handler(drain_payload, {})
        assert_untouched(k8s)

    def test_longest_node_name_is_valid(self):
        name = '.'.join(['a' * 63, 'b' * 63, 'c' * 63, 'd' * 61])
        assert len(name) == 253
        assert handler._validate({'action': 'drain_node', 'node': name}) == ('drain_node', None, None, None)

    def test_drain_ignores_namespace_deployment_and_replicas(self):
        event = {'action': 'drain_node', 'node': NODE,
                 'namespace': 'kube-system', 'deployment': 'Not_A_Name', 'replicas': 'junk'}
        assert handler._validate(event) == ('drain_node', None, None, None)

    def test_drain_needs_no_target_fields(self):
        assert handler._validate({'action': 'drain_node', 'node': NODE}) == ('drain_node', None, None, None)


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
        k8s.api.on('PATCH', '/apis/apps/v1/namespaces/payments/deployments/checkout', (200, {}))

        handler.lambda_handler(runbook_payload, {})

        assert len(k8s.api.calls) == 1

    def test_allowed_namespaces_defaults_to_apps(self, k8s, runbook_payload, monkeypatch):
        monkeypatch.delenv('ALLOWED_NAMESPACES')
        runbook_payload['action'] = 'restart'
        k8s.api.on('PATCH', DEPLOYMENT_PATH, (200, {}))

        handler.lambda_handler(runbook_payload, {})
        assert len(k8s.api.calls) == 1

        runbook_payload['namespace'] = 'payments'
        with pytest.raises(handler.InvalidRequest, match=r'ALLOWED_NAMESPACES \(apps\)'):
            handler.lambda_handler(runbook_payload, {})

    def test_max_replicas_override(self, k8s, runbook_payload, monkeypatch):
        monkeypatch.setenv('MAX_REPLICAS', '20')
        runbook_payload.update(action='scale', replicas=20)
        k8s.api.on('GET', HPA_PATH, (404, 'not found'))
        k8s.api.on('PATCH', DEPLOYMENT_PATH, (200, {}))

        result = handler.lambda_handler(runbook_payload, {})
        assert result['scaled_to'] == 20

        runbook_payload['replicas'] = 21
        with pytest.raises(handler.InvalidRequest, match='from 1 to 20'):
            handler.lambda_handler(runbook_payload, {})

    def test_max_replicas_default_allows_ten(self, k8s, runbook_payload):
        runbook_payload.update(action='scale', replicas=10)
        k8s.api.on('GET', HPA_PATH, (404, 'not found'))
        k8s.api.on('PATCH', DEPLOYMENT_PATH, (200, {}))

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
        k8s.api.on('PATCH', DEPLOYMENT_PATH, (200, {}))

        handler.lambda_handler(runbook_payload, {})

        k8s.cluster.assert_called_once_with('test-cluster')
        k8s.token.assert_called_once_with('test-cluster', 'us-west-2')

    def test_region_fallback_to_aws_region(self, k8s, runbook_payload, monkeypatch):
        """Falls back to AWS_REGION when REGION is not set"""
        monkeypatch.delenv('REGION')
        monkeypatch.setenv('AWS_REGION', 'us-gov-west-1')
        runbook_payload['action'] = 'restart'
        k8s.api.on('PATCH', DEPLOYMENT_PATH, (200, {}))

        handler.lambda_handler(runbook_payload, {})

        k8s.token.assert_called_once_with('test-cluster', 'us-gov-west-1')


class TestFakeK8s:
    def test_unrouted_request_fails_the_test(self, k8s, runbook_payload):
        """So an action can't make a request a test didn't expect and still pass"""
        runbook_payload['action'] = 'restart'

        with pytest.raises(AssertionError, match=f'unexpected request: PATCH {DEPLOYMENT_PATH}'):
            handler.lambda_handler(runbook_payload, {})
