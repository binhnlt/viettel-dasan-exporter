"""Map extracted fields/tables to Prometheus metrics and expose them via a custom collector."""
from __future__ import annotations

import logging
import math
import re
import threading
import time
from urllib.parse import urlparse

import requests
from prometheus_client.core import CounterMetricFamily, GaugeMetricFamily
from prometheus_client.registry import Collector

from .client import LoginError, ModemClient, Page
from .config import Config
from .parse import Extracted, Field, Table, extract

log = logging.getLogger(__name__)

NUMBER_RE = re.compile(r"^\s*([-+]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?(?:[eE][-+]?\d+)?)\s*([A-Za-z°%µ/]{0,8})\s*$")
DURATION_PART = re.compile(r"(\d+)\s*(d|day|days|h|hr|hrs|hour|hours|m|min|mins|minute|minutes|s|sec|secs|second|seconds)\b", re.IGNORECASE)
HMS_RE = re.compile(r"^(?:(\d+)\s*(?:d|days?)[,\s]*)?(\d{1,3}):(\d{2}):(\d{2})$", re.IGNORECASE)
SECRET_RE = re.compile(r"pass|pwd|psk|secret|token|wpa|wep|key|session|cookie|csrf|nonce", re.IGNORECASE)

INFO_FIELDS = [
    (re.compile(r"model|product\s*(name|class)|device\s*(type|model)", re.IGNORECASE), "model"),
    (re.compile(r"software\s*version|firmware|sw\s*ver|^version$", re.IGNORECASE), "software_version"),
    (re.compile(r"hardware\s*version|hw\s*ver", re.IGNORECASE), "hardware_version"),
    (re.compile(r"boot\s*(loader)?\s*version", re.IGNORECASE), "boot_version"),
    (re.compile(r"(gpon|pon|ont)\s*serial|serial\s*n(umber|o)|^sn$", re.IGNORECASE), "serial_number"),
    (re.compile(r"system\s*mac|base\s*mac|^mac\s*address$|device\s*mac", re.IGNORECASE), "mac_address"),
    (re.compile(r"mac\s*oui|^oui$", re.IGNORECASE), "mac_oui"),
    (re.compile(r"manufacturer|vendor", re.IGNORECASE), "manufacturer"),
    (re.compile(r"^(device|host)\s*name$", re.IGNORECASE), "device_name"),
]

UP_WORDS = {"up", "connected", "enabled", "enable", "on", "active", "online", "link up", "ok", "normal", "established", "yes", "o5", "true"}
DOWN_WORDS = {"down", "disconnected", "disabled", "disable", "off", "inactive", "offline", "link down", "no link", "no", "false", "failed", "error", "not connected"}

RX_RE = re.compile(r"\b(rx|receiv\w*|recv|in(bound)?|down(stream|load)?)\b|received", re.IGNORECASE)
TX_RE = re.compile(r"\b(tx|sent|send|transmit\w*|out(bound)?|up(stream|load)?)\b|sent", re.IGNORECASE)
COUNTER_KINDS = [
    (re.compile(r"byte|octet", re.IGNORECASE), "bytes"),
    (re.compile(r"packet|pkt|frame", re.IGNORECASE), "packets"),
    (re.compile(r"error|err\b|crc|fcs", re.IGNORECASE), "errors"),
    (re.compile(r"drop|discard", re.IGNORECASE), "drops"),
]


def snake(text: str) -> str:
    s = re.sub(r"[^0-9a-zA-Z]+", "_", text).strip("_").lower()
    return s or "unknown"


def parse_number(value: str) -> tuple[float, str] | None:
    m = NUMBER_RE.match(value)
    if not m:
        return None
    try:
        return float(m.group(1).replace(",", "")), m.group(2)
    except ValueError:
        return None


def parse_duration(value: str) -> float | None:
    v = value.strip()
    m = HMS_RE.match(v)
    if m:
        d, h, mi, s = (int(x) if x else 0 for x in m.groups())
        return d * 86400 + h * 3600 + mi * 60 + s
    parts = DURATION_PART.findall(v)
    if not parts:
        return None
    # Everything except the matched parts and separators must be empty.
    rest = DURATION_PART.sub("", v)
    if re.sub(r"[\s,and]+", "", rest):
        return None
    mult = {"d": 86400, "h": 3600, "m": 60, "s": 1}
    total = 0
    for num, unit in parts:
        u = unit.lower()
        key = "m" if u.startswith("mi") or u == "m" else u[0]
        total += int(num) * mult[key]
    return float(total)


