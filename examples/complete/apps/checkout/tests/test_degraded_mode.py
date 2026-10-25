"""
Tests for reading degraded mode from SSM

The runbook's degrade action sets /checkout/degraded_mode in SSM. The app
follows that parameter, refreshing at most every DEGRADED_REFRESH_SECONDS,
so degraded mode turns on without a restart.
"""
import importlib
import logging

import pytest
from botocore.exceptions import ClientError, EndpointConnectionError, NoCredentialsError

PARAM = "/checkout/degraded_mode"


def throttled():
    return ClientError({"Error": {"Code": "ThrottlingException", "Message": "Rate exceeded"}}, "GetParameter")


def unreachable():
    return EndpointConnectionError(endpoint_url="https://ssm.us-east-1.amazonaws.com")


class FakeSSM:
    """Returns the queued values in order, repeating the last one. An Exception
    in the queue is raised instead."""

    def __init__(self, *values):
        self.values = list(values)
        self.calls = 0

    def get_parameter(self, Name):
        self.calls += 1
        value = self.values.pop(0) if len(self.values) > 1 else self.values[0]
        if isinstance(value, Exception):
            raise value
        return {"Parameter": {"Name": Name, "Value": value}}


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


@pytest.fixture
def checkout(monkeypatch, clear_registry):
    """The app module, freshly loaded with SSM off and no injected faults"""
    monkeypatch.delenv("DEGRADED_PARAM", raising=False)
    monkeypatch.setenv("DEGRADED_MODE", "false")
    monkeypatch.setenv("ERROR_RATE", "0.0")
    monkeypatch.setenv("LATENCY_MS", "0")
    monkeypatch.setenv("CHAOS", "false")
    import app
    clear_registry()
    importlib.reload(app)
    return app


@pytest.fixture
def clock():
    return Clock()


def make(checkout, ssm, clock, initial=False, refresh=15):
    factory_calls = []

    def factory():
        factory_calls.append(1)
        return ssm

    mode = checkout.DegradedMode(initial, param=PARAM, refresh_seconds=refresh,
                                 client_factory=factory, clock=clock)
    mode.factory_calls = factory_calls
    return mode


def gauge(checkout):
    from prometheus_client import REGISTRY
    return REGISTRY.get_sample_value("checkout_degraded_mode_enabled")


class TestReader:

    def test_without_a_param_it_never_calls_ssm(self, checkout, clock):
        calls = []
        mode = checkout.DegradedMode(True, param=None, client_factory=lambda: calls.append(1), clock=clock)
        assert mode.current() is True
        assert calls == []

    def test_module_default_is_ssm_off(self, checkout):
        assert checkout.DEGRADED.param is None

    def test_first_call_reads_ssm(self, checkout, clock):
        ssm = FakeSSM("true")
        mode = make(checkout, ssm, clock)
        assert mode.current() is True
        assert ssm.calls == 1

    def test_client_is_created_lazily_and_once(self, checkout, clock):
        ssm = FakeSSM("false")
        mode = make(checkout, ssm, clock)
        assert mode.factory_calls == []
        mode.current()
        clock.now += 30
        mode.current()
        assert len(mode.factory_calls) == 1
        assert ssm.calls == 2

    def test_cached_within_the_refresh_window(self, checkout, clock):
        ssm = FakeSSM("false", "true")
        mode = make(checkout, ssm, clock)
        assert mode.current() is False
        clock.now += 14.9
        assert mode.current() is False
        assert ssm.calls == 1

    def test_refreshed_after_the_window(self, checkout, clock):
        ssm = FakeSSM("false", "true")
        mode = make(checkout, ssm, clock)
        mode.current()
        clock.now += 15
        assert mode.current() is True
        assert ssm.calls == 2

    @pytest.mark.parametrize("raw,expected", [("true", True), ("TRUE", True), (" true\n", True),
                                              ("false", False), ("1", False), ("", False)])
    def test_parameter_values(self, checkout, clock, raw, expected):
        assert make(checkout, FakeSSM(raw), clock).current() is expected

    def test_ssm_failure_keeps_the_last_value(self, checkout, clock):
        ssm = FakeSSM("true", throttled())
        mode = make(checkout, ssm, clock)
        assert mode.current() is True
        clock.now += 15
        assert mode.current() is True

    def test_starts_from_the_env_value_when_ssm_fails(self, checkout, clock):
        mode = make(checkout, FakeSSM(NoCredentialsError()), clock, initial=True)
        assert mode.current() is True

    def test_warns_once_per_failure_streak(self, checkout, clock, caplog):
        ssm = FakeSSM(throttled(), unreachable(), "false", throttled())
        mode = make(checkout, ssm, clock)
        with caplog.at_level(logging.WARNING, logger="checkout"):
            for _ in range(4):
                mode.current()
                clock.now += 15
        warnings = [r for r in caplog.records if r.name == "checkout" and r.levelno == logging.WARNING]
        # The first two failures are one streak; the last starts another after the success
        assert len(warnings) == 2
        assert PARAM in warnings[0].getMessage()

    def test_only_one_thread_refreshes_at_a_time(self, checkout, clock):
        ssm = FakeSSM("true")
        mode = make(checkout, ssm, clock)
        mode._lock.acquire()
        try:
            # Another thread is mid-refresh: keep serving the last value
            assert mode.current() is False
            assert ssm.calls == 0
        finally:
            mode._lock.release()
        assert mode.current() is True


