FROM python:3.13-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py .
RUN useradd --uid 10001 --create-home --shell /usr/sbin/nologin app
USER 10001
EXPOSE 8080
# One worker: prometheus_client keeps a registry per process, so with several
# workers each scrape would only see one worker's counters. Scale with replicas.
CMD ["gunicorn", "-b", "0.0.0.0:8080", "app:app", "--workers", "1", "--threads", "8"]