def status_value(value: str) -> float | None:
    v = value.strip().lower()
    if v in UP_WORDS or v.startswith(("up", "connected", "link up")):
        return 1.0
    if v in DOWN_WORDS or v.startswith(("down", "disconnected", "link down")):
        return 0.0
    return None


class _Families:
    """Accumulates samples per metric, de-duplicating identical label sets.

    Samples of one metric may carry different label names; missing labels are
    filled with "" on output so every sample of a metric has the same label set.
    """

    def __init__(self, modem: str):
        self.modem = modem
        self.defs: dict[str, tuple[str, str, list[str]]] = {}
        self.samples: dict[str, dict[tuple, float]] = {}

    def add(self, name: str, help_: str, labels: dict[str, str], value: float, kind: str = "gauge") -> None:
        if value is None or (isinstance(value, float) and (math.isnan(value) or math.isinf(value))):
            return
        labels = {"modem": self.modem, **{k: str(v)[:128] for k, v in labels.items()}}
        if name not in self.defs:
            self.defs[name] = (kind, help_, [])
            self.samples[name] = {}
        names = self.defs[name][2]
        names.extend(n for n in labels if n not in names)
        key = tuple(sorted(labels.items()))
        self.samples[name].setdefault(key, float(value))

    def families(self):
        for name, (kind, help_, names) in self.defs.items():
            cls = CounterMetricFamily if kind == "counter" else GaugeMetricFamily
            fam = cls(name, help_, labels=names)
            for key, value in self.samples[name].items():
                labels = dict(key)
                fam.add_metric([labels.get(n, "") for n in names], value)
            yield fam


def _counter_name(text: str) -> str | None:
    kind = next((k for rx, k in COUNTER_KINDS if rx.search(text)), None)
    if not kind:
        return None
    if RX_RE.search(text):
        return f"modem_interface_rx_{kind}"
    if TX_RE.search(text):
        return f"modem_interface_tx_{kind}"
    return None


def _optical(label: str, num: float, unit: str) -> tuple[str, str, float] | None:
    """Known physical measurements (GPON optics, CPU, memory)."""
    u = unit.lower().replace("µ", "u")
    l = label.lower()
    if re.search(r"power", l) and (re.search(r"\b(rx|receiv\w*|input|recv)\b", l) or "rx" in l):
        if u == "mw" and num > 0:
            num = 10 * math.log10(num)
        elif u == "uw" and num > 0:
            num = 10 * math.log10(num / 1000)
        return "modem_optical_rx_power_dbm", "Received optical power (dBm)", num
    if re.search(r"power", l) and (re.search(r"\b(tx|transmit\w*|output)\b", l) or "tx" in l):
        if u == "mw" and num > 0:
            num = 10 * math.log10(num)
        elif u == "uw" and num > 0:
            num = 10 * math.log10(num / 1000)
        return "modem_optical_tx_power_dbm", "Transmitted optical power (dBm)", num
    if "temp" in l:
        if u in ("f", "°f"):
            num = (num - 32) * 5 / 9
        return "modem_temperature_celsius", "Temperature (°C)", num
    if "bias" in l:
        if u == "ua":
            num /= 1000
        return "modem_optical_bias_current_milliamps", "Laser bias current (mA)", num
    if re.search(r"volt|vcc|supply", l):
        if u == "mv":
            num /= 1000
        return "modem_optical_supply_voltage_volts", "Transceiver supply voltage (V)", num
    if "cpu" in l and (u == "%" or re.search(r"usage|util|load", l)):
        return "modem_cpu_usage_percent", "CPU usage (%)", num
    if re.search(r"mem|ram", l):
        if u == "%" or re.search(r"usage|util", l):
            return "modem_memory_usage_percent", "Memory usage (%)", num
        mult = {"kb": 1024, "k": 1024, "mb": 1024**2, "m": 1024**2, "gb": 1024**3, "b": 1, "": 1}.get(u)
        if mult:
            kind = "free" if re.search(r"free|avail", l) else "total" if "total" in l else "used" if "used" in l else None
            if kind:
                return f"modem_memory_{kind}_bytes", f"Memory {kind} (bytes)", num * mult
    return None


