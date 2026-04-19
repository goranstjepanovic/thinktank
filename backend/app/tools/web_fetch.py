"""
Web page fetcher — headless Chromium via Playwright.

Renders the full page including JavaScript, then extracts readable text.
Used when search snippets aren't enough and the full page content is needed.

Setup (one-time after pip install):
    playwright install chromium
"""
import asyncio
import time
from dataclasses import dataclass

_MAX_CONTENT_CHARS = 12_000
_NAVIGATE_TIMEOUT_MS = 20_000


@dataclass
class FetchResult:
    url: str
    title: str = ""
    content: str = ""
    truncated: bool = False
    error: str | None = None
    duration_ms: int = 0


async def fetch_webpage(url: str, timeout_seconds: int = 25) -> FetchResult:
    """
    Fetch a URL using a headless Chromium browser, wait for JS to settle,
    and return the visible text content (not raw HTML).
    """
    start = time.monotonic()

    try:
        from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout
    except ImportError:
        return FetchResult(
            url=url,
            error="Playwright is not installed. Run: pip install playwright && playwright install chromium",
            duration_ms=0,
        )

    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                # Block images, fonts, and media to speed up page load
                await page.route(
                    "**/*",
                    lambda route: (
                        asyncio.ensure_future(route.abort())
                        if route.request.resource_type in ("image", "media", "font", "stylesheet")
                        else asyncio.ensure_future(route.continue_())
                    ),
                )
                try:
                    await page.goto(url, wait_until="networkidle", timeout=_NAVIGATE_TIMEOUT_MS)
                except PlaywrightTimeout:
                    # networkidle timed out — grab whatever rendered so far
                    pass

                title = await page.title()
                # inner_text gives visible text without HTML tags
                raw = await page.inner_text("body")
            finally:
                await browser.close()

        # Collapse runs of blank lines down to a single blank line
        lines = raw.splitlines()
        cleaned_lines: list[str] = []
        prev_blank = False
        for line in lines:
            stripped = line.strip()
            if not stripped:
                if not prev_blank:
                    cleaned_lines.append("")
                prev_blank = True
            else:
                cleaned_lines.append(stripped)
                prev_blank = False
        content = "\n".join(cleaned_lines).strip()

        truncated = len(content) > _MAX_CONTENT_CHARS
        if truncated:
            content = content[:_MAX_CONTENT_CHARS]

        return FetchResult(
            url=url,
            title=title,
            content=content,
            truncated=truncated,
            duration_ms=int((time.monotonic() - start) * 1000),
        )

    except Exception as exc:
        return FetchResult(
            url=url,
            error=str(exc),
            duration_ms=int((time.monotonic() - start) * 1000),
        )
