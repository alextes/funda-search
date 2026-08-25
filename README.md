# funda-search

Personal tool for house hunting on [funda.nl](https://www.funda.nl). Funda is good but cumbersome: lots of clicking, and you end up scanning every description for the same things. This repo automates the boring parts: fetch new listings, compute the numbers that matter, and show everything in one overview.

## How it gets the data

This was the big unknown, so it was the first proof of concept. Findings:

- **Plain HTTP to www.funda.nl is blocked** by DataDome bot protection (you get a "Je bent bijna op de pagina die je zoekt" challenge page).
- **[pyfunda](https://github.com/0xMH/pyfunda) still provides listing details**, but its anonymous search endpoint can require a token. The app tries that search first, then falls back to Funda's public server-rendered search using `curl-cffi` with a Chrome-compatible TLS fingerprint.
- **The fallback needs no login, browser session, or captured token.** It reads only canonical listing links from the HTML, stops once it reaches a page already present in local history, and continues to use pyfunda's structured detail response for enrichment.

The mobile API gives us everything: price, floor area, rooms, energy label, wijk + buurt, coordinates, full description, photos, and floor plan URLs. The app records Funda price and market-status changes as an append-only observation history; older records receive one clearly marked legacy snapshot because changes from before tracking began cannot be reconstructed.

Opinion scores and house-hunt progress are independent. A listing can keep its 0–3 score while its personal status moves through call, viewing requested, viewing planned, viewed, bid, sold, or bought. Personal statuses are shared across signed-in browsers and stored in `data/tracking_statuses.json`. Sold and otherwise unavailable listings remain in the generated overview but are hidden by default; uncheck **hide sold** to revisit them.

The overview also spatially joins listing coordinates against Amsterdam's **Woningwaardekaart 2025**. Its bands are based on interpolated Kadaster transaction €/m² and are deliberately displayed as a historic range, not a current valuation. The bundled source file is `reference/woningwaarde-2025.geojson`; refresh it from the municipality with:

```bash
.venv/bin/python fetch.py --refresh-price-bands
```

Districts are spatially joined from Amsterdam's official **Wijken** boundaries, so discovery does not depend on Funda's search API supplying a district field. The bundled source file is `reference/wijken.geojson`; refresh it and backfill missing districts with:

```bash
.venv/bin/python fetch.py --refresh-districts
```

## Usage

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

**Server mode** (the normal way): serves the overview and keeps it fresh — a background
loop re-fetches whenever the data is older than `fetch_interval_seconds` (default 60s).

```bash
.venv/bin/python server.py                     # http://127.0.0.1:8000
.venv/bin/python server.py --host 0.0.0.0 --port 8000 --interval 60
curl localhost:8000/healthz                    # last fetch time, count, last error
```

**One-shot mode**:

```bash
.venv/bin/python fetch.py               # fetch new listings + regenerate overview.html
.venv/bin/python fetch.py --render-only # just regenerate overview.html
open overview.html
```

Search settings (city, price/area filters, number of pages, and the Dam and Science Park reference points for distance) live in [config.json](config.json).

## How it works

- `fetch.py` searches funda (newest first), skips listings already in `data/listings.json`, fetches details for the new ones (description, coordinates, floor plans), and computes derived fields: **€/m²**, **2025 local transaction-price band**, and straight-line distances to **Dam Square** and **Science Park 303**.
- `data/listings.json` is the state: everything we've ever seen, keyed by listing id. New-since-last-run rows get a "nieuw" badge in the overview.
- `overview.html` is a self-contained page: sortable columns (click a header), click a row to expand the description, links to the funda page and the floor plan.

## Roadmap

See [FEATURES.md](FEATURES.md).
