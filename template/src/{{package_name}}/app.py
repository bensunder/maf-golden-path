"""HTTP entrypoint: ``uvicorn <package>.app:app``."""

from agentkit.hosting import create_app

from .agent import create_agent

app = create_app(create_agent)
