"""
Web search tool — two backends, zero required config.

Backend selection (checked in order):
  1. Tavily  — if TAVILY_API_KEY is set in .env (free tier: 1 000 req/month)
               Sign up at https://app.tavily.com
  2. DuckDuckGo — no API key needed; uses the `duckduckgo-search` package.
                  Works out of the box; rate-limited by DDG at heavy usage.

Models invoke `web_search` as a tool call during deep-analysis stages.
"""
import asyncio
import time
from dataclasses import dataclass, field


@dataclass
class SearchHit:
    title: str
    url: str
    snippet: str


@dataclass
class WebSearchResult:
    query: str
    backend: str = ""
    results: list[SearchHit] = field(default_factory=list)
    error: str | None = None
    duration_ms: int = 0


# ---------------------------------------------------------------------------
# Tavily backend
# ---------------------------------------------------------------------------

async def _tavily_search(query: str, api_key: str, max_results: int, timeout: int) -> WebSearchResult:
    import httpx

    start = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(
                "https://api.tavily.com/search",
                json={
                    "api_key": api_key,
                    "query": query,
                    "max_results": max_results,
                    "search_depth": "basic",
                    "include_answer": False,
                },
            )
            if not resp.is_success:
                return WebSearchResult(
                    query=query, backend="tavily",
                    error=f"Tavily API error {resp.status_code}: {resp.text[:300]}",
                    duration_ms=int((time.monotonic() - start) * 1000),
                )
            data = resp.json()
            hits = [
                SearchHit(title=r.get("title", ""), url=r.get("url", ""), snippet=r.get("content", ""))
                for r in data.get("results", [])[:max_results]
            ]
            return WebSearchResult(
                query=query, backend="tavily", results=hits,
                duration_ms=int((time.monotonic() - start) * 1000),
            )
    except Exception as exc:
        return WebSearchResult(
            query=query, backend="tavily", error=str(exc),
            duration_ms=int((time.monotonic() - start) * 1000),
        )


# ---------------------------------------------------------------------------
# DuckDuckGo backend (synchronous library → run in thread pool)
# ---------------------------------------------------------------------------

def _ddg_search_sync(query: str, max_results: int) -> list[SearchHit]:
    from duckduckgo_search import DDGS
    hits = []
    with DDGS() as ddgs:
        for r in ddgs.text(query, max_results=max_results):
            hits.append(SearchHit(
                title=r.get("title", ""),
                url=r.get("href", ""),
                snippet=r.get("body", ""),
            ))
    return hits


async def _ddg_search(query: str, max_results: int, timeout: int) -> WebSearchResult:
    start = time.monotonic()
    try:
        hits = await asyncio.wait_for(
            asyncio.get_event_loop().run_in_executor(None, _ddg_search_sync, query, max_results),
            timeout=float(timeout),
        )
        return WebSearchResult(
            query=query, backend="duckduckgo", results=hits,
            duration_ms=int((time.monotonic() - start) * 1000),
        )
    except asyncio.TimeoutError:
        return WebSearchResult(
            query=query, backend="duckduckgo",
            error=f"DuckDuckGo search timed out after {timeout}s",
            duration_ms=int((time.monotonic() - start) * 1000),
        )
    except Exception as exc:
        return WebSearchResult(
            query=query, backend="duckduckgo", error=str(exc),
            duration_ms=int((time.monotonic() - start) * 1000),
        )


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

async def web_search(
    query: str,
    tavily_api_key: str = "",
    max_results: int = 5,
    timeout_seconds: int = 15,
) -> WebSearchResult:
    """
    Search the web using the best available backend.
    Tavily is preferred when an API key is configured; DuckDuckGo is the fallback.
    """
    if tavily_api_key:
        return await _tavily_search(query, tavily_api_key, max_results, timeout_seconds)
    return await _ddg_search(query, max_results, timeout_seconds)
