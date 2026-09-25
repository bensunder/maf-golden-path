"""A ready-made chat page for the agent at ``/chat``, using the ``<agentkit-chat>`` web component.

    app = create_app(create_agent, channels=[AgUiChannel(), WebChat()])

Behind Container Apps Easy Auth the page and the AG-UI endpoint share the sign-in cookie, so there is
nothing to configure. To embed the chat in another app on the same origin, load
``/chat/agentkit-chat.js`` and add ``<agentkit-chat endpoint="/v1/agui">``.
"""

from __future__ import annotations

import html
from importlib import resources
from typing import Any

from fastapi.responses import HTMLResponse, Response

from agentkit.hosting import AgentKitSettings, ConversationService

__all__ = ["WebChat"]

_CSP = ("default-src 'none'; script-src 'self'; connect-src 'self'; style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
_SECURITY_HEADERS = {"Content-Security-Policy": _CSP, "X-Content-Type-Options": "nosniff",
                     "Referrer-Policy": "no-referrer", "Cache-Control": "no-cache"}


def _static(name: str) -> str:
    return resources.files("agentkit.channels").joinpath("static", name).read_text(encoding="utf-8")


class WebChat:
    def __init__(self, path: str = "/chat", *, endpoint: str = "/v1/agui", title: str | None = None):
        self.path = path.rstrip("/")
        self.endpoint = endpoint
        self.title = title

    def install(self, app: Any, service: ConversationService, settings: AgentKitSettings) -> None:
        script_path = f"{self.path}/agentkit-chat.js"
        page = (_static("chat.html")
                .replace("{{title}}", html.escape(self.title or settings.service_name))
                .replace("{{endpoint}}", html.escape(self.endpoint))
                .replace("{{script}}", html.escape(script_path)))
        script = _static("agentkit-chat.js")

        async def chat_page() -> HTMLResponse:
            return HTMLResponse(page, headers=_SECURITY_HEADERS)

        async def chat_script() -> Response:
            return Response(script, media_type="text/javascript", headers=_SECURITY_HEADERS)

        app.add_api_route(self.path, chat_page, methods=["GET"], include_in_schema=False)
        app.add_api_route(script_path, chat_script, methods=["GET"], include_in_schema=False)
