#!/usr/bin/env python3
"""Command line client and MCP server for Price Tracker. Standard library only.

    python pricetracker.py list
    python pricetracker.py deals
    python pricetracker.py add https://example.com/product
    python pricetracker.py mcp        # stdio MCP server for AI assistants

Point it at your instance with PRICETRACKER_URL (default http://localhost:8080)
and, if you set a login, PRICETRACKER_USER / PRICETRACKER_PASSWORD.
"""
import argparse
import base64
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = os.environ.get("PRICETRACKER_URL", "http://localhost:8080").rstrip("/")
USER = os.environ.get("PRICETRACKER_USER", "admin")
PASSWORD = os.environ.get("PRICETRACKER_PASSWORD", "")
UNAVAILABLE = ("out_of_stock", "discontinued")


class ApiError(Exception):
    pass


def api(path: str, method: str = "GET", body: dict | None = None):
    headers = {"Content-Type": "application/json"} if body is not None else {}
    if PASSWORD:
        headers["Authorization"] = "Basic " + base64.b64encode(f"{USER}:{PASSWORD}".encode()).decode()
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(f"{BASE}/api{path}", data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=180) as response:
            raw = response.read()
    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read()).get("detail")
        except ValueError:
            detail = None
        raise ApiError(detail if isinstance(detail, str) else f"HTTP {e.code} from {BASE}")
    except urllib.error.URLError as e:
        raise ApiError(f"Could not reach {BASE}: {e.reason}")
    return json.loads(raw) if raw else None


# ---------- derived figures (mirrors the web dashboard) ----------

def unit_price(source: dict) -> float:
    return source["price"] / (source.get("units") or 1)


def best_series(product: dict) -> list[list]:
    """Lowest unit price across all sources over time, as [timestamp, price] steps."""
    events = sorted(
        (t, i, price / (u.get("units") or 1))
        for i, u in enumerate(product["urls"]) for t, price in u["history"]
    )
    current, out = {}, []
    for t, i, value in events:
        current[i] = value
        best = min(current.values())
        if out and out[-1][0] == t:
            out[-1][1] = best
        elif not out or out[-1][1] != best:
            out.append([t, best])
    return out


def summarize(product: dict) -> dict:
    priced = [u for u in product["urls"] if u["price"] is not None]
    buyable = [u for u in priced if u["availability"] not in UNAVAILABLE]
    best = min(buyable or priced, key=unit_price, default=None)
    series = best_series(product)
    values = [v for _, v in series]
    price = unit_price(best) if best else None
    change = (series[-1][1] - series[-2][1]) / series[-2][1] * 100 if len(series) > 1 else None
    target = product["target_price"]
    at_low = len(series) > 1 and price is not None and price <= min(values) and price < max(values)
    return {
        "id": product["id"],
        "name": product["name"] or (product["urls"][0]["url"] if product["urls"] else "Untitled"),
        "tags": product["tags"],
        "price": price,
        "currency": best["currency"] if best else None,
        "unit": product.get("unit_label") if any((u.get("units") or 1) != 1 for u in product["urls"]) else None,
        "store": best["store_name"] if best else None,
        "url": best["url"] if best else None,
        "in_stock": bool(buyable),
        "lowest": min(values, default=None),
        "highest": max(values, default=None),
        "target_price": target,
        "last_change_percent": round(change, 1) if change is not None else None,
        "last_change_at": series[-1][0] if len(series) > 1 else None,
        "good_to_buy": bool(buyable) and price is not None and ((target is not None and price <= target) or at_low),
        "sources": len(product["urls"]),
        "failing": any(u["last_error"] for u in product["urls"]),
        "paused": product["paused"],
    }


def find(product_id: int) -> dict:
    for product in api("/products"):
        if product["id"] == product_id:
            return product
    raise ApiError(f"No product with id {product_id}")


def deals() -> list[dict]:
    return [s for s in map(summarize, api("/products")) if s["good_to_buy"]]


def drops(days: float = 7) -> list[dict]:
    since = time.time() - days * 86400
    rows = [s for s in map(summarize, api("/products"))
            if s["last_change_percent"] is not None and s["last_change_percent"] < 0 and s["last_change_at"] >= since]
    return sorted(rows, key=lambda s: s["last_change_percent"])


