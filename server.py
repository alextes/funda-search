#!/usr/bin/env python3
"""Serve the overview and keep the listing data fresh.

A background thread re-fetches whenever the last check is older than the
configured interval (default 60s), so the list is always at most that old
while the server runs; roughly hourly it also re-checks status/price of
stored listings so sold / under-offer entries don't linger. The HTTP side
serves the latest overview.html from disk (written atomically by
fetch.render) and a small ratings API shared by everyone using the page.

If FUNDA_SEARCH_PASSWORD is set, everything except /healthz sits behind a
login form; the session cookie lasts 30 days.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import secrets as secrets_mod
import sys
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

import fetch as core

DEFAULT_INTERVAL_S = 60
DEFAULT_STATUS_INTERVAL_S = 3600
SESSION_MAX_AGE_S = 30 * 24 * 3600

PASSWORD = os.environ.get("FUNDA_SEARCH_PASSWORD")
RATINGS_FILE = core.ROOT / "data" / "ratings.json"
SECRET_FILE = core.ROOT / "data" / ".session-secret"

state = {"last_fetch": None, "fetch_count": 0, "last_error": None, "last_status_refresh": None}
ratings_lock = threading.Lock()

LOGIN_PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>funda-search · login</title>
<style>
  body {{ font-family: -apple-system, system-ui, sans-serif; display: flex; align-items: center;
         justify-content: center; min-height: 80vh; }}
  form {{ display: flex; gap: .5rem; }}
  input {{ font-size: 1rem; padding: .5rem .7rem; border: 1px solid #ccc; border-radius: 4px; }}
  button {{ font-size: 1rem; padding: .5rem 1rem; border: 1px solid #f7a100; background: #f7a100;
           color: #fff; border-radius: 4px; cursor: pointer; }}
  .err {{ color: #c00; margin-right: 1rem; }}
</style></head>
<body><form method="post" action="login">{error}
  <input type="password" name="password" placeholder="password" autofocus>
  <button>enter</button>
</form></body></html>
"""


def log(msg: str) -> None:
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


def session_token() -> str:
    """Stable token derived from the password and a per-install secret."""
    if not SECRET_FILE.exists():
        SECRET_FILE.parent.mkdir(exist_ok=True)
        SECRET_FILE.write_text(secrets_mod.token_hex(32))
    secret = SECRET_FILE.read_text().strip()
    return hmac.new(secret.encode(), (PASSWORD or "").encode(), hashlib.sha256).hexdigest()


def load_ratings() -> dict:
    if RATINGS_FILE.exists():
        return json.loads(RATINGS_FILE.read_text())
    return {}


def fetch_once() -> None:
    config = core.load_config()  # re-read each round so config edits apply live
    listings = core.load_listings()
    total, new = core.fetch(config, listings)
    if new:
        core.save_listings(listings)
    core.render(config, listings)
    state["last_fetch"] = datetime.now()
    state["fetch_count"] += 1
    state["last_error"] = None
    log(f"fetch #{state['fetch_count']}: {total} in search, {new} new")


def status_refresh_once() -> None:
    config = core.load_config()
    listings = core.load_listings()
    changed = core.refresh_statuses(listings)
    core.save_listings(listings)
    if changed:
        core.render(config, listings)
    state["last_status_refresh"] = datetime.now()
    log(f"status refresh done ({changed} changes)")


def fetch_loop(interval: float, status_interval: float) -> None:
    last_status = 0.0
    while True:
        started = time.monotonic()
        try:
            fetch_once()
        except Exception as e:
            state["last_error"] = f"{datetime.now():%Y-%m-%d %H:%M:%S} {e}"
            log(f"fetch failed: {e}")
        if time.monotonic() - last_status > status_interval:
            try:
                status_refresh_once()
            except Exception as e:
                state["last_error"] = f"{datetime.now():%Y-%m-%d %H:%M:%S} status refresh: {e}"
                log(f"status refresh failed: {e}")
            last_status = time.monotonic()
        time.sleep(max(1.0, interval - (time.monotonic() - started)))


