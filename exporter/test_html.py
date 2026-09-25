"""Tests for the generic HTML driver: value parsing and an end-to-end scrape of
the in-process fake modem (fake_modem.py). No network access beyond localhost.
"""
import threading
from http.server import ThreadingHTTPServer

import fake_modem
import pytest
from modem_exporter.config import Config
from modem_exporter.metrics import ModemCollector, parse_duration, parse_number
from modem_exporter.parse import extract


# --- value parsing -----------------------------------------------------------
@pytest.mark.parametrize("text,seconds", [
    ("11 days, 19 hours, 1 minute, 23 seconds", 11 * 86400 + 19 * 3600 + 60 + 23),
    ("1d 2h 3m 4s", 86400 + 7200 + 180 + 4),
    ("02:03:04", 7384),
    ("3 days 01:00:00", 3 * 86400 + 3600),
])
def test_parse_duration(text, seconds):
    assert parse_duration(text) == seconds


@pytest.mark.parametrize("text", ["VT5.2.2140138", "24:43:E2:EE:2A:28", "192.168.1.1", "Up"])
def test_parse_number_rejects_non_numbers(text):
    assert parse_number(text) is None


def test_parse_number_units():
    assert parse_number("-18.52 dBm") == (-18.52, "dBm")
    assert parse_number("1,234,567") == (1234567.0, "")
    assert parse_number("47%") == (47.0, "%")


def test_extract_label_value_rows():
    ex = extract("/p", "text/html", fake_modem.PAGES["/status/device.html"])
    fields = {f.label: f.value for f in ex.fields}
    assert fields["Model Name"] == "H646GM-V"
    assert fields["GPON Serial Number"] == "DSNW28ee2a28"


def test_extract_table_with_rowspan_colspan():
    ex = extract("/p", "text/html", fake_modem.PAGES["/status/stats.html"])
    (table,) = ex.tables
    assert table.columns[1:4] == ["Received Bytes", "Received Packets", "Received Errors"]
    assert table.rows[0][0] == "LAN1"


# --- end-to-end against the fake modem ----------------------------------------
@pytest.fixture
def fake_url():
    server = ThreadingHTTPServer(("127.0.0.1", 0), fake_modem.H)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    fake_modem.CALLS.clear()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def scrape(url, password="secret"):
    cfg = Config(url=url, username="admin", password=password, driver="html", timeout=5)
    families = {f.name: f for f in ModemCollector(cfg).collect()}
    return families, {(s.name, tuple(sorted((k, v) for k, v in s.labels.items() if k != "modem"))): s.value
                      for f in families.values() for s in f.samples}


def test_scrape_fake_modem(fake_url):
    _, values = scrape(fake_url)
    assert values[("modem_up", ())] == 1
    assert values[("modem_uptime_seconds", ())] == 11 * 86400 + 19 * 3600 + 60 + 23
    assert values[("modem_optical_rx_power_dbm", ())] == -18.52
    assert values[("modem_interface_rx_bytes_total", (("interface", "WAN"),))] == 99887766


def test_scrape_never_follows_dangerous_links_or_leaks_secrets(fake_url):
    _, values = scrape(fake_url)
    assert not any("reboot" in c for c in fake_modem.CALLS)
    assert "supersecret" not in str(values)


def test_scrape_wrong_password_reports_down(fake_url):
    _, values = scrape(fake_url, password="nope")
    assert values[("modem_up", ())] == 0
    assert values[("modem_login_backoff_seconds", ())] > 0
