import asyncio
import base64
import json
import logging
import os
import random
import re
import secrets
import sqlite3
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal
from urllib.parse import parse_qsl, urlencode, urlparse

import apprise
import httpx
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from . import ai, db
from .extract import UNAVAILABLE
from .fetch import ScrapeError, _cookie_pairs, fetch_body, fetch_image, scrape

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("pricetracker")

DEFAULT_INTERVAL = int(os.environ.get("CHECK_INTERVAL_MINUTES", "360"))
AUTH_USERNAME = os.environ.get("AUTH_USERNAME", "admin")
AUTH_PASSWORD = os.environ.get("AUTH_PASSWORD", "")
SEARXNG_URL = os.environ.get("SEARXNG_URL", "")
STATIC = Path(__file__).parent / "static"
# The release this build is, from the VERSION file at the repo root. Bumping
# that file on main publishes a new release.
VERSION_FILE = Path(__file__).parent.parent / "VERSION"
VERSION = VERSION_FILE.read_text().strip() if VERSION_FILE.exists() else "dev"
UPDATE_CHECK = os.environ.get("UPDATE_CHECK", "true").lower() in ("1", "true", "yes")
UPDATE_REPO = os.environ.get("UPDATE_REPO", "tet0r/price-tracker")
# 12 requests an hour, well inside GitHub's limit of 60 for anonymous callers
UPDATE_INTERVAL = 5 * 60
# Product photos are copied here so they still load when a shop blocks hotlinking
IMAGE_DIR = Path(db.DATA_DIR) / "images"

# Query parameters that don't change which product a URL points at
TRACKING_PARAMS = {
    "gclid", "fbclid", "msclkid", "dclid", "igshid", "srsltid", "mc_cid", "mc_eid",
    "ref", "ref_", "tag", "linkcode", "linkid", "_encoding", "spm", "scm", "cmpid",
}
TRACKING_PREFIXES = ("utm_", "pd_rd_", "pf_rd_", "hsa_", "mkt_")
DISCORD_WEBHOOK_RE = re.compile(r"https://(ptb\.|canary\.)?discord(app)?\.com/api/webhooks/\d+/[\w-]+")
DISCORD_COLORS = {"drop": 0x16A34A, "target": 0x2563EB, "stock": 0xD97706}


# ---------- helpers ----------

def one(sql: str, params: tuple, what: str) -> dict:
    rows = db.query(sql, params)
    if not rows:
        raise HTTPException(404, f"{what} not found")
    return rows[0]


def get_product(product_id: int) -> dict:
    return one("SELECT * FROM products WHERE id = ?", (product_id,), "Product")


def load_store(row: dict) -> dict:
    row["domains"] = json.loads(row["domains"])
    row["strategies"] = json.loads(row["strategies"])
    row["missing_price_oos"] = bool(row["missing_price_oos"])
    row["ai_extract"] = bool(row.get("ai_extract"))
    return row


# Secrets are never sent to the browser in full. The UI gets a masked form and
# sends it back untouched when the user hasn't edited it.
MASK = "••••"


def mask_url(value: str) -> str:
    """scheme://••••tail — enough to recognise an entry without revealing it."""
    scheme, sep, rest = value.partition("://")
    return f"{scheme}{sep}{MASK}{rest[-3:]}" if sep else MASK


def mask_lines(value: str) -> str:
    return "\n".join(mask_url(line.strip()) for line in value.splitlines() if line.strip())


def mask_cookies(value: str) -> str:
    return "; ".join(f"{name}={MASK}" for name, _ in _cookie_pairs(value))


def unmask_lines(submitted: str, stored: str) -> str:
    """Swap any still-masked line back for the stored secret it stands for."""
    originals = [line.strip() for line in stored.splitlines() if line.strip()]
    out = []
    for line in (l.strip() for l in submitted.splitlines()):
        match = next((o for o in originals if mask_url(o) == line), None)
        if match:
            originals.remove(match)
        if line and (match or MASK not in line):
            out.append(match or line)
    return "\n".join(out)


def public_store(store: dict) -> dict:
    return store | {"cookies": mask_cookies(store["cookies"])}


def get_store(store_id: int) -> dict:
    return load_store(one("SELECT * FROM stores WHERE id = ?", (store_id,), "Store"))


