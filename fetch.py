#!/usr/bin/env python3
"""Fetch new funda listings, enrich them, and render an HTML overview.

Uses pyfunda (reverse-engineered funda mobile API) — no scraping, no browser.
State lives in data/listings.json; every run only fetches details for
listings we haven't seen before, then regenerates overview.html.
"""

from __future__ import annotations

import argparse
import html
import io
import json
import math
import sys
import time
import urllib.request
from datetime import date, datetime
from pathlib import Path

from funda import Funda
from PIL import Image

ROOT = Path(__file__).parent
DATA_FILE = ROOT / "data" / "listings.json"
OVERVIEW_FILE = ROOT / "overview.html"
CONFIG_FILE = ROOT / "config.json"

DETAIL_FETCH_DELAY_S = 1.0
IMAGE_FETCH_DELAY_S = 0.15
# statuses that mean the listing is off the market and should leave the overview
GONE_STATUSES = {"sold", "unavailable", "withdrawn", "rented"}
BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
    " (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
# Listers don't always categorize floor plans; they then appear as regular
# photos, anywhere in the set. Plans are dark line art on a mostly white
# page, so classify on pixel stats (measured: photos <= 0.27 white, plans
# >= 0.7; blank placeholder pages have ~0 dark pixels, plans >= 0.012).
FLOORPLAN_WHITE_MIN = 0.5
FLOORPLAN_GRAY_MIN = 0.5
FLOORPLAN_DARK_MIN = 0.008


def detect_floorplans(photo_urls: list[str]) -> list[str]:
    """Return photo URLs that look like floor plans (pixel-stats heuristic)."""
    found = []
    for url in photo_urls:
        small = url.replace(".jpg", "_360.jpg")
        try:
            req = urllib.request.Request(small, headers={"User-Agent": BROWSER_UA})
            data = urllib.request.urlopen(req, timeout=15).read()
            img = Image.open(io.BytesIO(data)).convert("RGB").resize((160, 120))
        except Exception:
            continue
        px = list(img.getdata())
        n = len(px)
        white = sum(1 for r, g, b in px if r > 230 and g > 230 and b > 230) / n
        gray = sum(1 for r, g, b in px if abs(r - g) < 12 and abs(g - b) < 12 and abs(r - b) < 12) / n
        dark = sum(1 for r, g, b in px if r < 120 and g < 120 and b < 120) / n
        if white > FLOORPLAN_WHITE_MIN and gray > FLOORPLAN_GRAY_MIN and dark > FLOORPLAN_DARK_MIN:
            found.append(url)
        time.sleep(IMAGE_FETCH_DELAY_S)
    return found


def load_config() -> dict:
    return json.loads(CONFIG_FILE.read_text())


def load_listings() -> dict[str, dict]:
    if DATA_FILE.exists():
        return json.loads(DATA_FILE.read_text())
    return {}


def write_atomic(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text)
    tmp.replace(path)


def save_listings(listings: dict[str, dict]) -> None:
    DATA_FILE.parent.mkdir(exist_ok=True)
    write_atomic(DATA_FILE, json.dumps(listings, indent=1, ensure_ascii=False))


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def search_pages(client: Funda, config: dict) -> list:
    filters = {k: v for k, v in config.get("filters", {}).items() if v is not None}
    items = []
    for page in range(config.get("pages", 3)):
        batch = client.search(config["location"], sort="newest", page=page, **filters)
        if not batch:
            break
        items.extend(batch)
    return items


