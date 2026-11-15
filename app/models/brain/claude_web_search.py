"""Web search through Claude's server-side ``web_search`` tool.

The workspace's links (a tutorial, an article, practice problems) need a model that can
search the web, not one that remembers URLs. Claude runs the search on Anthropic's
servers and answers from the pages it found, so the same key that serves chat serves
the search; Gemini's grounded search is the fallback (``workspace_builder._search_web``).
"""

import logging
import re

import app.core.config as _cfg

logger = logging.getLogger(__name__)

# The basic search tool: one search, its results straight into the answer. The newer
# version filters results by running code first, which for "find one good link" cost
# about ten times the input tokens when measured and sometimes failed in the sandbox.
WEB_SEARCH_TOOL = "web_search_20250305"
# Searches one answer may run: two cover "one video and one article"; each is billed.
WEB_SEARCH_MAX_USES = 2
WEB_SEARCH_MAX_TOKENS = 1024
# A server-tool turn can pause (stop_reason "pause_turn") and be sent back to go on.
_MAX_RESUMES = 2


async def claude_web_search(
    user_prompt: str,
    system_prompt: str = "You are a helpful research assistant. Return real URLs you found.",
) -> str:
    """Answer ``user_prompt`` from a live web search, or "" when the search cannot run.

    Only links the search itself returned are kept: a line whose URL is not among the
    results is dropped, so a link recalled from memory never reaches the user. ""
    (logged) when no Anthropic credential is configured, the call fails or no search ran: the caller falls back or says honestly that links are unavailable. The
    tokens used are reported to the metering around the call, if any.
    """
    if not (_cfg.ANTHROPIC_API_KEY or _cfg.ANTHROPIC_AUTH_TOKEN):
        return ""
    from app.infrastructure.llm.claude import shared_client
    from app.infrastructure.llm.transport import report_token_usage

    messages: list[dict] = [{"role": "user", "content": user_prompt}]
    try:
        client = shared_client(_cfg)
        for _attempt in range(_MAX_RESUMES + 1):
            reply = await client.messages.create(
                model=_cfg.CLAUDE_MODEL,
                max_tokens=WEB_SEARCH_MAX_TOKENS,
                system=system_prompt,
                messages=messages,
                tools=[{
                    "type": WEB_SEARCH_TOOL,
                    "name": "web_search",
                    "max_uses": WEB_SEARCH_MAX_USES,
                }],
                output_config={"effort": "low"},
                timeout=_cfg.LLM_TIMEOUT_SECONDS,
            )
            usage = reply.usage
            report_token_usage(
                input_tokens=int(usage.input_tokens or 0),
                output_tokens=int(usage.output_tokens or 0),
                cache_read_tokens=int(getattr(usage, "cache_read_input_tokens", 0) or 0),
            )
            if reply.stop_reason != "pause_turn":
                break
            messages.append({"role": "assistant", "content": reply.content})
        found = _found_urls(reply.content)
        if not found:
            # No search ran (or it found nothing): whatever the text says is from memory.
            logger.warning("Claude web search returned no results")
            return ""
        text = "".join(block.text for block in reply.content if block.type == "text")
        return _only_found_links(text, found)
    except Exception as exc:
        logger.warning("Claude web search failed (%s)", type(exc).__name__)
        return ""


_URL = re.compile(r"https?://[^\s|<>\"')\]]+")


def _normal(url: str) -> str:
    return url.rstrip(".,;/").lower()


def _found_urls(content: list) -> frozenset[str]:
    """The URLs the search returned, from the reply's ``web_search_tool_result`` blocks."""
    found: set[str] = set()
    for block in content:
        if block.type != "web_search_tool_result" or not isinstance(block.content, list):
            continue  # an errored search carries an error object, not a list
        for result in block.content:
            url = getattr(result, "url", None)
            if url:
                found.add(_normal(url))
    return frozenset(found)


def _only_found_links(text: str, found: frozenset[str]) -> str:
    """``text`` without the lines that carry a URL the search did not return."""
    kept = [
        line for line in text.splitlines()
        if all(_normal(url) in found for url in _URL.findall(line))
    ]
    return "\n".join(kept).strip()
