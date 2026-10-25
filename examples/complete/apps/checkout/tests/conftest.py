"""
Pytest configuration and fixtures for checkout service tests
"""
import pytest
import os
from app import app as flask_app


@pytest.fixture
def app():
    """Create and configure a test instance of the Flask app"""
    # Set test environment variables
    os.environ['DEGRADED_MODE'] = 'false'
    os.environ['ERROR_RATE'] = '0.0'
    os.environ['LATENCY_MS'] = '0'
    os.environ['CHAOS'] = 'false'

    flask_app.config.update({
        'TESTING': True,
    })

    yield flask_app

    # Cleanup
    os.environ.pop('DEGRADED_MODE', None)
    os.environ.pop('ERROR_RATE', None)
    os.environ.pop('LATENCY_MS', None)
    os.environ.pop('CHAOS', None)


@pytest.fixture
def client(app):
    """Create a test client for the Flask app"""
    return app.test_client()


@pytest.fixture
def runner(app):
    """Create a test CLI runner for the Flask app"""
    return app.test_cli_runner()


@pytest.fixture
def clear_registry():
    """Fixture to clear Prometheus registry for tests that reload the app module"""
    from prometheus_client import REGISTRY

    def _clear():
        """Clear application metrics from registry"""
        collectors_to_remove = []
        for collector in list(REGISTRY._collector_to_names.keys()):
            collector_name = collector.__class__.__name__
            if collector_name not in ['ProcessCollector', 'PlatformCollector', 'GCCollector']:
                collectors_to_remove.append(collector)

        for collector in collectors_to_remove:
            try:
                REGISTRY.unregister(collector)
            except Exception:
                pass

    # Don't clear before - let tests call it explicitly when needed
    yield _clear
    # Don't clear after - leave metrics registered for subsequent tests
