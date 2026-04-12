# tests/test_hooks.py
import pytest
from app.orchestrator.hooks import ActionHooks, HookDecision, HookResult


@pytest.mark.asyncio
async def test_hooks_allow_by_default():
    hooks = ActionHooks()
    result = await hooks.execute("PreModuleExecution", module="planning")
    assert result.decision == HookDecision.ALLOW


@pytest.mark.asyncio
async def test_hooks_deny_stops_execution():
    hooks = ActionHooks()
    async def deny_handler(**ctx):
        return HookResult(decision=HookDecision.DENY, reason="blocked")
    hooks.register("PreModuleExecution", deny_handler)
    result = await hooks.execute("PreModuleExecution", module="planning")
    assert result.decision == HookDecision.DENY
    assert result.reason == "blocked"


@pytest.mark.asyncio
async def test_hooks_ask_stops_execution():
    hooks = ActionHooks()
    async def ask_handler(**ctx):
        return HookResult(decision=HookDecision.ASK, reason="need consent")
    hooks.register("PreModuleExecution", ask_handler)
    result = await hooks.execute("PreModuleExecution", module="planning")
    assert result.decision == HookDecision.ASK


@pytest.mark.asyncio
async def test_hooks_modify_returns_modified_input():
    hooks = ActionHooks()
    async def pii_handler(**ctx):
        return HookResult(decision=HookDecision.MODIFY, modified_input={"prompt": "REDACTED"})
    hooks.register("PreCloudLLM", pii_handler)
    result = await hooks.execute("PreCloudLLM", prompt="my name is John")
    assert result.decision == HookDecision.MODIFY
    assert result.modified_input["prompt"] == "REDACTED"


@pytest.mark.asyncio
async def test_hooks_first_deny_wins():
    hooks = ActionHooks()
    async def allow_handler(**ctx):
        return HookResult(decision=HookDecision.ALLOW)
    async def deny_handler(**ctx):
        return HookResult(decision=HookDecision.DENY, reason="denied")
    hooks.register("PreModuleExecution", allow_handler)
    hooks.register("PreModuleExecution", deny_handler)
    result = await hooks.execute("PreModuleExecution", module="planning")
    assert result.decision == HookDecision.DENY


@pytest.mark.asyncio
async def test_pii_filter_hook_strips_email():
    from app.orchestrator.hooks import pii_filter_hook
    result = await pii_filter_hook(prompt="Contact john@example.com for details")
    assert result.decision == HookDecision.MODIFY
    assert "john@example.com" not in result.modified_input["prompt"]
    assert "[EMAIL]" in result.modified_input["prompt"]
