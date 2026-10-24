from flask import Flask, Response, jsonify
from prometheus_client import Counter, Histogram, Gauge, generate_latest, CONTENT_TYPE_LATEST
import logging, os, threading, time, random

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

app = Flask(__name__)
log = logging.getLogger("checkout")

REQS = Counter("http_requests_total", "HTTP requests", ["route", "code"])
LAT  = Histogram("http_request_duration_seconds", "Request latency", ["route"])
INFLIGHT = Gauge("in_flight_requests", "In-flight requests")
CHECKOUT_OK = Counter("checkout_success_total", "Successful checkouts")
CHECKOUT_FAIL = Counter("checkout_failure_total", "Failed checkouts")
DEP_FAIL = Counter("dependency_errors_total", "Dependency errors", ["dependency"])
DEGRADED_ON = Gauge("checkout_degraded_mode_enabled", "1 while degraded mode is on")

ERROR_RATE = float(os.getenv("ERROR_RATE", "0.00"))
LATENCY_MS = int(os.getenv("LATENCY_MS", "0"))
CHAOS = os.getenv("CHAOS", "false").lower() == "true"


def _ssm_client():
    # Short timeouts and no retries: a slow SSM shouldn't hold up a checkout
    return boto3.client("ssm", config=Config(connect_timeout=1, read_timeout=1, retries={"max_attempts": 1}))


class DegradedMode:
    """Degraded mode flag. Starts from DEGRADED_MODE and, when DEGRADED_PARAM is set,
    follows that SSM parameter so the runbook can flip it without a restart."""

    def __init__(self, initial, param=None, refresh_seconds=15.0, client_factory=_ssm_client, clock=time.monotonic):
        self.value = initial
        self.param = param
        self.refresh_seconds = refresh_seconds
        self._client_factory = client_factory
        self._client = None
        self._clock = clock
        self._checked_at = None
        self._failing = False
        self._lock = threading.Lock()
        DEGRADED_ON.set(1 if initial else 0)

    def _due(self):
        return self._checked_at is None or self._clock() - self._checked_at >= self.refresh_seconds

    def current(self):
        # One thread refreshes; the others keep using the last value meanwhile
        if self.param and self._due() and self._lock.acquire(blocking=False):
            try:
                if self._due():
                    self._checked_at = self._clock()
                    self._refresh()
            finally:
                self._lock.release()
        return self.value

    def _refresh(self):
        try:
            if self._client is None:
                self._client = self._client_factory()
            value = self._client.get_parameter(Name=self.param)["Parameter"]["Value"]
            self.value = value.strip().lower() == "true"
            self._failing = False
        except (BotoCoreError, ClientError) as exc:
            # Warn once per run of failures, not on every refresh
            if not self._failing:
                log.warning("Can't read %s, keeping degraded mode %s: %s",
                            self.param, "on" if self.value else "off", exc)
            self._failing = True
        DEGRADED_ON.set(1 if self.value else 0)


DEGRADED = DegradedMode(
    initial=os.getenv("DEGRADED_MODE", "false").lower() == "true",
    param=os.getenv("DEGRADED_PARAM") or None,
    refresh_seconds=float(os.getenv("DEGRADED_REFRESH_SECONDS", "15")),
)

@app.route("/")
def home():
    return "OK -- Flask is running"

@app.get("/metrics")
def metrics():
    # Refresh here too, so the degraded gauge is current even with no checkout traffic
    DEGRADED.current()
    return Response(generate_latest(), mimetype=CONTENT_TYPE_LATEST)

@app.get("/healthz")
def healthz():
    return "ok", 200

@app.get("/readyz")
def readyz():
    return ("ready", 200)

@app.get("/checkout")
def checkout():
    route = "/checkout"
    INFLIGHT.inc()
    start = time.time()
    code = 200
    degraded = DEGRADED.current()
    try:
        if CHAOS:
            if random.random() < 0.02:
                DEP_FAIL.labels("redis").inc()
            if random.random() < 0.01:
                DEP_FAIL.labels("rds").inc()

        if not degraded and LATENCY_MS > 0:
            time.sleep(LATENCY_MS / 1000.0)

        if (not degraded) and random.random() < ERROR_RATE:
            code = 500
            CHECKOUT_FAIL.inc()
            REQS.labels(route, str(code)).inc()
            return jsonify({"status": "fail"}), 500

        CHECKOUT_OK.inc()
        REQS.labels(route, str(code)).inc()
        return jsonify({"status": "ok", "degraded": degraded}), 200
    finally:
        LAT.labels(route).observe(time.time() - start)
        INFLIGHT.dec()

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=8080)
