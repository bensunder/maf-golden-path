"""Memory errors. Each carries a stable machine-readable ``code`` and the HTTP status the API maps it to."""

from __future__ import annotations

__all__ = [
    "MemoryConflict",
    "MemoryError_",
    "MemoryNotFound",
    "MemoryRateLimited",
    "MemoryRejected",
    "MemoryUnavailable",
]


class MemoryError_(Exception):  # noqa: N801 - trailing underscore: never shadow the builtin MemoryError
    status: int = 500
    code: str = "memory_error"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        if code:
            self.code = code


class MemoryRejected(MemoryError_):
    """The text breaks a memory rule (personal data, instructions, too long, empty). Nothing was stored."""

    status = 422
    code = "memory_rejected"


class MemoryNotFound(MemoryError_):
    status = 404
    code = "memory_not_found"


class MemoryConflict(MemoryError_):
    """Another writer changed the document first. The service retries; the API reports it if retries run out."""

    status = 409
    code = "memory_conflict"


class MemoryRateLimited(MemoryError_):
    status = 429
    code = "memory_rate_limited"

    def __init__(self, message: str, *, retry_after: int) -> None:
        super().__init__(message)
        self.retry_after = max(1, int(retry_after))


class MemoryUnavailable(MemoryError_):
    """The store timed out, failed, or the circuit breaker is open. Nothing was changed."""

    status = 503
    code = "memory_unavailable"

    def __init__(self, message: str, *, retry_after: int = 30) -> None:
        super().__init__(message)
        self.retry_after = max(1, int(retry_after))
