#!/usr/bin/env python3
"""Live smoke test for ClaudeProvider: two tiny calls to the real API, well under one cent.

    1. "Reply with the single word: ready" — effort low, max_tokens=16.
    2. A structured call into ``Ping(ok: bool)``.

Both go through ClaudeProvider (cached system block, effort, server-side refusal
fallback, structured outputs) inside MeteredProvider, so the token usage printed is
what the usage ledger would record. The credential comes from .env, loaded exactly
as the app loads it, and is never printed. If the API rejects it as an API key, it
is tried once more as an auth token.

Usage, from the Jarvis-Engine directory:
    .venv/bin/python scripts/smoke_claude.py

Exit status: 0 both calls answered; 2 the credential was rejected every way; 1 otherwise.
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import anthropic
from pydantic import BaseModel, Field

import app.core.config as cfg  # loads .env the way the app does
from app.domain.entities import LLMUsageEvent
from app.domain.errors import LLMError
from app.domain.events import DomainEvent, EventBus, EventListener, LLMCallCompleted
from app.domain.ports import LLMRequest
from app.infrastructure.llm.claude import ClaudeProvider, build_client
from app.infrastructure.llm.decorators import MeteredProvider

_SYSTEM_PROMPT = "You are a health check. Answer exactly as asked, nothing more."


class Ping(BaseModel):
    ok: bool = Field(description="True when you can read this message.")


class _ProbeProvider(ClaudeProvider):
    """The production request shape, with max_tokens=16 for the plain-text probe."""

    def _max_tokens(self, req: LLMRequest) -> int:
        return 16 if req.response_schema is None else super()._max_tokens(req)


class _UsageLog(EventListener):
    def __init__(self) -> None:
        self.usage: list[LLMUsageEvent] = []

    async def handle(self, event: DomainEvent) -> None:
        self.usage.append(event.usage)


async def _two_calls(label: str) -> None:
    bus = EventBus()
    log = _UsageLog()
    bus.subscribe(LLMCallCompleted, log)
    async with build_client(cfg) as client:
        provider = _ProbeProvider(cfg, client=client)
        metered = MeteredProvider(provider, bus, provider.model_for_role)
        calls = [
            LLMRequest(
                task="smoke_text",
                role="fast",
                prompt="Reply with the single word: ready",
                system_prompt=_SYSTEM_PROMPT,
                timeout_s=30,
            ),
            LLMRequest(
                task="smoke_schema",
                role="fast",
                prompt="Health check: set ok to true.",
                system_prompt=_SYSTEM_PROMPT,
                response_schema=Ping,
                timeout_s=30,
            ),
        ]
        for req in calls:
            answer = await metered.complete(req)
            usage = log.usage[-1]
            print(
                f"[{label}] {req.task}: provider={usage.provider} model={usage.model} "
                f"answer={answer!r} input_tokens={usage.input_tokens} "
                f"output_tokens={usage.output_tokens} cache_read_tokens={usage.cache_read_tokens} "
                f"latency_ms={usage.latency_ms}"
            )


def _rejection(exc: LLMError) -> anthropic.APIStatusError | None:
    cause = exc.__cause__
    if isinstance(cause, (anthropic.AuthenticationError, anthropic.PermissionDeniedError)):
        return cause
    return None


async def main() -> int:
    secret = cfg.ANTHROPIC_API_KEY or cfg.ANTHROPIC_AUTH_TOKEN
    if not secret:
        print("No ANTHROPIC_API_KEY or ANTHROPIC_AUTH_TOKEN in .env")
        return 1
    # (label, api key, auth token) — the provider picks its credential from config.
    attempts = []
    if cfg.ANTHROPIC_API_KEY:
        attempts.append(("api_key", secret, ""))
    attempts.append(("auth_token", "", secret))

    for label, api_key, auth_token in attempts:
        cfg.ANTHROPIC_API_KEY, cfg.ANTHROPIC_AUTH_TOKEN = api_key, auth_token
        try:
            await _two_calls(label)
        except LLMError as exc:
            rejected = _rejection(exc)
            if rejected is None:
                cause = type(exc.__cause__).__name__ if exc.__cause__ else "no cause"
                print(f"[{label}] FAILED: {exc.code} ({cause})")
                return 1
            print(
                f"[{label}] credential rejected: {type(rejected).__name__} "
                f"HTTP {rejected.status_code} ({rejected.type})"
            )
            continue
        except anthropic.APIStatusError as exc:  # BadRequestError propagates from the provider
            print(f"[{label}] FAILED: {type(exc).__name__} HTTP {exc.status_code}: {exc.message}")
            return 1
        print(f"OK — authenticated as {label}")
        return 0
    print("blocked: credential rejected")
    return 2


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
