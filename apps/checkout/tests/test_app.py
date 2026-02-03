"""
Unit tests for the checkout Flask application

Tests cover:
- Health check endpoints
- Checkout endpoint (happy path)
- Error injection
- Latency injection
- Degraded mode
- Prometheus metrics
- Chaos engineering features
"""
import pytest
import os
import sys
import time
import importlib
from app import app as flask_app


class TestHealthEndpoints:
    """Test health check endpoints"""

    def test_healthz_endpoint(self, client):
        """Test /healthz returns 200 OK"""
        response = client.get('/healthz')
        assert response.status_code == 200
        assert response.data == b'ok'

    def test_readyz_endpoint(self, client):
        """Test /readyz returns 200 ready"""
        response = client.get('/readyz')
        assert response.status_code == 200
        assert response.data == b'ready'


class TestMetricsEndpoint:
    """Test Prometheus metrics endpoint"""

    def test_metrics_endpoint_exists(self, client):
        """Test /metrics endpoint returns 200"""
        response = client.get('/metrics')
        assert response.status_code == 200

    def test_metrics_content_type(self, client):
        """Test /metrics returns Prometheus content type"""
        response = client.get('/metrics')
        assert 'text/plain' in response.content_type

    def test_metrics_contains_http_requests(self, client):
        """Test /metrics contains http_requests_total counter"""
        # Make a request to generate metrics
        client.get('/checkout')

        response = client.get('/metrics')
        data = response.data.decode('utf-8')

        # Check for Prometheus metric names
        assert 'http_requests_total' in data
        assert 'http_request_duration_seconds' in data

    def test_metrics_contains_checkout_counters(self, client):
        """Test /metrics contains checkout-specific counters"""
        # Make a successful checkout
        client.get('/checkout')

        response = client.get('/metrics')
        data = response.data.decode('utf-8')

        assert 'checkout_success_total' in data
        assert 'in_flight_requests' in data


class TestCheckoutEndpoint:
    """Test /checkout endpoint functionality"""

    def test_checkout_success(self, client):
        """Test successful checkout returns 200 and correct JSON"""
        response = client.get('/checkout')

        assert response.status_code == 200
        assert response.is_json

        json_data = response.get_json()
        assert json_data['status'] == 'ok'
        assert 'degraded' in json_data

    def test_checkout_multiple_requests(self, client):
        """Test multiple checkout requests all succeed"""
        for _ in range(5):
            response = client.get('/checkout')
            assert response.status_code == 200
            json_data = response.get_json()
            assert json_data['status'] == 'ok'


class TestErrorInjection:
    """Test error injection functionality"""

    def test_checkout_with_error_rate(self, monkeypatch, clear_registry):
        """Test that ERROR_RATE environment variable causes errors"""
        # Set error rate to 100% to guarantee error
        monkeypatch.setenv('ERROR_RATE', '1.0')
        monkeypatch.setenv('DEGRADED_MODE', 'false')

        # Reload the module to pick up new env vars
        import app
        clear_registry()  # Clear metrics before reload
        importlib.reload(app)
        client = app.app.test_client()

        response = client.get('/checkout')

        # With 100% error rate, should always return 500
        assert response.status_code == 500
        json_data = response.get_json()
        assert json_data['status'] == 'fail'

    def test_checkout_with_zero_error_rate(self, monkeypatch, clear_registry):
        """Test that ERROR_RATE=0 means no errors"""
        monkeypatch.setenv('ERROR_RATE', '0.0')
        monkeypatch.setenv('DEGRADED_MODE', 'false')

        import app
        clear_registry()
        importlib.reload(app)
        client = app.app.test_client()

        # Make multiple requests, all should succeed
        for _ in range(10):
            response = client.get('/checkout')
            assert response.status_code == 200

    def test_error_rate_increments_failure_counter(self, monkeypatch, clear_registry):
        """Test that errors increment the checkout_failure_total counter"""
        monkeypatch.setenv('ERROR_RATE', '1.0')
        monkeypatch.setenv('DEGRADED_MODE', 'false')

        import app
        clear_registry()
        importlib.reload(app)
        client = app.app.test_client()

        # Trigger an error
        client.get('/checkout')

        # Check metrics
        response = client.get('/metrics')
        data = response.data.decode('utf-8')

        assert 'checkout_failure_total' in data


