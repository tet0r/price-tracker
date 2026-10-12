import asyncio
import os

import httpx

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
    async with httpx.AsyncClient(
        headers=headers, follow_redirects=True, timeout=25, http2=True
    ) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        return resp.text


async def fetch_browser(url: str, cookies: str = "") -> str:
    from playwright.async_api import async_playwright

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
            page = await context.new_page()
            await page.goto(url, wait_until="domcontentloaded", timeout=45000)
            try:
                await page.wait_for_load_state("networkidle", timeout=8000)
            except Exception:
                pass
            return await page.content()
        finally:
            await browser.close()


async def scrape(url: str, store: dict) -> tuple[Extracted, str]:
    """Fetch and extract using a store's config. Returns (extracted, method_used).

    A result counts when it has a price, or when the page is recognisably a
    product that can't be bought right now (so there may be no price to find).
    """
    fetchers = {"http": fetch_http, "browser": fetch_browser}
    service = store.get("service") or "auto"
    if service == "auto":
        order = ["browser", "http"] if store.get("last_method") == "browser" else ["http", "browser"]
    else:
        order = [service]
    if not USE_BROWSER:
        order = [m for m in order if m != "browser"]
    if not order:
        raise ScrapeError("browser fetching is disabled (USE_BROWSER=false)")

    errors, partial = [], Extracted()
    for method in order:
        try:
            body = await fetchers[method](url, store.get("cookies") or "")
        except httpx.HTTPStatusError as e:
            errors.append(f"{method}: site returned HTTP {e.response.status_code}")
            continue
        except Exception as e:
            detail = str(e).splitlines()[0][:150] if str(e) else "failed"
            errors.append(f"{method}: {type(e).__name__}: {detail}")
            continue
        found = extract(body, url, store.get("strategies"))
        if not found.price and found.name and store.get("missing_price_oos"):
            found.availability = "out_of_stock"
        if found.price or found.availability in UNAVAILABLE:
            return found, method
        partial.name = partial.name or found.name
        partial.image = partial.image or found.image
        errors.append(f"{method}: no price found on page")
    raise ScrapeError("; ".join(errors), partial)
