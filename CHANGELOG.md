# Changelog

All notable changes to Price Tracker are listed here, newest first. Version numbers follow [semantic versioning](https://semver.org/). The section for a version becomes its GitHub release notes.

## [Unreleased]

### Added

- An alert when a product needs attention: once one of its stores has failed 2 checks in a row (set with `ATTENTION_ALERT_AFTER`, `0` to turn off). It is sent once per run of failures, in-app, to Discord and to Apprise.
- When a store refuses requests (HTTP 403, 429 or 503, or a bot-check page), checks on that store pause: for as long as the store asks, or 30 minutes doubling with each refusal up to a day. A live countdown shows when it will try again, on the product card, the product view, the "Need attention" list and the Stores page.

### Changed

- A refusal now says so ("costco.com is refusing automated requests (HTTP 429, rate limited)") instead of "no price found", and the tracker no longer retries with the headless browser straight after one.

## [1.1.1] - 2026-10-12

### Changed

- The app now checks for a new release every 5 minutes instead of twice a day, and the update banner appears without reloading the page.

## [1.1.0] - 2026-10-12

### Added

- A "View" dropdown on the dashboard to show 10 to 100 products per page, with numbered pages when there are more.
- The "Good to buy", "Dropped this week" and "Need attention" tiles and section titles open a pop-in listing the products involved and why.

## [1.0.0] - 2026-10-12

First versioned release.

### Tracking

- Track a product by pasting its URL from any site; price, title, picture and availability are detected automatically.
- Add several store URLs to one product and compare them; the cheapest source that can be bought is the product's price.
- Unit pricing: give each source a pack size to compare by price per tablet, bag, litre and so on.
- Availability tracking: in stock, pre-order, back order, out of stock, discontinued.
- Price history chart per source with lowest, highest and average price.
- Check frequency per product from 5 minutes to 24 hours, with pause.
- Product pictures are taken from the store page and kept locally so they still show when a shop blocks hotlinking.
- Tracking parameters in URLs are ignored when detecting duplicates.

### Stores

- A store (per-domain scraping setup) is created automatically for each new site.
- Per-field extraction strategies: automatic, schema.org, CSS selector, regex or JSON path.
- Fetch pages by plain HTTP or a headless browser for JavaScript-heavy sites, with optional cookies.
- Test a store's setup against a URL before saving it.
- Optional AI help (Anthropic, OpenAI, Gemini or Ollama): read a price the rules miss, or suggest a store's rules.

### Alerts

- Alerts for a target price reached, price drops (with an optional minimum percentage) and back in stock.
- Delivered in-app, to Discord through a webhook, and to any Apprise service (ntfy, Telegram, Gotify, Pushover, Slack, email and more).

### Dashboard

- Products grouped by tag; drag to reorder groups and products, drag between groups to re-tag, collapse groups.
- Summary stats and "good to buy now", "recently dropped" and "needs attention" sections, each of which can be hidden.
- Product search through a SearXNG instance.
- Light and dark mode with a toggle.

### Running it

- Published as a Docker image for amd64 and arm64 at `ghcr.io/tet0r/price-tracker`.
- Optional login through `AUTH_PASSWORD`.
- The running version is shown in the app, which checks GitHub for newer releases and alerts when one is available.

### Security

- The app runs as an unprivileged user inside the container, with an up-to-date browser.
- Private network addresses are refused unless `ALLOW_PRIVATE_URLS` is set; picture downloads are always limited to public addresses.
- Saved secrets (webhooks, notification URLs, store cookies, AI key) are never sent back to the browser in full.
- The saved AI key is cleared if the provider or its address changes.

[Unreleased]: https://github.com/tet0r/price-tracker/compare/v1.1.1...HEAD
[1.1.1]: https://github.com/tet0r/price-tracker/compare/v1.1.0...v1.1.1
[1.1.0]: https://github.com/tet0r/price-tracker/compare/v1.0.0...v1.1.0
[1.0.0]: https://github.com/tet0r/price-tracker/releases/tag/v1.0.0
