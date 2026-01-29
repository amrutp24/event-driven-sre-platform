from flask import Flask, Response, jsonify
from prometheus_client import Counter, Histogram, Gauge, generate_latest, CONTENT_TYPE_LATEST
import os, time, random

app = Flask(__name__)

REQS = Counter("http_requests_total", "HTTP requests", ["route", "code"])
LAT  = Histogram("http_request_duration_seconds", "Request latency", ["route"])
INFLIGHT = Gauge("in_flight_requests", "In-flight requests")
CHECKOUT_OK = Counter("checkout_success_total", "Successful checkouts")
CHECKOUT_FAIL = Counter("checkout_failure_total", "Failed checkouts")
DEP_FAIL = Counter("dependency_errors_total", "Dependency errors", ["dependency"])

DEGRADED = os.getenv("DEGRADED_MODE", "false").lower() == "true"
ERROR_RATE = float(os.getenv("ERROR_RATE", "0.00"))
LATENCY_MS = int(os.getenv("LATENCY_MS", "0"))
CHAOS = os.getenv("CHAOS", "false").lower() == "true"

@app.get("/metrics")
def metrics():
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
    try:
        if CHAOS:
            if random.random() < 0.02:
                DEP_FAIL.labels("redis").inc()
            if random.random() < 0.01:
                DEP_FAIL.labels("rds").inc()

        if not DEGRADED and LATENCY_MS > 0:
            time.sleep(LATENCY_MS / 1000.0)

        if (not DEGRADED) and random.random() < ERROR_RATE:
            code = 500
            CHECKOUT_FAIL.inc()
            REQS.labels(route, str(code)).inc()
            return jsonify({"status": "fail"}), 500

        CHECKOUT_OK.inc()
        REQS.labels(route, str(code)).inc()
        return jsonify({"status": "ok", "degraded": DEGRADED}), 200
    finally:
        LAT.labels(route).observe(time.time() - start)
        INFLIGHT.dec()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
