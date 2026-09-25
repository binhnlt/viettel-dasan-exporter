"""Entry point.

    python -m modem_exporter            # serve /metrics (default)
    python -m modem_exporter once       # scrape once and print metrics to stdout
    python -m modem_exporter discover   # crawl the web UI and dump pages + parsed fields to ./dump
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import signal
import sys
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from prometheus_client.registry import CollectorRegistry

from .config import Config, load_dotenv
from .metrics import ModemCollector
from .parse import extract

log = logging.getLogger("modem_exporter")


def make_handler(registry: CollectorRegistry):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            path = self.path.split("?")[0]
            if path == "/metrics":
                body, ctype, code = generate_latest(registry), CONTENT_TYPE_LATEST, 200
            elif path in ("/health", "/-/healthy"):
                body, ctype, code = b"ok\n", "text/plain", 200
            elif path == "/":
                body = b"<html><body><h1>Modem exporter</h1><p><a href='/metrics'>Metrics</a></p></body></html>"
                ctype, code = "text/html", 200
            else:
                body, ctype, code = b"not found\n", "text/plain", 404
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt, *args):
            log.debug("%s - %s", self.address_string(), fmt % args)

    return Handler


def serve(cfg: Config) -> None:
    registry = CollectorRegistry()
    registry.register(ModemCollector(cfg))
    server = ThreadingHTTPServer((cfg.listen_address, cfg.listen_port), make_handler(registry))
    signal.signal(signal.SIGTERM, lambda *_: (server.shutdown(), sys.exit(0)))
    log.info("Exporter for %s listening on http://%s:%d/metrics", cfg.url, cfg.listen_address, cfg.listen_port)
    server.serve_forever()


def once(cfg: Config) -> None:
    registry = CollectorRegistry()
    registry.register(ModemCollector(cfg))
    sys.stdout.write(generate_latest(registry).decode())


def discover(cfg: Config, out_dir: str) -> None:
    from . import dzs
    from .client import ModemClient

    os.makedirs(out_dir, exist_ok=True)
    client = ModemClient(cfg)
    if cfg.driver == "dzs" or (cfg.driver == "auto" and dzs.detect(client.session, cfg.url, cfg.timeout)):
        driver = dzs.DzsDriver(cfg, client._new_session)
        try:
            objects = driver.dump()
        finally:
            driver.logout()
        with open(os.path.join(out_dir, "dzs_objects.json"), "w", encoding="utf-8") as fh:
            json.dump(objects, fh, indent=2, ensure_ascii=False)
        print(f"DZS API: saved {len(objects)} object(s) to {out_dir}/dzs_objects.json (secrets masked)")
        return
    # Save the login page first; it is the most useful thing to inspect when login fails.
    try:
        r = client.get("/")
        with open(os.path.join(out_dir, "_login_page.html"), "w", encoding="utf-8") as fh:
            fh.write(r.text)
    except Exception as exc:  # noqa: BLE001
        log.error("Cannot reach %s: %s", cfg.url, exc)
        return
    pages = client.discover()
    summary = {}
    for p in pages:
        fname = re.sub(r"[^\w.-]+", "_", p.path.strip("/")) or "index"
        with open(os.path.join(out_dir, fname), "w", encoding="utf-8") as fh:
            fh.write(p.text)
        ex = extract(p.path, p.content_type, p.text)
        summary[p.path] = {
            "file": fname,
            "content_type": p.content_type,
            "fields": [asdict(f) for f in ex.fields],
            "tables": [asdict(t) for t in ex.tables],
        }
    for entry in summary.values():
        for f in entry["fields"]:
            if re.search(r"pass|pwd|psk|key|secret", f["label"], re.IGNORECASE):
                f["value"] = "***"
    with open(os.path.join(out_dir, "_summary.json"), "w", encoding="utf-8") as fh:
        json.dump(summary, fh, indent=2, ensure_ascii=False)
    client.logout()
    n_fields = sum(len(e["fields"]) for e in summary.values())
    print(f"Saved {len(pages)} page(s) with {n_fields} field(s) to {out_dir}/ (see _summary.json)")


def main() -> None:
    parser = argparse.ArgumentParser(prog="modem_exporter", description="Prometheus exporter for ISP modem web UIs")
    parser.add_argument("command", nargs="?", default="serve", choices=["serve", "once", "discover"])
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--out", default="dump", help="output directory for `discover`")
    args = parser.parse_args()

    load_dotenv(args.env_file)
    cfg = Config.from_env()
    logging.basicConfig(level=cfg.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("urllib3").setLevel(logging.WARNING)

    {"serve": lambda: serve(cfg), "once": lambda: once(cfg), "discover": lambda: discover(cfg, args.out)}[args.command]()


if __name__ == "__main__":
    main()