def normalize_url(raw: str) -> str:
    url = raw.strip()
    if not re.match(r"https?://", url, re.I):
        url = "https://" + url
    host = urlparse(url).hostname or ""
    if "." not in host or " " in url:
        raise HTTPException(400, "That doesn't look like a product URL")
    return url


def host_of(url: str) -> str:
    return (urlparse(url).hostname or "").lower().removeprefix("www.")


def url_key(url: str) -> str:
    """Normalised identity of a product page: host, path and meaningful query params."""
    parsed = urlparse(url)
    path = re.sub(r"/ref=[^/]*", "", parsed.path).rstrip("/")
    params = sorted(
        (k, v) for k, v in parse_qsl(parsed.query)
        if k.lower() not in TRACKING_PARAMS and not k.lower().startswith(TRACKING_PREFIXES)
    )
    return host_of(url) + path + ("?" + urlencode(params) if params else "")


def store_for(url: str) -> int:
    """Find the store whose domain matches the URL, creating an auto-detect one if needed."""
    host = host_of(url)
    matches = [
        (len(domain), row["id"])
        for row in db.query("SELECT id, domains FROM stores")
        for domain in json.loads(row["domains"])
        if host == domain or host.endswith("." + domain)
    ]
    if matches:
        return max(matches)[1]
    return db.execute(
        "INSERT INTO stores (name, domains, created_at) VALUES (?, ?, ?)",
        (host, json.dumps([host]), int(time.time())),
    )


def add_url(product_id: int, url: str) -> int:
    try:
        return db.execute(
            "INSERT INTO urls (product_id, store_id, url, url_key) VALUES (?, ?, ?, ?)",
            (product_id, store_for(url), url, url_key(url)),
        )
    except sqlite3.IntegrityError:
        raise HTTPException(409, "You're already tracking that URL")


def image_file(product_id: int) -> Path | None:
    return next(IMAGE_DIR.glob(f"{product_id}.*"), None)


async def cache_image(product: dict, page_url: str) -> None:
    if not product["image"] or image_file(product["id"]):
        return
    downloaded = await fetch_image(product["image"], page_url)
    if downloaded:
        IMAGE_DIR.mkdir(parents=True, exist_ok=True)
        (IMAGE_DIR / f"{product['id']}.{downloaded[1]}").write_bytes(downloaded[0])


def best_price(product_id: int) -> float | None:
    """Lowest price per unit among sources that can actually be bought right now.

    Pack size defaults to 1, so this is the plain price unless unit pricing is in use.
    """
    best = best_source(product_id)
    return best["unit_price"] if best else None


def best_source(product_id: int) -> dict | None:
    rows = db.query(
        "SELECT u.url, u.availability, u.units, u.price / u.units AS unit_price, "
        "s.name AS store, s.currency FROM urls u JOIN stores s ON s.id = u.store_id "
        "WHERE u.product_id = ? AND u.price IS NOT NULL ORDER BY unit_price, u.id",
        (product_id,),
    )
    return next((r for r in rows if r["availability"] not in UNAVAILABLE), None)


def serialize(product: dict) -> dict:
    urls = db.query(
        "SELECT u.*, s.name AS store_name, s.currency FROM urls u "
        "JOIN stores s ON s.id = u.store_id WHERE u.product_id = ? ORDER BY u.id",
        (product["id"],),
    )
    for u in urls:
        history = db.query(
            "SELECT checked_at, price FROM prices WHERE url_id = ? ORDER BY checked_at", (u["id"],)
        )
        u["history"] = [[h["checked_at"], h["price"]] for h in history]
        del u["url_key"]
    product["urls"] = urls
    cached = image_file(product["id"])
    if cached:
        product["image"] = f"/api/products/{product['id']}/image?v={int(cached.stat().st_mtime)}"
    product["tags"] = json.loads(product["tags"])
    for key in ("notify_on_drop", "notify_back_in_stock", "paused"):
        product[key] = bool(product[key])
    return product


# ---------- notifications ----------

async def send_external(title: str, body: str) -> bool:
    urls = [u.strip() for u in db.get_setting("notify_urls").splitlines() if u.strip()]
    if not urls:
        return False
    notifier = apprise.Apprise()
    for url in urls:
        notifier.add(url)
    return bool(await asyncio.to_thread(notifier.notify, title=title, body=body))


