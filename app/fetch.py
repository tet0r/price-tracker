import asyncio
import os

import httpx

from . import ai, netguard
from .extract import UNAVAILABLE, Extracted, extract

USE_BROWSER = os.environ.get("USE_BROWSER", "true").lower() in ("1", "true", "yes")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Upgrade-Insecure-Requests": "1",
}

IMAGE_TYPES = {
    "image/jpeg": "jpg", "image/png": "png", "image/webp": "webp", "image/gif": "gif", "image/avif": "avif",
}
MAX_IMAGE_BYTES = 5_000_000
MAX_PAGE_BYTES = 15_000_000

# One Chromium at a time keeps memory use predictable
_browser_slot = asyncio.Semaphore(1)


class ScrapeError(Exception):
    def __init__(self, message: str, partial: Extracted | None = None):
        super().__init__(message)
        self.partial = partial or Extracted()


def _cookie_pairs(cookies: str) -> list[tuple[str, str]]:
    pairs = (part.strip().partition("=") for part in cookies.split(";"))
    return [(name.strip(), value.strip()) for name, sep, value in pairs if sep and name.strip()]


async def fetch_http(url: str, cookies: str = "") -> str:
    headers = dict(HEADERS)
    if cookies.strip():
        headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in _cookie_pairs(cookies))
    hooks = {} if netguard.ALLOW_PRIVATE else {"request": [netguard.httpx_guard]}
    async with httpx.AsyncClient(
        headers=headers, follow_redirects=True, timeout=25, http2=True, event_hooks=hooks
    ) as client:
        async with client.stream("GET", url) as resp:
            resp.raise_for_status()
            data = b""
            async for chunk in resp.aiter_bytes():
                data += chunk
                if len(data) > MAX_PAGE_BYTES:
                    raise ValueError("page is larger than 15 MB")
            return data.decode(resp.encoding or "utf-8", errors="replace")


async def fetch_image(url: str, referer: str) -> tuple[bytes, str] | None:
    """Download a product photo. Returns (bytes, file extension), or None if it isn't a usable image.

    The URL is chosen by the scraped page, so it must be a public address even
    when private product URLs are allowed.
    """
    headers = {"User-Agent": USER_AGENT, "Accept": "image/avif,image/webp,image/*,*/*;q=0.8", "Referer": referer}
    hooks = {"request": [netguard.httpx_guard]}
    try:
        async with httpx.AsyncClient(headers=headers, follow_redirects=True, timeout=20, event_hooks=hooks) as client:
            async with client.stream("GET", url) as resp:
                kind = resp.headers.get("content-type", "").split(";")[0].strip().lower()
                if resp.status_code != 200 or kind not in IMAGE_TYPES:
                    return None
                data = b""
                async for chunk in resp.aiter_bytes():
                    data += chunk
                    if len(data) > MAX_IMAGE_BYTES:
                        return None
    except (httpx.HTTPError, netguard.BlockedURL):
        return None
    return (data, IMAGE_TYPES[kind]) if data else None


async def _block_private(route) -> None:
    """Playwright route handler: stop the page, its redirects and its scripts reaching private addresses."""
    if await netguard.is_public(route.request.url):
        await route.continue_()
    else:
        await route.abort("blockedbyclient")


async def fetch_browser(url: str, cookies: str = "") -> str:
    from playwright.async_api import async_playwright

    if not netguard.ALLOW_PRIVATE:
        await netguard.require_public(url)
    async with _browser_slot, async_playwright() as pw:
        browser = await pw.chromium.launch(
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
            ]
        )
        try:
            context = await browser.new_context(
                user_agent=USER_AGENT, locale="en-US", viewport={"width": 1366, "height": 900}
            )
            if cookies.strip():
                await context.add_cookies(
                    [{"name": k, "value": v, "url": url} for k, v in _cookie_pairs(cookies)]
                )
            if not netguard.ALLOW_PRIVATE:
                await context.route("**/*", _block_private)
            page = await context.new_page()
            await page.goto(url, wait_until="domcontentloaded", timeout=45000)
            try:
                await page.wait_for_load_state("networkidle", timeout=8000)
            except Exception:
                pass
            return await page.content()
        finally:
            await browser.close()


def _fetch_order(store: dict) -> list[str]:
    service = store.get("service") or "auto"
    if service == "auto":
        order = ["browser", "http"] if store.get("last_method") == "browser" else ["http", "browser"]
    else:
        order = [service]
    if not USE_BROWSER:
        order = [m for m in order if m != "browser"]
    if not order:
        raise ScrapeError("browser fetching is disabled (USE_BROWSER=false)")
    return order


async def _fetch(method: str, url: str, store: dict) -> str:
    """Fetch a page, turning any failure into a ScrapeError with a readable reason."""
    fetcher = fetch_http if method == "http" else fetch_browser
    try:
        return await fetcher(url, store.get("cookies") or "")
    except httpx.HTTPStatusError as e:
        raise ScrapeError(f"{method}: site returned HTTP {e.response.status_code}")
    except Exception as e:
        detail = str(e).splitlines()[0][:150] if str(e) else "failed"
        raise ScrapeError(f"{method}: {type(e).__name__}: {detail}")


async def fetch_body(url: str, store: dict) -> tuple[str, str]:
    """Return (body, method) from the first fetch method that works for this store."""
    errors = []
    for method in _fetch_order(store):
        try:
            return await _fetch(method, url, store), method
        except ScrapeError as e:
            errors.append(str(e))
    raise ScrapeError("; ".join(errors))


async def scrape(url: str, store: dict) -> tuple[Extracted, str]:
    """Fetch and extract using a store's config. Returns (extracted, method_used).

    A result counts when it has a price, or when the page is recognisably a
    product that can't be bought right now (so there may be no price to find).
    """
    errors, partial, last = [], Extracted(), None
    for method in _fetch_order(store):
        try:
            body = await _fetch(method, url, store)
        except ScrapeError as e:
            errors.append(str(e))
            continue
        last = (body, method)
        found = extract(body, url, store.get("strategies"))
        if not found.price and found.name and store.get("missing_price_oos"):
            found.availability = "out_of_stock"
        if found.price or found.availability in UNAVAILABLE:
            return found, method
        partial.name = partial.name or found.name
        partial.image = partial.image or found.image
        errors.append(f"{method}: no price found on page")

    # Last resort, only fills a genuine gap: let AI read the page the rules couldn't
    if last and store.get("ai_extract") and ai.enabled():
        try:
            found = await ai.extract_price(last[0], url)
        except ai.AIError as e:
            errors.append(f"ai: {e}")
        else:
            if found:
                found.name, found.image = partial.name or found.name, partial.image
                return found, last[1]
            errors.append("ai: not confident about the price")
    raise ScrapeError("; ".join(errors), partial)
