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
        fp = l.get("floorplans") or []
        fp_cell = (
            f'<td><a href="{html.escape(fp[0]["thumbnail_url"])}" target="_blank">plattegrond</a></td>'
            if fp
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
            f"""<tr class="{'new' if is_new else ''}" data-desc="{desc[:2000]}">
  <td class="photo">{photo}</td>
  <td class="addr"><a href="{html.escape(l['url'])}" target="_blank">{html.escape(l['title'] or '?')}</a>
      {'<span class="badge">nieuw</span>' if is_new else ''}</td>
  {td(l.get('wijk'))}
  {td(l.get('neighbourhood'))}
  <td data-sort="{l.get('price') or 0}">{price}</td>
  {td(l.get('living_area'), ' m²')}
  <td data-sort="{l.get('price_per_m2') or 0}">{ppm2}</td>
  {td(l.get('rooms'))}
  {td(l.get('energy_label'))}
  <td data-sort="{l.get('distance_km') or 999}">{l.get('distance_km') if l.get('distance_km') is not None else '–'} km</td>
  {fp_cell}
  {td(l.get('publication_date'))}
</tr>"""
        )

    page = f"""<!doctype html>
<html lang="nl">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>funda-search · {html.escape(config['location'])}</title>
<style>
  :root {{ font-family: -apple-system, system-ui, sans-serif; }}
  body {{ margin: 2rem; color: #1a1a1a; }}
  h1 {{ font-size: 1.3rem; }} .meta {{ color: #666; font-size: .85rem; margin-bottom: 1rem; }}
  table {{ border-collapse: collapse; width: 100%; font-size: .85rem; }}
  th, td {{ text-align: left; padding: .45rem .6rem; border-bottom: 1px solid #e5e5e5; white-space: nowrap; }}
  th {{ cursor: pointer; user-select: none; position: sticky; top: 0; background: #fff; }}
  th:hover {{ color: #f7a100; }}
  tr.new {{ background: #fffbe8; }}
  .badge {{ background: #f7a100; color: #fff; border-radius: 3px; font-size: .7rem; padding: .1rem .35rem; margin-left: .4rem; }}
  .photo img {{ width: 72px; height: 48px; object-fit: cover; border-radius: 4px; display: block; }}
  .addr a {{ color: #0071b3; text-decoration: none; }} .addr a:hover {{ text-decoration: underline; }}
  tr {{ cursor: pointer; }}
  tr.desc-row {{ cursor: auto; }} tr.desc-row td {{ white-space: normal; color: #444; background: #fafafa; max-width: 60rem; }}
</style>
</head>
<body>
<h1>funda-search · {html.escape(config['location'])}</h1>
<p class="meta">{len(rows)} listings · gegenereerd {datetime.now().strftime('%Y-%m-%d %H:%M')} · klik kolomkop om te sorteren, klik rij voor omschrijving</p>
<table id="t">
<thead><tr>
  <th></th><th>Adres</th><th>Wijk</th><th>Buurt</th><th>Prijs</th><th>Oppervlakte</th><th>€/m²</th>
  <th>Kamers</th><th>Label</th><th>Afstand centrum</th><th>Plattegrond</th><th>Geplaatst</th>
</tr></thead>
<tbody>
{chr(10).join(body_rows)}
</tbody>
</table>
<script>
const tbody = document.querySelector('#t tbody');
document.querySelectorAll('#t th').forEach((th, i) => th.addEventListener('click', () => {{
  const rows = [...tbody.querySelectorAll('tr:not(.desc-row)')];
  document.querySelectorAll('.desc-row').forEach(r => r.remove());
  const dir = th.dataset.dir = th.dataset.dir === 'asc' ? 'desc' : 'asc';
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
  const tr = e.target.closest('tr');
  if (!tr || tr.classList.contains('desc-row') || e.target.closest('a')) return;
  const next = tr.nextElementSibling;
  if (next && next.classList.contains('desc-row')) {{ next.remove(); return; }}
  const desc = tr.dataset.desc;
  if (!desc) return;
  const row = document.createElement('tr');
  row.className = 'desc-row';
  row.innerHTML = `<td colspan="12">${{desc.replace(/</g, '&lt;')}}</td>`;
  tr.after(row);
}});
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
