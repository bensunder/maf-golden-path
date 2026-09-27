# The sample agent (with web chat and the console) for a plain Docker host, built from this checkout's
# packages: no private feed, no Azure needed. Build context is the repo root (see docker-compose.yml).
FROM python:3.11-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
COPY packages ./packages
RUN pip install "./packages/agentkit-telemetry[azure]" ./packages/agentkit-guardrails "./packages/agentkit-hosting[redis]" \
    ./packages/agentkit-tools "./packages/agentkit-knowledge[blob]" "./packages/agentkit-channels[teams]" \
    ./packages/agentkit-testing
COPY examples/order-status-agent ./agent
RUN pip install ./agent

# The console shows the quality gate for exactly this build: run the offline evals now and keep the report.
WORKDIR /app/agent
RUN pytest -q -p no:cacheprovider tests/test_evals.py --agentkit-eval-report evals/gate-report.json

RUN useradd --create-home --uid 10001 agent
USER agent
ENV PORT=8000
EXPOSE 8000
CMD ["sh", "-c", "uvicorn order_status_agent.app:app --host 0.0.0.0 --port ${PORT} --proxy-headers --forwarded-allow-ips='*'"]