def build_metrics(fam: _Families, extracted: list[Extracted]) -> None:
    info: dict[str, str] = {}
    for ex in extracted:
        for f in ex.fields:
            _map_field(fam, f, info)
        for t in ex.tables:
            _map_table(fam, t)
    if info:
        fam.add("modem_info", "Static modem information (value is always 1)", dict(sorted(info.items())), 1)


def _map_field(fam: _Families, f: Field, info: dict[str, str]) -> None:
    label, value = f.label, f.value
    if SECRET_RE.search(label):
        return  # never export credentials / Wi-Fi keys
    base = {"page": f.page, "section": f.section, "field": label}

    for rx, key in INFO_FIELDS:
        if rx.search(label) and not parse_number(value):
            info.setdefault(key, value)
            return

    if re.search(r"up\s*time|uptime|running\s*time|online\s*time|connect(ed|ion)?\s*time", label, re.IGNORECASE):
        secs = parse_duration(value)
        if secs is not None:
            if re.fullmatch(r"(system\s*)?up\s*time", label.strip(), re.IGNORECASE) or "system" in f.section.lower() or "device" in f.section.lower():
                fam.add("modem_uptime_seconds", "Modem system uptime in seconds", {}, secs)
            else:
                fam.add("modem_connection_uptime_seconds", "Uptime of a connection/interface in seconds",
                        {"section": f.section, "field": label}, secs)
            return

    num = parse_number(value)
    if num is not None:
        n, unit = num
        known = _optical(label, n, unit) if f.section != "js" else None
        if known:
            name, help_, v = known
            fam.add(name, help_, {"section": f.section} if "optical" not in name else {}, v)
            return
        cname = _counter_name(label)
        if cname:
            fam.add(cname, cname.replace("modem_interface_", "Interface ").replace("_", " ") + " counter",
                    {"interface": f.section or f.page}, n, kind="counter")
            return
        fam.add("modem_field_value", "Numeric value scraped from the modem web UI", {**base, "unit": unit}, n)
        return

    if re.search(r"(pon|gpon|ont|onu).*(state|status)", label, re.IGNORECASE):
        m = re.search(r"\bO(\d)\b", value, re.IGNORECASE)
        if m:
            fam.add("modem_pon_state", "GPON ONU activation state (5 = O5 operational)", {}, int(m.group(1)))

    st = status_value(value)
    if st is not None:
        fam.add("modem_field_status", "Status field from the modem web UI (1 = up/enabled, 0 = down/disabled)", base, st)
    if len(value) <= 64:
        fam.add("modem_field_info", "Text value scraped from the modem web UI (value is always 1)", {**base, "value": value}, 1)


def _map_table(fam: _Families, t: Table) -> None:
    if not t.rows:
        return
    table_name = t.section or "table"
    for row in t.rows:
        key = row[0] or "row"
        if SECRET_RE.search(key):
            continue
        for col, cell in zip(t.columns[1:], row[1:]):
            if SECRET_RE.search(col):
                continue
            num = parse_number(cell)
            header = f"{table_name} {col}"
            if num is None:
                st = status_value(cell)
                if st is not None:
                    fam.add("modem_table_status", "Status cell from a modem web UI table (1 = up, 0 = down)",
                            {"page": t.page, "table": table_name, "row": key, "column": col}, st)
                continue
            n, unit = num
            cname = _counter_name(col) or _counter_name(header)
            if cname:
                fam.add(cname, cname.replace("modem_interface_", "Interface ").replace("_", " ") + " counter",
                        {"interface": key}, n, kind="counter")
                continue
            fam.add("modem_table_value", "Numeric cell from a modem web UI table",
                    {"page": t.page, "table": table_name, "row": key, "column": col, "unit": unit}, n)


def _has_data(ex: Extracted) -> bool:
    return bool(ex.fields or ex.tables)


