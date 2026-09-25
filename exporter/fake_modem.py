"""Tiny fake modem web UI used to test the exporter without real hardware.

Run: python tests/fake_modem.py 8080   (login: admin / secret)
"""
import http.cookies
import secrets
import sys
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SESSIONS: set[str] = set()
CALLS: list[str] = []
TOKEN = "tok123"

LOGIN = f"""<html><body><form method="post" action="/login.cgi">
<input type="hidden" name="csrf_token" value="{TOKEN}">
<input type="text" name="Username"><input type="password" name="Password">
<input type="submit" value="Login"></form></body></html>"""

INDEX = """<html><body><ul id="menu">
<li><a href="/status/device.html">Device</a></li>
<li><a href="/status/pon.html">PON</a></li>
<li><a href="/status/stats.html">Statistics</a></li>
<li><a href="/admin/reboot.cgi">Reboot</a></li>
<li><a href="/logout.cgi">Logout</a></li></ul>
<script>var menu = ["/status/wifi.asp"];</script></body></html>"""

PAGES = {
    "/status/device.html": """<html><body><div class="card"><div class="card-title">Device Information</div>
<div class="row"><label>Model Name</label><span>H646GM-V</span></div>
<div class="row"><label>Software Version</label><span>VT5.2.2140138</span></div>
<div class="row"><label>Uptime</label><span>11 days, 19 hours, 1 minute, 23 seconds</span></div>
<div class="row"><label>MAC OUI</label><span>24:43:e2</span></div>
<div class="row"><label>GPON Serial Number</label><span>DSNW28ee2a28</span></div>
<div class="row"><label>System MAC Address</label><span>24:43:E2:EE:2A:28</span></div>
<div class="row"><label>CPU Usage</label><span>12 %</span></div>
<div class="row"><label>Memory Usage</label><span>47%</span></div></div></body></html>""",
    "/status/pon.html": """<html><body><h3>Optical Information</h3><table>
<tr><td>PON Status</td><td>O5</td></tr><tr><td>Link Status</td><td>Up</td></tr>
<tr><td>Rx Optical Power</td><td>-18.52 dBm</td></tr><tr><td>Tx Optical Power</td><td>2.31 dBm</td></tr>
<tr><td>Temperature</td><td>45.2 C</td></tr><tr><td>Supply Voltage</td><td>3.28 V</td></tr>
<tr><td>Bias Current</td><td>14.6 mA</td></tr></table></body></html>""",
    "/status/stats.html": """<html><body><h3>LAN/WAN Statistics</h3><table>
<tr><th rowspan=2>Interface</th><th colspan=3>Received</th><th colspan=3>Sent</th></tr>
<tr><th>Bytes</th><th>Packets</th><th>Errors</th><th>Bytes</th><th>Packets</th><th>Errors</th></tr>
<tr><td>LAN1</td><td>1,234,567</td><td>9000</td><td>0</td><td>7654321</td><td>8000</td><td>1</td></tr>
<tr><td>WAN</td><td>99887766</td><td>55555</td><td>2</td><td>11223344</td><td>44444</td><td>0</td></tr>
</table></body></html>""",
    "/status/wifi.asp": """<html><body><h3>WLAN</h3><table>
<tr><td>SSID</td><td>MyWifi</td></tr><tr><td>WPA Key</td><td>supersecret</td></tr>
<tr><td>Status</td><td>Enabled</td></tr><tr><td>Associated Clients</td><td>5</td></tr></table>
<script>var wlChannel = "6"; var txPowerLevel = 100;</script></body></html>""",
}


class H(BaseHTTPRequestHandler):
    def _session(self):
        c = http.cookies.SimpleCookie(self.headers.get("Cookie", ""))
        return c["sid"].value if "sid" in c and c["sid"].value in SESSIONS else None

    def _send(self, body, code=200, headers=None):
        self.send_response(code)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(body.encode())

    def do_GET(self):
        CALLS.append(self.path)
        if "reboot" in self.path:
            print("!!! REBOOT CALLED", flush=True)
        if self.path == "/logout.cgi":
            SESSIONS.discard(self._session())
            return self._send("bye")
        if not self._session():
            return self._send(LOGIN)
        if self.path in ("/", "/index.html"):
            return self._send(INDEX)
        if self.path in PAGES:
            return self._send(PAGES[self.path])
        self._send("nf", 404)

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        data = urllib.parse.parse_qs(self.rfile.read(n).decode())
        ok = data.get("Username") == ["admin"] and data.get("Password") == ["secret"] and data.get("csrf_token") == [TOKEN]
        if not ok:
            return self._send(LOGIN)
        sid = secrets.token_hex(8)
        SESSIONS.add(sid)
        self._send("", 302, {"Set-Cookie": f"sid={sid}; Path=/", "Location": "/"})

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", int(sys.argv[1]) if len(sys.argv) > 1 else 8080), H).serve_forever()
