"""The Jarvis error hierarchy.

Every error carries a stable machine-readable ``code`` and the HTTP status the
API layer answers with; ``app/api/error_handlers.py`` turns them into the one
error envelope. ``message`` is user-facing text — never put ``str(exc)`` of a
lower-level exception in it.

``ValidationError`` here shares its name with Pydantic's. In a module that uses
both, import this module (``from app.domain import errors``) instead of the name.
"""


class JarvisError(Exception):
    """Base of every error Jarvis raises on purpose."""

    code: str = "internal_error"
    http_status: int = 500

    def __init__(self, message: str, *, details: dict | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details


class DomainError(JarvisError):
    """A rule of the domain was broken; the request cannot be honoured as asked."""


class ValidationError(DomainError):
    code = "validation_error"
    http_status = 422


class PayloadTooLargeError(ValidationError):
    """The request body is larger than any valid request could be; refused unread."""

    code = "payload_too_large"
    http_status = 413


class NotFoundError(DomainError):
    code = "not_found"
    http_status = 404


class ConflictError(DomainError):
    code = "conflict"
    http_status = 409


class InvalidStateTransition(ConflictError):
    code = "invalid_state_transition"


class StaleVersionError(ConflictError):
    code = "stale_version"


class DraftOutdatedError(ConflictError):
    """Every slot in the draft has already begun: accepting it would plan only the past."""

    code = "draft_outdated"


class ScheduleInfeasibleError(DomainError):
    code = "schedule_infeasible"
    http_status = 422


class InfrastructureError(JarvisError):
    """Something Jarvis depends on (database, model) failed; the request was fine."""


class PersistenceError(InfrastructureError):
    code = "persistence_unavailable"
    http_status = 503


class EmbeddingUnavailableError(InfrastructureError):
    """The text embedder could not run (model missing, failed to load or answer)."""

    code = "embedding_unavailable"
    http_status = 503


class DocumentReadTimeoutError(InfrastructureError):
    """The document reader did not finish in time; the request was fine."""

    code = "document_read_timeout"
    http_status = 503


class LLMError(InfrastructureError):
    code = "llm_error"
    http_status = 503


class LLMTimeoutError(LLMError):
    code = "llm_timeout"


class LLMUnavailableError(LLMError):
    code = "llm_unavailable"


class LLMOutputParseError(LLMError):
    code = "llm_output_parse"


class LLMRefusedError(LLMError):
    """The model declined the request. Another provider may answer; asking this one again won't."""

    code = "llm_refused"


def public_message(exc: BaseException, fallback: str) -> str:
    """The text a user may see about ``exc``: a ``JarvisError``'s own message, else ``fallback``.

    Anything else's ``str()`` can carry a connection string, a query or a stack
    detail; log it, and show ``fallback``.
    """
    return exc.message if isinstance(exc, JarvisError) else fallback
