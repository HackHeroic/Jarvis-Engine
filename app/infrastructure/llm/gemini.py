"""Gemini in the cloud, reached through the LiteLLM transport."""

from collections.abc import AsyncIterator
from contextlib import aclosing
from types import ModuleType

from pydantic import BaseModel

from app.domain.entities import LLMLocation
from app.domain.ports import LLMProvider, LLMRequest
from app.infrastructure.llm.transport import coerce_to_schema, translated_errors


class GeminiProvider(LLMProvider):
    """Gemini via ``litellm_conf.hybrid_route_query(force_cloud=True)``.

    Not ``gemini_primary_route``: that retries a failed cloud call on the local model
    inside itself, which hid the real error from the chain, swallowed code bugs, and
    dialled LM Studio even when the call had to skip it. Fallback belongs to the chain.

    ``cfg`` is the config module, read at call time. The transport is looked up on its
    module at call time so tests that patch ``litellm_conf.hybrid_route_query`` reach it.
    """

    name = "gemini"
    location = LLMLocation.CLOUD

    def __init__(self, cfg: ModuleType) -> None:
        self._cfg = cfg

    def model_for_role(self, role: str) -> str:
        """Return the Gemini model; one model serves every role."""
        return self._cfg.GEMINI_MODEL

    def is_available(self) -> bool:
        """Available whenever a Gemini key is configured."""
        return bool(self._cfg.GEMINI_API_KEY)

    async def complete(self, req: LLMRequest) -> str | BaseModel:
        import app.models.brain.litellm_conf as _llm

        with translated_errors(parse_errors=(_llm.EmptyLLMResponseError,)):
            result = await _llm.hybrid_route_query(
                user_prompt=req.prompt,
                system_prompt=req.system_prompt,
                response_schema=req.response_schema,
                force_cloud=True,
                conversation_history=req.conversation_history,
            )
            if req.response_schema is None:
                return result
            return coerce_to_schema(result, req.response_schema)

    async def stream(self, req: LLMRequest) -> AsyncIterator[tuple[str, str]]:
        import app.models.brain.litellm_conf as _llm

        with translated_errors():
            tokens = await _llm.hybrid_route_query(
                user_prompt=req.prompt,
                system_prompt=req.system_prompt,
                response_schema=None,
                force_cloud=True,
                conversation_history=req.conversation_history,
                stream=True,
            )
            async with aclosing(tokens):
                async for item in tokens:
                    yield item
