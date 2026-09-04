#!/usr/bin/env python3
"""Fetch new funda listings, enrich them, and render an HTML overview.

Uses pyfunda for listing details and its search API when available. If Funda's
search API rejects anonymous requests, discovery falls back to the public
server-rendered search page. State lives in data/listings.json; every run only
fetches details for listings we haven't seen before, then regenerates the
table and map views.
"""

from __future__ import annotations

import argparse
import html
import io
import json
import math
import re
import sys
import time
import urllib.request
from datetime import date, datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlencode, urljoin, urlsplit

from curl_cffi import requests as curl_requests
from funda import Funda, SearchError
from PIL import Image

ROOT = Path(__file__).parent
DATA_FILE = ROOT / "data" / "listings.json"
OVERVIEW_FILE = ROOT / "overview.html"
CONFIG_FILE = ROOT / "config.json"
ROW_BATCH_SIZE = 128
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
DISTRICTS_FILE = ROOT / "reference" / "wijken.geojson"
DISTRICTS_URL = (
    "https://maps.amsterdam.nl/open_geodata/geojson_lnglat.php/"
    "wijken.geojson?KAARTLAAG=INDELING_WIJK&THEMA=gebiedsindeling"
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
FUNDA_SEARCH_URL = "https://www.funda.nl/zoeken/koop"
FUNDA_LISTING_PATH = re.compile(r"^/detail/koop/.+/(\d+)/?$")
FUNDA_BROCHURE_URL = re.compile(
    r'https://cloud\.funda\.nl/[^"\'<>\s]+\.pdf(?:\?[^"\'<>\s]*)?', re.IGNORECASE
)
WEBSITE_PAGE_SIZE = 15
WEBSITE_MAX_PAGES = 20


def map_path() -> Path:
    return ROOT / "map.html"


class _ListingLinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.urls: list[str] = []
        self._seen: set[str] = set()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "a":
            return
        href = dict(attrs).get("href")
        if not href:
            return
        parsed = urlsplit(urljoin(FUNDA_SEARCH_URL, href))
        if not FUNDA_LISTING_PATH.match(parsed.path):
            return
        url = f"https://www.funda.nl{parsed.path}"
        if url not in self._seen:
            self._seen.add(url)
            self.urls.append(url)


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


def refresh_districts() -> None:
    """Download and validate Amsterdam's public administrative-district GeoJSON."""
    req = urllib.request.Request(DISTRICTS_URL, headers={"User-Agent": BROWSER_UA})
    payload = urllib.request.urlopen(req, timeout=30).read().decode()
    data = json.loads(payload)
    if data.get("type") != "FeatureCollection" or not data.get("features"):
        raise ValueError("district download is not a non-empty GeoJSON FeatureCollection")
    if not all((feature.get("properties") or {}).get("Wijk") for feature in data["features"]):
        raise ValueError("district download has features without Wijk names")
    write_atomic(DISTRICTS_FILE, json.dumps(data, separators=(",", ":")))
    print(
        f"wrote {DISTRICTS_FILE.relative_to(ROOT)} "
        f"with {len(data['features'])} districts"
    )


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


def load_districts() -> list[dict]:
    if not DISTRICTS_FILE.exists():
        return []
    data = json.loads(DISTRICTS_FILE.read_text())
    return [
        {"name": feature["properties"]["Wijk"], "geometry": feature["geometry"]}
        for feature in data.get("features", [])
        if (feature.get("properties") or {}).get("Wijk") and feature.get("geometry")
    ]


def district_for_listing(listing: dict, districts: list[dict]) -> str | None:
    lat, lon = listing.get("lat"), listing.get("lon")
    if lat is None or lon is None:
        return None
    for district in districts:
        if _point_in_geometry(lon, lat, district["geometry"]):
            return district["name"]
    return None


def ensure_districts(listings: dict[str, dict]) -> int:
    """Backfill missing districts from Amsterdam's administrative boundaries."""
    districts = load_districts()
    changed = 0
    for listing in listings.values():
        if listing.get("wijk"):
            continue
        district = district_for_listing(listing, districts)
        if district:
            listing["wijk"] = district
            changed += 1
    if changed:
        print(f"district backfill: {changed} listings")
    return changed


def write_atomic(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text)
    tmp.replace(path)


def row_batch_path(index: int) -> Path:
    """Return the generated HTML fragment for a one-based listing batch."""
    if index < 1:
        raise ValueError("row batch index must be positive")
    return ROOT / "data" / "overview_batches" / f"{index}.html"


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


def public_listing_id(url: str | None) -> str | None:
    """Extract Funda's public listing ID from a canonical detail URL."""
    if not url:
        return None
    match = FUNDA_LISTING_PATH.match(urlsplit(url).path)
    return match.group(1) if match else None


def website_search_url(config: dict, page: int = 1) -> str:
    """Build the public website equivalent of the configured API search."""
    filters = config.get("filters", {})
    params: list[tuple[str, str | int]] = [("selected_area", config["location"])]

    def add_range(name: str, lower_key: str, upper_key: str) -> None:
        lower, upper = filters.get(lower_key), filters.get(upper_key)
        if lower is not None or upper is not None:
            lower_text = "" if lower is None else lower
            upper_text = "" if upper is None else upper
            params.append((name, f"{lower_text}-{upper_text}"))

    add_range("price", "min_price", "max_price")
    add_range("floor_area", "min_area", "max_area")
    add_range("bedrooms", "min_bedrooms", "max_bedrooms")
    params.append(("sort", "publish_date_utc_desc"))
    if page > 1:
        params.append(("page", page))
    return f"{FUNDA_SEARCH_URL}?{urlencode(params)}"


def parse_listing_urls(page: str) -> list[str]:
    parser = _ListingLinkParser()
    parser.feed(page)
    return parser.urls


def parse_brochure_url(page: str) -> str | None:
    """Extract the direct Funda-hosted brochure PDF from a detail page."""
    match = FUNDA_BROCHURE_URL.search(page)
    if not match:
        return None
    url = html.unescape(match.group(0))
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname != "cloud.funda.nl":
        return None
    return url


def fetch_brochure_url(listing_url: str, *, session=None) -> str | None:
    """Fetch a public Funda detail page and return its brochure PDF URL."""
    client = session or curl_requests.Session()
    response = client.get(
        listing_url,
        impersonate="chrome124",
        timeout=30,
        allow_redirects=True,
    )
    if response.status_code != 200:
        raise RuntimeError(f"brochure lookup failed with status {response.status_code}")
    if "Je bent bijna op de pagina" in response.text:
        raise RuntimeError("brochure lookup returned a bot challenge")
    return parse_brochure_url(response.text)


def enrich_brochure(record: dict, *, session=None) -> None:
    """Best-effort brochure discovery that never drops an otherwise valid listing."""
    if record.get("brochure_url") or not record.get("url"):
        return
    try:
        record["brochure_url"] = fetch_brochure_url(record["url"], session=session)
    except Exception as error:
        label = record.get("title") or record["url"]
        print(f"  brochure lookup failed for {label}: {error}", file=sys.stderr)


def search_website(
    config: dict,
    listings: dict[str, dict],
    *,
    session=None,
) -> list[str]:
    """Discover listing URLs from Funda's anonymous server-rendered search."""
    known_ids = {
        listing_id
        for listing in listings.values()
        if (listing_id := public_listing_id(listing.get("url")))
    }
    max_pages = max(1, int(config.get("website_max_pages", WEBSITE_MAX_PAGES)))
    initial_pages = max(1, min(int(config.get("pages", 3)), max_pages))
    client = session or curl_requests.Session()
    found: list[str] = []
    seen: set[str] = set()

    for page_number in range(1, max_pages + 1):
        response = client.get(
            website_search_url(config, page_number),
            impersonate="chrome124",
            timeout=30,
            allow_redirects=True,
        )
        if response.status_code != 200:
            raise RuntimeError(f"website search failed with status {response.status_code}")
        if (
            "Je bent bijna op de pagina" in response.text
            or "__NUXT_DATA__" not in response.text
        ):
            raise RuntimeError("website search returned a bot challenge")
        page_urls = parse_listing_urls(response.text)
        if not page_urls:
            raise RuntimeError("website search returned no listing links")

        for url in page_urls:
            if url not in seen:
                seen.add(url)
                found.append(url)

        page_ids = {
            listing_id
            for url in page_urls
            if (listing_id := public_listing_id(url))
        }
        if known_ids and page_ids and page_ids <= known_ids:
            break
        if not known_ids and page_number >= initial_pages:
            break
        if len(page_urls) < WEBSITE_PAGE_SIZE:
            break

    return found


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

    lat = lon = distance_km = work_distance_km = None
    if detail.location:
        lat, lon = detail.location.latitude, detail.location.longitude
        center = config["center"]
        distance_km = round(haversine_km(lat, lon, center["lat"], center["lon"]), 1)
        work = config.get("work")
        if work:
            work_distance_km = round(
                haversine_km(lat, lon, work["lat"], work["lon"]), 1
            )

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
        "work_distance_km": work_distance_km,
        "floorplans": floorplans,
        "photo_url": photo_url,
        "photo_urls": photos,
        "description": detail.description,
        "status": str(detail.status or item.status or ""),
    }
    record_observation(record, price=price, status=record["status"])
    return record


