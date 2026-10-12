# Price Tracker

Self-hosted price tracker. Paste a product URL from any site; it records the price, re-checks on a schedule, charts the history, and alerts you on drops. Feature set modelled on [PriceBuddy](https://pricebuddy.jez.me/features.html), in a single container with SQLite.

## Install

```bash
docker compose up -d --build
```

Open http://localhost:8080. Data lives in `./data/tracker.db` — back up that folder.

## Features

- **Any store** — paste a URL and the price, title, image and availability are auto-detected. A *store* (per-domain scraping config) is created automatically.
- **Compare stores** — add several URLs to one product; the cheapest buyable source is the product's price.
- **Availability** — in stock, pre-order, back order, out of stock, discontinued. Out-of-stock sources don't count as the best price.
- **Price history** — step chart per source, with lowest / highest / time-weighted average.
- **Scheduling** — per-product check frequency from 5 minutes to 24 hours, or pause a product.
- **Alerts** — target price, price drops (optionally only above a % threshold), back in stock. Shown in-app and sent via any [Apprise URL](https://github.com/caronc/apprise/wiki) (ntfy, Discord, Telegram, Gotify, Pushover, Slack, email, …).
- **Tags and filters** — tag products; filter the dashboard by tag, *Good to buy now*, *Recently dropped*, *Needs attention*.
- **JavaScript sites** — falls back to headless Chromium when a plain HTTP request is blocked or finds no price.
- **Duplicate detection** — tracking parameters (`utm_*`, `gclid`, `fbclid`, Amazon `ref`, …) are ignored when matching URLs.

## Stores

If a site shows the wrong price or none, open **Stores → Edit** and set a strategy per field (title, price, image, availability):

| Strategy | Value |
|---|---|
| Auto | schema.org JSON-LD → meta tags → common price elements |
| Schema.org | JSON-LD only |
| CSS selector | `.price`, or `meta[property="og:price:amount"]\|content` to read an attribute |
| Regex | First capture group, run on the raw page, e.g. `"price":\s*"(.*?)"` |
| JSON path | Dot path into a JSON response, e.g. `product.variants.0.price` |

Also per store: currency, fetch method (auto / HTTP / headless browser), cookies to send, and *treat a missing price as out of stock*. Use **Test** to try a config against a URL before saving.

## Configuration (`docker-compose.yml`)

| Variable | Default | |
|---|---|---|
| `CHECK_INTERVAL_MINUTES` | `360` | Default frequency for new products |
| `USE_BROWSER` | `true` | Headless Chromium fallback. `false` saves RAM but breaks JS-heavy sites |
| `AUTH_USERNAME` / `AUTH_PASSWORD` | `admin` / empty | Set a password to require a login (HTTP basic auth) |

## Notes

- With no `AUTH_PASSWORD` there is no login. Keep it on your LAN, or set one and use HTTPS via a reverse proxy.
- Some sites (Amazon in particular) serve CAPTCHAs to automated requests; checks against them fail intermittently.
- Prices are stored without a currency and shown in the store's currency. Mixing currencies in one product gives wrong comparisons.
- Not included from PriceBuddy: multi-user accounts, AI-assisted scraping, unit pricing, drag-to-arrange dashboard, SearXNG search, browser extension, CLI/MCP.