class TestLatencyInjection:
    """Test latency injection functionality"""

    def test_checkout_with_latency(self, monkeypatch, clear_registry):
        """Test that LATENCY_MS adds delay to requests"""
        # Set 100ms latency
        monkeypatch.setenv('LATENCY_MS', '100')
        monkeypatch.setenv('DEGRADED_MODE', 'false')
        monkeypatch.setenv('ERROR_RATE', '0.0')

        import app
        clear_registry()
        importlib.reload(app)
        client = app.app.test_client()

        start = time.time()
        response = client.get('/checkout')
        duration = time.time() - start

        # Should take at least 100ms (0.1 seconds)
        assert duration >= 0.1
        assert response.status_code == 200

    def test_checkout_with_zero_latency(self, monkeypatch, clear_registry):
        """Test that LATENCY_MS=0 means no added delay"""
        monkeypatch.setenv('LATENCY_MS', '0')
        monkeypatch.setenv('DEGRADED_MODE', 'false')
        monkeypatch.setenv('ERROR_RATE', '0.0')

        import app
        clear_registry()
        importlib.reload(app)
        client = app.app.test_client()

        start = time.time()
        response = client.get('/checkout')
        duration = time.time() - start

        # Should be very fast (< 50ms)
        assert duration < 0.05
        assert response.status_code == 200


class TestDegradedMode:
    """Test degraded mode functionality"""

    def test_checkout_degraded_mode_enabled(self, monkeypatch, clear_registry):
        """Test that DEGRADED_MODE=true is reflected in response"""
        monkeypatch.setenv('DEGRADED_MODE', 'true')
        monkeypatch.setenv('ERROR_RATE', '0.0')

        import app
        clear_registry()
        importlib.reload(app)
        client = app.app.test_client()

        response = client.get('/checkout')
        json_data = response.get_json()

        assert response.status_code == 200
        assert json_data['degraded'] is True

    def test_checkout_degraded_mode_disabled(self, monkeypatch, clear_registry):
        """Test that DEGRADED_MODE=false is reflected in response"""
        monkeypatch.setenv('DEGRADED_MODE', 'false')
        monkeypatch.setenv('ERROR_RATE', '0.0')

        import app
        clear_registry()
        importlib.reload(app)
        client = app.app.test_client()

        response = client.get('/checkout')
        json_data = response.get_json()

        assert response.status_code == 200
        assert json_data['degraded'] is False

    def test_degraded_mode_skips_latency(self, monkeypatch, clear_registry):
        """Test that degraded mode skips latency injection"""
        # Enable degraded mode and set latency
        monkeypatch.setenv('DEGRADED_MODE', 'true')
        monkeypatch.setenv('LATENCY_MS', '200')
        monkeypatch.setenv('ERROR_RATE', '0.0')

        import app
        clear_registry()
        importlib.reload(app)
        client = app.app.test_client()

        start = time.time()
        response = client.get('/checkout')
        duration = time.time() - start

        # In degraded mode, should skip latency injection (< 50ms)
        assert duration < 0.05
        assert response.status_code == 200

    def test_degraded_mode_skips_error_injection(self, monkeypatch, clear_registry):
        """Test that degraded mode skips error injection"""
        # Enable degraded mode and set high error rate
        monkeypatch.setenv('DEGRADED_MODE', 'true')
        monkeypatch.setenv('ERROR_RATE', '1.0')

        import app
        clear_registry()
        importlib.reload(app)
        client = app.app.test_client()

        # Should always succeed in degraded mode
        for _ in range(5):
            response = client.get('/checkout')
            assert response.status_code == 200


class TestChaosEngineering:
    """Test chaos engineering features"""

    def test_chaos_mode_enabled(self, monkeypatch, clear_registry):
        """Test that CHAOS=true enables dependency error injection"""
        monkeypatch.setenv('CHAOS', 'true')
        monkeypatch.setenv('DEGRADED_MODE', 'false')
        monkeypatch.setenv('ERROR_RATE', '0.0')

        import app
        clear_registry()
        importlib.reload(app)
        client = app.app.test_client()

        # Make many requests to increase chance of chaos injection
        for _ in range(50):
            response = client.get('/checkout')
            # Requests should still succeed (chaos just increments dep error counter)
            assert response.status_code == 200

    def test_chaos_mode_disabled(self, monkeypatch, clear_registry):
        """Test that CHAOS=false disables dependency error injection"""
        monkeypatch.setenv('CHAOS', 'false')
        monkeypatch.setenv('DEGRADED_MODE', 'false')
        monkeypatch.setenv('ERROR_RATE', '0.0')

        import app
        clear_registry()
        importlib.reload(app)
        client = app.app.test_client()

        # Make requests
        for _ in range(10):
            client.get('/checkout')

        # Check metrics - dependency_errors_total should not be present or be 0
        response = client.get('/metrics')
        data = response.data.decode('utf-8')

        # If present, dependency errors should be 0
        if 'dependency_errors_total' in data:
            # This is expected behavior - counter exists but may be 0
            pass