async def send_discord(title: str, body: str, link: str = "", image: str = "", kind: str = "drop") -> bool:
    """Post an alert to the Discord channel webhook as an embed. False if none is set."""
    webhook = db.get_setting("discord_webhook")
    if not webhook:
        return False
    embed = {"title": title[:256], "description": body[:2000], "color": DISCORD_COLORS[kind]}
    if link:
        embed["url"] = link
    if image:
        embed["thumbnail"] = {"url": image}
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(webhook, json={"username": "Price Tracker", "embeds": [embed]})
    if resp.status_code >= 400:
        log.warning("Discord webhook returned HTTP %s: %s", resp.status_code, resp.text[:200])
        return False
    return True


async def notify(product: dict | None, title: str, body: str, link: str = "", kind: str = "drop") -> None:
    """Record an in-app alert and send it to Discord and Apprise. `product` is None for app-level alerts."""
    db.execute(
        "INSERT INTO notifications (product_id, title, body, created_at) VALUES (?, ?, ?, ?)",
        (product and product["id"], title, f"{body}\n{link}" if link else body, int(time.time())),
    )
    image = (product and product["image"]) or ""
    for send in (
        lambda: send_discord(title, body, link, image, kind),
        lambda: send_external(title, f"{body}\n{link}" if link else body),
    ):
        try:
            await send()
        except Exception:
            log.exception("notification failed")


async def notify_changes(product: dict, old: float | None, new: float | None, was_sold_out: bool) -> None:
    if new is None:
        return
    name = (product["name"] or "Product")[:80]
    source = best_source(product["id"]) or {"url": "", "store": "", "currency": None, "units": 1}
    per_unit = f" per {product['unit_label'] or 'unit'}" if source["units"] != 1 else ""

    def money(value: float) -> str:
        amount = f"{value:,.2f} {source['currency']}" if source["currency"] else f"{value:,.2f}"
        return amount + per_unit

    where, link = f" at {source['store']}", source["url"]
    target = product["target_price"]
    if was_sold_out and product["notify_back_in_stock"]:
        await notify(product, f"Back in stock: {name}", f"{money(new)}{where}", link, "stock")
    if target is not None and new <= target and (old is None or old > target):
        await notify(product, f"Target price reached: {name}",
                     f"{money(new)} (target {money(target)}){where}", link, "target")
    elif product["notify_on_drop"] and old is not None and new < old:
        percent = (old - new) / old * 100
        if percent >= product["drop_percent"]:
            await notify(product, f"Price drop: {name}",
                         f"{money(old)} → {money(new)} (-{percent:.1f}%){where}", link, "drop")


# ---------- checking ----------

async def check_url(u: dict) -> None:
    store, now = get_store(u["store_id"]), int(time.time())
    try:
        found, method = await scrape(u["url"], store)
    except ScrapeError as e:
        log.warning("check failed for %s: %s", u["url"], e)
        db.execute("UPDATE urls SET last_checked = ?, last_error = ? WHERE id = ?", (now, str(e), u["id"]))
        found = e.partial
    else:
        if found.price and (u["price"] is None or abs(found.price - u["price"]) >= 0.005):
            db.execute(
                "INSERT INTO prices (url_id, price, checked_at) VALUES (?, ?, ?)",
                (u["id"], found.price, now),
            )
        db.execute(
            "UPDATE urls SET price = COALESCE(?, price), availability = ?, last_checked = ?, "
            "last_error = NULL WHERE id = ?",
            (found.price, found.availability, now, u["id"]),
        )
        db.execute(
            "UPDATE stores SET last_method = ?, currency = COALESCE(currency, ?) WHERE id = ?",
            (method, found.currency, store["id"]),
        )
        log.info("%s -> %s %s (%s via %s)", u["url"], found.price, found.availability, found.source, method)
    db.execute(
        "UPDATE products SET name = COALESCE(name, ?), image = COALESCE(image, ?) WHERE id = ?",
        (found.name, found.image, u["product_id"]),
    )
    await cache_image(get_product(u["product_id"]), u["url"])


