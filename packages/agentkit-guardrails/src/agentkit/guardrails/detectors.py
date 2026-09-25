"""Prompt-injection detectors: Azure AI Content Safety Prompt Shields, plus an offline heuristic."""

from __future__ import annotations

import logging
import re
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

import httpx

__all__ = [
    "HeuristicInjectionDetector",
    "InjectionDetector",
    "PromptShieldsDetector",
    "Verdict",
]

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Verdict:
    attack: bool
    source: str = "user_prompt"  # user_prompt | document
    detector: str = ""
    detail: str | None = None

    @classmethod
    def clean(cls, detector: str) -> Verdict:
        return cls(attack=False, detector=detector)


@runtime_checkable
class InjectionDetector(Protocol):
    name: str

    async def analyze(self, user_prompt: str, documents: Sequence[str] = ()) -> Verdict: ...


_DEFAULT_PATTERNS = [
    r"\bignore\s+(all\s+|any\s+|the\s+)?(previous|prior|above|earlier)\s+(instructions|prompts|rules|messages)",
    r"\bdisregard\s+(all\s+|any\s+|the\s+|your\s+)?(previous|prior|above|system)?\s*(instructions|prompts|rules)",
    r"\bforget\s+(all\s+|your\s+|the\s+)?(previous\s+|prior\s+)?(instructions|rules)",
    r"\b(reveal|print|show|repeat|output)\s+(me\s+)?(your|the)\s+(system\s+prompt|hidden\s+instructions|instructions)",
    r"\byou\s+are\s+now\s+(dan|in\s+developer\s+mode|jailbroken|unrestricted)",
    r"\b(act|behave)\s+as\s+.{0,40}\b(without|no)\s+(restrictions|filters|guardrails|rules)",
    r"\bjailbreak\b",
    r"<\s*/?\s*(system|im_start|im_end)\s*>",
]


class HeuristicInjectionDetector:
    """Regex screen for well-known injection phrasing.

    For local dev and tests, and as a fallback. It is not a substitute for Prompt Shields:
    paraphrased or encoded attacks will get through.
    """

    name = "heuristic"

    def __init__(self, extra_patterns: Sequence[str] = ()) -> None:
        self._patterns = [re.compile(p, re.IGNORECASE) for p in (*_DEFAULT_PATTERNS, *extra_patterns)]

    def _match(self, text: str) -> str | None:
        for pattern in self._patterns:
            if pattern.search(text or ""):
                return pattern.pattern
        return None

    async def analyze(self, user_prompt: str, documents: Sequence[str] = ()) -> Verdict:
        if hit := self._match(user_prompt):
            return Verdict(attack=True, source="user_prompt", detector=self.name, detail=hit)
        for doc in documents:
            if hit := self._match(doc):
                return Verdict(attack=True, source="document", detector=self.name, detail=hit)
        return Verdict.clean(self.name)


TokenProvider = Callable[[], Awaitable[str]]


class PromptShieldsDetector:
    """Azure AI Content Safety Prompt Shields (user-prompt and document attacks).

    Authenticate with ``token_provider`` (Entra ID, preferred; e.g. from
    :func:`entra_token_provider`) or ``api_key``. Text longer than the service
    limit is analyzed in chunks. On service errors the detector fails closed by default.
    """

    name = "prompt_shields"
    API_VERSION = "2024-09-01"
    MAX_CHARS = 10_000

    def __init__(
        self,
        endpoint: str,
        *,
        token_provider: TokenProvider | None = None,
        api_key: str | None = None,
        fail_closed: bool = True,
        timeout: float = 5.0,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        if not (token_provider or api_key):
            raise ValueError("PromptShieldsDetector needs token_provider (Entra ID) or api_key")
        self._url = f"{endpoint.rstrip('/')}/contentsafety/text:shieldPrompt?api-version={self.API_VERSION}"
        self._token_provider = token_provider
        self._api_key = api_key
        self._fail_closed = fail_closed
        self._client = http_client or httpx.AsyncClient(timeout=timeout)

    async def _headers(self) -> dict[str, str]:
        if self._token_provider:
            return {"Authorization": f"Bearer {await self._token_provider()}"}
        return {"Ocp-Apim-Subscription-Key": self._api_key or ""}

    def _chunks(self, text: str) -> list[str]:
        text = text or ""
        return [text[i : i + self.MAX_CHARS] for i in range(0, len(text), self.MAX_CHARS)] or [""]

    async def _call(self, user_prompt: str, documents: Sequence[str]) -> Verdict:
        response = await self._client.post(
            self._url,
            headers=await self._headers(),
            json={"userPrompt": user_prompt, "documents": list(documents)},
        )
        response.raise_for_status()
        body = response.json()
        if body.get("userPromptAnalysis", {}).get("attackDetected"):
            return Verdict(attack=True, source="user_prompt", detector=self.name)
        for index, doc in enumerate(body.get("documentsAnalysis") or []):
            if doc.get("attackDetected"):
                return Verdict(attack=True, source="document", detector=self.name, detail=f"document[{index}]")
        return Verdict.clean(self.name)

    async def analyze(self, user_prompt: str, documents: Sequence[str] = ()) -> Verdict:
        try:
            for chunk in self._chunks(user_prompt):
                verdict = await self._call(chunk, [])
                if verdict.attack:
                    return verdict
            doc_chunks = [c for d in documents for c in self._chunks(d) if c]
            for i in range(0, len(doc_chunks), 5):
                verdict = await self._call("", doc_chunks[i : i + 5])
                if verdict.attack:
                    return verdict
            return Verdict.clean(self.name)
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("Prompt Shields call failed: %s", exc)
            if self._fail_closed:
                return Verdict(attack=True, detector=self.name, detail=f"shield unavailable: {type(exc).__name__}")
            return Verdict.clean(self.name)


def entra_token_provider(credential, scope: str = "https://cognitiveservices.azure.com/.default") -> TokenProvider:
    """Wrap an ``azure.identity.aio`` credential as a token provider."""

    async def _get() -> str:
        token = await credential.get_token(scope)
        return token.token

    return _get
