#!/usr/bin/env python3
"""Fetch new funda listings, enrich them, and render an HTML overview.

Uses pyfunda (reverse-engineered funda mobile API) — no scraping, no browser.
State lives in data/listings.json; every run only fetches details for
listings we haven't seen before, then regenerates overview.html.
"""

from __future__ import annotations

import argparse
import html
import json
import math
import sys
import time
from datetime import date, datetime
from pathlib import Path

from funda import Funda

ROOT = Path(__file__).parent
DATA_FILE = ROOT / "data" / "listings.json"
OVERVIEW_FILE = ROOT / "overview.html"
CONFIG_FILE = ROOT / "config.json"

DETAIL_FETCH_DELAY_S = 1.0


def load_config() -> dict:
    return json.loads(CONFIG_FILE.read_text())


def load_listings() -> dict[str, dict]:
    if DATA_FILE.exists():
        return json.loads(DATA_FILE.read_text())
    return {}


def save_listings(listings: dict[str, dict]) -> None:
    DATA_FILE.parent.mkdir(exist_ok=True)
    DATA_FILE.write_text(json.dumps(listings, indent=1, ensure_ascii=False))


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

    floorplans = []
    for fp in detail.media.floorplans or []:
        floorplans.append(
            {
                "thumbnail_url": fp.thumbnail_url,
                "page_url": fp.url,
                "embed_url": fp.embed_url,
            }
        )

    photo_url = None
    photos = detail.media.photo_urls or []
    if photos:
        photo_url = photos[0]

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
        "description": detail.description,
        "status": str(item.status or ""),
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


