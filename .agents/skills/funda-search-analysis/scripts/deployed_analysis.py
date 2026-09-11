#!/usr/bin/env python3
"""Read and complete funda-search analysis requests through the deployed API."""

from __future__ import annotations

import argparse
import json
import sys
from http.cookiejar import CookieJar
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPCookieProcessor, Request, build_opener


DEFAULT_BASE_URL = "https://donut-chaise.exe.xyz"
DEFAULT_ENV_FILE = Path("/Users/alextes/code/funda-search-deploy/envs/donut-chaise.env")
RISK_VALUES = {"low", "medium", "high", "unknown"}


def read_password(path: Path) -> str:
    if not path.is_file():
        raise ValueError(f"credential file not found: {path}")
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key.strip() == "FUNDA_SEARCH_PASSWORD":
            value = value.strip()
            if (value.startswith('"') and value.endswith('"')) or (
                value.startswith("'") and value.endswith("'")
            ):
                value = value[1:-1]
            if value:
                return value
    raise ValueError(f"FUNDA_SEARCH_PASSWORD is missing from {path}")


class Client:
    def __init__(self, base_url: str, password: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.cookies = CookieJar()
        self.opener = build_opener(HTTPCookieProcessor(self.cookies))
        body = urlencode({"password": password}).encode()
        self._open(Request(f"{self.base_url}/login", data=body, method="POST"))
        if not any(cookie.name == "fs_session" for cookie in self.cookies):
            raise RuntimeError("login failed: no session cookie returned")

    def _open(self, request: Request):
        try:
            return self.opener.open(request, timeout=30)
        except HTTPError as error:
            detail = error.read().decode(errors="replace").strip()
            raise RuntimeError(f"HTTP {error.code}: {detail}") from error
        except URLError as error:
            raise RuntimeError(f"connection failed: {error.reason}") from error

    def get_json(self, path: str):
        response = self._open(Request(f"{self.base_url}{path}", method="GET"))
        return json.load(response)

    def post_json(self, path: str, payload: object) -> int:
        body = json.dumps(payload, ensure_ascii=False).encode()
        request = Request(
            f"{self.base_url}{path}",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        response = self._open(request)
        return response.status


def validate_analysis(analysis: object) -> dict:
    if not isinstance(analysis, dict):
        raise ValueError("analysis must be a JSON object")
    for section in ("market", "vve", "erfpacht"):
        if not isinstance(analysis.get(section), dict):
            raise ValueError(f"analysis.{section} must be an object")
    for field in ("flags", "questions", "sources"):
        if not isinstance(analysis.get(field), list):
            raise ValueError(f"analysis.{field} must be a list")
    for field in ("flags", "questions"):
        if not all(isinstance(item, str) and item.strip() for item in analysis[field]):
            raise ValueError(f"analysis.{field} entries must be non-empty strings")
    market = analysis["market"]
    for field in ("estimate_low", "estimate_high"):
        if not isinstance(market.get(field), (int, float)):
            raise ValueError(f"analysis.market.{field} must be numeric")
    if market["estimate_low"] > market["estimate_high"]:
        raise ValueError("analysis.market estimate range is reversed")
    for section in ("vve", "erfpacht"):
        if analysis[section].get("risk") not in RISK_VALUES:
            raise ValueError(f"analysis.{section}.risk is invalid")
    for source in analysis["sources"]:
        if not isinstance(source, dict) or not isinstance(source.get("label"), str):
            raise ValueError("analysis.sources entries need a label and URL")
        url = source.get("url")
        if not isinstance(url, str) or not url.startswith(("https://", "http://")):
            raise ValueError("analysis.sources URLs must use HTTP(S)")
    external = market.get("external")
    if external is not None:
        if not isinstance(external, dict):
            raise ValueError("analysis.market.external must be an object")
        url = external.get("url")
        if url is not None and (
            not isinstance(url, str) or not url.startswith(("https://", "http://"))
        ):
            raise ValueError("analysis.market.external URL must use HTTP(S)")
    return analysis


def queue(client: Client, *, json_output: bool) -> None:
    state = client.get_json("/analysis-state.json")
    requests = state.get("requests") or {}
    listings = client.get_json("/listings.json")
    rows = []
    for listing_id, request in requests.items():
        listing = listings.get(str(listing_id), {})
        rows.append(
            {
                "id": str(listing_id),
                "requested_at": request.get("requested_at"),
                "title": listing.get("title"),
                "status": listing.get("status"),
                "price": listing.get("price"),
                "listing_url": request.get("listing_url") or listing.get("url"),
                "brochure_url": request.get("brochure_url") or listing.get("brochure_url"),
            }
        )
    rows.sort(key=lambda row: row.get("requested_at") or "", reverse=True)
    if json_output:
        print(json.dumps(rows, indent=2, ensure_ascii=False))
        return
    if not rows:
        print("No pending analysis requests.")
        return
    for row in rows:
        price = f"EUR {row['price']:,}" if isinstance(row["price"], int) else "price unknown"
        print(
            f"{row['requested_at'] or 'time unknown'}  {row['id']}  "
            f"{row['title'] or 'title unknown'}  {price}  {row['status'] or 'status unknown'}"
        )


def listing(client: Client, listing_id: str) -> None:
    listings = client.get_json("/listings.json")
    state = client.get_json("/analysis-state.json")
    record = listings.get(str(listing_id))
    if record is None:
        raise ValueError(f"listing {listing_id} is absent from deployed listings")
    output = {
        "listing": record,
        "request": (state.get("requests") or {}).get(str(listing_id)),
        "analysis": (state.get("analyses") or {}).get(str(listing_id)),
    }
    print(json.dumps(output, indent=2, ensure_ascii=False))


def save(client: Client, listing_id: str, path: Path) -> None:
    raw = json.loads(path.read_text())
    analysis = raw.get("analysis") if isinstance(raw, dict) and "analysis" in raw else raw
    analysis = validate_analysis(analysis)
    status = client.post_json("/analysis", {"id": str(listing_id), "analysis": analysis})
    if status != 204:
        raise RuntimeError(f"save returned unexpected HTTP status {status}")
    state = client.get_json("/analysis-state.json")
    saved = (state.get("analyses") or {}).get(str(listing_id))
    pending = str(listing_id) in (state.get("requests") or {})
    if saved is None or pending:
        raise RuntimeError("save verification failed: analysis missing or request still pending")
    print(
        json.dumps(
            {
                "id": str(listing_id),
                "saved": True,
                "updated_at": saved.get("updated_at"),
                "request_cleared": not pending,
                "estimate_low": (saved.get("market") or {}).get("estimate_low"),
                "estimate_high": (saved.get("market") or {}).get("estimate_high"),
                "vve_risk": (saved.get("vve") or {}).get("risk"),
                "erfpacht_risk": (saved.get("erfpacht") or {}).get("risk"),
            },
            indent=2,
            ensure_ascii=False,
        )
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV_FILE)
    subparsers = parser.add_subparsers(dest="command", required=True)
    queue_parser = subparsers.add_parser("queue", help="list pending requests")
    queue_parser.add_argument("--json", action="store_true", dest="json_output")
    listing_parser = subparsers.add_parser("listing", help="show deployed listing context")
    listing_parser.add_argument("listing_id")
    save_parser = subparsers.add_parser("save", help="save and verify an analysis")
    save_parser.add_argument("listing_id")
    save_parser.add_argument("--analysis", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        client = Client(args.base_url, read_password(args.env_file))
        if args.command == "queue":
            queue(client, json_output=args.json_output)
        elif args.command == "listing":
            listing(client, args.listing_id)
        elif args.command == "save":
            save(client, args.listing_id, args.analysis)
        return 0
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