# ---------- output ----------

def money(value, currency) -> str:
    if value is None:
        return "-"
    return f"{value:,.2f} {currency}" if currency else f"{value:,.2f}"


def print_table(rows: list[dict]) -> None:
    if not rows:
        print("Nothing to show.")
        return
    lines = [("ID", "PRICE", "CHANGE", "STORE", "NAME")]
    for s in rows:
        change = f"{s['last_change_percent']:+.1f}%" if s["last_change_percent"] is not None else ""
        flags = "".join(flag for flag, on in (
            (" [deal]", s["good_to_buy"]), (" [sold out]", not s["in_stock"] and s["price"] is not None),
            (" [failing]", s["failing"]), (" [paused]", s["paused"])) if on)
        price = money(s["price"], s["currency"]) + (f"/{s['unit'] or 'unit'}" if s["unit"] is not None else "")
        lines.append((str(s["id"]), price, change, s["store"] or "-", s["name"][:60] + flags))
    widths = [max(len(line[i]) for line in lines) for i in range(4)]
    for line in lines:
        print("  ".join(cell.ljust(widths[i]) for i, cell in enumerate(line[:4])) + "  " + line[4])


def print_product(product: dict) -> None:
    s = summarize(product)
    print(f"#{s['id']}  {s['name']}")
    print(f"  Price    {money(s['price'], s['currency'])}   low {money(s['lowest'], s['currency'])}"
          f"   high {money(s['highest'], s['currency'])}")
    if s["target_price"] is not None:
        print(f"  Target   {money(s['target_price'], s['currency'])}")
    if s["tags"]:
        print(f"  Tags     {', '.join(s['tags'])}")
    print("  Sources")
    for u in product["urls"]:
        note = u["last_error"] or (u["availability"] or "").replace("_", " ")
        print(f"    {money(u['price'], u['currency']):>14}  {u['store_name']}  {note}")
        print(f"    {'':>14}  {u['url']}")


def emit(args, data, human) -> None:
    if args.json:
        print(json.dumps(data, indent=2))
    else:
        human(data)


# ---------- MCP server (JSON-RPC over stdio, one message per line) ----------

def _tool(name, description, properties=None, required=()):
    return {"name": name, "description": description, "inputSchema": {
        "type": "object", "properties": properties or {}, "required": list(required)}}


_ID = {"product_id": {"type": "integer", "description": "Product id from list_products"}}
_URL = {"url": {"type": "string", "description": "Product page URL"}}
TOOLS = [
    _tool("list_products", "List tracked products with current best price, store, last change and flags.",
          {"tag": {"type": "string", "description": "Only products with this tag"}}),
    _tool("get_product", "Full detail for one product: every source URL, its price, availability and price history.",
          _ID, ["product_id"]),
    _tool("find_deals", "Products that are good to buy now: at or below their target price, or at their lowest tracked price."),
    _tool("recent_drops", "Products whose best price fell recently, biggest drop first.",
          {"days": {"type": "number", "description": "Look-back window in days (default 7)"}}),
    _tool("add_product", "Start tracking a product page URL. Fetches the price immediately.", _URL, ["url"]),
    _tool("add_source", "Add another store's URL to an existing product, to compare prices.", {**_ID, **_URL},
          ["product_id", "url"]),
    _tool("check_product", "Re-check a product's prices right now.", _ID, ["product_id"]),
]


def call_tool(name: str, args: dict):
    if name == "list_products":
        rows = [summarize(p) for p in api("/products")]
        return [r for r in rows if not args.get("tag") or args["tag"] in r["tags"]]
    if name == "get_product":
        return find(int(args["product_id"]))
    if name == "find_deals":
        return deals()
    if name == "recent_drops":
        return drops(float(args.get("days") or 7))
    if name == "add_product":
        return summarize(api("/products", "POST", {"url": args["url"]}))
    if name == "add_source":
        return summarize(api(f"/products/{int(args['product_id'])}/urls", "POST", {"url": args["url"]}))
    if name == "check_product":
        return summarize(api(f"/products/{int(args['product_id'])}/check", "POST"))
    raise ApiError(f"Unknown tool {name}")