class Handler(BaseHTTPRequestHandler):
    def authed(self) -> bool:
        if not PASSWORD:
            return True
        cookies = self.headers.get("Cookie", "")
        for part in cookies.split(";"):
            name, _, value = part.strip().partition("=")
            if name == "fs_session" and hmac.compare_digest(value, session_token()):
                return True
        return False

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]
        if path == "/healthz":
            body = "".join(f"{k}: {v}\n" for k, v in state.items())
            self.respond(200, "text/plain; charset=utf-8", body.encode())
            return
        if not self.authed():
            self.respond(200, "text/html; charset=utf-8", LOGIN_PAGE.format(error="").encode())
            return
        if path in ("/", "/index.html", "/overview.html"):
            if not core.OVERVIEW_FILE.exists():
                self.respond(503, "text/plain", b"no overview yet, first fetch still running")
                return
            self.respond(200, "text/html; charset=utf-8", core.OVERVIEW_FILE.read_bytes())
        elif path == "/ratings.json":
            with ratings_lock:
                body = json.dumps(load_ratings()).encode()
            self.respond(200, "application/json; charset=utf-8", body)
        elif path == "/listings.json":
            self.respond(200, "application/json; charset=utf-8", core.DATA_FILE.read_bytes())
        else:
            self.respond(404, "text/plain", b"not found")

    def do_POST(self) -> None:
        path = self.path.split("?", 1)[0]
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""

        if path == "/login":
            password = parse_qs(body.decode(errors="replace")).get("password", [""])[0]
            if PASSWORD and hmac.compare_digest(password, PASSWORD):
                self.send_response(303)
                self.send_header("Location", "/")
                self.send_header(
                    "Set-Cookie",
                    f"fs_session={session_token()}; Max-Age={SESSION_MAX_AGE_S}; Path=/; HttpOnly; SameSite=Lax",
                )
                self.end_headers()
            else:
                page = LOGIN_PAGE.format(error='<span class="err">wrong password</span>')
                self.respond(200, "text/html; charset=utf-8", page.encode())
            return

        if not self.authed():
            self.respond(403, "text/plain", b"forbidden")
            return

        if path == "/rate":
            try:
                data = json.loads(body)
                listing_id = str(data["id"])
                score = data["score"]
                if score is not None and (not isinstance(score, int) or not 0 <= score <= 3):
                    raise ValueError("score must be null or 0-3")
            except (ValueError, KeyError, json.JSONDecodeError) as e:
                self.respond(400, "text/plain", f"bad request: {e}".encode())
                return
            with ratings_lock:
                ratings = load_ratings()
                if score is None:
                    ratings.pop(listing_id, None)
                else:
                    ratings[listing_id] = score
                RATINGS_FILE.parent.mkdir(exist_ok=True)
                core.write_atomic(RATINGS_FILE, json.dumps(ratings, indent=1))
            self.respond(204, "text/plain", b"")
        else:
            self.respond(404, "text/plain", b"not found")

    def respond(self, status: int, content_type: str, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if body:
            self.wfile.write(body)

    def log_message(self, fmt: str, *args) -> None:
        pass  # keep stdout for fetch-loop logs


def main() -> None:
    sys.stdout.reconfigure(line_buffering=True)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument(
        "--interval",
        type=float,
        default=None,
        help=f"seconds between fetches (default: config fetch_interval_seconds or {DEFAULT_INTERVAL_S})",
    )
    args = parser.parse_args()

    config = core.load_config()
    interval = args.interval
    if interval is None:
        interval = config.get("fetch_interval_seconds", DEFAULT_INTERVAL_S)
    status_interval = config.get("status_refresh_interval_seconds", DEFAULT_STATUS_INTERVAL_S)

    threading.Thread(target=fetch_loop, args=(interval, status_interval), daemon=True).start()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    auth = "password-protected" if PASSWORD else "no password (set FUNDA_SEARCH_PASSWORD)"
    log(f"serving on http://{args.host}:{args.port} (fetch {interval:.0f}s, status refresh {status_interval:.0f}s, {auth})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log("shutting down")


if __name__ == "__main__":
    main()