class ModemCollector(Collector):
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.client = ModemClient(cfg)
        self.modem = cfg.name or urlparse(cfg.url).hostname or cfg.url
        self.lock = threading.Lock()
        self.cached: list = []
        self.cached_at = 0.0
        self.pages: list[str] = list(cfg.pages)
        self.discovered_at = 0.0
        self.scrapes_total = 0
        self.errors_total = 0
        self.driver = None  # DzsDriver, or None for the generic HTML scraper
        self.driver_name = ""
        self.login_blocked_until = 0.0

    def _select_driver(self) -> None:
        if self.driver_name:
            return
        from . import dzs

        wanted = self.cfg.driver
        if wanted in ("dzs", "auto") and (wanted == "dzs" or dzs.detect(self.client.session, self.cfg.url, self.cfg.timeout)):
            self.driver = dzs.DzsDriver(self.cfg, self.client._new_session)
            self.driver_name = "dzs"
        else:
            self.driver_name = "html"
        log.info("Using %s driver for %s", self.driver_name, self.cfg.url)

    # ------------------------------------------------------------ scraping
    def _fetch_paths(self, paths: list[str]) -> list[Page]:
        out = []
        for path in paths:
            try:
                p = self.client.fetch(path)
            except requests.RequestException as exc:
                log.warning("Fetch %s failed: %s", path, exc)
                continue
            if p:
                out.append(p)
        return out

    def _html_pages(self) -> list[Page]:
        if self.cfg.pages:
            return self._fetch_paths(self.cfg.pages)
        stale = time.time() - self.discovered_at > self.cfg.rediscover_seconds
        if not self.pages or stale:
            pages = self.client.discover()
            useful = [p for p in pages if _has_data(extract(p.path, p.content_type, p.text))]
            self.pages = [p.path for p in useful]
            self.discovered_at = time.time()
            log.info("Using %d page(s) with data: %s", len(self.pages), ", ".join(self.pages))
            return useful
        return self._fetch_paths(self.pages)

    def _scrape_html(self, fam: _Families) -> int:
        pages = self._html_pages()
        build_metrics(fam, [extract(p.path, p.content_type, p.text) for p in pages])
        return len(pages)

    def scrape(self) -> list:
        fam = _Families(self.modem)
        start = time.time()
        up, count = 0, 0
        if time.time() < self.login_blocked_until:
            log.info("Login backoff active for %.0fs more; not contacting the modem", self.login_blocked_until - time.time())
        else:
            try:
                self._select_driver()
                count = self.driver.scrape(fam) if self.driver else self._scrape_html(fam)
                up = 1 if count else 0
            except LoginError as exc:
                wait = max(getattr(exc, "retry_after", 0.0), self.cfg.login_backoff_seconds)
                self.login_blocked_until = time.time() + wait
                log.error("Login failed: %s (next attempt in %.0fs)", exc, wait)
                self.errors_total += 1
            except requests.RequestException as exc:
                log.error("Modem unreachable: %s", exc)
                self.errors_total += 1
                self.client.logged_in = False
                if self.driver:
                    self.driver.token = None
            except Exception:
                log.exception("Unexpected scrape error")
                self.errors_total += 1
            finally:
                if self.cfg.logout_after_scrape:
                    try:
                        (self.driver or self.client).logout()
                    except Exception as exc:  # noqa: BLE001
                        log.debug("Logout failed: %s", exc)
        self.scrapes_total += 1
        fam.add("modem_up", "1 if the modem was scraped successfully", {}, up)
        fam.add("modem_exporter_info", "Exporter driver in use", {"driver": self.driver_name or "unknown"}, 1)
        fam.add("modem_scrape_duration_seconds", "Time spent scraping the modem", {}, time.time() - start)
        fam.add("modem_scrape_objects", "Number of pages/API objects scraped", {}, count)
        fam.add("modem_scrape_last_timestamp_seconds", "Unix time of the last scrape", {}, time.time())
        fam.add("modem_login_backoff_seconds", "Seconds until the next login attempt is allowed (0 = none)", {},
                max(0.0, self.login_blocked_until - time.time()))
        fam.add("modem_exporter_scrapes", "Scrapes performed by the exporter", {}, self.scrapes_total, kind="counter")
        fam.add("modem_exporter_scrape_errors", "Failed scrapes", {}, self.errors_total, kind="counter")
        return list(fam.families())

    def collect(self):
        with self.lock:
            if not self.cached or time.time() - self.cached_at >= self.cfg.cache_seconds:
                self.cached = self.scrape()
                self.cached_at = time.time()
            yield from self.cached