def fetch(config: dict, listings: dict[str, dict]) -> tuple[int, int]:
    website_client = curl_requests.Session()
    with Funda() as client:
        try:
            items = search_pages(client, config)
        except SearchError as error:
            print(f"search API unavailable ({error}); using website fallback")
            urls = search_website(config, listings, session=website_client)
            known_ids = {
                listing_id
                for listing in listings.values()
                if (listing_id := public_listing_id(listing.get("url")))
            }
            new_urls = [url for url in urls if public_listing_id(url) not in known_ids]
            print(f"website search returned {len(urls)} listings, {len(new_urls)} new")

            for n, url in enumerate(new_urls, 1):
                try:
                    detail = client.listing(url)
                    key = str(detail.global_id or detail.id)
                    if key in listings:
                        continue
                    record = build_record(detail, detail, config)
                    record["url"] = url
                    enrich_brochure(record, session=website_client)
                    listings[key] = record
                    print(f"  [{n}/{len(new_urls)}] {detail.title}")
                except Exception as detail_error:
                    print(
                        f"  [{n}/{len(new_urls)}] {url} FAILED: {detail_error}",
                        file=sys.stderr,
                    )
                time.sleep(DETAIL_FETCH_DELAY_S)
            return len(urls), len(new_urls)

        new_items = [i for i in items if str(i.global_id or i.id) not in listings]
        print(f"search returned {len(items)} listings, {len(new_items)} new")

        for n, item in enumerate(new_items, 1):
            key = str(item.global_id or item.id)
            try:
                detail = client.listing(item.global_id or item.id)
                record = build_record(item, detail, config)
                enrich_brochure(record, session=website_client)
                listings[key] = record
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