async def check_product(product: dict) -> None:
    urls = db.query("SELECT * FROM urls WHERE product_id = ?", (product["id"],))
    old = best_price(product["id"])
    was_sold_out = old is None and any(u["price"] is not None for u in urls)
    for i, u in enumerate(urls):
        if i:
            await asyncio.sleep(random.uniform(1, 3))
        await check_url(u)
    db.execute("UPDATE products SET last_checked = ? WHERE id = ?", (int(time.time()), product["id"]))
    await notify_changes(get_product(product["id"]), old, best_price(product["id"]), was_sold_out)


async def scheduler() -> None:
    while True:
        try:
            due = db.query(
                "SELECT * FROM products WHERE paused = 0 "
                "AND COALESCE(last_checked, 0) + interval_minutes * 60 <= ? ORDER BY last_checked",
                (int(time.time()),),
            )
            for product in due:
                await check_product(product)
                await asyncio.sleep(random.uniform(2, 8))
        except Exception:
            log.exception("scheduler error")
        await asyncio.sleep(60)


# ---------- updates ----------

def version_tuple(version: str) -> tuple[int, ...]:
    return tuple(int(n) for n in re.findall(r"\d+", version)[:3])


def available_update() -> dict | None:
    """The newer release found by the last check, if there is one."""
    latest = db.get_setting("latest_version")
    if not UPDATE_CHECK or VERSION == "dev" or not latest or version_tuple(latest) <= version_tuple(VERSION):
        return None
    return {"version": latest, "url": db.get_setting("latest_url")}


async def check_for_update() -> None:
    """Ask GitHub for the latest release and alert once per new version."""
    async with httpx.AsyncClient(timeout=15, headers={"Accept": "application/vnd.github+json"}) as client:
        resp = await client.get(f"https://api.github.com/repos/{UPDATE_REPO}/releases/latest")
    if resp.status_code != 200:
        return
    release = resp.json()
    latest, url = str(release.get("tag_name", "")).lstrip("v"), str(release.get("html_url", ""))
    if not version_tuple(latest):
        return
    db.set_setting("latest_version", latest)
    db.set_setting("latest_url", url if url.startswith("https://github.com/") else "")
    update = available_update()
    if update and db.get_setting("update_notified") != latest:
        db.set_setting("update_notified", latest)
        await notify(
            None, f"Price Tracker {latest} is available",
            f"You are running {VERSION}. Update with: docker compose pull && docker compose up -d",
            update["url"], "target",
        )


async def update_checker() -> None:
    await asyncio.sleep(30)
    while True:
        try:
            await check_for_update()
        except Exception:
            log.exception("update check failed")
        await asyncio.sleep(UPDATE_INTERVAL)


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init()
    log.info("Price Tracker %s", VERSION)
    tasks = [asyncio.create_task(scheduler())]
    if UPDATE_CHECK and VERSION != "dev":
        tasks.append(asyncio.create_task(update_checker()))
    yield
    for task in tasks:
        task.cancel()


app = FastAPI(title="Price Tracker", version=VERSION, lifespan=lifespan)


@app.middleware("http")
async def basic_auth(request: Request, call_next):
    if AUTH_PASSWORD:
        ok = False
        scheme, _, token = request.headers.get("authorization", "").partition(" ")
        if scheme.lower() == "basic":
            try:
                user, _, password = base64.b64decode(token).decode().partition(":")
                ok = secrets.compare_digest(user.encode(), AUTH_USERNAME.encode()) and \
                    secrets.compare_digest(password.encode(), AUTH_PASSWORD.encode())
            except ValueError:
                pass
        if not ok:
            return Response(status_code=401, headers={"WWW-Authenticate": 'Basic realm="Price Tracker"'})
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


# ---------- products ----------

class UrlIn(BaseModel):
    url: str


class ProductPatch(BaseModel):
    name: str | None = None
    unit_label: str | None = None
    tags: list[str] | None = None
    target_price: float | None = Field(None, gt=0)
    notify_on_drop: bool | None = None
    drop_percent: float | None = Field(None, ge=0, le=100)
    notify_back_in_stock: bool | None = None
    interval_minutes: int | None = Field(None, ge=5, le=1440)
    paused: bool | None = None


class OrderIn(BaseModel):
    ids: list[int]


class UrlPatch(BaseModel):
    units: float = Field(gt=0)