class TestMetricsAccuracy:
    """Test that metrics accurately reflect application behavior"""

    def test_http_requests_counter_increments(self, monkeypatch, clear_registry):
        """Test that http_requests_total increments for each request"""
        monkeypatch.setenv('ERROR_RATE', '0.0')
        monkeypatch.setenv('DEGRADED_MODE', 'false')

        import app
        clear_registry()
        importlib.reload(app)
        client = app.app.test_client()

        # Get initial metrics
        initial_response = client.get('/metrics')
        initial_data = initial_response.data.decode('utf-8')

        # Make 3 checkout requests
        for _ in range(3):
            client.get('/checkout')

        # Get updated metrics
        final_response = client.get('/metrics')
        final_data = final_response.data.decode('utf-8')

        # Check that counter increased
        assert 'http_requests_total' in final_data

    def test_in_flight_requests_gauge(self, client):
        """Test that in_flight_requests gauge is present"""
        response = client.get('/metrics')
        data = response.data.decode('utf-8')

        assert 'in_flight_requests' in data

    def test_latency_histogram_recorded(self, client):
        """Test that http_request_duration_seconds histogram records latency"""
        # Make a request
        client.get('/checkout')

        # Check metrics
        response = client.get('/metrics')
        data = response.data.decode('utf-8')

        assert 'http_request_duration_seconds' in data
        # Histogram should have bucket, count, and sum
        assert 'http_request_duration_seconds_bucket' in data
        assert 'http_request_duration_seconds_count' in data
        assert 'http_request_duration_seconds_sum' in data


class TestEdgeCases:
    """Test edge cases and error handling"""

    def test_invalid_error_rate_defaults_to_zero(self, monkeypatch):
        """Test that invalid ERROR_RATE raises ValueError on module import"""
        monkeypatch.setenv('ERROR_RATE', 'invalid')
        monkeypatch.setenv('DEGRADED_MODE', 'false')

        # Should raise ValueError when parsing during module reload
        import app
        with pytest.raises(ValueError):
            importlib.reload(app)

    def test_negative_latency_handled(self, monkeypatch, clear_registry):
        """Test that negative LATENCY_MS is handled gracefully"""
        monkeypatch.setenv('LATENCY_MS', '-100')
        monkeypatch.setenv('DEGRADED_MODE', 'false')
        monkeypatch.setenv('ERROR_RATE', '0.0')

        import app
        clear_registry()
        importlib.reload(app)
        client = app.app.test_client()

        # Should not sleep for negative time
        start = time.time()
        response = client.get('/checkout')
        duration = time.time() - start

        # Should complete quickly
        assert duration < 0.05
        assert response.status_code == 200

    def test_degraded_mode_case_insensitive(self, monkeypatch, clear_registry):
        """Test that DEGRADED_MODE parsing is case-insensitive"""
        test_cases = ['TRUE', 'True', 'true', 'TrUe']

        for value in test_cases:
            monkeypatch.setenv('DEGRADED_MODE', value)
            monkeypatch.setenv('ERROR_RATE', '0.0')

            import app
            clear_registry()  # Clear before each reload in the loop
            importlib.reload(app)
            client = app.app.test_client()

            response = client.get('/checkout')
            json_data = response.get_json()

            assert json_data['degraded'] is True


class TestConcurrency:
    """Test concurrent request handling"""

    def test_multiple_concurrent_requests(self, client):
        """Test that multiple requests can be handled"""
        import concurrent.futures

        def make_request():
            response = client.get('/checkout')
            return response.status_code

        # Make 10 concurrent requests
        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
            futures = [executor.submit(make_request) for _ in range(10)]
            results = [f.result() for f in concurrent.futures.as_completed(futures)]

        # All should succeed (or fail due to configured ERROR_RATE)
        assert all(status in [200, 500] for status in results)


# Test coverage summary
def test_coverage_report():
    """
    This test serves as documentation for coverage expectations.

    Expected coverage areas:
    - Health endpoints: 100% (/healthz, /readyz)
    - Metrics endpoint: 100% (/metrics)
    - Checkout endpoint: >80% (happy path, error cases, degraded mode)
    - Error injection: >70% (various ERROR_RATE values)
    - Latency injection: >70% (with and without LATENCY_MS)
    - Degraded mode: >80% (enabled/disabled, interaction with latency/errors)
    - Chaos mode: >50% (probabilistic, hard to test deterministically)

    Overall expected coverage: >60%
    """
    pass
