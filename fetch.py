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
from datetime import date, datetime, timezone
from pathlib import Path

from funda import Funda
from PIL import Image

ROOT = Path(__file__).parent
DATA_FILE = ROOT / "data" / "listings.json"
OVERVIEW_FILE = ROOT / "overview.html"
CONFIG_FILE = ROOT / "config.json"
PRICE_BANDS_FILE = ROOT / "reference" / "woningwaarde-2025.geojson"
PRICE_BANDS_YEAR = 2025
PRICE_BANDS_URL = (
    "https://maps.amsterdam.nl/open_geodata/geojson_lnglat.php/"
    "woningwaarde-2025.geojson?KAARTLAAG=WONINGWAARDE_2025&THEMA=woningwaarde"
)
PRICE_BANDS_SOURCE_URL = (
    "https://ckan.bertha.geodan.nl/nl/dataset/"
    "amsterdam-open-geodata-641-woningwaarde-2025"
)

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


def observed_at() -> str:
    """Return a stable, timezone-aware timestamp for a new observation."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def ensure_histories(listings: dict[str, dict]) -> bool:
    """Backfill one honest snapshot event for records created before history existed."""
    changed = False
    for listing in listings.values():
        first_observed = listing.get("first_seen") or listing.get("publication_date") or observed_at()
        if "price_history" not in listing:
            listing["price_history"] = []
            if listing.get("price") is not None:
                listing["price_history"].append(
                    {
                        "price": listing["price"],
                        "observed_at": first_observed,
                        "source": "legacy_snapshot",
                    }
                )
            changed = True
        if "status_history" not in listing:
            listing["status_history"] = []
            if listing.get("status"):
                listing["status_history"].append(
                    {
                        "status": listing["status"],
                        "observed_at": first_observed,
                        "source": "legacy_snapshot",
                    }
                )
            changed = True
    return changed


def record_observation(
    listing: dict,
    *,
    price: int | None = None,
    status: str | None = None,
    timestamp: str | None = None,
    source: str = "funda",
) -> bool:
    """Append price/status change events, suppressing unchanged poll observations."""
    timestamp = timestamp or observed_at()
    changed = False
    if price is not None:
        history = listing.setdefault("price_history", [])
        if not history or history[-1].get("price") != price:
            history.append({"price": price, "observed_at": timestamp, "source": source})
            changed = True
    if status:
        history = listing.setdefault("status_history", [])
        if not history or history[-1].get("status") != status:
            history.append({"status": status, "observed_at": timestamp, "source": source})
            changed = True
    return changed


def refresh_price_bands() -> None:
    """Download and validate the municipality's public 2025 price-band GeoJSON."""
    req = urllib.request.Request(PRICE_BANDS_URL, headers={"User-Agent": BROWSER_UA})
    payload = urllib.request.urlopen(req, timeout=30).read().decode()
    data = json.loads(payload)
    if data.get("type") != "FeatureCollection" or not data.get("features"):
        raise ValueError("price-band download is not a non-empty GeoJSON FeatureCollection")
    write_atomic(PRICE_BANDS_FILE, json.dumps(data, separators=(",", ":")))
    print(f"wrote {PRICE_BANDS_FILE.relative_to(ROOT)} with {len(data['features'])} bands")


def _point_in_ring(lon: float, lat: float, ring: list) -> bool:
    """Ray-casting point-in-polygon test for one GeoJSON linear ring."""
    inside = False
    previous = len(ring) - 1
    for current, coordinate in enumerate(ring):
        current_lon, current_lat = coordinate[:2]
        previous_lon, previous_lat = ring[previous][:2]
        crosses = (current_lat > lat) != (previous_lat > lat)
        if crosses:
            boundary_lon = (
                (previous_lon - current_lon)
                * (lat - current_lat)
                / (previous_lat - current_lat)
                + current_lon
            )
            if lon < boundary_lon:
                inside = not inside
        previous = current
    return inside


