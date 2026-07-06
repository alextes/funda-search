#!/usr/bin/env python3
"""Serve the overview and keep the listing data fresh.

A background thread re-fetches whenever the last check is older than the
configured interval (default 60s), so the list is always at most that old
while the server runs. The HTTP side only ever serves the latest
overview.html from disk (written atomically by fetch.render).
"""

from __future__ import annotations

import argparse
import sys
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import fetch as core

DEFAULT_INTERVAL_S = 60

state = {"last_fetch": None, "fetch_count": 0, "last_error": None}


def log(msg: str) -> None:
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


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


def fetch_loop(interval: float) -> None:
    while True:
        started = time.monotonic()
        try:
            fetch_once()
        except Exception as e:
            state["last_error"] = f"{datetime.now():%Y-%m-%d %H:%M:%S} {e}"
            log(f"fetch failed: {e}")
        time.sleep(max(1.0, interval - (time.monotonic() - started)))


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html", "/overview.html"):
            if not core.OVERVIEW_FILE.exists():
                self.respond(503, "text/plain", b"no overview yet, first fetch still running")
                return
            self.respond(200, "text/html; charset=utf-8", core.OVERVIEW_FILE.read_bytes())
        elif path == "/healthz":
            body = (
                f"last_fetch: {state['last_fetch']}\n"
                f"fetch_count: {state['fetch_count']}\n"
                f"last_error: {state['last_error']}\n"
            )
            self.respond(200, "text/plain; charset=utf-8", body.encode())
        elif path == "/listings.json":
            self.respond(200, "application/json; charset=utf-8", core.DATA_FILE.read_bytes())
        else:
            self.respond(404, "text/plain", b"not found")

    def respond(self, status: int, content_type: str, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
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

    interval = args.interval
    if interval is None:
        interval = core.load_config().get("fetch_interval_seconds", DEFAULT_INTERVAL_S)

    threading.Thread(target=fetch_loop, args=(interval,), daemon=True).start()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    log(f"serving on http://{args.host}:{args.port} (fetch interval {interval:.0f}s)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log("shutting down")


if __name__ == "__main__":
    main()
