FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY crypto_monitor/ ./crypto_monitor/

ENV PYTHONUNBUFFERED=1 \
    CRYPTO_MONITOR_CONFIG=/app/config.yaml \
    CRYPTO_MONITOR_ENV=/app/.env \
    CRYPTO_MONITOR_CACHE=/root/.cache/crypto_monitor

# Collects prices/headlines on a loop and emits the trade signal
CMD ["python", "-m", "crypto_monitor.docker_entrypoint"]