class TestGauge:

    def test_gauge_follows_the_flag(self, checkout, clock):
        ssm = FakeSSM("true", "false")
        mode = make(checkout, ssm, clock)
        assert gauge(checkout) == 0
        mode.current()
        assert gauge(checkout) == 1
        clock.now += 15
        mode.current()
        assert gauge(checkout) == 0

    def test_gauge_starts_from_the_env_value(self, checkout, clock):
        checkout.DegradedMode(True, param=None, clock=clock)
        assert gauge(checkout) == 1

    def test_metrics_endpoint_exposes_the_gauge(self, checkout, clock, monkeypatch):
        monkeypatch.setattr(checkout, "DEGRADED", make(checkout, FakeSSM("true"), clock))
        client = checkout.app.test_client()
        client.get("/checkout")
        data = client.get("/metrics").data.decode("utf-8")
        assert "checkout_degraded_mode_enabled 1.0" in data

    def test_scrape_refreshes_the_gauge_without_checkout_traffic(self, checkout, clock, monkeypatch):
        monkeypatch.setattr(checkout, "DEGRADED", make(checkout, FakeSSM("false", "true"), clock))
        client = checkout.app.test_client()
        assert "checkout_degraded_mode_enabled 0.0" in client.get("/metrics").data.decode("utf-8")
        clock.now += 15
        assert "checkout_degraded_mode_enabled 1.0" in client.get("/metrics").data.decode("utf-8")


class TestCheckoutFollowsSSM:

    def test_degraded_from_ssm_skips_injected_errors_and_latency(self, checkout, clock, monkeypatch):
        monkeypatch.setattr(checkout, "ERROR_RATE", 1.0)
        monkeypatch.setattr(checkout, "LATENCY_MS", 2000)
        sleeps = []
        monkeypatch.setattr(checkout.time, "sleep", sleeps.append)
        monkeypatch.setattr(checkout, "DEGRADED", make(checkout, FakeSSM("true"), clock))

        response = checkout.app.test_client().get("/checkout")

        assert response.status_code == 200
        assert response.get_json() == {"status": "ok", "degraded": True}
        assert sleeps == []

    def test_not_degraded_still_injects_faults(self, checkout, clock, monkeypatch):
        monkeypatch.setattr(checkout, "ERROR_RATE", 1.0)
        monkeypatch.setattr(checkout, "LATENCY_MS", 2000)
        sleeps = []
        monkeypatch.setattr(checkout.time, "sleep", sleeps.append)
        monkeypatch.setattr(checkout, "DEGRADED", make(checkout, FakeSSM("false"), clock))

        response = checkout.app.test_client().get("/checkout")

        assert response.status_code == 500
        assert sleeps == [2.0]

    def test_runbook_turning_it_on_takes_effect_without_a_restart(self, checkout, clock, monkeypatch):
        monkeypatch.setattr(checkout, "ERROR_RATE", 1.0)
        monkeypatch.setattr(checkout, "DEGRADED", make(checkout, FakeSSM("false", "true"), clock))
        client = checkout.app.test_client()

        assert client.get("/checkout").status_code == 500
        clock.now += 15
        assert client.get("/checkout").status_code == 200