def render(config: dict, listings: dict[str, dict]) -> None:
    today = date.today().isoformat()
    rows = sorted(
        listings.values(),
        key=lambda l: (l.get("first_seen") or "", l.get("publication_date") or ""),
        reverse=True,
    )

    def td(value, suffix="") -> str:
        if value is None or value == "":
            return "<td>–</td>"
        return f"<td>{html.escape(str(value))}{suffix}</td>"

    body_rows = []
    for l in rows:
        is_new = l.get("first_seen") == today
        fps = l.get("floorplans") or []
        fp_urls = " ".join(fp["thumbnail_url"] for fp in fps)
        fp_cell = (
            f'<td><a href="{html.escape(fps[0]["thumbnail_url"])}" target="_blank">floor plan</a></td>'
            if fps
            else "<td>–</td>"
        )
        photo = (
            f'<img src="{html.escape(l["photo_url"])}" loading="lazy" alt="">'
            if l.get("photo_url")
            else ""
        )
        price = f"€ {l['price']:,}".replace(",", ".") if l.get("price") else "–"
        ppm2 = f"€ {l['price_per_m2']:,}".replace(",", ".") if l.get("price_per_m2") else "–"
        desc = html.escape(l.get("description") or "")
        body_rows.append(
            f"""<tr class="{'new' if is_new else ''}" data-id="{l['id']}" data-desc="{desc}" data-fp="{html.escape(fp_urls)}" data-lat="{l.get('lat') or ''}" data-lon="{l.get('lon') or ''}">
  <td class="photo">{photo}</td>
  <td class="addr"><a href="{html.escape(l['url'])}" target="_blank">{html.escape(l['title'] or '?')}</a>
      {'<span class="badge">new</span>' if is_new else ''}</td>
  {td(l.get('wijk'))}
  {td(l.get('neighbourhood'))}
  <td data-sort="{l.get('price') or 0}">{price}</td>
  {td(l.get('living_area'), ' m²')}
  <td data-sort="{l.get('price_per_m2') or 0}">{ppm2}</td>
  {td(l.get('rooms'))}
  {td(l.get('energy_label'))}
  <td data-sort="{l.get('distance_km') or 999}">{l.get('distance_km') if l.get('distance_km') is not None else '–'} km</td>
  {fp_cell}
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
  tr.new {{ background: #fffbe8; }}
  .badge {{ background: #f7a100; color: #fff; border-radius: 3px; font-size: .7rem; padding: .1rem .35rem; margin-left: .4rem; }}
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
  .fold-right .maplink {{ font-size: .8rem; display: inline-block; margin: .3rem 0 .8rem; color: #0071b3; }}
  .fold-right img {{ max-width: 100%; border: 1px solid #e5e5e5; border-radius: 4px; margin-bottom: .5rem; display: block; }}
  .fold-right .none {{ color: #999; }}
</style>
</head>
<body>
<h1>funda-search · {html.escape(config['location'])}</h1>
<p class="meta">{len(rows)} listings · generated {datetime.now().strftime('%Y-%m-%d %H:%M')} · click a column header to sort, click a row for description &amp; floor plan</p>
<div class="controls">
  <label><input type="checkbox" id="hideRated"> hide rated</label>
  <label><input type="checkbox" id="hideNo" checked> hide "not interesting" (✕)</label>
  <span id="counts" class="meta"></span>
</div>
<table id="t">
<thead><tr>
  <th></th><th>Address</th><th>District</th><th>Neighbourhood</th><th>Price</th><th>Area</th><th>€/m²</th>
  <th>Rooms</th><th>Energy</th><th>Distance</th><th>Floor plan</th><th>Listed</th><th data-defdesc="1">Score</th>
</tr></thead>
<tbody>
{chr(10).join(body_rows)}
</tbody>
</table>
<script>
const tbody = document.querySelector('#t tbody');
const ratings = JSON.parse(localStorage.getItem('funda-ratings') || '{{}}');

for (const cell of document.querySelectorAll('td.listed')) {{
  const iso = cell.dataset.date;
  if (!iso) {{ cell.dataset.sort = 9999; continue; }}
  const days = Math.max(0, Math.round((Date.now() - new Date(iso + 'T00:00')) / 86400000));
  cell.textContent = days === 0 ? 'today' : days === 1 ? 'yesterday' : `${{days}}d ago`;
  cell.dataset.sort = days;
}}
const hideRated = document.getElementById('hideRated');
const hideNo = document.getElementById('hideNo');

function saveRatings() {{ localStorage.setItem('funda-ratings', JSON.stringify(ratings)); }}

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
    const hide = (hideRated.checked && s !== undefined) || (hideNo.checked && s === 0);
    tr.style.display = hide ? 'none' : '';
    const next = tr.nextElementSibling;
    if (next && next.classList.contains('desc-row')) next.style.display = hide ? 'none' : '';
    if (!hide) visible++;
  }}
  document.getElementById('counts').textContent = `${{visible}} shown · ${{rated}} rated`;
}}

hideRated.addEventListener('change', applyFilters);
hideNo.addEventListener('change', applyFilters);

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

tbody.addEventListener('click', e => {{
  const btn = e.target.closest('.rate button');
  if (btn) {{
    const tr = btn.closest('tr');
    const s = +btn.dataset.s;
    if (ratings[tr.dataset.id] === s) delete ratings[tr.dataset.id];
    else ratings[tr.dataset.id] = s;
    saveRatings(); applyRatings(); applyFilters();
    return;
  }}
  const tr = e.target.closest('tr');
  if (!tr || tr.classList.contains('desc-row') || e.target.closest('a')) return;
  const next = tr.nextElementSibling;
  if (next && next.classList.contains('desc-row')) {{ next.remove(); return; }}
  const desc = tr.dataset.desc || '';
  const fps = (tr.dataset.fp || '').split(' ').filter(Boolean);
  const fpHtml = fps.length
    ? fps.map(u => `<img src="${{u}}" loading="lazy" alt="floor plan">`).join('')
    : '<span class="none">no floor plan</span>';
  const lat = parseFloat(tr.dataset.lat), lon = parseFloat(tr.dataset.lon);
  let mapHtml = '';
  if (!isNaN(lat) && !isNaN(lon)) {{
    const bbox = `${{lon - 0.01}},${{lat - 0.006}},${{lon + 0.01}},${{lat + 0.006}}`;
    mapHtml = `<iframe loading="lazy" src="https://www.openstreetmap.org/export/embed.html?bbox=${{bbox}}&layer=mapnik&marker=${{lat}},${{lon}}"></iframe>
      <a class="maplink" href="https://www.google.com/maps?q=${{lat}},${{lon}}" target="_blank">open in Google Maps</a>`;
  }}
  const row = document.createElement('tr');
  row.className = 'desc-row';
  const cell = document.createElement('td');
  cell.colSpan = 13;
  const fold = document.createElement('div');
  fold.className = 'fold';
  const descDiv = document.createElement('div');
  descDiv.className = 'fold-desc';
  descDiv.textContent = desc;
  const fpDiv = document.createElement('div');
  fpDiv.className = 'fold-right';
  fpDiv.innerHTML = mapHtml + fpHtml;
  fold.append(descDiv, fpDiv);
  cell.append(fold);
  row.append(cell);
  tr.after(row);
}});

applyRatings();
applyFilters();
</script>
</body>
</html>
"""
    OVERVIEW_FILE.write_text(page)
    print(f"wrote {OVERVIEW_FILE.relative_to(ROOT)} with {len(rows)} listings")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--render-only", action="store_true", help="regenerate overview.html without fetching"
    )
    args = parser.parse_args()

    config = load_config()
    listings = load_listings()

    if not args.render_only:
        fetch(config, listings)
        save_listings(listings)

    render(config, listings)


if __name__ == "__main__":
    main()