def render_map(config: dict, rows: list[dict]) -> None:
    """Render the lightweight, filterable map view from the overview dataset."""
    map_listings = []
    missing_coordinates = 0
    for listing in rows:
        lat, lon = listing.get("lat"), listing.get("lon")
        if lat is None or lon is None:
            missing_coordinates += 1
            continue
        map_listings.append(
            {
                "id": str(listing["id"]),
                "title": listing.get("title") or "?",
                "url": listing.get("url") or "",
                "photo": listing.get("photo_url") or "",
                "lat": lat,
                "lon": lon,
                "price": listing.get("price"),
                "area": listing.get("living_area"),
                "price_per_m2": listing.get("price_per_m2"),
                "rooms": listing.get("rooms"),
                "energy": listing.get("energy_label"),
                "district": listing.get("wijk"),
                "neighbourhood": listing.get("neighbourhood"),
                "market_status": listing.get("status") or "",
                "market_gone": listing.get("status") in GONE_STATUSES,
                "date": listing.get("publication_date") or listing.get("first_seen") or "",
            }
        )

    listing_json = json.dumps(
        map_listings, ensure_ascii=False, separators=(",", ":")
    ).replace("<", "\\u003c")
    location = html.escape(config.get("location") or "Amsterdam")
    page = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>funda-search · __LOCATION__ map</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<style>
  :root { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; color: #1a1a1a; }
  * { box-sizing: border-box; }
  html, body { height: 100%; margin: 0; }
  body { display: grid; grid-template-rows: auto minmax(22rem, 1fr); background: #f6f6f4; }
  header { position: relative; z-index: 1000; padding: .85rem 1.15rem .75rem; background: rgba(255,255,255,.97);
           border-bottom: 1px solid #ddd; box-shadow: 0 1px 5px rgba(0,0,0,.08); }
  .topline { display: flex; align-items: baseline; justify-content: space-between; gap: 1rem; }
  h1 { margin: 0; font-size: 1.25rem; }
  .views { display: flex; gap: .25rem; }
  .views a { padding: .3rem .6rem; border: 1px solid #ccc; border-radius: 4px; color: #555; text-decoration: none;
             font-size: .82rem; background: #fff; }
  .views a.active { color: #fff; border-color: #0071b3; background: #0071b3; }
  .controls { display: flex; flex-wrap: wrap; align-items: end; gap: .6rem 1rem; margin-top: .7rem; font-size: .8rem; }
  .control { display: grid; gap: .2rem; color: #555; }
  .control span { font-size: .7rem; font-weight: 600; letter-spacing: .02em; text-transform: uppercase; }
  select, button, input[type="search"] { min-height: 2rem; border: 1px solid #bbb; border-radius: 4px; background: #fff; color: #333;
                                          padding: .3rem .55rem; font: inherit; }
  input[type="search"] { width: 16rem; }
  button { cursor: pointer; }
  button:hover { border-color: #f7a100; color: #9b6200; }
  label.check { display: flex; align-items: center; gap: .3rem; min-height: 2rem; cursor: pointer; white-space: nowrap; }
  #summary { margin-left: auto; min-height: 2rem; display: flex; align-items: center; color: #666; white-space: nowrap; }
  #map { width: 100%; height: 100%; background: #dce5e9; }
  #mapError { position: fixed; left: 50%; top: 55%; z-index: 1100; transform: translate(-50%,-50%); max-width: 30rem;
              padding: .8rem 1rem; border: 1px solid #c77; border-radius: 6px; color: #7a2020; background: #fff; }
  #mapError[hidden] { display: none; }
  .legend { display: flex; align-items: center; gap: .35rem; padding: .35rem .45rem; border-radius: 4px;
            background: rgba(255,255,255,.94); box-shadow: 0 1px 5px rgba(0,0,0,.2); color: #555; font-size: .72rem; }
  .legend .dot { width: .85rem; height: .85rem; border: 2px solid #fff; border-radius: 50%; box-shadow: 0 0 0 1px #777; }
  .listing-marker { display: grid; place-items: center; width: 28px !important; height: 28px !important; margin: -14px 0 0 -14px !important;
                    border: 2px solid #fff; border-radius: 50%; color: #fff; box-shadow: 0 1px 4px rgba(0,0,0,.55);
                    font: 700 12px/1 -apple-system, sans-serif; }
  .score-u { background: #0071b3; }
  .score-0 { background: #777; }
  .score-1 { background: #8b9aa5; }
  .score-2 { background: #e28a00; }
  .score-3 { background: #2d8a4e; }
  .listing-marker.tracked { box-shadow: 0 0 0 3px #222, 0 2px 6px rgba(0,0,0,.45); }
  .popup { width: 15rem; }
  .popup img { width: 100%; height: 8rem; object-fit: cover; border-radius: 4px; margin-bottom: .45rem; background: #eee; }
  .popup h2 { margin: 0 0 .3rem; font-size: 1rem; }
  .popup .meta { color: #555; line-height: 1.45; }
  .popup .status { margin-top: .35rem; color: #333; font-weight: 600; }
  .popup a { display: inline-block; margin-top: .55rem; color: #0071b3; }
  @media (max-width: 800px) {
    header { padding: .7rem; }
    .controls { gap: .45rem .7rem; }
    #summary { width: 100%; margin-left: 0; min-height: auto; }
  }
</style>
</head>
<body>
<header>
  <div class="topline">
    <h1>funda-search · __LOCATION__</h1>
    <nav class="views" aria-label="View"><a href="overview.html">table</a><a href="map.html" class="active">map</a></nav>
  </div>
  <div class="controls">
    <label class="control"><span>Search</span><input type="search" id="search" placeholder="address, district, or neighbourhood" autocomplete="off"></label>
    <label class="control"><span>Listings</span><select id="scope">
      <option value="128">128 most recent</option><option value="all">all</option>
    </select></label>
    <label class="control"><span>Minimum score</span><select id="minScore">
      <option value="">any</option><option value="1">1+</option><option value="2">2+</option><option value="3">3 only</option>
    </select></label>
    <label class="control"><span>Tracking status</span><select id="tracking">
      <option value="">any</option><option value="untracked">untracked</option><option value="call">call</option>
      <option value="viewing_requested">viewing requested</option><option value="viewing_planned">viewing planned</option>
      <option value="viewed">viewed</option><option value="bid">bid</option><option value="sold">sold</option><option value="bought">bought</option>
    </select></label>
    <label class="check" title="Default range is €500k–€750k"><input type="checkbox" id="widerPrice"> wider €400k–€850k</label>
    <label class="check"><input type="checkbox" id="hideRated"> hide rated</label>
    <label class="check"><input type="checkbox" id="hideNo" checked> hide not interesting</label>
    <label class="check"><input type="checkbox" id="hideUO" checked> hide under offer</label>
    <label class="check"><input type="checkbox" id="hideSold" checked> hide sold</label>
    <button type="button" id="fit">fit markers</button>
    <button type="button" id="reset">reset</button>
    <span id="summary"></span>
  </div>
</header>
<div id="map" role="application" aria-label="Map of Amsterdam listings"></div>
<div id="mapError" hidden></div>
<script id="listingData" type="application/json">__LISTINGS__</script>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script>
const listings = JSON.parse(document.getElementById('listingData').textContent);
const totalListingCount = __TOTAL__;
const missingCoordinateCount = __MISSING__;
const controls = {
  search: document.getElementById('search'),
  scope: document.getElementById('scope'),
  minScore: document.getElementById('minScore'),
  tracking: document.getElementById('tracking'),
  widerPrice: document.getElementById('widerPrice'),
  hideRated: document.getElementById('hideRated'),
  hideNo: document.getElementById('hideNo'),
  hideUO: document.getElementById('hideUO'),
  hideSold: document.getElementById('hideSold'),
};
let ratings = {};
let trackingStatuses = {};
let map;
let markerLayer;

function euro(value) {
  return value === null || value === undefined ? '–' : `€ ${Number(value).toLocaleString('nl-NL')}`;
}

function scoreFor(listing) {
  return Object.prototype.hasOwnProperty.call(ratings, listing.id) ? Number(ratings[listing.id]) : null;
}

function trackingFor(listing) { return trackingStatuses[listing.id] || ''; }

function matches(listing) {
  const score = scoreFor(listing);
  const tracking = trackingFor(listing);
  const minScore = controls.minScore.value;
  const query = controls.search.value.trim().toLocaleLowerCase('nl-NL');
  const searchable = [listing.title, listing.district, listing.neighbourhood]
    .filter(Boolean).join(' ').toLocaleLowerCase('nl-NL');
  if (query && !searchable.includes(query)) return false;
  if (!controls.widerPrice.checked
      && (!listing.price || listing.price < 500000 || listing.price > 750000)) return false;
  if (controls.hideRated.checked && score !== null) return false;
  if (controls.hideNo.checked && score === 0) return false;
  if (minScore && (score === null || score < Number(minScore))) return false;
  if (controls.hideUO.checked && listing.market_status === 'negotiations') return false;
  const sold = tracking === 'sold' || (listing.market_gone && tracking !== 'bought');
  if (controls.hideSold.checked && sold) return false;
  if (controls.tracking.value === 'untracked' && tracking) return false;
  if (controls.tracking.value && controls.tracking.value !== 'untracked' && tracking !== controls.tracking.value) return false;
  return true;
}

function popupFor(listing) {
  const root = document.createElement('div');
  root.className = 'popup';
  if (listing.photo) {
    const image = document.createElement('img');
    image.loading = 'lazy';
    image.src = listing.photo;
    image.alt = '';
    root.append(image);
  }
  const title = document.createElement('h2');
  title.textContent = listing.title;
  root.append(title);
  const facts = [euro(listing.price)];
  if (listing.area) facts.push(`${listing.area} m²`);
  if (listing.price_per_m2) facts.push(`${euro(listing.price_per_m2)}/m²`);
  if (listing.energy) facts.push(`energy ${listing.energy}`);
  const meta = document.createElement('div');
  meta.className = 'meta';
  meta.textContent = facts.join(' · ');
  root.append(meta);
  const place = document.createElement('div');
  place.className = 'meta';
  place.textContent = [listing.district, listing.neighbourhood].filter(Boolean).join(' · ');
  root.append(place);
  const score = scoreFor(listing);
  const tracking = trackingFor(listing);
  const status = document.createElement('div');
  status.className = 'status';
  status.textContent = `score ${score === null ? 'unrated' : score}${tracking ? ` · ${tracking.replaceAll('_', ' ')}` : ''}`;
  root.append(status);
  const link = document.createElement('a');
  link.href = listing.url;
  link.target = '_blank';
  link.rel = 'noopener';
  link.textContent = 'open on Funda';
  root.append(link);
  return root;
}

function markerFor(listing) {
  const score = scoreFor(listing);
  const tracking = trackingFor(listing);
  const icon = L.divIcon({
    className: '',
    html: `<div class="listing-marker score-${score === null ? 'u' : score}${tracking ? ' tracked' : ''}">${score === null ? '·' : score}</div>`,
    iconSize: [28, 28],
    iconAnchor: [14, 14],
    popupAnchor: [0, -12],
  });
  return L.marker([listing.lat, listing.lon], {icon, title: listing.title}).bindPopup(() => popupFor(listing), {maxWidth: 260});
}

function selectedListings() {
  const filtered = listings.filter(matches);
  return controls.scope.value === '128' ? filtered.slice(0, 128) : filtered;
}

function renderMarkers(fit = false) {
  if (!markerLayer) return;
  markerLayer.clearLayers();
  const selected = selectedListings();
  for (const listing of selected) markerLayer.addLayer(markerFor(listing));
  document.getElementById('summary').textContent =
    `${selected.length} shown · ${listings.length} geocoded · ${missingCoordinateCount} without coordinates`;
  if (fit && selected.length) map.fitBounds(markerLayer.getBounds().pad(.12), {maxZoom: 14});
}

async function loadSharedState() {
  const localRatings = JSON.parse(localStorage.getItem('funda-ratings') || '{}');
  const localTracking = JSON.parse(localStorage.getItem('funda-tracking-statuses') || '{}');
  try {
    const response = await fetch('ratings.json', {cache: 'no-store'});
    ratings = response.ok ? await response.json() : localRatings;
  } catch { ratings = localRatings; }
  try {
    const response = await fetch('tracking-statuses.json', {cache: 'no-store'});
    trackingStatuses = response.ok ? await response.json() : localTracking;
  } catch { trackingStatuses = localTracking; }
}

function resetFilters() {
  controls.search.value = '';
  controls.scope.value = '128';
  controls.minScore.value = '';
  controls.tracking.value = '';
  controls.widerPrice.checked = false;
  controls.hideRated.checked = false;
  controls.hideNo.checked = true;
  controls.hideUO.checked = true;
  controls.hideSold.checked = true;
  renderMarkers(false);
  map.setView([52.3676, 4.9041], 12);
}

async function start() {
  if (typeof L === 'undefined') {
    const error = document.getElementById('mapError');
    error.textContent = 'The map library could not be loaded. Check the network connection and reload.';
    error.hidden = false;
    return;
  }
  map = L.map('map', {preferCanvas: true}).setView([52.3676, 4.9041], 12);
  L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {
    maxZoom: 19,
    attribution: '&copy; OpenStreetMap contributors',
  }).addTo(map);
  markerLayer = L.featureGroup().addTo(map);
  const legend = L.control({position: 'bottomleft'});
  legend.onAdd = () => {
    const node = L.DomUtil.create('div', 'legend');
    node.innerHTML = '<span class="dot score-u"></span>unrated <span class="dot score-0"></span>0 <span class="dot score-1"></span>1 <span class="dot score-2"></span>2 <span class="dot score-3"></span>3';
    return node;
  };
  legend.addTo(map);
  await loadSharedState();
  renderMarkers(false);
}

for (const control of [controls.scope, controls.widerPrice, controls.hideNo, controls.hideUO, controls.hideSold]) {
  control.addEventListener('change', () => renderMarkers(false));
}
let searchTimer;
controls.search.addEventListener('input', () => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => renderMarkers(false), 120);
});
controls.minScore.addEventListener('change', () => {
  if (controls.minScore.value) controls.hideRated.checked = false;
  renderMarkers(false);
});
controls.hideRated.addEventListener('change', () => {
  if (controls.hideRated.checked) controls.minScore.value = '';
  renderMarkers(false);
});
controls.tracking.addEventListener('change', () => {
  if (controls.tracking.value === 'sold') controls.hideSold.checked = false;
  renderMarkers(false);
});
document.getElementById('fit').addEventListener('click', () => renderMarkers(true));
document.getElementById('reset').addEventListener('click', resetFilters);
start();
</script>
</body>
</html>
"""
    page = (
        page.replace("__LOCATION__", location)
        .replace("__LISTINGS__", listing_json)
        .replace("__TOTAL__", str(len(rows)))
        .replace("__MISSING__", str(missing_coordinates))
    )
    write_atomic(map_path(), page)


def render(config: dict, listings: dict[str, dict]) -> None:
    bands = load_price_bands()
    filters = config.get("filters", {})
    min_area = filters.get("min_area")
    min_price = filters.get("min_price")
    max_price = filters.get("max_price")
    min_bedrooms = filters.get("min_bedrooms")

    def visible(l: dict) -> bool:
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

    def clipped_td(value, class_name: str) -> str:
        if value is None or value == "":
            return f'<td class="{class_name}">–</td>'
        escaped = html.escape(str(value))
        return f'<td class="{class_name}" title="{escaped}">{escaped}</td>'

    def format_euros(value: int) -> str:
        return f"€ {value:,}".replace(",", ".")

    def distance_to(l: dict, destination: dict | None, stored_key: str) -> float | None:
        lat, lon = l.get("lat"), l.get("lon")
        if destination and lat is not None and lon is not None:
            return round(
                haversine_km(lat, lon, destination["lat"], destination["lon"]), 1
            )
        return l.get(stored_key)

    def distance_td(value: float | None) -> str:
        if value is None:
            return '<td data-sort="999">–</td>'
        return f'<td data-sort="{value}">{value} km</td>'

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
        center_distance = distance_to(l, config.get("center"), "distance_km")
        work_distance = distance_to(l, config.get("work"), "work_distance_km")
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
        search_text = " ".join(
            str(value)
            for value in (l.get("title"), l.get("wijk"), l.get("neighbourhood"))
            if value
        ).lower()
        body_rows.append(
            f"""<tr data-id="{l['id']}" data-search="{html.escape(search_text)}" data-district="{html.escape(l.get('wijk') or '')}" data-price="{l.get('price') or 0}" data-status="{html.escape(l.get('status') or '')}" data-market-gone="{int(l.get('status') in GONE_STATUSES)}" data-desc="{desc}" data-fp="{html.escape(fp_data)}" data-lat="{l.get('lat') or ''}" data-lon="{l.get('lon') or ''}" data-photos="{html.escape(photo_urls)}" data-history="{html.escape(history_data)}" data-brochure="{html.escape(l.get('brochure_url') or '')}">
  <td class="photo">{photo}</td>
  <td class="addr"><a href="{html.escape(l['url'])}" target="_blank" title="{html.escape(l['title'] or '?')}">{html.escape(l['title'] or '?')}</a>{'<span class="uo-tag">under offer</span>' if l.get('status') == 'negotiations' else ''}</td>
  <td class="tracking" data-sort=""><select class="tracking-select" aria-label="Tracking status for {html.escape(l['title'] or '?')}" aria-describedby="statusLegend">
    <option value="">—</option>
    <option value="call">call</option>
    <option value="viewing_requested">requested</option>
    <option value="viewing_planned">planned</option>
    <option value="viewed">viewed</option>
    <option value="bid">bid</option>
    <option value="sold">sold</option>
    <option value="bought">bought</option>
  </select></td>
  {clipped_td(l.get('wijk'), 'district')}
  {clipped_td(l.get('neighbourhood'), 'neighbourhood')}
  <td data-sort="{l.get('price') or 0}">{price}</td>
  {td(l.get('living_area'), ' m²')}
  <td data-sort="{l.get('price_per_m2') or 0}">{ppm2}</td>
  <td class="band {band_comparison}" data-sort="{band_sort}" title="Amsterdam Woningwaardekaart 2025: interpolated transaction-price band">{band_label}{f'<span>{band_comparison}</span>' if band_comparison else ''}</td>
  {td(l.get('rooms'))}
  {td(l.get('energy_label'))}
  {distance_td(center_distance)}
  {distance_td(work_distance)}
  <td class="listed" data-date="{html.escape(l.get('publication_date') or '')}" title="{html.escape(l.get('publication_date') or '')}">–</td>
  <td class="score" data-sort="-1"><div class="rate">
    <button data-s="0" title="reviewed, not interesting">✕</button>
    <button data-s="1">1</button>
    <button data-s="2">2</button>
    <button data-s="3">3</button>
  </div></td>
</tr>"""
        )

    initial_body_rows = body_rows[:ROW_BATCH_SIZE]
    row_batches = [
        body_rows[start : start + ROW_BATCH_SIZE]
        for start in range(ROW_BATCH_SIZE, len(body_rows), ROW_BATCH_SIZE)
    ]
    batch_directory = row_batch_path(1).parent
    batch_directory.mkdir(parents=True, exist_ok=True)
    active_batch_files = set()
    for index, batch in enumerate(row_batches, start=1):
        batch_file = row_batch_path(index)
        write_atomic(batch_file, "\n".join(batch))
        active_batch_files.add(batch_file)
    for stale_batch in batch_directory.glob("*.html"):
        if stale_batch not in active_batch_files:
            stale_batch.unlink()

    districts = sorted(
        {str(listing.get("wijk")) for listing in rows if listing.get("wijk")},
        key=str.casefold,
    )
    district_json = json.dumps(districts, ensure_ascii=False, separators=(",", ":")).replace(
        "<", "\\u003c"
    )

    page = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>funda-search · {html.escape(config['location'])}</title>
<style>
  :root {{ font-family: -apple-system, system-ui, sans-serif; }}
  body {{ margin: 1.25rem; color: #1a1a1a; }}
  h1 {{ font-size: 1.3rem; }} .meta {{ color: #666; font-size: .85rem; }}
  .page-head {{ display: flex; align-items: baseline; justify-content: space-between; gap: 1rem; }}
  .views {{ display: flex; gap: .25rem; }}
  .views a {{ padding: .3rem .6rem; border: 1px solid #ccc; border-radius: 4px; color: #555;
              text-decoration: none; font-size: .82rem; background: #fff; }}
  .views a.active {{ color: #fff; border-color: #0071b3; background: #0071b3; }}
  .controls {{ margin: .6rem 0 1rem; font-size: .85rem; display: flex; flex-wrap: wrap; gap: .7rem .8rem; align-items: center; color: #333; }}
  .controls label {{ cursor: pointer; user-select: none; }}
  .controls .search-field {{ display: flex; align-items: center; gap: .45rem; cursor: default; }}
  .controls input[type="search"] {{ width: 16rem; min-height: 2rem; border: 1px solid #bbb; border-radius: 4px;
                                     padding: .3rem .55rem; color: #333; background: #fff; font: inherit; }}
  .district-filter {{ position: relative; }}
  .district-filter > summary {{ min-height: 2rem; display: flex; align-items: center; border: 1px solid #bbb; border-radius: 4px;
                                padding: .3rem .55rem; background: #fff; cursor: pointer; user-select: none; list-style: none; }}
  .district-filter > summary::-webkit-details-marker {{ display: none; }}
  .district-filter > summary::after {{ content: '▾'; margin-left: .45rem; color: #777; }}
  .district-filter[open] > summary {{ border-color: #f7a100; }}
  .district-menu {{ position: absolute; top: calc(100% + .3rem); left: 0; z-index: 6; width: 18rem; max-height: 24rem; overflow: auto;
                    padding: .65rem; border: 1px solid #bbb; border-radius: 5px; background: #fff; box-shadow: 0 4px 16px rgba(0,0,0,.18); }}
  .district-menu strong {{ display: block; margin-bottom: .4rem; font-size: .78rem; }}
  .district-options {{ display: grid; gap: .25rem; }}
  .district-options label {{ display: flex; gap: .4rem; align-items: baseline; cursor: pointer; }}
  .district-menu button {{ margin-top: .65rem; border: 1px solid #bbb; border-radius: 4px; padding: .3rem .5rem;
                           background: #fff; color: #555; cursor: pointer; font: inherit; }}
  table {{ border-collapse: collapse; width: 100%; font-size: .82rem; }}
  th, td {{ text-align: left; padding: .4rem .45rem; border-bottom: 1px solid #e5e5e5; white-space: nowrap; }}
  th {{ cursor: pointer; user-select: none; position: sticky; top: 0; background: #fff; }}
  th:hover {{ color: #f7a100; }}
  .photo img {{ width: 72px; height: 48px; object-fit: cover; border-radius: 4px; display: block; }}
  th.addr, td.addr {{ width: 10rem; max-width: 10rem; }}
  .addr a {{ color: #0071b3; text-decoration: none; display: block; overflow: hidden;
             text-overflow: ellipsis; white-space: nowrap; }}
  .addr a:hover {{ text-decoration: underline; }}
  th.tracking, td.tracking {{ width: 7rem; max-width: 7rem; }}
  th.district, td.district {{ width: 7.5rem; max-width: 7.5rem; overflow: hidden;
                              text-overflow: ellipsis; }}
  th.neighbourhood, td.neighbourhood {{ width: 9rem; max-width: 9rem; overflow: hidden;
                                        text-overflow: ellipsis; }}
  td.band span {{ display: block; width: fit-content; margin-top: .15rem; padding: .05rem .3rem;
                  border-radius: 3px; font-size: .68rem; color: #555; background: #eee; }}
  td.band.below span {{ color: #176b36; background: #e4f4e9; }}
  td.band.above span {{ color: #8b2f28; background: #f9e7e5; }}
  .history {{ margin-bottom: 1rem; padding-bottom: .8rem; border-bottom: 1px solid #ddd; color: #555; }}
  .history strong {{ display: block; color: #222; margin-bottom: .3rem; }}
  .history .event {{ font-size: .8rem; line-height: 1.5; }}
  .analysis {{ margin-bottom: 1rem; padding: .8rem; border: 1px solid #ddd; border-radius: 6px; background: #fff; }}
  .analysis-head {{ display: flex; align-items: center; justify-content: space-between; gap: .8rem; margin-bottom: .7rem; }}
  .analysis-head strong {{ color: #222; }}
  .analysis-head span, .analysis-meta {{ color: #777; font-size: .75rem; }}
  .analysis-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(10rem, 1fr)); gap: .5rem; }}
  .analysis-card {{ border: 1px solid #e5e5e5; border-radius: 5px; padding: .55rem; font-size: .8rem; line-height: 1.4; }}
  .analysis-card h4 {{ margin: 0 0 .3rem; font-size: .78rem; text-transform: uppercase; letter-spacing: .03em; color: #666; }}
  .analysis-value {{ font-weight: 600; color: #222; margin-bottom: .25rem; }}
  .risk {{ display: inline-block; margin-left: .3rem; padding: .05rem .3rem; border-radius: 3px; font-size: .68rem; font-weight: 600; }}
  .risk-low {{ background: #def1e4; color: #25633a; }}
  .risk-medium {{ background: #fff0c9; color: #795900; }}
  .risk-high {{ background: #f8dddd; color: #8b2525; }}
  .risk-unknown {{ background: #eee; color: #666; }}
  .analysis-list {{ margin: .65rem 0 0; padding-left: 1.2rem; font-size: .8rem; line-height: 1.4; }}
  .analysis-sources {{ margin-top: .6rem; font-size: .75rem; }}
  .analysis-sources a {{ margin-right: .7rem; }}
  .analysis button {{ border: 1px solid #0071b3; background: #fff; color: #0071b3; border-radius: 4px; padding: .3rem .55rem; cursor: pointer; }}
  .analysis button:disabled {{ border-color: #bbb; color: #888; cursor: default; }}
  tr {{ cursor: pointer; }}
  .rate {{ display: flex; gap: .2rem; }}
  .rate button {{ width: 1.7rem; height: 1.7rem; border: 1px solid #ccc; background: #fff; border-radius: 4px;
                  cursor: pointer; font-size: .8rem; color: #555; }}
  .rate button:hover {{ border-color: #f7a100; color: #f7a100; }}
  .rate button.on {{ background: #f7a100; border-color: #f7a100; color: #fff; }}
  .rate button[data-s="0"].on {{ background: #999; border-color: #999; }}
  .tracking-select {{ width: 7rem; border: 1px solid #ccc; background: #fff; border-radius: 4px;
                      padding: .3rem .4rem; color: #444; font: inherit; cursor: pointer; }}
  tr[data-tracking="sold"] .tracking-select {{ color: #777; }}
  tr[data-tracking="bought"] .tracking-select {{ border-color: #f7a100; color: #9b6200; }}
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
  body.hide-sold tr[data-tracking="sold"],
  body.hide-sold tr[data-market-gone="1"]:not([data-tracking="bought"]) {{ display: none; }}
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
  .load-more {{ display: flex; justify-content: center; align-items: center; gap: .7rem; margin: 1rem 0 2rem; }}
  .load-more button {{ border: 1px solid #aaa; background: #fff; color: #333; border-radius: 4px; padding: .45rem .8rem; cursor: pointer; font: inherit; }}
  .load-more button:hover {{ border-color: #f7a100; color: #9b6200; }}
  .load-more button:disabled {{ border-color: #ddd; color: #999; cursor: default; }}
  .load-more[hidden], .load-more button[hidden] {{ display: none; }}
</style>
</head>
<body class="hide-sold">
<div class="page-head"><h1>funda-search · {html.escape(config['location'])}</h1>
  <nav class="views" aria-label="View"><a href="overview.html" class="active">table</a><a href="map.html">map</a></nav>
</div>
<p class="meta">{len(rows)} listings · {len(initial_body_rows)} loaded initially · generated {datetime.now().strftime('%Y-%m-%d %H:%M')} · click a column header to sort, click a row for description &amp; floor plan, click a photo for the photo grid</p>
<p class="meta">2025 band = historic, interpolated transaction €/m² from the <a href="{PRICE_BANDS_SOURCE_URL}" target="_blank">Amsterdam Woningwaardekaart</a>; “below/within/above” compares the current asking €/m² with that unadjusted band.</p>
<p class="meta" id="statusLegend"><strong>Status:</strong> call · viewing requested · viewing planned · viewed · bid · sold · bought</p>
<p class="meta">keys: <kbd>j</kbd>/<kbd>k</kbd> or <kbd>↓</kbd>/<kbd>↑</kbd> move · <kbd>enter</kbd> fold · <kbd>p</kbd> photos · <kbd>x</kbd>/<kbd>0</kbd>–<kbd>3</kbd> rate · <kbd>f</kbd> open funda · <kbd>esc</kbd> close</p>
<div class="controls">
  <label class="search-field">Search <input type="search" id="search" placeholder="address, district, or neighbourhood" autocomplete="off"></label>
  <label title="Default range is €500k–€750k"><input type="checkbox" id="widerPrice"> wider €400k–€850k</label>
  <label title="Temporarily include every district without changing the saved district selection"><input type="checkbox" id="districtFilterEnabled" checked> district filter</label>
  <details class="district-filter">
    <summary id="districtSummary">configure districts</summary>
    <div class="district-menu">
      <strong>Hide districts</strong>
      <div class="district-options" id="districtOptions"></div>
      <button type="button" id="clearDistricts">clear district selection</button>
    </div>
  </details>
  <label><input type="checkbox" id="hideRated"> hide rated</label>
  <label><input type="checkbox" id="hideNo" checked> hide "not interesting" (✕)</label>
  <label><input type="checkbox" id="hideUO" checked> hide under offer</label>
  <label><input type="checkbox" id="hideSold" checked> hide sold</label>
  <span id="counts" class="meta"></span>
</div>
<table id="t">
<thead><tr>
  <th></th><th class="addr">Address</th><th class="tracking">Status</th><th class="district">District</th><th class="neighbourhood">Neighbourhood</th><th>Price</th><th>Area</th><th>€/m²</th>
  <th>2025 band</th><th>Rooms</th><th>Energy</th><th title="Straight-line distance to Dam Square">Dam</th><th title="Straight-line distance to Science Park 303">SP 303</th><th>Listed</th><th data-defdesc="1">Score</th>
</tr></thead>
<tbody>
{chr(10).join(initial_body_rows)}
</tbody>
</table>
<div class="load-more" id="loadMoreWrap"{' hidden' if not row_batches else ''}>
  <button id="loadMore" type="button">load more</button>
  <span id="loadProgress" class="meta"></span>
</div>
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
const totalListingCount = {len(rows)};
const rowBatchCount = {len(row_batches)};
const rowBatchSize = {ROW_BATCH_SIZE};
let nextRowBatch = 1;

function hydrateListedDates(root = document) {{
  for (const cell of root.querySelectorAll('td.listed')) {{
    const iso = cell.dataset.date;
    if (!iso) {{ cell.dataset.sort = 9999; continue; }}
    const days = Math.max(0, Math.round((Date.now() - new Date(iso + 'T00:00')) / 86400000));
    cell.textContent = days === 0 ? 'today' : days === 1 ? 'yesterday' : `${{days}}d ago`;
    cell.dataset.sort = days;
  }}
}}
hydrateListedDates();
const hideRated = document.getElementById('hideRated');
const hideNo = document.getElementById('hideNo');
const hideUO = document.getElementById('hideUO');
const hideSold = document.getElementById('hideSold');
const search = document.getElementById('search');
const widerPrice = document.getElementById('widerPrice');
const availableDistricts = {district_json};
const districtOptions = document.getElementById('districtOptions');
const districtSummary = document.getElementById('districtSummary');
const districtFilterEnabledInput = document.getElementById('districtFilterEnabled');
const DISTRICT_STORAGE_KEY = 'funda-hidden-districts';
const DISTRICT_FILTER_ENABLED_STORAGE_KEY = 'funda-district-filter-enabled';
let excludedDistricts = new Set();
let districtFilterEnabled = true;
try {{
  const storedDistricts = JSON.parse(localStorage.getItem(DISTRICT_STORAGE_KEY) || '[]');
  excludedDistricts = new Set(storedDistricts.filter(district => availableDistricts.includes(district)));
  const storedEnabled = localStorage.getItem(DISTRICT_FILTER_ENABLED_STORAGE_KEY);
  if (storedEnabled !== null) districtFilterEnabled = storedEnabled === 'true';
}} catch (error) {{}}
districtFilterEnabledInput.checked = districtFilterEnabled;

function updateDistrictSummary() {{
  const count = excludedDistricts.size;
  districtSummary.textContent = count ? `configure districts (${{count}} hidden)` : 'configure districts';
}}

function saveExcludedDistricts() {{
  localStorage.setItem(DISTRICT_STORAGE_KEY, JSON.stringify([...excludedDistricts]));
  updateDistrictSummary();
}}

for (const district of availableDistricts) {{
  const label = document.createElement('label');
  const input = document.createElement('input');
  input.type = 'checkbox';
  input.value = district;
  input.checked = excludedDistricts.has(district);
  input.addEventListener('change', () => {{
    if (input.checked) excludedDistricts.add(district);
    else excludedDistricts.delete(district);
    saveExcludedDistricts();
    applyFilters();
  }});
  label.append(input, document.createTextNode(district));
  districtOptions.append(label);
}}
document.getElementById('clearDistricts').addEventListener('click', () => {{
  excludedDistricts.clear();
  for (const input of districtOptions.querySelectorAll('input')) input.checked = false;
  saveExcludedDistricts();
  applyFilters();
}});
districtFilterEnabledInput.addEventListener('change', () => {{
  districtFilterEnabled = districtFilterEnabledInput.checked;
  localStorage.setItem(DISTRICT_FILTER_ENABLED_STORAGE_KEY, String(districtFilterEnabled));
  applyFilters();
}});
updateDistrictSummary();

// ratings and personal tracking statuses live on the server (shared across
// browsers/people); localStorage is the fallback for a statically opened page
let ratings = {{}};
let serverRatings = false;
let trackingStatuses = {{}};
let serverTrackingStatuses = false;
let listingAnalyses = {{}};
let analysisRequests = {{}};

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

function postTrackingStatus(id, status) {{
  fetch('track', {{
    method: 'POST',
    headers: {{'Content-Type': 'application/json'}},
    body: JSON.stringify({{id, status}}),
  }}).catch(() => {{}});
}}

function saveTrackingStatus(id, status) {{
  if (!status) delete trackingStatuses[id];
  else trackingStatuses[id] = status;
  if (serverTrackingStatuses) postTrackingStatus(id, status || null);
  else localStorage.setItem('funda-tracking-statuses', JSON.stringify(trackingStatuses));
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

async function initState() {{
  const local = JSON.parse(localStorage.getItem('funda-ratings') || '{{}}');
  const localTracking = JSON.parse(localStorage.getItem('funda-tracking-statuses') || '{{}}');
  try {{
    const res = await fetch('ratings.json', {{cache: 'no-store'}});
    if (res.ok) {{ ratings = await res.json(); serverRatings = true; }}
  }} catch (e) {{}}
  try {{
    const res = await fetch('tracking-statuses.json', {{cache: 'no-store'}});
    if (res.ok) {{
      trackingStatuses = await res.json();
      serverTrackingStatuses = true;
    }}
  }} catch (e) {{}}
  try {{
    const res = await fetch('analysis-state.json', {{cache: 'no-store'}});
    if (res.ok) {{
      const state = await res.json();
      listingAnalyses = state.analyses || {{}};
      analysisRequests = state.requests || {{}};
    }}
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
  if (serverTrackingStatuses) {{
    for (const [id, status] of Object.entries(localTracking)) {{
      if (!(id in trackingStatuses)) {{
        trackingStatuses[id] = status;
        postTrackingStatus(id, status);
      }}
    }}
  }} else {{
    trackingStatuses = localTracking;
  }}
  applyRatings();
  applyTrackingStatuses();
  applyFilters();
}}

function listingRows() {{ return [...tbody.querySelectorAll('tr[data-id]')]; }}

const loadMoreWrap = document.getElementById('loadMoreWrap');
const loadMoreButton = document.getElementById('loadMore');
const loadProgress = document.getElementById('loadProgress');
let rowLoadPromise = null;
let allRowsPromise = null;
let loadingAllReason = '';

function updateLoadMore() {{
  const loaded = listingRows().length;
  const remaining = Math.max(0, totalListingCount - loaded);
  loadProgress.textContent = loadingAllReason
    ? `loading all ${{totalListingCount}} listings for ${{loadingAllReason}}…`
    : `${{loaded}} of ${{totalListingCount}} loaded`;
  if (nextRowBatch > rowBatchCount || !remaining) {{
    loadMoreButton.hidden = true;
    return;
  }}
  loadMoreWrap.hidden = false;
  loadMoreButton.hidden = false;
  loadMoreButton.disabled = Boolean(loadingAllReason);
  loadMoreButton.textContent = loadingAllReason
    ? 'loading…'
    : `load ${{Math.min(rowBatchSize, remaining)}} more`;
}}

function loadMoreRows(reapplySort = true) {{
  if (rowLoadPromise) return rowLoadPromise;
  rowLoadPromise = (async () => {{
    loadMoreButton.disabled = true;
    loadMoreButton.textContent = 'loading…';
    try {{
      const response = await fetch(`listing-rows/${{nextRowBatch}}.html`, {{cache: 'no-store'}});
      if (!response.ok) throw new Error(`HTTP ${{response.status}}`);
      const template = document.createElement('template');
      template.innerHTML = await response.text();
      hydrateListedDates(template.content);
      tbody.append(template.content);
      nextRowBatch++;
      applyRatings();
      applyTrackingStatuses();
      if (reapplySort) reapplyActiveSort();
      applyFilters();
      updateLoadMore();
      return true;
    }} catch (error) {{
      loadMoreButton.disabled = false;
      loadMoreButton.textContent = 'try loading again';
      loadMoreButton.title = `Load failed: ${{error.message}}`;
      return false;
    }} finally {{
      rowLoadPromise = null;
    }}
  }})();
  return rowLoadPromise;
}}

loadMoreButton.addEventListener('click', () => loadMoreRows());
updateLoadMore();

function applyRatings() {{
  for (const tr of listingRows()) {{
    const s = ratings[tr.dataset.id];
    tr.querySelectorAll('.rate button').forEach(b =>
      b.classList.toggle('on', s !== undefined && +b.dataset.s === s));
    tr.querySelector('td.score').dataset.sort = s === undefined ? -1 : s;
  }}
}}

function applyTrackingStatuses() {{
  for (const tr of listingRows()) {{
    const status = trackingStatuses[tr.dataset.id] || '';
    tr.dataset.tracking = status;
    tr.querySelector('.tracking-select').value = status;
    tr.querySelector('td.tracking').dataset.sort = status;
  }}
}}

function applyFilters() {{
  let visible = 0, rated = 0, tracked = 0, sold = 0;
  const query = search.value.trim().toLocaleLowerCase('nl-NL');
  document.body.classList.toggle('hide-sold', hideSold.checked);
  for (const tr of listingRows()) {{
    const s = ratings[tr.dataset.id];
    const trackingStatus = trackingStatuses[tr.dataset.id] || '';
    if (s !== undefined) rated++;
    if (trackingStatus) tracked++;
    const isSold = trackingStatus === 'sold'
      || (tr.dataset.marketGone === '1' && trackingStatus !== 'bought');
    if (isSold) sold++;
    const price = Number(tr.dataset.price);
    const hide = (!widerPrice.checked && (!price || price < 500000 || price > 750000))
      || (districtFilterEnabled && excludedDistricts.has(tr.dataset.district))
      || (hideRated.checked && s !== undefined) || (hideNo.checked && s === 0)
      || (hideUO.checked && tr.dataset.status === 'negotiations')
      || (hideSold.checked && isSold)
      || (query && !tr.dataset.search.includes(query));
    tr.style.display = hide ? 'none' : '';
    const next = tr.nextElementSibling;
    if (next && next.classList.contains('desc-row')) {{
      if (hide) disposeFold(next);
      else next.style.display = '';
    }}
    if (!hide) visible++;
  }}
  document.getElementById('counts').textContent =
    `${{visible}} shown · ${{listingRows().length}}/${{totalListingCount}} loaded · ${{rated}} rated · ${{tracked}} tracked · ${{sold}} sold`;
}}

hideRated.addEventListener('change', applyFilters);
hideNo.addEventListener('change', applyFilters);
hideUO.addEventListener('change', applyFilters);
hideSold.addEventListener('change', applyFilters);
widerPrice.addEventListener('change', applyFilters);

const tableHeaders = [...document.querySelectorAll('#t th')];

function sortRowsBy(th, i, toggle = true) {{
  document.querySelectorAll('.desc-row').forEach(disposeFold);
  const rows = listingRows();
  let dir = th.dataset.dir;
  if (toggle) {{
    dir = dir
      ? (dir === 'asc' ? 'desc' : 'asc')
      : (th.dataset.defdesc ? 'desc' : 'asc');
    tableHeaders.forEach(other => {{ if (other !== th) delete other.dataset.dir; }});
    th.dataset.dir = dir;
  }}
  if (!dir) return;
  rows.sort((a, b) => {{
    const av = a.cells[i]?.dataset.sort ?? a.cells[i]?.textContent.trim() ?? '';
    const bv = b.cells[i]?.dataset.sort ?? b.cells[i]?.textContent.trim() ?? '';
    const an = parseFloat(av), bn = parseFloat(bv);
    const cmp = (!isNaN(an) && !isNaN(bn)) ? an - bn : av.localeCompare(bv);
    return dir === 'asc' ? cmp : -cmp;
  }});
  rows.forEach(r => tbody.appendChild(r));
}}

function reapplyActiveSort() {{
  const index = tableHeaders.findIndex(th => th.dataset.dir);
  if (index !== -1) sortRowsBy(tableHeaders[index], index, false);
}}

async function loadAllRows(reason) {{
  if (listingRows().length >= totalListingCount) return true;
  if (allRowsPromise) return allRowsPromise;
  loadingAllReason = reason;
  updateLoadMore();
  allRowsPromise = (async () => {{
    while (nextRowBatch <= rowBatchCount) {{
      if (!await loadMoreRows(false)) return false;
    }}
    return listingRows().length === totalListingCount;
  }})();
  try {{
    return await allRowsPromise;
  }} finally {{
    loadingAllReason = '';
    allRowsPromise = null;
    updateLoadMore();
  }}
}}

let tableSearchTimer;
search.addEventListener('input', () => {{
  clearTimeout(tableSearchTimer);
  applyFilters();
  if (!search.value.trim()) return;
  tableSearchTimer = setTimeout(async () => {{
    await stateReady;
    if (await loadAllRows('searching')) applyFilters();
  }}, 150);
}});

tableHeaders.forEach((th, i) =>
  th.addEventListener('click', async () => {{
    await stateReady;
    if (await loadAllRows('sorting')) sortRowsBy(th, i);
  }}));

function rate(tr, s) {{
  const id = tr.dataset.id;
  saveRating(id, ratings[id] === s ? null : s);
  applyRatings(); applyFilters();
}}

function track(tr, status) {{
  saveTrackingStatus(tr.dataset.id, status);
  applyTrackingStatuses();
  applyFilters();
}}

function euro(value) {{
  return value === null || value === undefined ? ''
    : `€ ${{Number(value).toLocaleString('nl-NL')}}`;
}}

function estimateText(estimate) {{
  if (!estimate) return 'Not available';
  if (estimate.value !== null && estimate.value !== undefined) return euro(estimate.value);
  if (estimate.low !== null && estimate.low !== undefined
      && estimate.high !== null && estimate.high !== undefined) {{
    return `${{euro(estimate.low)}}–${{Number(estimate.high).toLocaleString('nl-NL')}}`;
  }}
  return 'Not available';
}}

async function requestAnalysis(id, panel) {{
  const button = panel.querySelector('button');
  button.disabled = true;
  button.textContent = 'Requesting…';
  try {{
    const res = await fetch('request-analysis', {{
      method: 'POST',
      headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{id}}),
    }});
    if (!res.ok) throw new Error(await res.text());
    analysisRequests[id] = await res.json();
    if (analysisRequests[id].brochure_url) {{
      const listingRow = listingRows().find(row => row.dataset.id === id);
      if (listingRow) listingRow.dataset.brochure = analysisRequests[id].brochure_url;
    }}
    panel.replaceWith(buildAnalysisPanel(id));
  }} catch (e) {{
    button.disabled = false;
    button.textContent = 'Request analysis';
    button.title = `Request failed: ${{e.message}}`;
  }}
}}

function analysisCard(title, section, value) {{
  const card = document.createElement('div');
  card.className = 'analysis-card';
  const heading = document.createElement('h4');
  heading.textContent = title;
  if (section?.risk) {{
    const risk = document.createElement('span');
    risk.className = `risk risk-${{section.risk}}`;
    risk.textContent = section.risk;
    heading.append(risk);
  }}
  const primary = document.createElement('div');
  primary.className = 'analysis-value';
  primary.textContent = value || 'Not established';
  const summary = document.createElement('div');
  summary.textContent = section?.summary || '';
  card.append(heading, primary, summary);
  return card;
}}

function brochureSource(url) {{
  if (!url) return null;
  const source = document.createElement('div');
  source.className = 'analysis-sources';
  source.append('Brochure: ');
  const link = document.createElement('a');
  link.href = url;
  link.target = '_blank';
  link.textContent = 'download PDF';
  source.append(link);
  return source;
}}

function buildAnalysisPanel(id) {{
  const panel = document.createElement('section');
  panel.className = 'analysis';
  panel.dataset.listingId = id;
  const analysis = listingAnalyses[id];
  const pending = analysisRequests[id];
  const row = listingRows().find(candidate => candidate.dataset.id === id);
  const brochureUrl = analysis?.brochure_url || pending?.brochure_url || row?.dataset.brochure;
  const head = document.createElement('div');
  head.className = 'analysis-head';
  const title = document.createElement('strong');
  title.textContent = 'Due-diligence snapshot';
  head.append(title);

  if (!analysis) {{
    const button = document.createElement('button');
    button.type = 'button';
    button.disabled = Boolean(pending);
    button.textContent = pending ? 'Analysis requested' : 'Request analysis';
    if (!pending) button.addEventListener('click', () => requestAnalysis(id, panel));
    head.append(button);
    const note = document.createElement('div');
    note.className = 'analysis-meta';
    note.textContent = pending
      ? `Queued ${{(pending.requested_at || '').slice(0, 10)}}.`
      : 'Request a sourced review of market value, VvE, erfpacht and listing risks.';
    panel.append(head, note);
    if (brochureUrl) panel.append(brochureSource(brochureUrl));
    return panel;
  }}

  const status = document.createElement('span');
  status.textContent = pending
    ? 'refresh requested'
    : `updated ${{(analysis.updated_at || '').slice(0, 10)}}`;
  head.append(status);
  const grid = document.createElement('div');
  grid.className = 'analysis-grid';
  const market = analysis.market || {{}};
  const range = market.estimate_low !== undefined && market.estimate_high !== undefined
    ? `${{euro(market.estimate_low)}}–${{Number(market.estimate_high).toLocaleString('nl-NL')}}`
    : 'Range not established';
  const marketCard = analysisCard('Market indication', market, range);
  if (market.external) {{
    const external = document.createElement('div');
    external.className = 'analysis-meta';
    const label = `${{market.external.label || 'External model'}}: ${{estimateText(market.external)}}`;
    if (market.external.url) {{
      const link = document.createElement('a');
      link.href = market.external.url;
      link.target = '_blank';
      link.textContent = label;
      external.append(link);
    }} else {{
      external.textContent = label;
    }}
    if (market.external.caveat) external.title = market.external.caveat;
    marketCard.append(external);
  }}
  const vve = analysis.vve || {{}};
  const vveValue = vve.monthly_eur === null || vve.monthly_eur === undefined
    ? 'Contribution unknown' : `${{euro(vve.monthly_eur)}} / month`;
  const erfpacht = analysis.erfpacht || {{}};
  grid.append(
    marketCard,
    analysisCard('VvE', vve, vveValue),
    analysisCard('Erfpacht', erfpacht, erfpacht.headline || 'Not established'),
  );
  panel.append(head, grid);
  if (brochureUrl) panel.append(brochureSource(brochureUrl));

  for (const [label, items] of [['What jumps out', analysis.flags], ['Questions before bidding', analysis.questions]]) {{
    if (!items?.length) continue;
    const list = document.createElement('ul');
    list.className = 'analysis-list';
    const first = document.createElement('li');
    const strong = document.createElement('strong');
    strong.textContent = label;
    first.append(strong);
    list.append(first);
    for (const item of items) {{
      const li = document.createElement('li');
      li.textContent = item;
      list.append(li);
    }}
    panel.append(list);
  }}
  if (analysis.sources?.length) {{
    const sources = document.createElement('div');
    sources.className = 'analysis-sources';
    sources.append('Sources: ');
    for (const source of analysis.sources) {{
      const link = document.createElement('a');
      link.href = source.url;
      link.target = '_blank';
      link.textContent = source.label;
      sources.append(link);
    }}
    panel.append(sources);
  }}
  const button = document.createElement('button');
  button.type = 'button';
  button.disabled = Boolean(pending);
  button.textContent = pending ? 'Refresh requested' : 'Request refresh';
  if (!pending) button.addEventListener('click', () => requestAnalysis(id, panel));
  panel.append(button);
  return panel;
}}

function disposeFold(row) {{
  if (!row?.classList.contains('desc-row')) return;
  // Floorplanner embeds create WebGL contexts in a cross-origin document.
  // Explicitly navigate and detach every iframe before dropping the fold so
  // Chromium can release those contexts instead of retaining them until GC.
  for (const frame of row.querySelectorAll('iframe')) {{
    frame.src = 'about:blank';
    frame.removeAttribute('src');
    frame.remove();
  }}
  row.remove();
}}

function toggleFold(tr) {{
  const next = tr.nextElementSibling;
  if (next && next.classList.contains('desc-row')) {{ disposeFold(next); return; }}
  // Keep at most one expanded listing. Besides making the interaction clearer,
  // this bounds the number of live Floorplanner/WebGL documents on the page.
  document.querySelectorAll('.desc-row').forEach(disposeFold);
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
  cell.colSpan = 15;
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
  descDiv.append(buildAnalysisPanel(id), historyDiv, descriptionText);
  const fpDiv = document.createElement('div');
  fpDiv.className = 'fold-right';
  fpDiv.innerHTML = photosLink + mapHtml + fpHtml;
  if (tr.dataset.brochure) {{
    const brochure = document.createElement('a');
    brochure.className = 'maplink';
    brochure.href = tr.dataset.brochure;
    brochure.target = '_blank';
    brochure.textContent = 'download brochure (PDF)';
    fpDiv.prepend(document.createElement('br'));
    fpDiv.prepend(brochure);
  }}
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
  if (e.target.closest('.tracking-select')) return;
  select(tr);
  if (e.target.closest('td.photo')) {{ openGrid(tr); return; }}
  if (e.target.closest('a')) return;
  toggleFold(tr);
}});

tbody.addEventListener('change', e => {{
  const selector = e.target.closest('.tracking-select');
  if (!selector) return;
  track(selector.closest('tr'), selector.value);
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
      if (open) disposeFold(open);
      else select(null);
      break;
    }}
  }}
}});

const stateReady = initState();
</script>
</body>
</html>
"""
    write_atomic(OVERVIEW_FILE, page)
    render_map(config, rows)
    print(
        f"wrote {OVERVIEW_FILE.relative_to(ROOT)} and {map_path().relative_to(ROOT)} "
        f"with {len(rows)} listings"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--render-only", action="store_true", help="regenerate table and map without fetching"
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
    parser.add_argument(
        "--refresh-districts",
        action="store_true",
        help="download Amsterdam's administrative districts, backfill, then re-render",
    )
    args = parser.parse_args()

    config = load_config()
    listings = load_listings()
    histories_changed = ensure_histories(listings)

    if args.refresh_price_bands:
        refresh_price_bands()
    if args.refresh_districts:
        refresh_districts()

    if (
        args.backfill_floorplans
        or args.backfill_photos
        or args.refresh_status
        or args.refresh_price_bands
        or args.refresh_districts
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

    districts_changed = ensure_districts(listings)
    if histories_changed or districts_changed:
        save_listings(listings)

    render(config, listings)


if __name__ == "__main__":
    main()