@app.get("/api/products")
def list_products():
    return [serialize(p) for p in db.query("SELECT * FROM products ORDER BY position, id DESC")]


@app.post("/api/products/order", status_code=204)
def order_products(body: OrderIn):
    for position, product_id in enumerate(body.ids):
        db.execute("UPDATE products SET position = ? WHERE id = ?", (position, product_id))


@app.post("/api/products", status_code=201)
async def add_product(body: UrlIn):
    url = normalize_url(body.url)
    if db.query("SELECT 1 FROM urls WHERE url_key = ?", (url_key(url),)):
        raise HTTPException(409, "You're already tracking that URL")
    product_id = db.execute(
        "INSERT INTO products (interval_minutes, created_at, position) "
        "VALUES (?, ?, (SELECT COALESCE(MIN(position), 0) - 1 FROM products))",
        (DEFAULT_INTERVAL, int(time.time())),
    )
    add_url(product_id, url)
    await check_product(get_product(product_id))
    return serialize(get_product(product_id))


@app.patch("/api/products/{product_id}")
def update_product(product_id: int, body: ProductPatch):
    get_product(product_id)
    changes = body.model_dump(exclude_unset=True)
    # Only these may be cleared; a null for anything else means "leave as is"
    clearable = ("name", "unit_label", "target_price")
    changes = {k: v for k, v in changes.items() if v is not None or k in clearable}
    for key in ("name", "unit_label"):
        if key in changes:
            changes[key] = (changes[key] or "").strip() or None
    if "tags" in changes:
        changes["tags"] = json.dumps(sorted({t.strip() for t in changes["tags"] if t.strip()}))
    if changes:
        db.execute(
            f"UPDATE products SET {', '.join(f'{k} = ?' for k in changes)} WHERE id = ?",
            (*changes.values(), product_id),
        )
    return serialize(get_product(product_id))


@app.delete("/api/products/{product_id}", status_code=204)
def delete_product(product_id: int):
    get_product(product_id)
    db.execute("DELETE FROM products WHERE id = ?", (product_id,))
    cached = image_file(product_id)
    if cached:
        cached.unlink(missing_ok=True)


@app.get("/api/products/{product_id}/image")
def product_image(product_id: int):
    cached = image_file(product_id)
    if not cached:
        raise HTTPException(404, "No image")
    return FileResponse(cached, headers={"Cache-Control": "private, max-age=604800"})


@app.post("/api/products/{product_id}/check")
async def check_now(product_id: int):
    await check_product(get_product(product_id))
    return serialize(get_product(product_id))


@app.post("/api/products/{product_id}/urls", status_code=201)
async def add_product_url(product_id: int, body: UrlIn):
    get_product(product_id)
    url_id = add_url(product_id, normalize_url(body.url))
    await check_url(one("SELECT * FROM urls WHERE id = ?", (url_id,), "URL"))
    return serialize(get_product(product_id))


@app.patch("/api/urls/{url_id}")
def update_url(url_id: int, body: UrlPatch):
    u = one("SELECT * FROM urls WHERE id = ?", (url_id,), "URL")
    db.execute("UPDATE urls SET units = ? WHERE id = ?", (body.units, url_id))
    return serialize(get_product(u["product_id"]))


@app.delete("/api/urls/{url_id}")
def delete_url(url_id: int):
    u = one("SELECT * FROM urls WHERE id = ?", (url_id,), "URL")
    db.execute("DELETE FROM urls WHERE id = ?", (url_id,))
    return serialize(get_product(u["product_id"]))


# ---------- stores ----------

class Strategy(BaseModel):
    type: Literal["auto", "schema", "selector", "regex", "json"] = "auto"
    value: str = ""


class StoreIn(BaseModel):
    name: str = Field(min_length=1)
    domains: list[str] = []
    currency: str | None = None
    service: Literal["auto", "http", "browser"] = "auto"
    cookies: str = ""
    strategies: dict[Literal["title", "price", "image", "availability"], Strategy] = {}
    missing_price_oos: bool = False
    ai_extract: bool = False


class StoreTest(StoreIn):
    url: str
    # The saved store being edited, so its masked cookies can be filled back in
    id: int | None = None


