"""The on-device model in LM Studio, reached through the LiteLLM transport."""

from collections.abc import AsyncIterator
from contextlib import aclosing
from types import ModuleType

from pydantic import BaseModel

from app.domain.entities import LLMLocation
from app.domain.errors import LLMUnavailableError
from app.domain.ports import LLMProvider, LLMRequest
from app.infrastructure.llm.messages import fold_system_notes
from app.infrastructure.llm.transport import coerce_to_schema, translated_errors


class LocalLMStudioProvider(LLMProvider):
    """Local Gemma via ``litellm_conf.hybrid_route_query``, model chosen by role.

    ``cfg`` is the config module, read at call time so startup model detection and
    the ``*_PRIMARY`` switches are honoured. The transport is looked up on its module
    at call time too, so tests that patch ``litellm_conf.hybrid_route_query`` reach it.
    History system notes are folded into the prompt (``fold_system_notes``): Gemma's chat
    template rejects a system turn mid-conversation, and LM Studio's prefix cache keeps
    the stable system prompt and earlier turns.
    """

    name = "local"
    location = LLMLocation.LOCAL

    def __init__(self, cfg: ModuleType) -> None:
        self._cfg = cfg

    def model_for_role(self, role: str) -> str:
        """Return the local model serving ``role``; empty for roles local does not serve."""
        if role == "primary":
            return self._cfg.GEMMA_PRIMARY_MODEL
        if role == "fast":
            return self._cfg.GEMMA_FAST_MODEL
        return ""

    def is_available(self) -> bool:
        """Off when a cloud tier is primary or no local model is configured."""
        cfg = self._cfg
        if cfg.GEMINI_PRIMARY or cfg.CLAUDE_PRIMARY:
            return False
        return bool(self.model_for_role("primary") or self.model_for_role("fast"))

    def _model(self, req: LLMRequest) -> str:
        model = self.model_for_role(req.role)
        if not model:
            raise LLMUnavailableError(
                "No local model serves this request.", details={"role": req.role}
            )
        return model

    async def complete(self, req: LLMRequest) -> str | BaseModel:
        import app.models.brain.litellm_conf as _llm

        model = self._model(req)
        history, prompt = fold_system_notes(req.conversation_history, req.prompt)
        with translated_errors(parse_errors=(_llm.EmptyLLMResponseError,)):
            result = await _llm.hybrid_route_query(
                user_prompt=prompt,
                system_prompt=req.system_prompt,
                response_schema=req.response_schema,
                model_override=model,
                conversation_history=history,
            )
            if req.response_schema is None:
                return result
            return coerce_to_schema(result, req.response_schema)

    async def stream(self, req: LLMRequest) -> AsyncIterator[tuple[str, str]]:
        import app.models.brain.litellm_conf as _llm

        model = self._model(req)
        history, prompt = fold_system_notes(req.conversation_history, req.prompt)
        with translated_errors():
            tokens = await _llm.hybrid_route_query(
                user_prompt=prompt,
                system_prompt=req.system_prompt,
                response_schema=None,
                model_override=model,
                conversation_history=history,
                stream=True,
            )
            async with aclosing(tokens):
                async for item in tokens:
                    yield item
