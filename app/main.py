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
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from . import db
from .extract import UNAVAILABLE
from .fetch import ScrapeError, scrape

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("pricetracker")

DEFAULT_INTERVAL = int(os.environ.get("CHECK_INTERVAL_MINUTES", "360"))
AUTH_USERNAME = os.environ.get("AUTH_USERNAME", "admin")
AUTH_PASSWORD = os.environ.get("AUTH_PASSWORD", "")
STATIC = Path(__file__).parent / "static"

# Query parameters that don't change which product a URL points at
TRACKING_PARAMS = {
    "gclid", "fbclid", "msclkid", "dclid", "igshid", "srsltid", "mc_cid", "mc_eid",
    "ref", "ref_", "tag", "linkcode", "linkid", "_encoding", "spm", "scm", "cmpid",
}
TRACKING_PREFIXES = ("utm_", "pd_rd_", "pf_rd_", "hsa_", "mkt_")


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
    return row


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


def best_price(product_id: int) -> float | None:
    """Lowest price among sources that can actually be bought right now."""
    rows = db.query(
        "SELECT price, availability FROM urls WHERE product_id = ? AND price IS NOT NULL",
        (product_id,),
    )
    buyable = [r["price"] for r in rows if r["availability"] not in UNAVAILABLE]
    return min(buyable) if buyable else None


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


async def notify(product: dict, title: str, body: str) -> None:
    db.execute(
        "INSERT INTO notifications (product_id, title, body, created_at) VALUES (?, ?, ?, ?)",
        (product["id"], title, body, int(time.time())),
    )
    try:
        await send_external(title, body)
    except Exception:
        log.exception("notification failed")


async def notify_changes(product: dict, old: float | None, new: float | None, was_sold_out: bool) -> None:
    if new is None:
        return
    name = (product["name"] or "Product")[:80]
    cheapest = db.query(
        "SELECT u.url, s.name AS store, s.currency FROM urls u JOIN stores s ON s.id = u.store_id "
        "WHERE u.product_id = ? AND u.price = ? ORDER BY u.id LIMIT 1",
        (product["id"], new),
    )
    source = cheapest[0] if cheapest else {"url": "", "store": "", "currency": None}

    def money(value: float) -> str:
        return f"{value:,.2f} {source['currency']}" if source["currency"] else f"{value:,.2f}"

    where = f" at {source['store']}\n{source['url']}"
    target = product["target_price"]
    if was_sold_out and product["notify_back_in_stock"]:
        await notify(product, f"Back in stock: {name}", f"{money(new)}{where}")
    if target is not None and new <= target and (old is None or old > target):
        await notify(product, f"Target price reached: {name}", f"{money(new)} (target {money(target)}){where}")
    elif product["notify_on_drop"] and old is not None and new < old:
        percent = (old - new) / old * 100
        if percent >= product["drop_percent"]:
            await notify(product, f"Price drop: {name}", f"{money(old)} → {money(new)} (-{percent:.1f}%){where}")


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


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init()
    task = asyncio.create_task(scheduler())
    yield
    task.cancel()


app = FastAPI(title="Price Tracker", lifespan=lifespan)


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
    return await call_next(request)


# ---------- products ----------

class UrlIn(BaseModel):
    url: str


class ProductPatch(BaseModel):
    name: str | None = None
    tags: list[str] | None = None
    target_price: float | None = Field(None, gt=0)
    notify_on_drop: bool | None = None
    drop_percent: float | None = Field(None, ge=0, le=100)
    notify_back_in_stock: bool | None = None
    interval_minutes: int | None = Field(None, ge=5, le=1440)
    paused: bool | None = None


@app.get("/api/products")
def list_products():
    return [serialize(p) for p in db.query("SELECT * FROM products ORDER BY created_at DESC, id DESC")]


@app.post("/api/products", status_code=201)
async def add_product(body: UrlIn):
    url = normalize_url(body.url)
    if db.query("SELECT 1 FROM urls WHERE url_key = ?", (url_key(url),)):
        raise HTTPException(409, "You're already tracking that URL")
    product_id = db.execute(
        "INSERT INTO products (interval_minutes, created_at) VALUES (?, ?)",
        (DEFAULT_INTERVAL, int(time.time())),
    )
    add_url(product_id, url)
    await check_product(get_product(product_id))
    return serialize(get_product(product_id))


