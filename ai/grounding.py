"""Google Search grounding helpers for Gemini responses."""

import re
from typing import Any, Optional

# Questions that should trigger Google Search (news, IPO, macro, latest data)
_SEARCH_HINTS = re.compile(
    r"\b(aaj|today|latest|abhi|current|news|ipo|gmp|nav|fii|dii|"
    r"rbi|repo rate|budget|earnings|result|quarterly|listing|"
    r"open ipo|upcoming|calendar|announcement|policy|inflation|"
    r"top gainer|top loser|market news|sector rotation|bulk deal)\b",
    re.I,
)


def needs_google_search(question: str) -> bool:
    return bool(_SEARCH_HINTS.search(question))


def extract_grounding_sources(response: Any) -> list[dict[str, str]]:
    """Pull web source titles/URLs from Gemini grounding_metadata."""
    if not response or not getattr(response, "candidates", None):
        return []

    candidates = response.candidates
    if not candidates:
        return []

    gm = getattr(candidates[0], "grounding_metadata", None)
    if not gm or not getattr(gm, "grounding_chunks", None):
        return []

    sources: list[dict[str, str]] = []
    seen: set[str] = set()
    for chunk in gm.grounding_chunks:
        web = getattr(chunk, "web", None)
        if not web:
            continue
        uri = getattr(web, "uri", None) or ""
        if not uri or uri in seen:
            continue
        seen.add(uri)
        title = getattr(web, "title", None) or _title_from_uri(uri)
        sources.append({"title": title or "Web source", "url": uri})
        if len(sources) >= 6:
            break
    return sources


def extract_search_queries(response: Any) -> list[str]:
    if not response or not getattr(response, "candidates", None):
        return []
    gm = getattr(response.candidates[0], "grounding_metadata", None)
    if not gm:
        return []
    return list(getattr(gm, "web_search_queries", None) or [])[:5]


def _title_from_uri(uri: str) -> str:
    try:
        if "://" in uri:
            host = uri.split("://", 1)[1].split("/")[0]
            return host.replace("www.", "")
    except Exception:
        pass
    return "Source"
