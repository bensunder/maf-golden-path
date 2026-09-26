FROM python:3.11-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install .
# The console lists the eval cases and shows the offline gate report the deploy pipeline writes here.
COPY evals ./evals
RUN useradd --create-home --uid 10001 agent
USER agent
ENV PORT=8000 AGENTKIT_ENVIRONMENT=prod AGENTKIT_AUTH_MODE=managed_identity AGENTKIT_REQUIRE_USER=true
EXPOSE 8000
CMD ["sh", "-c", "uvicorn order_status_agent.app:app --host 0.0.0.0 --port ${PORT}"]
