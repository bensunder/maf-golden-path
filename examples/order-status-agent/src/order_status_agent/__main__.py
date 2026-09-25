"""Run locally: ``python -m order_status_agent`` (or use DevUI for the inner loop)."""

import os

import uvicorn

if __name__ == "__main__":
    uvicorn.run("order_status_agent.app:app", host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
