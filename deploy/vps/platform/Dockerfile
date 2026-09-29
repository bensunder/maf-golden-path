# The VPS platform service (server.py). It runs docker compose against this host's Docker, so it gets the
# Docker CLI and the compose and buildx plugins from the official image, plus git and copier for the template.
FROM docker:27-cli AS docker

FROM python:3.11-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/* \
    && git config --system --add safe.directory '*'
COPY --from=docker /usr/local/bin/docker /usr/local/bin/docker
COPY --from=docker /usr/local/libexec/docker/cli-plugins /usr/local/libexec/docker/cli-plugins
RUN pip install "fastapi>=0.115" "uvicorn>=0.30" "httpx>=0.27" "pyyaml>=6" "copier>=9.3" "mcp>=1.30" "cryptography>=43"
WORKDIR /app
COPY server.py connectors.py templates.py security.py agentcalls.py ./
EXPOSE 8000 8001
CMD ["python", "server.py"]
