# syntax=docker/dockerfile:1
# Any agent generated from the kit's template (or the sample), with its web chat and console, for a plain
# Docker host. The build context is the agent's folder, filtered by Dockerfile.dockerignore next to this file
# (not the agent's own .dockerignore, which leaves out tests/: the evals run here). The kit's packages come
# from this checkout, the named context "kit" (docker-compose.yml and agentctl.py set both).
FROM python:3.11-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
COPY --from=kit packages ./packages
RUN pip install "./packages/agentkit-telemetry[azure]" ./packages/agentkit-guardrails "./packages/agentkit-hosting[redis]" \
    ./packages/agentkit-tools "./packages/agentkit-knowledge[blob]" "./packages/agentkit-channels[teams]" \
    ./packages/agentkit-testing
COPY --from=kit deploy/vps/install_agent.py ./install_agent.py
COPY . ./agent
RUN python install_agent.py /app/agent

# The console shows the quality gate for exactly this build: run the offline evals now and keep the report.
WORKDIR /app/agent
RUN pytest -q -p no:cacheprovider tests/test_evals.py --agentkit-eval-report evals/gate-report.json

RUN useradd --create-home --uid 10001 agent
USER agent
ENV PORT=8000
EXPOSE 8000
CMD ["sh", "-c", "exec uvicorn \"$(cat /app/agent-app)\" --host 0.0.0.0 --port ${PORT} --proxy-headers --forwarded-allow-ips='*'"]