def store_columns(body: StoreIn, store_id: int | None = None) -> dict:
    domains = [d.strip().lower().removeprefix("www.") for d in body.domains if d.strip()]
    cookies = body.cookies.strip()
    if store_id is not None and MASK in cookies:
        saved = get_store(store_id)["cookies"]
        if cookies != mask_cookies(saved):
            raise HTTPException(400, "Re-enter the cookies in full to change them")
        cookies = saved
    elif MASK in cookies:
        raise HTTPException(400, "Enter the cookies in full")
    return {
        "name": body.name.strip(),
        "domains": json.dumps(domains),
        "currency": (body.currency or "").strip().upper() or None,
        "service": body.service,
        "cookies": cookies,
        "strategies": json.dumps({k: v.model_dump() for k, v in body.strategies.items()}),
        "missing_price_oos": int(body.missing_price_oos),
        "ai_extract": int(body.ai_extract),
    }


@app.get("/api/stores")
def list_stores():
    rows = db.query(
        "SELECT s.*, (SELECT COUNT(*) FROM urls WHERE store_id = s.id) AS url_count "
        "FROM stores s ORDER BY s.name COLLATE NOCASE"
    )
    return [public_store(load_store(r)) for r in rows]


@app.post("/api/stores", status_code=201)
def create_store(body: StoreIn):
    cols = store_columns(body) | {"created_at": int(time.time())}
    store_id = db.execute(
        f"INSERT INTO stores ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
        tuple(cols.values()),
    )
    return public_store(get_store(store_id))


@app.put("/api/stores/{store_id}")
def update_store(store_id: int, body: StoreIn):
    get_store(store_id)
    cols = store_columns(body, store_id)
    db.execute(
        f"UPDATE stores SET {', '.join(f'{k} = ?' for k in cols)}, last_method = NULL WHERE id = ?",
        (*cols.values(), store_id),
    )
    return public_store(get_store(store_id))


@app.delete("/api/stores/{store_id}", status_code=204)
def delete_store(store_id: int):
    get_store(store_id)
    if db.query("SELECT 1 FROM urls WHERE store_id = ? LIMIT 1", (store_id,)):
        raise HTTPException(409, "This store still has tracked URLs. Remove those first.")
    db.execute("DELETE FROM stores WHERE id = ?", (store_id,))


@app.post("/api/stores/test")
async def test_store(body: StoreTest):
    """Try a store config (saved or not) against a product URL and show what it extracts."""
    store = load_store(store_columns(body, body.id))
    try:
        found, method = await scrape(normalize_url(body.url), store)
    except ScrapeError as e:
        return {"error": str(e), "title": e.partial.name, "image": e.partial.image}
    return {
        "title": found.name, "price": found.price, "currency": found.currency, "image": found.image,
        "availability": found.availability, "method": method, "source": found.source,
    }


@app.post("/api/stores/suggest")
async def suggest_store_rules(body: StoreTest):
    """Ask the AI provider for extraction rules for this page. Nothing is saved."""
    try:
        html, _ = await fetch_body(normalize_url(body.url), load_store(store_columns(body, body.id)))
        return await ai.suggest_strategies(html, body.url)
    except (ScrapeError, ai.AIError) as e:
        raise HTTPException(502, str(e))


# ---------- search ----------

@app.get("/api/search")
async def search(q: str):
    """Search for product pages through the configured SearXNG instance."""
    base = (db.get_setting("searxng_url") or SEARXNG_URL).rstrip("/")
    if not base:
        raise HTTPException(400, "Set a SearXNG URL in Settings to search")
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            resp = await client.get(f"{base}/search", params={"q": q, "format": "json"})
    except httpx.HTTPError as e:
        raise HTTPException(502, f"Could not reach SearXNG: {type(e).__name__}")
    if resp.status_code == 403:
        raise HTTPException(502, "SearXNG refused the request. Enable the json format in its settings.yml (search.formats).")
    try:
        resp.raise_for_status()
        results = resp.json().get("results", [])
    except (httpx.HTTPError, ValueError):
        raise HTTPException(502, f"SearXNG returned an unexpected response (HTTP {resp.status_code})")
    tracked = {r["url_key"] for r in db.query("SELECT url_key FROM urls")}
    return [
        {
            "title": r.get("title") or r["url"], "url": r["url"], "host": host_of(r["url"]),
            "snippet": (r.get("content") or "")[:200], "tracked": url_key(r["url"]) in tracked,
        }
        for r in results[:15] if str(r.get("url", "")).startswith(("http://", "https://"))
    ]