def build_record(item, detail, config: dict) -> dict:
    addr = item.address
    wijk = None
    try:
        wijk = item.raw["_source"]["address"].get("wijk")
    except (KeyError, TypeError):
        pass

    price = item.price.amount if item.price else None
    area = detail.living_area or item.living_area
    price_per_m2 = round(price / area) if price and area else None

    lat = lon = distance_km = None
    if detail.location:
        lat, lon = detail.location.latitude, detail.location.longitude
        center = config["center"]
        distance_km = round(haversine_km(lat, lon, center["lat"], center["lon"]), 1)

    photos = list(detail.media.photo_urls or [])
    photo_url = photos[0] if photos else None

    floorplans = []
    for fp in detail.media.floorplans or []:
        floorplans.append(
            {
                "thumbnail_url": fp.thumbnail_url,
                "page_url": fp.url,
                "embed_url": fp.embed_url,
            }
        )
    if not floorplans:
        floorplans = [
            {"thumbnail_url": u, "page_url": None, "embed_url": None, "detected": True}
            for u in detect_floorplans(photos)
        ]

    pub_date = detail.publication_date or getattr(item, "publication_date", None)
    if pub_date is not None:
        pub_date = str(pub_date)[:10]

    return {
        "id": item.global_id or item.id,
        "url": item.url,
        "title": item.title,
        "postcode": addr.postcode if addr else None,
        "neighbourhood": addr.neighbourhood if addr else None,
        "wijk": wijk,
        "city": addr.city if addr else None,
        "price": price,
        "living_area": area,
        "price_per_m2": price_per_m2,
        "rooms": detail.rooms_count or item.rooms_count,
        "bedrooms": detail.bedrooms or item.bedrooms,
        "energy_label": str(detail.energy_label or item.energy_label or ""),
        "publication_date": pub_date,
        "first_seen": date.today().isoformat(),
        "lat": lat,
        "lon": lon,
        "distance_km": distance_km,
        "floorplans": floorplans,
        "photo_url": photo_url,
        "photo_urls": photos,
        "description": detail.description,
        "status": str(detail.status or item.status or ""),
    }


def fetch(config: dict, listings: dict[str, dict]) -> tuple[int, int]:
    with Funda() as client:
        items = search_pages(client, config)
        new_items = [i for i in items if str(i.global_id or i.id) not in listings]
        print(f"search returned {len(items)} listings, {len(new_items)} new")

        for n, item in enumerate(new_items, 1):
            key = str(item.global_id or item.id)
            try:
                detail = client.listing(item.global_id or item.id)
                listings[key] = build_record(item, detail, config)
                print(f"  [{n}/{len(new_items)}] {item.title}")
            except Exception as e:
                print(f"  [{n}/{len(new_items)}] {item.title} FAILED: {e}", file=sys.stderr)
            time.sleep(DETAIL_FETCH_DELAY_S)

    return len(items), len(new_items)


def refresh_statuses(listings: dict[str, dict]) -> int:
    """Re-fetch status and price for listings not yet known to be off the market."""
    todo = [l for l in listings.values() if l.get("status") not in GONE_STATUSES]
    changed = 0
    with Funda() as client:
        for l in todo:
            try:
                detail = client.listing(l["id"])
            except Exception as e:
                if "404" in str(e) or "not found" in str(e).lower():
                    l["status"] = "unavailable"
                    changed += 1
                else:
                    print(f"  status check failed for {l['title']}: {e}", file=sys.stderr)
                time.sleep(DETAIL_FETCH_DELAY_S)
                continue
            new_status = str(detail.status or l.get("status") or "")
            if new_status != l.get("status"):
                print(f"  {l['title']}: {l.get('status') or '?'} -> {new_status}")
                l["status"] = new_status
                changed += 1
            price = detail.price.amount if detail.price else None
            if price and price != l.get("price"):
                print(f"  {l['title']}: price {l.get('price')} -> {price}")
                l["price"] = price
                if l.get("living_area"):
                    l["price_per_m2"] = round(price / l["living_area"])
                changed += 1
            time.sleep(DETAIL_FETCH_DELAY_S)
    print(f"status refresh: {len(todo)} checked, {changed} changes")
    return changed


def backfill_photos(listings: dict[str, dict]) -> None:
    """Fetch and store the full photo URL list for listings missing it."""
    todo = [l for l in listings.values() if "photo_urls" not in l]
    print(f"{len(todo)} listings without photo lists")
    with Funda() as client:
        for n, l in enumerate(todo, 1):
            try:
                detail = client.listing(l["id"])
                l["photo_urls"] = list(detail.media.photo_urls or [])
                print(f"  [{n}/{len(todo)}] {l['title']}: {len(l['photo_urls'])} photos")
            except Exception as e:
                print(f"  [{n}/{len(todo)}] {l['title']} FAILED: {e}", file=sys.stderr)
            time.sleep(DETAIL_FETCH_DELAY_S)


def backfill_floorplans(listings: dict[str, dict]) -> None:
    """Detect floor plans for stored listings that don't have any."""
    todo = [l for l in listings.values() if not l.get("floorplans")]
    print(f"{len(todo)} listings without floor plans")
    with Funda() as client:
        for n, l in enumerate(todo, 1):
            try:
                detail = client.listing(l["id"])
                photos = list(detail.media.photo_urls or [])
                detected = detect_floorplans(photos)
                l["floorplans"] = [
                    {"thumbnail_url": u, "page_url": None, "embed_url": None, "detected": True}
                    for u in detected
                ]
                print(f"  [{n}/{len(todo)}] {l['title']}: {len(detected)} detected")
            except Exception as e:
                print(f"  [{n}/{len(todo)}] {l['title']} FAILED: {e}", file=sys.stderr)
            time.sleep(DETAIL_FETCH_DELAY_S)


