"""What the workspace's web searches share: the failure they raise and the link parsing.

The search itself (``_search_web``) lives in ``workspace_builder``, the one function
allowed to call a web search (``tests/test_layering.py``).
"""

import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from app.domain.errors import LLMUnavailableError
from app.schemas.workspace import StudyAsset

WEB_RESULTS_UNAVAILABLE = "Web results are unavailable right now, so no links are shown"


class WebSearchUnavailableError(LLMUnavailableError):
    """Gemini's grounded web search did not answer; there are no web results to show."""

    code = "web_search_unavailable"


# A web search: (user prompt, system prompt) -> the raw answer, or WebSearchUnavailableError.
SearchWeb = Callable[[str, str], Awaitable[str]]
# A learning-style search: (task title, learning style) -> link assets.
StyleSearch = Callable[[str, str], Awaitable[list[StudyAsset]]]

_URL = re.compile(r"https?://[^\s|]+")
# Problem sites, and the asset type of a link to one.
_PROBLEM_SITES = {"leetcode.com": "leetcode_link", "codeforces.com": "codeforces_link"}


@dataclass(frozen=True)
class FoundLink:
    """A link a search answered: its URL, its title and the line it was on."""

    url: str
    title: str
    line: str


def found_links(raw: str) -> list[FoundLink]:
    """Each distinct URL in a search's answer, in order, with the title after its ``|``
    (else the URL's start)."""
    links: list[FoundLink] = []
    seen: set[str] = set()
    for line in (ln.strip() for ln in raw.replace("\r", "\n").split("\n")):
        match = _URL.search(line)
        if not match:
            continue
        url = match.group(0).rstrip(".,;)")
        if url in seen:
            continue
        seen.add(url)
        title = line.split("|")[-1].strip() if "|" in line else url[:60]
        links.append(FoundLink(url=url, title=title[:200], line=line))
    return links


def is_problem_site(url: str) -> bool:
    """Whether ``url`` is a LeetCode or Codeforces page."""
    return any(site in url for site in _PROBLEM_SITES)


def link_type(url: str) -> str:
    """The asset type of a link: a problem site's, a video, else an article."""
    for site, asset_type in _PROBLEM_SITES.items():
        if site in url:
            return asset_type
    if "youtube.com" in url or "youtu.be" in url:
        return "youtube_link"
    return "article_link"