# ---------- notifications & settings ----------

class SettingsIn(BaseModel):
    notify_urls: str = ""
    discord_webhook: str = ""
    searxng_url: str = ""
    ai_provider: str = ""
    ai_model: str = ""
    ai_base_url: str = ""
    # Left empty to keep the stored key
    ai_api_key: str = ""


@app.get("/api/notifications")
def list_notifications():
    return db.query("SELECT * FROM notifications ORDER BY id DESC LIMIT 50")


@app.post("/api/notifications/read", status_code=204)
def mark_notifications_read():
    db.execute("UPDATE notifications SET read = 1 WHERE read = 0")


@app.get("/api/settings")
def get_settings():
    """Everything the UI needs. Secrets come back masked, and the AI key not at all."""
    webhook = db.get_setting("discord_webhook")
    return {
        "notify_urls": mask_lines(db.get_setting("notify_urls")),
        "discord_webhook": mask_url(webhook) if webhook else "",
        "searxng_url": db.get_setting("searxng_url") or SEARXNG_URL,
        "ai_provider": db.get_setting("ai_provider"),
        "ai_model": db.get_setting("ai_model"),
        "ai_base_url": db.get_setting("ai_base_url"),
        "ai_key_set": bool(ai.config()["api_key"]),
        "features": {"search": bool(db.get_setting("searxng_url") or SEARXNG_URL), "ai": ai.enabled()},
        "dashboard": json.loads(db.get_setting("dashboard", "{}")),
        "version": VERSION,
        "update": available_update(),
    }


@app.put("/api/settings")
def put_settings(body: SettingsIn):
    if body.ai_provider and body.ai_provider not in ai.PROVIDERS:
        raise HTTPException(400, "Unknown AI provider")
    saved_webhook = db.get_setting("discord_webhook")
    webhook = body.discord_webhook.strip()
    if saved_webhook and webhook == mask_url(saved_webhook):
        webhook = saved_webhook
    elif webhook and not DISCORD_WEBHOOK_RE.fullmatch(webhook):
        raise HTTPException(400, "That isn't a Discord webhook URL (https://discord.com/api/webhooks/…)")
    # A saved key was entered for one destination. If that destination changes,
    # the key has to be entered again rather than being sent somewhere new.
    moved = (body.ai_provider.strip(), body.ai_base_url.strip()) != (
        db.get_setting("ai_provider"), db.get_setting("ai_base_url"))
    if body.ai_api_key.strip():
        db.set_setting("ai_api_key", body.ai_api_key.strip())
    elif moved:
        db.set_setting("ai_api_key", "")
    db.set_setting("discord_webhook", webhook)
    db.set_setting("notify_urls", unmask_lines(body.notify_urls, db.get_setting("notify_urls")))
    for key in ("searxng_url", "ai_provider", "ai_model", "ai_base_url"):
        db.set_setting(key, getattr(body, key).strip())
    return get_settings()


@app.put("/api/dashboard", status_code=204)
def put_dashboard(body: dict):
    """Dashboard layout: group order, collapsed groups and hidden sections."""
    db.set_setting("dashboard", json.dumps(body))


@app.post("/api/settings/test")
async def test_notification():
    """Send a test alert to every configured service and say which ones accepted it."""
    targets = {"Discord": (db.get_setting("discord_webhook"), send_discord),
               "Apprise": (db.get_setting("notify_urls").strip(), send_external)}
    results = {}
    for name, (configured, send) in targets.items():
        if configured:
            try:
                results[name] = await send("Price Tracker", "Test notification — alerts are working.")
            except Exception:
                log.exception("test notification failed")
                results[name] = False
    if not results:
        raise HTTPException(400, "Add a Discord webhook or an Apprise URL first")
    failed = [name for name, ok in results.items() if not ok]
    if failed:
        raise HTTPException(502, f"{' and '.join(failed)} failed — check the URL and the container logs")
    return {"sent": list(results)}


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/favicon.svg")
@app.get("/favicon.ico")
def favicon():
    return FileResponse(STATIC / "favicon.svg", media_type="image/svg+xml")
