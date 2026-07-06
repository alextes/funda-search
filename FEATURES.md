# Features

## Done

- [x] Proof of concept: get listing data out of funda (via pyfunda / mobile API — see README)
- [x] Track seen listings, only fetch new ones (`data/listings.json`)
- [x] HTML overview: photo, address, wijk/buurt, price, m², €/m², rooms, energy label, distance from center, floor plan link, description
- [x] Sortable columns, expandable descriptions, "new" badge for listings first seen today
- [x] Rate listings 0–3 (0 = reviewed, not interesting), persisted in localStorage; filters to hide rated / hide 0-rated; sortable score column
- [x] Fold-out shows description (left) next to floor plan image (right)
- [x] Detect uncategorized floor plans hiding among regular photos (pixel-stats heuristic: mostly white/gray page with dark line art); `--backfill-floorplans` for stored listings
- [x] Minimum living area filter (config `filters.min_area`, applied to search and render)
- [x] Photo grid per listing (click row thumbnail, fold-out link, or `p`); grid image click opens full-res original
- [x] Keyboard shortcuts: `j`/`k` move, `enter` fold, `p` photos, `x`/`0`–`3` rate (auto-advances when the row hides), `f` open funda, `esc` close
- [x] Minimal in-page slideshow over the grid: `←`/`→` or `j`/`k` navigate (wraps), click right/left side of the photo for next/prev, `esc` back to grid
- [x] Price band filter (config `min_price`/`max_price`, applied to search and render)

## Planned

- [x] ~~Daily morning search~~ superseded: `server.py` keeps the list at most `fetch_interval_seconds` (60s) old while running
- [x] Deployed to exe.dev: https://donut-chaise.exe.xyz (public); tooling in the private funda-search-deploy repo
- [x] Status refresh (~hourly): sold/withdrawn listings drop out, under-offer listings get dimmed + "under offer" tag with a hide toggle (default on); price changes tracked too
- [x] Password gate on the server (FUNDA_SEARCH_PASSWORD env, 30-day session cookie)
- [x] Server-side shared ratings (`data/ratings.json`, POST /rate) — multiple people/browsers see the same ratings; localStorage remains the static-page fallback and migrates over on first load
- [x] Minimum bedrooms filter (config `filters.min_bedrooms`)
- [ ] **Description scanning** — automatically check each description for the recurring criteria (to define: e.g. erfpacht/eigen grond, fundering, VvE health, balkon/buitenruimte, bouwjaar...) and show the verdicts as columns
- [ ] **Better filtering** — filter the overview client-side (price range, wijk, min m²)
- [ ] **Ratings beyond one browser** — localStorage is per-browser/origin; consider an export button or a tiny local server that writes ratings to `data/ratings.json`
- [ ] **Travel time** — realistic bike/transit time to chosen points (work, center) instead of straight-line distance
- [ ] **Price history / sold data** — pyfunda exposes price history; flag price drops
- [ ] **Notifications** — ping (email/Telegram) when a new listing matches the criteria

## Ideas / someday

- [ ] **Overbidding estimate per neighborhood** — funda's sold data (pyfunda supports `category="sold"` + price history) could show typical sold-vs-asking deltas per wijk, to estimate the *real* cost of a listing

- [ ] Score listings against a personal weighting (€/m², location, outdoor space, ...)
- [ ] Map view of active listings
- [ ] Track listing status changes (under offer, sold) over time
- [ ] GitHub Pages deploy of the overview (careful: public)
