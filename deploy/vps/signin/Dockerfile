# Email sign-in for the VPS stack (SIGN_IN=email): replaces oauth2-proxy in front of agents; see signin.py.
FROM python:3.11-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
RUN pip install "starlette==1.8.0" "uvicorn==0.54.0" "httpx==0.28.1" "python-multipart==0.0.32" \
    && useradd --create-home --uid 10002 signin
WORKDIR /app
COPY signin.py ./
USER signin
EXPOSE 4180
CMD ["python", "signin.py"]