def _point_in_geometry(lon: float, lat: float, geometry: dict) -> bool:
    coordinates = geometry.get("coordinates") or []
    if geometry.get("type") == "Polygon":
        polygons = [coordinates]
    elif geometry.get("type") == "MultiPolygon":
        polygons = coordinates
    else:
        return False
    return any(
        polygon
        and _point_in_ring(lon, lat, polygon[0])
        and not any(_point_in_ring(lon, lat, hole) for hole in polygon[1:])
        for polygon in polygons
    )


def _parse_price_band(properties: dict) -> dict | None:
    label = str(properties.get("LABEL") or "").strip()
    lower = properties.get("SELECTIE")
    if not label or not isinstance(lower, (int, float)):
        return None
    upper = None
    if "-" in label:
        try:
            lower_text, upper_text = label.split("-", 1)
            lower, upper = int(lower_text.strip()), int(upper_text.strip())
        except ValueError:
            return None
    return {
        "year": PRICE_BANDS_YEAR,
        "lower": int(lower),
        "upper": int(upper) if upper is not None else None,
        "raw_label": label,
    }


def load_price_bands() -> list[dict]:
    if not PRICE_BANDS_FILE.exists():
        return []
    data = json.loads(PRICE_BANDS_FILE.read_text())
    bands = []
    for feature in data.get("features", []):
        band = _parse_price_band(feature.get("properties") or {})
        geometry = feature.get("geometry")
        if band and geometry:
            band["geometry"] = geometry
            bands.append(band)
    return bands