def render(config: dict, listings: dict[str, dict]) -> None:
    filters = config.get("filters", {})
    min_area = filters.get("min_area")
    min_price = filters.get("min_price")
    max_price = filters.get("max_price")
    min_bedrooms = filters.get("min_bedrooms")

    def visible(l: dict) -> bool:
        if l.get("status") in GONE_STATUSES:
            return False
        if min_area and l.get("living_area") and l["living_area"] < min_area:
            return False
        if min_price and l.get("price") and l["price"] < min_price:
            return False
        if max_price and l.get("price") and l["price"] > max_price:
            return False
        # bedrooms 0/None means unreported — keep those rather than losing real options
        if min_bedrooms and l.get("bedrooms") and l["bedrooms"] < min_bedrooms:
            return False
        return True

    rows = sorted(
        filter(visible, listings.values()),
        key=lambda l: (l.get("first_seen") or "", l.get("publication_date") or ""),
        reverse=True,
    )

    def td(value, suffix="") -> str:
        if value is None or value == "":
            return "<td>–</td>"
        return f"<td>{html.escape(str(value))}{suffix}</td>"

    body_rows = []
    for l in rows:
        fps = l.get("floorplans") or []
        fp_data = json.dumps(
            [
                {
                    "img": fp["thumbnail_url"],
                    "embed": fp.get("embed_url"),
                    "detected": fp.get("detected", False),
                }
                for fp in fps
            ]
        )
        photo = (
            f'<img src="{html.escape(l["photo_url"])}" loading="lazy" alt="">'
            if l.get("photo_url")
            else ""
        )
        price = f"€ {l['price']:,}".replace(",", ".") if l.get("price") else "–"
        ppm2 = f"€ {l['price_per_m2']:,}".replace(",", ".") if l.get("price_per_m2") else "–"
        desc = html.escape(l.get("description") or "")
        photo_urls = " ".join(l.get("photo_urls") or [])
        body_rows.append(
            f"""<tr data-id="{l['id']}" data-status="{html.escape(l.get('status') or '')}" data-desc="{desc}" data-fp="{html.escape(fp_data)}" data-lat="{l.get('lat') or ''}" data-lon="{l.get('lon') or ''}" data-photos="{html.escape(photo_urls)}">
  <td class="photo">{photo}</td>
  <td class="addr"><a href="{html.escape(l['url'])}" target="_blank">{html.escape(l['title'] or '?')}</a>{'<span class="uo-tag">under offer</span>' if l.get('status') == 'negotiations' else ''}</td>
  {td(l.get('wijk'))}
  {td(l.get('neighbourhood'))}
  <td data-sort="{l.get('price') or 0}">{price}</td>
  {td(l.get('living_area'), ' m²')}
  <td data-sort="{l.get('price_per_m2') or 0}">{ppm2}</td>
  {td(l.get('rooms'))}
  {td(l.get('energy_label'))}
  <td data-sort="{l.get('distance_km') or 999}">{l.get('distance_km') if l.get('distance_km') is not None else '–'} km</td>
  <td class="listed" data-date="{html.escape(l.get('publication_date') or '')}" title="{html.escape(l.get('publication_date') or '')}">–</td>
  <td class="score" data-sort="-1"><div class="rate">
    <button data-s="0" title="reviewed, not interesting">✕</button>
    <button data-s="1">1</button>
    <button data-s="2">2</button>
    <button data-s="3">3</button>
  </div></td>
</tr>"""
        )

    page = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>funda-search · {html.escape(config['location'])}</title>
