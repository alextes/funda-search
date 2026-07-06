# funda-search

Personal tool for house hunting on [funda.nl](https://www.funda.nl). Funda is good but cumbersome: lots of clicking, and you end up scanning every description for the same things. This repo automates the boring parts: fetch new listings, compute the numbers that matter, and show everything in one overview.

## How it gets the data

This was the big unknown, so it was the first proof of concept. Findings:

- **Plain HTTP to www.funda.nl is blocked** by DataDome bot protection (you get a "Je bent bijna op de pagina die je zoekt" challenge page).
- **[pyfunda](https://github.com/0xMH/pyfunda) works.** It talks to funda's reverse-engineered mobile app API (`*.funda.io`), which returns clean JSON: no scraping, no browser, no CAPTCHA. This is the approach used here.
- **Fallbacks if pyfunda breaks:** the website embeds full listing data in a `__NUXT_DATA__` JSON blob (devalue format) that can be extracted from a real Chrome session, which passes DataDome fine. Plain HTML scraping of the cards also works from a real browser.

The mobile API gives us everything: price, floor area, rooms, energy label, wijk + buurt, coordinates, full description, photos, and floor plan URLs.

## Usage

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

.venv/bin/python fetch.py               # fetch new listings + regenerate overview.html
.venv/bin/python fetch.py --render-only # just regenerate overview.html
open overview.html
```

Search settings (city, price/area filters, number of pages, "center" reference point for distance) live in [config.json](config.json).

## How it works

- `fetch.py` searches funda (newest first), skips listings already in `data/listings.json`, fetches details for the new ones (description, coordinates, floor plans), and computes derived fields: **€/m²** and **distance from the center** (haversine to Dam).
- `data/listings.json` is the state: everything we've ever seen, keyed by listing id. New-since-last-run rows get a "nieuw" badge in the overview.
- `overview.html` is a self-contained page: sortable columns (click a header), click a row to expand the description, links to the funda page and the floor plan.

## Roadmap

See [FEATURES.md](FEATURES.md).