def price_band_for_listing(listing: dict, bands: list[dict]) -> dict | None:
    lat, lon = listing.get("lat"), listing.get("lon")
    if lat is None or lon is None:
        return None
    for band in bands:
        if _point_in_geometry(lon, lat, band["geometry"]):
            return {key: value for key, value in band.items() if key != "geometry"}
    return None


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

    record = {
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
    record_observation(record, price=price, status=record["status"])
    return record


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
    ensure_histories(listings)
    with Funda() as client:
        for l in todo:
            try:
                detail = client.listing(l["id"])
            except Exception as e:
                if "404" in str(e) or "not found" in str(e).lower():
                    record_observation(l, status="unavailable")
                    l["status"] = "unavailable"
                    changed += 1
                else:
                    print(f"  status check failed for {l['title']}: {e}", file=sys.stderr)
                time.sleep(DETAIL_FETCH_DELAY_S)
                continue
            new_status = str(detail.status or l.get("status") or "")
            if new_status != l.get("status"):
                print(f"  {l['title']}: {l.get('status') or '?'} -> {new_status}")
                record_observation(l, status=new_status)
                l["status"] = new_status
                changed += 1
            price = detail.price.amount if detail.price else None
            if price and price != l.get("price"):
                print(f"  {l['title']}: price {l.get('price')} -> {price}")
                record_observation(l, price=price)
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
    bands = load_price_bands()
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

    def format_euros(value: int) -> str:
        return f"€ {value:,}".replace(",", ".")

    def format_band(band: dict | None, asking_ppm2: int | None) -> tuple[str, int, str]:
        if not band:
            return "–", 0, ""
        lower, upper = band["lower"], band["upper"]
        label = (
            f"{format_euros(lower)}–{upper:,}/m²".replace(",", ".")
            if upper is not None
            else f"> {format_euros(lower)}/m²"
        )
        comparison = ""
        if asking_ppm2 is not None:
            if asking_ppm2 < lower:
                comparison = "below"
            elif upper is not None and asking_ppm2 > upper:
                comparison = "above"
            else:
                comparison = "within"
        return label, lower, comparison

    body_rows = []
    for l in rows:
        band = price_band_for_listing(l, bands)
        band_label, band_sort, band_comparison = format_band(band, l.get("price_per_m2"))
        history_data = json.dumps(
            {
                "prices": l.get("price_history") or [],
                "statuses": l.get("status_history") or [],
            },
            separators=(",", ":"),
        )
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
        price = format_euros(l["price"]) if l.get("price") else "–"
        ppm2 = format_euros(l["price_per_m2"]) if l.get("price_per_m2") else "–"
        desc = html.escape(l.get("description") or "")
        photo_urls = " ".join(l.get("photo_urls") or [])
        body_rows.append(
            f"""<tr data-id="{l['id']}" data-status="{html.escape(l.get('status') or '')}" data-desc="{desc}" data-fp="{html.escape(fp_data)}" data-lat="{l.get('lat') or ''}" data-lon="{l.get('lon') or ''}" data-photos="{html.escape(photo_urls)}" data-history="{html.escape(history_data)}">
  <td class="photo">{photo}</td>
  <td class="addr"><a href="{html.escape(l['url'])}" target="_blank">{html.escape(l['title'] or '?')}</a>{'<span class="uo-tag">under offer</span>' if l.get('status') == 'negotiations' else ''}</td>
  {td(l.get('wijk'))}
  {td(l.get('neighbourhood'))}
  <td data-sort="{l.get('price') or 0}">{price}</td>
  {td(l.get('living_area'), ' m²')}
  <td data-sort="{l.get('price_per_m2') or 0}">{ppm2}</td>
  <td class="band {band_comparison}" data-sort="{band_sort}" title="Amsterdam Woningwaardekaart 2025: interpolated transaction-price band">{band_label}{f'<span>{band_comparison}</span>' if band_comparison else ''}</td>
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
  td.band span {{ display: block; width: fit-content; margin-top: .15rem; padding: .05rem .3rem;
                  border-radius: 3px; font-size: .68rem; color: #555; background: #eee; }}
  td.band.below span {{ color: #176b36; background: #e4f4e9; }}
  td.band.above span {{ color: #8b2f28; background: #f9e7e5; }}
  .history {{ margin-bottom: 1rem; padding-bottom: .8rem; border-bottom: 1px solid #ddd; color: #555; }}
  .history strong {{ display: block; color: #222; margin-bottom: .3rem; }}
  .history .event {{ font-size: .8rem; line-height: 1.5; }}
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
  .fp-note {{ position: relative; margin: .3rem 0; }}
  .fp-note .fp-name {{ font-family: ui-monospace, monospace; font-size: .8em; }}
  .fp-note a {{ color: #0071b3; }}
  .fp-note .fp-peek {{ display: none; position: absolute; bottom: 1.4rem; left: 0; width: 260px; margin: 0;
                      background: #fff; box-shadow: 0 3px 14px rgba(0,0,0,.25); z-index: 5; }}
  .fp-note .fp-name:hover ~ .fp-peek, .fp-note a:hover ~ .fp-peek {{ display: block; }}
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
<p class="meta">2025 band = historic, interpolated transaction €/m² from the <a href="{PRICE_BANDS_SOURCE_URL}" target="_blank">Amsterdam Woningwaardekaart</a>; “below/within/above” compares the current asking €/m² with that unadjusted band.</p>
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
  <th>2025 band</th><th>Rooms</th><th>Energy</th><th>Distance</th><th>Listed</th><th data-defdesc="1">Score</th>
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
  const fps = JSON.parse(tr.dataset.fp || '[]');
  const history = JSON.parse(tr.dataset.history || '{{"prices":[],"statuses":[]}}');
  // interactive Floorplanner embed when funda has one (the static thumbnail is
  // only 900px); otherwise the full-res detected image, click-through to open.
  // detected plans come from a heuristic, so they carry a "not a floor plan"
  // flag button — flags are stored server-side to hide misfires and to collect
  // labeled mistakes for tuning the detector; flagged ones collapse to a
  // one-line note (with the filename, so several notes are tellable apart)
  // that stays undoable across fold reopens
  const fpHtml = fps.length
    ? fps.map(f => f.embed
        ? `<iframe class="fp-embed" loading="lazy" src="${{f.embed}}"></iframe>`
        : `<div class="fp-wrap" data-url="${{f.img}}" data-detected="${{f.detected ? 1 : 0}}"></div>`
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
  cell.colSpan = 13;
  const fold = document.createElement('div');
  fold.className = 'fold';
  const descDiv = document.createElement('div');
  descDiv.className = 'fold-desc';
  const historyDiv = document.createElement('div');
  historyDiv.className = 'history';
  const historyTitle = document.createElement('strong');
  historyTitle.textContent = 'Observed history';
  historyDiv.append(historyTitle);
  const events = [
    ...(history.prices || []).map(event => ({{...event, kind: 'price'}})),
    ...(history.statuses || []).map(event => ({{...event, kind: 'status'}})),
  ].sort((a, b) => (a.observed_at || '').localeCompare(b.observed_at || ''));
  if (!events.length) {{
    const empty = document.createElement('div');
    empty.className = 'event';
    empty.textContent = 'No observations yet';
    historyDiv.append(empty);
  }} else {{
    for (const event of events) {{
      const line = document.createElement('div');
      line.className = 'event';
      const value = event.kind === 'price'
        ? `asking € ${{Number(event.price).toLocaleString('nl-NL')}}`
        : `status ${{event.status}}`;
      line.textContent = `${{event.observed_at || '?'}} · ${{value}}`;
      historyDiv.append(line);
    }}
  }}
  const descriptionText = document.createElement('div');
  descriptionText.textContent = tr.dataset.desc || '';
  descDiv.append(historyDiv, descriptionText);
  const fpDiv = document.createElement('div');
  fpDiv.className = 'fold-right';
  fpDiv.innerHTML = photosLink + mapHtml + fpHtml;
  const gl = fpDiv.querySelector('[data-open-grid]');
  if (gl) gl.addEventListener('click', e => {{ e.preventDefault(); openGrid(tr); }});
  function renderFpWrap(wrap) {{
    const url = wrap.dataset.url;
    if ((fpFlags[id] || []).includes(url)) {{
      const name = url.split('/').pop();
      wrap.innerHTML = `<div class="none fp-note"><span class="fp-name">${{name}}</span> flagged as not a floor plan · <a href="#">undo</a>
        <img class="fp-peek" loading="lazy" src="${{url.replace('.jpg', '_360.jpg')}}" alt=""></div>`;
      wrap.querySelector('a').addEventListener('click', e => {{
        e.preventDefault(); saveFpFlag(id, url, false); renderFpWrap(wrap);
      }});
    }} else {{
      wrap.innerHTML = `<a href="${{url}}" target="_blank"><img src="${{url}}" loading="lazy" alt="floor plan"></a>${{
        wrap.dataset.detected === '1' ? '<button class="fp-flag" title="hide and record as a detector mistake">not a floor plan ✕</button>' : ''
      }}`;
      wrap.querySelector('.fp-flag')?.addEventListener('click', e => {{
        e.preventDefault(); saveFpFlag(id, url, true); renderFpWrap(wrap);
      }});
    }}
  }}
  fpDiv.querySelectorAll('.fp-wrap').forEach(renderFpWrap);
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
    parser.add_argument(
        "--refresh-price-bands",
        action="store_true",
        help="download Amsterdam's 2025 transaction-price bands, then re-render",
    )
    args = parser.parse_args()

    config = load_config()
    listings = load_listings()
    histories_changed = ensure_histories(listings)

    if args.refresh_price_bands:
        refresh_price_bands()

    if (
        args.backfill_floorplans
        or args.backfill_photos
        or args.refresh_status
        or args.refresh_price_bands
    ):
        if args.backfill_floorplans:
            backfill_floorplans(listings)
        if args.backfill_photos:
            backfill_photos(listings)
        if args.refresh_status:
            refresh_statuses(listings)
        histories_changed = histories_changed or bool(
            args.backfill_floorplans or args.backfill_photos or args.refresh_status
        )
    elif not args.render_only:
        _, new = fetch(config, listings)
        histories_changed = histories_changed or bool(new)

    if histories_changed:
        save_listings(listings)

    render(config, listings)


if __name__ == "__main__":
    main()
