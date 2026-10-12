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
- **Unit pricing** — give each source a pack size and the product a unit name to compare by price per tablet, bag, litre, etc.
- **Availability** — in stock, pre-order, back order, out of stock, discontinued. Out-of-stock sources don't count as the best price.
- **Price history** — step chart per source, with lowest / highest / time-weighted average.
- **Scheduling** — per-product check frequency from 5 minutes to 24 hours, or pause a product.
- **Alerts** — target price, price drops (optionally only above a % threshold), back in stock. Shown in-app and sent via any [Apprise URL](https://github.com/caronc/apprise/wiki) (ntfy, Discord, Telegram, Gotify, Pushover, Slack, email, …).
- **Customisable dashboard** — products grouped by tag. Drag groups and products to reorder, drag a product to another group to re-tag it, collapse groups. **Customize** shows or hides the summary stats and the *Good to buy now*, *Recently dropped* and *Needs attention* sections.
- **Product search** — with a SearXNG instance, type a product name instead of a URL, or find a product at other stores.
- **AI-assisted scraping** (optional) — recover a price the rules miss, or have AI propose a store's rules.
- **JavaScript sites** — falls back to headless Chromium when a plain HTTP request is blocked or finds no price.
- **Duplicate detection** — tracking parameters (`utm_*`, `gclid`, `fbclid`, Amazon `ref`, …) are ignored when matching URLs.
- **Browser extension** and **CLI / MCP server** — see below.

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

## Product search (SearXNG)

Set your SearXNG address under **Settings → Product search** (or `SEARXNG_URL`). The instance must allow JSON output — in its `settings.yml`:

```yaml
search:
  formats: [html, json]
```

## AI-assisted scraping

Off by default. Under **Settings → AI-assisted scraping** choose Anthropic, OpenAI, Gemini or a local Ollama, and give a model and API key (the key can also come from `AI_API_KEY`). Then, per store:

- **Let AI read the price when these rules find none** — only fills a gap: it never overrides a scraped price, is skipped for out-of-stock pages, and only a confident answer is accepted.
- **Suggest rules with AI** — proposes CSS selectors from a product URL, shows what they extract, and saves nothing until you click Save.

The page's HTML (scripts and styling removed) is sent to the provider you choose. Pages over 150,000 characters after cleaning are cut to that length.

## Browser extension (Chrome / Edge)

1. Open `chrome://extensions`, turn on **Developer mode**, click **Load unpacked** and pick the `extension/` folder.
2. Click the extension, enter your Price Tracker address (and login, if you set one).

On any product page it shows whether you track it, the best price and each store's price, and tracks it in one click. The login is stored in the browser's extension storage unencrypted.

## CLI and MCP

`cli/pricetracker.py` needs only Python 3.10+, no packages.

```bash
export PRICETRACKER_URL=http://localhost:8080   # plus PRICETRACKER_USER / PRICETRACKER_PASSWORD if you set a login
python cli/pricetracker.py list
python cli/pricetracker.py deals
python cli/pricetracker.py drops --days 14
python cli/pricetracker.py add https://example.com/product
python cli/pricetracker.py show 3
```

Other commands: `history`, `add-source`, `check`, `stores`, `search`. Add `--json` before the command for raw output.

`python cli/pricetracker.py mcp` runs an MCP server over stdio so an AI assistant can read and add to your tracker. Example client config:

```json
{
  "mcpServers": {
    "price-tracker": {
      "command": "python",
      "args": ["/path/to/cli/pricetracker.py", "mcp"],
      "env": { "PRICETRACKER_URL": "http://localhost:8080" }
    }
  }
}
```

## Configuration (`docker-compose.yml`)

| Variable | Default | |
|---|---|---|
| `CHECK_INTERVAL_MINUTES` | `360` | Default frequency for new products |
| `USE_BROWSER` | `true` | Headless Chromium fallback. `false` saves RAM but breaks JS-heavy sites |
| `AUTH_USERNAME` / `AUTH_PASSWORD` | `admin` / empty | Set a password to require a login (HTTP basic auth) |
| `SEARXNG_URL` | empty | SearXNG address, if not set in Settings |
| `AI_API_KEY` | empty | AI provider key, if not set in Settings |

## Notes

- With no `AUTH_PASSWORD` there is no login. Keep it on your LAN, or set one and use HTTPS via a reverse proxy.
- Some sites (Amazon in particular) serve CAPTCHAs to automated requests; checks against them fail intermittently.
- Prices are stored without a currency and shown in the store's currency. Mixing currencies in one product gives wrong comparisons.
- With unit pricing in use, a product's price, target and alerts are all per unit.
- Dragging on the dashboard needs a mouse; it doesn't work on touch screens.
- Single user: there are no separate accounts.

## License

[MIT](LICENSE)