def handle(message: dict) -> dict | None:
    method, params = message.get("method"), message.get("params") or {}
    if "id" not in message:
        return None  # notification
    if method == "initialize":
        result = {
            "protocolVersion": params.get("protocolVersion", "2025-06-18"),
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "price-tracker", "version": "1.0.0"},
        }
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": TOOLS}
    elif method == "tools/call":
        try:
            data = call_tool(params.get("name"), params.get("arguments") or {})
            result = {"content": [{"type": "text", "text": json.dumps(data)}]}
        except (ApiError, KeyError, ValueError, TypeError) as e:
            result = {"content": [{"type": "text", "text": f"Error: {e}"}], "isError": True}
    else:
        return {"jsonrpc": "2.0", "id": message["id"], "error": {"code": -32601, "message": f"Unknown method {method}"}}
    return {"jsonrpc": "2.0", "id": message["id"], "result": result}


def serve_mcp() -> None:
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            reply = handle(json.loads(line))
        except ValueError:
            reply = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}
        if reply is not None:
            sys.stdout.write(json.dumps(reply) + "\n")
            sys.stdout.flush()


# ---------- commands ----------

def main() -> None:
    for stream in (sys.stdin, sys.stdout):
        stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Price Tracker command line client")
    parser.add_argument("--json", action="store_true", help="print raw JSON")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list", help="list tracked products").add_argument("--tag")
    sub.add_parser("show", help="show one product and its sources").add_argument("id", type=int)
    sub.add_parser("history", help="price history for a product").add_argument("id", type=int)
    sub.add_parser("deals", help="products that are good to buy now")
    sub.add_parser("drops", help="recent price drops").add_argument("--days", type=float, default=7)
    sub.add_parser("add", help="track a product URL").add_argument("url")
    source = sub.add_parser("add-source", help="add another store URL to a product")
    source.add_argument("id", type=int)
    source.add_argument("url")
    check = sub.add_parser("check", help="re-check prices now")
    check.add_argument("id", type=int, nargs="?")
    check.add_argument("--all", action="store_true")
    sub.add_parser("stores", help="list stores")
    sub.add_parser("search", help="search for product pages (needs SearXNG)").add_argument("query")
    sub.add_parser("mcp", help="run as an MCP server over stdio")
    args = parser.parse_args()

    if args.command == "mcp":
        return serve_mcp()
    if args.command == "list":
        rows = [s for s in map(summarize, api("/products")) if not args.tag or args.tag in s["tags"]]
        emit(args, rows, print_table)
    elif args.command == "show":
        emit(args, find(args.id), print_product)
    elif args.command == "history":
        product = find(args.id)
        rows = [{"at": time.strftime("%Y-%m-%d %H:%M", time.localtime(t)), "store": u["store_name"], "price": price}
                for u in product["urls"] for t, price in u["history"]]
        rows.sort(key=lambda r: r["at"])
        emit(args, rows, lambda rs: [print(f"{r['at']}  {r['price']:>12,.2f}  {r['store']}") for r in rs])
    elif args.command == "deals":
        emit(args, deals(), print_table)
    elif args.command == "drops":
        emit(args, drops(args.days), print_table)
    elif args.command == "add":
        emit(args, api("/products", "POST", {"url": args.url}), print_product)
    elif args.command == "add-source":
        emit(args, api(f"/products/{args.id}/urls", "POST", {"url": args.url}), print_product)
    elif args.command == "check":
        if not args.all and args.id is None:
            parser.error("give a product id or --all")
        ids = [p["id"] for p in api("/products")] if args.all else [args.id]
        emit(args, [summarize(api(f"/products/{i}/check", "POST")) for i in ids], print_table)
    elif args.command == "stores":
        emit(args, api("/stores"), lambda rows: [
            print(f"{s['name']:<28} {', '.join(s['domains']):<32} {s['currency'] or '-':<4} {s['url_count']} urls") for s in rows])
    elif args.command == "search":
        results = api("/search?q=" + urllib.parse.quote(args.query))
        emit(args, results, lambda rows: [print(f"{r['title']}\n  {r['url']}") for r in rows])


if __name__ == "__main__":
    try:
        main()
    except ApiError as e:
        sys.exit(f"Error: {e}")
