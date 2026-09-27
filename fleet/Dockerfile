# The fleet view. Build context is the repo root (see azure.yaml), so it installs this checkout's packages.
FROM python:3.11-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
COPY packages/agentkit-telemetry ./packages/agentkit-telemetry
COPY packages/agentkit-guardrails ./packages/agentkit-guardrails
COPY packages/agentkit-hosting ./packages/agentkit-hosting
COPY packages/agentkit-tools ./packages/agentkit-tools
COPY packages/agentkit-channels ./packages/agentkit-channels
RUN pip install ./packages/agentkit-telemetry ./packages/agentkit-guardrails ./packages/agentkit-hosting \
    ./packages/agentkit-tools ./packages/agentkit-channels
RUN useradd --create-home --uid 10001 fleet
USER fleet
ENV PORT=8000
EXPOSE 8000
CMD ["sh", "-c", "uvicorn agentkit.channels.fleet:app --host 0.0.0.0 --port ${PORT}"]