<style>
  :root {{ font-family: -apple-system, system-ui, sans-serif; }}
  body {{ margin: 2rem; color: #1a1a1a; }}
  h1 {{ font-size: 1.3rem; }} .meta {{ color: #666; font-size: .85rem; }}
  .controls {{ margin: .6rem 0 1rem; font-size: .85rem; display: flex; gap: 1.2rem; align-items: center; color: #333; }}
  .controls label {{ cursor: pointer; user-select: none; }}
  table {{ border-collapse: collapse; width: 100%; font-size: .85rem; }}
  th, td {{ text-align: left; padding: .45rem .6rem; border-bottom: 1px solid #e5e5e5; white-space: nowrap; }}
  th {{ cursor: pointer; user-select: none; position: sticky; top: 0; background: #fff; }}
  th:hover {{ color: #f7a100; }}
  .photo img {{ width: 72px; height: 48px; object-fit: cover; border-radius: 4px; display: block; }}
  .addr a {{ color: #0071b3; text-decoration: none; }} .addr a:hover {{ text-decoration: underline; }}
  tr {{ cursor: pointer; }}
  .rate {{ display: flex; gap: .2rem; }}
  .rate button {{ width: 1.7rem; height: 1.7rem; border: 1px solid #ccc; background: #fff; border-radius: 4px;
                  cursor: pointer; font-size: .8rem; color: #555; }}
  .rate button:hover {{ border-color: #f7a100; color: #f7a100; }}
  .rate button.on {{ background: #f7a100; border-color: #f7a100; color: #fff; }}
  .rate button[data-s="0"].on {{ background: #999; border-color: #999; }}
  tr.desc-row {{ cursor: auto; }} tr.desc-row > td {{ white-space: normal; background: #fafafa; }}
  .fold {{ display: flex; gap: 1.5rem; align-items: flex-start; }}
  .fold-desc {{ flex: 1 1 50%; color: #444; white-space: pre-line; max-width: 50%; }}
  .fold-right {{ flex: 1 1 50%; }}
  .fold-right iframe {{ width: 100%; height: 320px; border: 1px solid #e5e5e5; border-radius: 4px; display: block; }}
  .fold-right iframe.fp-embed {{ height: 600px; margin-bottom: .5rem; }}
  .fold-right .maplink {{ font-size: .8rem; display: inline-block; margin: .3rem 0 .8rem; color: #0071b3; }}
  .fold-right img {{ max-width: 100%; border: 1px solid #e5e5e5; border-radius: 4px; margin-bottom: .5rem; display: block; }}
  .fold-right .none {{ color: #999; margin-top: .5rem; }}
  .fp-wrap {{ position: relative; }}
  .fp-wrap .fp-flag {{ position: absolute; top: .5rem; right: .5rem; border: 1px solid #ccc; background: rgba(255,255,255,.92);
                      color: #666; border-radius: 4px; padding: .25rem .5rem; font-size: .75rem; cursor: pointer; opacity: 0; }}
  .fp-wrap:hover .fp-flag {{ opacity: 1; }}
  .fp-wrap .fp-flag:hover {{ border-color: #c00; color: #c00; }}
  kbd {{ background: #f0f0f0; border: 1px solid #ccc; border-radius: 3px; padding: 0 .3rem; font-size: .75rem; font-family: inherit; }}
  tr.sel > td {{ background: #eaf4fb; }}
  tr[data-status="negotiations"] {{ opacity: .55; }}
  .uo-tag {{ background: #e5e5e5; color: #555; border-radius: 3px; font-size: .7rem; padding: .1rem .35rem; margin-left: .4rem; }}
  #grid {{ position: fixed; inset: 0; background: rgba(255,255,255,.98); z-index: 10; overflow-y: auto; padding: 1rem; }}
  #grid header {{ display: flex; justify-content: space-between; align-items: center; margin-bottom: .8rem; }}
  #grid header span {{ font-weight: 600; }}
  #grid header button {{ border: 1px solid #ccc; background: #fff; border-radius: 4px; padding: .3rem .7rem; cursor: pointer; }}
  #grid .cells {{ display: grid; grid-template-columns: repeat(3, 1fr); gap: .5rem; }}
  #grid .cells img {{ width: 100%; aspect-ratio: 3/2; object-fit: cover; border-radius: 4px; cursor: pointer; display: block; }}
  #show {{ position: fixed; inset: 0; background: rgba(0,0,0,.93); z-index: 20; display: flex; align-items: center; justify-content: center; }}
  #grid[hidden], #show[hidden] {{ display: none; }}
  #show img {{ max-width: 96vw; max-height: 92vh; object-fit: contain; cursor: pointer; }}
  #show .bar {{ position: absolute; top: .8rem; right: 1rem; display: flex; gap: 1rem; align-items: center; color: #ddd; font-size: .85rem; }}
  #show .bar button {{ border: 1px solid #777; background: transparent; color: #ddd; border-radius: 4px; padding: .3rem .7rem; cursor: pointer; }}
</style>
</head>
<body>
<h1>funda-search · {html.escape(config['location'])}</h1>
<p class="meta">{len(rows)} listings · generated {datetime.now().strftime('%Y-%m-%d %H:%M')} · click a column header to sort, click a row for description &amp; floor plan, click a photo for the photo grid</p>
<p class="meta">keys: <kbd>j</kbd>/<kbd>k</kbd> or <kbd>↓</kbd>/<kbd>↑</kbd> move · <kbd>enter</kbd> fold · <kbd>p</kbd> photos · <kbd>x</kbd>/<kbd>0</kbd>–<kbd>3</kbd> rate · <kbd>f</kbd> open funda · <kbd>esc</kbd> close</p>
<div class="controls">
  <label><input type="checkbox" id="hideRated"> hide rated</label>
  <label><input type="checkbox" id="hideNo" checked> hide "not interesting" (✕)</label>
  <label><input type="checkbox" id="hideUO" checked> hide under offer</label>
  <span id="counts" class="meta"></span>
</div>
<table id="t">
<thead><tr>
  <th></th><th>Address</th><th>District</th><th>Neighbourhood</th><th>Price</th><th>Area</th><th>€/m²</th>
  <th>Rooms</th><th>Energy</th><th>Distance</th><th>Listed</th><th data-defdesc="1">Score</th>
</tr></thead>
<tbody>
{chr(10).join(body_rows)}
</tbody>
</table>
<div id="grid" hidden>
  <header><span id="gridTitle"></span><button id="gridClose">close (esc)</button></header>
  <div class="cells"></div>
</div>
<div id="show" hidden>
  <div class="bar"><span id="showCounter"></span><button id="showClose">close (esc)</button></div>
  <img id="showImg" alt="">
</div>
<script>
const tbody = document.querySelector('#t tbody');

for (const cell of document.querySelectorAll('td.listed')) {{
  const iso = cell.dataset.date;
  if (!iso) {{ cell.dataset.sort = 9999; continue; }}
  const days = Math.max(0, Math.round((Date.now() - new Date(iso + 'T00:00')) / 86400000));
  cell.textContent = days === 0 ? 'today' : days === 1 ? 'yesterday' : `${{days}}d ago`;
  cell.dataset.sort = days;
}}
const hideRated = document.getElementById('hideRated');
const hideNo = document.getElementById('hideNo');
const hideUO = document.getElementById('hideUO');

// ratings live on the server (shared across browsers/people); localStorage is
// the fallback when the page is opened statically (file://, python -m http.server)
let ratings = {{}};
let serverRatings = false;

function postRate(id, score) {{
  fetch('rate', {{
    method: 'POST',
    headers: {{'Content-Type': 'application/json'}},
    body: JSON.stringify({{id, score}}),
  }}).catch(() => {{}});
}}

function saveRating(id, score) {{
  if (score === null) delete ratings[id];
  else ratings[id] = score;
  if (serverRatings) postRate(id, score);
  else localStorage.setItem('funda-ratings', JSON.stringify(ratings));
}}

// false-positive floor plan flags: id -> [image urls]; same server-first,
// localStorage-fallback model as ratings
let fpFlags = {{}};
let serverFlags = false;

function saveFpFlag(id, url, flagged) {{
  const urls = fpFlags[id] || (fpFlags[id] = []);
  if (flagged && !urls.includes(url)) urls.push(url);
  if (!flagged) fpFlags[id] = urls.filter(u => u !== url);
  if (!fpFlags[id].length) delete fpFlags[id];
  if (serverFlags) {{
    fetch('flag-fp', {{
      method: 'POST',
      headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{id, url, flagged}}),
    }}).catch(() => {{}});
  }} else {{
    localStorage.setItem('funda-fpflags', JSON.stringify(fpFlags));
  }}
}}

async function initRatings() {{
  const local = JSON.parse(localStorage.getItem('funda-ratings') || '{{}}');
  try {{
    const res = await fetch('ratings.json', {{cache: 'no-store'}});
    if (res.ok) {{ ratings = await res.json(); serverRatings = true; }}
  }} catch (e) {{}}
  try {{
    const res = await fetch('fpflags.json', {{cache: 'no-store'}});
    if (res.ok) {{ fpFlags = await res.json(); serverFlags = true; }}
  }} catch (e) {{}}
  if (!serverFlags) fpFlags = JSON.parse(localStorage.getItem('funda-fpflags') || '{{}}');
  if (serverRatings) {{
    // one-time migration: push local ratings the server doesn't know yet
    for (const [id, s] of Object.entries(local)) {{
      if (!(id in ratings)) {{ ratings[id] = s; postRate(id, s); }}
    }}
  }} else {{
    ratings = local;
  }}
  applyRatings();
  applyFilters();
}}

function listingRows() {{ return [...tbody.querySelectorAll('tr[data-id]')]; }}

function applyRatings() {{
  for (const tr of listingRows()) {{
    const s = ratings[tr.dataset.id];
    tr.querySelectorAll('.rate button').forEach(b =>
      b.classList.toggle('on', s !== undefined && +b.dataset.s === s));
    tr.querySelector('td.score').dataset.sort = s === undefined ? -1 : s;
  }}
}}

function applyFilters() {{
  let visible = 0, rated = 0;
  for (const tr of listingRows()) {{
    const s = ratings[tr.dataset.id];
    if (s !== undefined) rated++;
    const hide = (hideRated.checked && s !== undefined) || (hideNo.checked && s === 0)
      || (hideUO.checked && tr.dataset.status === 'negotiations');
    tr.style.display = hide ? 'none' : '';
    const next = tr.nextElementSibling;
    if (next && next.classList.contains('desc-row')) next.style.display = hide ? 'none' : '';
    if (!hide) visible++;
  }}
  document.getElementById('counts').textContent = `${{visible}} shown · ${{rated}} rated`;
}}

hideRated.addEventListener('change', applyFilters);
hideNo.addEventListener('change', applyFilters);
hideUO.addEventListener('change', applyFilters);

document.querySelectorAll('#t th').forEach((th, i) => th.addEventListener('click', () => {{
  document.querySelectorAll('.desc-row').forEach(r => r.remove());
  const rows = listingRows();
  const dir = th.dataset.dir = th.dataset.dir
    ? (th.dataset.dir === 'asc' ? 'desc' : 'asc')
    : (th.dataset.defdesc ? 'desc' : 'asc');
  rows.sort((a, b) => {{
    const av = a.cells[i]?.dataset.sort ?? a.cells[i]?.textContent.trim() ?? '';
    const bv = b.cells[i]?.dataset.sort ?? b.cells[i]?.textContent.trim() ?? '';
    const an = parseFloat(av), bn = parseFloat(bv);
    const cmp = (!isNaN(an) && !isNaN(bn)) ? an - bn : av.localeCompare(bv);
    return dir === 'asc' ? cmp : -cmp;
  }});
  rows.forEach(r => tbody.appendChild(r));
}}));

function rate(tr, s) {{
  const id = tr.dataset.id;
  saveRating(id, ratings[id] === s ? null : s);
  applyRatings(); applyFilters();
}}

function toggleFold(tr) {{
  const next = tr.nextElementSibling;
  if (next && next.classList.contains('desc-row')) {{ next.remove(); return; }}
  const photos = (tr.dataset.photos || '').split(' ').filter(Boolean);
  const id = tr.dataset.id;
  const flagged = fpFlags[id] || [];
  const fps = JSON.parse(tr.dataset.fp || '[]').filter(f => !flagged.includes(f.img));
  // interactive Floorplanner embed when funda has one (the static thumbnail is
  // only 900px); otherwise the full-res detected image, click-through to open.
  // detected plans come from a heuristic, so they carry a "not a floor plan"
  // flag button — flags are stored server-side to hide misfires and to collect
  // labeled mistakes for tuning the detector
  const fpHtml = fps.length
    ? fps.map(f => f.embed
        ? `<iframe class="fp-embed" loading="lazy" src="${{f.embed}}"></iframe>`
        : `<div class="fp-wrap"><a href="${{f.img}}" target="_blank"><img src="${{f.img}}" loading="lazy" alt="floor plan"></a>${{
            f.detected ? `<button class="fp-flag" data-url="${{f.img}}" title="hide and record as a detector mistake">not a floor plan ✕</button>` : ''
          }}</div>`
      ).join('')
    : '<div class="none">no floor plan</div>';
  const lat = parseFloat(tr.dataset.lat), lon = parseFloat(tr.dataset.lon);
  let mapHtml = '';
  if (!isNaN(lat) && !isNaN(lon)) {{
    const bbox = `${{lon - 0.01}},${{lat - 0.006}},${{lon + 0.01}},${{lat + 0.006}}`;
    mapHtml = `<iframe loading="lazy" src="https://www.openstreetmap.org/export/embed.html?bbox=${{bbox}}&layer=mapnik&marker=${{lat}},${{lon}}"></iframe>
      <a class="maplink" href="https://www.google.com/maps?q=${{lat}},${{lon}}" target="_blank">open in Google Maps</a>`;
  }}
  const photosLink = photos.length
    ? `<a class="maplink" href="#" data-open-grid>browse ${{photos.length}} photos (p)</a><br>`
    : '';
  const row = document.createElement('tr');
  row.className = 'desc-row';
  const cell = document.createElement('td');
  cell.colSpan = 12;
  const fold = document.createElement('div');
  fold.className = 'fold';
  const descDiv = document.createElement('div');
  descDiv.className = 'fold-desc';
  descDiv.textContent = tr.dataset.desc || '';
  const fpDiv = document.createElement('div');
  fpDiv.className = 'fold-right';
  fpDiv.innerHTML = photosLink + mapHtml + fpHtml;
  const gl = fpDiv.querySelector('[data-open-grid]');
  if (gl) gl.addEventListener('click', e => {{ e.preventDefault(); openGrid(tr); }});
  function bindFlag(wrap) {{
    wrap.querySelector('.fp-flag')?.addEventListener('click', e => {{
      e.preventDefault();
      const url = wrap.querySelector('.fp-flag').dataset.url;
      saveFpFlag(id, url, true);
      wrap.innerHTML = '<div class="none">flagged as not a floor plan · <a href="#">undo</a></div>';
      wrap.querySelector('a').addEventListener('click', e2 => {{
        e2.preventDefault();
        saveFpFlag(id, url, false);
        wrap.innerHTML = `<a href="${{url}}" target="_blank"><img src="${{url}}" loading="lazy" alt="floor plan"></a>
          <button class="fp-flag" data-url="${{url}}" title="hide and record as a detector mistake">not a floor plan ✕</button>`;
        bindFlag(wrap);
      }});
    }});
  }}
  fpDiv.querySelectorAll('.fp-wrap').forEach(bindFlag);
  fold.append(descDiv, fpDiv);
  cell.append(fold);
  row.append(cell);
  tr.after(row);
}}

// --- photo grid ---
const grid = document.getElementById('grid');
let gridPhotos = [];

function openGrid(tr) {{
  const photos = (tr.dataset.photos || '').split(' ').filter(Boolean);
  if (!photos.length) return;
  gridPhotos = photos;
  document.getElementById('gridTitle').textContent =
    tr.querySelector('.addr a').textContent.trim() + ` · ${{photos.length}} photos`;
  const cells = grid.querySelector('.cells');
  cells.innerHTML = '';
  photos.forEach((url, i) => {{
    const img = document.createElement('img');
    img.src = url.replace('.jpg', '_1080.jpg');
    img.loading = 'lazy';
    img.addEventListener('click', () => openShow(i));
    cells.append(img);
  }});
  grid.hidden = false;
  grid.scrollTop = 0;
  document.body.style.overflow = 'hidden';
}}

function closeGrid() {{
  grid.hidden = true;
  document.body.style.overflow = '';
}}

document.getElementById('gridClose').addEventListener('click', closeGrid);

// --- slideshow (over the grid) ---
const show = document.getElementById('show');
const showImg = document.getElementById('showImg');
let showIdx = 0;

function renderShow() {{
  showImg.src = gridPhotos[showIdx];
  document.getElementById('showCounter').textContent = `${{showIdx + 1}} / ${{gridPhotos.length}}`;
  for (const d of [1, -1]) {{
    new Image().src = gridPhotos[(showIdx + d + gridPhotos.length) % gridPhotos.length];
  }}
}}

function openShow(i) {{ showIdx = i; show.hidden = false; renderShow(); }}
function closeShow() {{ show.hidden = true; showImg.src = ''; }}
function moveShow(delta) {{
  showIdx = (showIdx + delta + gridPhotos.length) % gridPhotos.length;
  renderShow();
}}

document.getElementById('showClose').addEventListener('click', closeShow);
showImg.addEventListener('click', e => {{
  const third = showImg.getBoundingClientRect();
  moveShow(e.clientX < third.left + third.width / 3 ? -1 : 1);
}});
show.addEventListener('click', e => {{ if (e.target === show) closeShow(); }});

tbody.addEventListener('click', e => {{
  const btn = e.target.closest('.rate button');
  if (btn) {{ rate(btn.closest('tr'), +btn.dataset.s); return; }}
  const tr = e.target.closest('tr');
  if (!tr || tr.classList.contains('desc-row')) return;
  select(tr);
  if (e.target.closest('td.photo')) {{ openGrid(tr); return; }}
  if (e.target.closest('a')) return;
  toggleFold(tr);
}});

// --- keyboard navigation ---
let sel = null;

function visibleRows() {{ return listingRows().filter(r => r.style.display !== 'none'); }}

function select(tr) {{
  if (sel) sel.classList.remove('sel');
  sel = tr;
  if (sel) {{
    sel.classList.add('sel');
    sel.scrollIntoView({{block: 'nearest', behavior: 'smooth'}});
  }}
}}

function move(delta) {{
  const rows = visibleRows();
  if (!rows.length) return;
  let i = sel ? rows.indexOf(sel) : -1;
  if (i === -1) {{ select(rows[delta > 0 ? 0 : rows.length - 1]); return; }}
  select(rows[Math.min(rows.length - 1, Math.max(0, i + delta))]);
}}

document.addEventListener('keydown', e => {{
  if (e.target.matches?.('input, textarea, select') || e.metaKey || e.ctrlKey || e.altKey) return;
  if (!show.hidden) {{
    switch (e.key) {{
      case 'ArrowRight': case 'j': case ' ': e.preventDefault(); moveShow(1); break;
      case 'ArrowLeft': case 'k': e.preventDefault(); moveShow(-1); break;
      case 'Escape': e.preventDefault(); closeShow(); break;
    }}
    return;
  }}
  if (!grid.hidden) {{
    switch (e.key) {{
      case 'Escape': case 'p': e.preventDefault(); closeGrid(); break;
      case 'j': case 'ArrowDown':
        e.preventDefault(); grid.scrollBy({{top: grid.clientHeight * 0.8, behavior: 'smooth'}}); break;
      case 'k': case 'ArrowUp':
        e.preventDefault(); grid.scrollBy({{top: -grid.clientHeight * 0.8, behavior: 'smooth'}}); break;
    }}
    return;
  }}
  const rated = () => {{
    // if rating hid the selected row, advance to the nearest visible one below
    if (sel && sel.style.display === 'none') {{
      const rows = listingRows();
      const from = rows.indexOf(sel);
      const nextVis = rows.slice(from + 1).find(r => r.style.display !== 'none')
        || rows.slice(0, from).reverse().find(r => r.style.display !== 'none');
      select(nextVis || null);
    }}
  }};
  switch (e.key) {{
    case 'j': case 'ArrowDown': e.preventDefault(); move(1); break;
    case 'k': case 'ArrowUp': e.preventDefault(); move(-1); break;
    case 'Enter': case ' ': if (sel) {{ e.preventDefault(); toggleFold(sel); }} break;
    case 'p': if (sel) {{ e.preventDefault(); openGrid(sel); }} break;
    case 'f': if (sel) window.open(sel.querySelector('.addr a').href, '_blank'); break;
    case 'x': case '0': if (sel) {{ rate(sel, 0); rated(); }} break;
    case '1': case '2': case '3': if (sel) {{ rate(sel, +e.key); rated(); }} break;
    case 'Escape': {{
      const open = document.querySelector('.desc-row');
      if (open) open.remove();
      else select(null);
      break;
    }}
  }}
}});

initRatings();
</script>
</body>
</html>
"""
    write_atomic(OVERVIEW_FILE, page)
    print(f"wrote {OVERVIEW_FILE.relative_to(ROOT)} with {len(rows)} listings")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--render-only", action="store_true", help="regenerate overview.html without fetching"
    )
    parser.add_argument(
        "--backfill-floorplans",
        action="store_true",
        help="detect floor plans for stored listings that have none, then re-render",
    )
    parser.add_argument(
        "--backfill-photos",
        action="store_true",
        help="store the full photo URL list for listings missing it, then re-render",
    )
    parser.add_argument(
        "--refresh-status",
        action="store_true",
        help="re-check status and price of stored listings, then re-render",
    )
    args = parser.parse_args()

    config = load_config()
    listings = load_listings()

    if args.backfill_floorplans or args.backfill_photos or args.refresh_status:
        if args.backfill_floorplans:
            backfill_floorplans(listings)
        if args.backfill_photos:
            backfill_photos(listings)
        if args.refresh_status:
            refresh_statuses(listings)
        save_listings(listings)
    elif not args.render_only:
        fetch(config, listings)
        save_listings(listings)

    render(config, listings)


if __name__ == "__main__":
    main()