@app.patch("/api/products/{product_id}")
def update_product(product_id: int, body: ProductPatch):
    get_product(product_id)
    changes = body.model_dump(exclude_unset=True)
    # Only these two may be cleared; a null for anything else means "leave as is"
    changes = {k: v for k, v in changes.items() if v is not None or k in ("name", "target_price")}
    if "name" in changes:
        changes["name"] = (changes["name"] or "").strip() or None
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


class StoreTest(StoreIn):
    url: str


def store_columns(body: StoreIn) -> dict:
    domains = [d.strip().lower().removeprefix("www.") for d in body.domains if d.strip()]
    return {
        "name": body.name.strip(),
        "domains": json.dumps(domains),
        "currency": (body.currency or "").strip().upper() or None,
        "service": body.service,
        "cookies": body.cookies.strip(),
        "strategies": json.dumps({k: v.model_dump() for k, v in body.strategies.items()}),
        "missing_price_oos": int(body.missing_price_oos),
    }


@app.get("/api/stores")
def list_stores():
    rows = db.query(
        "SELECT s.*, (SELECT COUNT(*) FROM urls WHERE store_id = s.id) AS url_count "
        "FROM stores s ORDER BY s.name COLLATE NOCASE"
    )
    return [load_store(r) for r in rows]


@app.post("/api/stores", status_code=201)
def create_store(body: StoreIn):
    cols = store_columns(body) | {"created_at": int(time.time())}
    store_id = db.execute(
        f"INSERT INTO stores ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
        tuple(cols.values()),
    )
    return get_store(store_id)


@app.put("/api/stores/{store_id}")
def update_store(store_id: int, body: StoreIn):
    get_store(store_id)
    cols = store_columns(body)
    db.execute(
        f"UPDATE stores SET {', '.join(f'{k} = ?' for k in cols)}, last_method = NULL WHERE id = ?",
        (*cols.values(), store_id),
    )
    return get_store(store_id)


@app.delete("/api/stores/{store_id}", status_code=204)
def delete_store(store_id: int):
    get_store(store_id)
    if db.query("SELECT 1 FROM urls WHERE store_id = ? LIMIT 1", (store_id,)):
        raise HTTPException(409, "This store still has tracked URLs. Remove those first.")
    db.execute("DELETE FROM stores WHERE id = ?", (store_id,))


@app.post("/api/stores/test")
async def test_store(body: StoreTest):
    """Try a store config (saved or not) against a product URL and show what it extracts."""
    store = load_store(store_columns(body))
    try:
        found, method = await scrape(normalize_url(body.url), store)
    except ScrapeError as e:
        return {"error": str(e), "title": e.partial.name, "image": e.partial.image}
    return {
        "title": found.name, "price": found.price, "currency": found.currency, "image": found.image,
        "availability": found.availability, "method": method, "source": found.source,
    }


# ---------- notifications & settings ----------

class SettingsIn(BaseModel):
    notify_urls: str = ""


@app.get("/api/notifications")
def list_notifications():
    return db.query("SELECT * FROM notifications ORDER BY id DESC LIMIT 50")


@app.post("/api/notifications/read", status_code=204)
def mark_notifications_read():
    db.execute("UPDATE notifications SET read = 1 WHERE read = 0")


@app.get("/api/settings")
def get_settings():
    return {"notify_urls": db.get_setting("notify_urls")}


@app.put("/api/settings")
def put_settings(body: SettingsIn):
    db.set_setting("notify_urls", body.notify_urls.strip())
    return get_settings()


@app.post("/api/settings/test")
async def test_notification():
    if not db.get_setting("notify_urls").strip():
        raise HTTPException(400, "Save at least one notification URL first")
    if not await send_external("Price Tracker", "Test notification — alerts are working."):
        raise HTTPException(502, "Notification failed — check the URL format and container logs")
    return {"ok": True}


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")
