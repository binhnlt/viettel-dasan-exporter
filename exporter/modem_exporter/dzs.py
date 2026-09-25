"""Driver for DASAN / DZS GPON ONTs with the Vue web UI and the JSON "dm" API
(e.g. Viettel H646GM-V). Endpoints were reverse-engineered from the web UI bundle:

    GET  /dm/sys/?cmd=Login                   -> login info (captcha mode, encodeEnable, model)
    POST /dm/sys/?cmd=Login                   -> {"Login":{"data":{"username","password","captcha"}}}
    GET  /dm/sys/?objs=Permission             -> objects readable by this account
    GET  /dm/tr98/?objs=<Object>&page=<Page>  -> data (Authorization: Bearer <token>)
    GET  /dm/sys/?cmd=Logout

Only GET requests are made after login; the API performs changes via POST/DELETE.
"""
from __future__ import annotations

import base64
import logging
import re
import time

import requests

from .client import LoginError
from .config import Config

log = logging.getLogger(__name__)

LOGIN = "/dm/sys/?cmd=Login"
LOGOUT = "/dm/sys/?cmd=Logout"
PERMISSION = "/dm/sys/?objs=Permission"

# Fallback when the Permission object can't be read. (object, page)
DEFAULT_OBJECTS = [
    ("DeviceInfo", "StatusPage-DeviceInfo"),
    ("PonPortStatus", "StatusPage-DeviceInfo"),
    ("LANPortStatus", "StatusPage-DeviceInfo"),
    ("WANObject", "StatusPage-DeviceInfo"),
    ("WANIPConnection", "StatusPage-DeviceInfo"),
    ("WANPPPConnection", "StatusPage-DeviceInfo"),
    ("WLANConfiguration", "StatusPage-DeviceInfo"),
    ("WLAN11acConfiguration", "StatusPage-DeviceInfo"),
    ("WLANCommon", "StatusPage-DeviceInfo"),
    ("WLAN11acCommon", "StatusPage-DeviceInfo"),
    ("LANAddressConfiguration", "StatusPage-DeviceInfo"),
    ("RouteTable", "StatusPage-DeviceInfo"),
    ("StatisticsPonObj", "StatusPage-TrafficStatistic"),
    ("LANStatistics", "StatusPage-TrafficStatistic"),
    ("StatisticsWanObj", "StatusPage-TrafficStatistic"),
    ("StatisticsWlanObj", "StatusPage-TrafficStatistic"),
    ("StatisticsWlan11acObj", "StatusPage-TrafficStatistic"),
    ("WLANAssociatedDevice", "StatusPage-CurrentWirelessUser"),
    ("DhcpLease", "StatusPage-DHCPLease"),
    ("Dhcpv6Lease", "StatusPage-DHCPLease"),
    ("ARPStatus", "StatusPage-ARP"),
    ("ARP6Status", "StatusPage-ARP"),
    ("CwmpStatus", "StatusPage-TR069"),
    ("BandSteeringStatus", "StatusPage-BandSteering"),
    ("WifiMeshTopo", "WifiMeshPage-Status"),
]
READ_PAGES = re.compile(r"^(StatusPage-[\w-]+|WifiMeshPage-Status)$")
UNSAFE_OBJ = re.compile(r"reboot|restore|factory|upgrade|reset|diagnostic|ping|trace", re.IGNORECASE)
SECRET = re.compile(r"pass|psk|secret|key|token|wep|radius", re.IGNORECASE)
LOCKED = 9897
LOGIN_ERRORS = {9895: "wrong username or password", 9896: "wrong username or password", 9898: "wrong captcha"}


def detect(session: requests.Session, base: str, timeout: float) -> bool:
    try:
        r = session.get(base + LOGIN, headers={"Content-Type": "application/json"}, timeout=timeout)
        return r.ok and "Login" in r.json()
    except (requests.RequestException, ValueError):
        return False


def _num(v) -> float | None:
    if isinstance(v, bool):
        return 1.0 if v else 0.0
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        try:
            return float(v.strip())
        except ValueError:
            return None
    return None


def _up(v) -> float:
    return 1.0 if str(v).strip().lower() in ("up", "connected", "true", "enable", "enabled", "1") else 0.0


def _hms(v: str) -> float | None:
    parts = str(v).split(":")
    if not all(p.isdigit() for p in parts) or not 1 <= len(parts) <= 4:
        return None
    total = 0
    for p, m in zip(reversed(parts), (1, 60, 3600, 86400)):
        total += int(p) * m
    return float(total)


def _rows(data) -> list[dict]:
    if isinstance(data, list):
        return [d for d in data if isinstance(d, dict)]
    return [data] if isinstance(data, dict) else []


class DzsDriver:
    name = "dzs"

    def __init__(self, cfg: Config, session_factory):
        self.cfg = cfg
        self.base = cfg.url
        self.new_session = session_factory
        self.session = session_factory()
        self.token: str | None = None
        self.token_expires = 0.0
        self.login_info: dict = {}
        self.objects: list[tuple[str, str]] | None = None
        self.denied: set[str] = set()

    # ------------------------------------------------------------- session
    def _headers(self) -> dict:
        h = {"Content-Type": "application/json"}
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        return h

    def login(self) -> None:
        self.session = self.new_session()
        self.token = None
        r = self.session.get(self.base + LOGIN, headers=self._headers(), timeout=self.cfg.timeout)
        info = r.json().get("Login", {}).get("data", {}) or {}
        self.login_info = info
        mode = info.get("mode")
        if mode == "captcha":
            raise LoginError("Modem requires an image captcha for login; automated login is not possible")
        captcha = info.get("value", "") if mode == "image" else ""
        pw = self.cfg.password
        if info.get("encodeEnable"):
            pw = base64.b64encode(pw.encode()).decode()
        body = {"Login": {"data": {"username": self.cfg.username, "password": pw, "captcha": captcha}}}
        r = self.session.post(self.base + LOGIN, json=body, headers=self._headers(), timeout=self.cfg.timeout)
        resp = r.json().get("Login", {})
        data = resp.get("data") or {}
        if not data.get("login", {}).get("authenticatedToken"):
            err = resp.get("error") or {}
            code = err.get("code")
            if code == LOCKED:
                exc = LoginError(f"Account locked by modem for {err.get('BannedTime')}s after too many failed logins")
                exc.retry_after = float(err.get("BannedTime") or 300)
                raise exc
            reason = err.get("message") or LOGIN_ERRORS.get(code) or str(resp)
            raise LoginError(f"Login rejected (code {code}): {reason}")
        login = data["login"]
        self.token = login["authenticatedToken"]
        timeout = float(login.get("timeout") or 300)
        self.token_expires = time.time() + max(30.0, timeout - 30)
        log.info("Logged in to %s (DZS API)", self.base)

    def logout(self) -> None:
        if not self.token:
            return
        try:
            self.session.get(self.base + LOGOUT, headers=self._headers(), timeout=self.cfg.timeout)
        except requests.RequestException as exc:
            log.debug("Logout failed: %s", exc)
        self.token = None

    def _get(self, url: str):
        for attempt in range(2):
            if not self.token or time.time() > self.token_expires:
                self.login()
            r = self.session.get(self.base + url, headers=self._headers(), timeout=self.cfg.timeout)
            if r.status_code == 401:
                self.token = None
                continue
            payload = r.json()
            obj = next(iter(payload.values()), {}) if isinstance(payload, dict) else {}
            if isinstance(obj, dict) and obj.get("status_code") == 401 and attempt == 0:
                self.token = None
                continue
            return obj
        raise LoginError(f"Session rejected while fetching {url}")

    def fetch(self, obj: str, page: str) -> dict | list | None:
        key = f"{page}_{obj}"
        if key in self.denied:
            return None
        res = self._get(f"/dm/tr98/?objs={obj}&page={page}")
        if not isinstance(res, dict) or res.get("status_code") != 200:
            if isinstance(res, dict) and res.get("status_code") == 403:
                self.denied.add(key)
                log.info("Object %s (page %s) not permitted for this account; skipping", obj, page)
            return None
        return res.get("data")

    def _discover_objects(self) -> list[tuple[str, str]]:
        try:
            perm = self._get(PERMISSION)
            entries = (perm.get("data") or {}).get("permission") or []
        except (requests.RequestException, ValueError, LoginError) as exc:
            log.warning("Could not read permissions (%s); using default object list", exc)
            return DEFAULT_OBJECTS
        seen, out = set(), []
        for e in entries:
            name, mode = e.get("name", ""), e.get("permission", "H")
            if "_" not in name or "R" not in mode:
                continue
            page, obj = name.split("_", 1)
            if not READ_PAGES.match(page) or UNSAFE_OBJ.search(obj) or obj in seen:
                continue
            seen.add(obj)
            out.append((obj, page))
        log.info("Readable objects: %s", ", ".join(o for o, _ in out))
        return out or DEFAULT_OBJECTS

    # ------------------------------------------------------------- scrape
    def scrape(self, fam) -> int:
        if self.objects is None:
            if not self.token:
                self.login()
            self.objects = self._discover_objects()
        data: dict[str, object] = {}
        for obj, page in self.objects:
            try:
                d = self.fetch(obj, page)
            except (requests.RequestException, ValueError) as exc:
                log.warning("Fetch %s failed: %s", obj, exc)
                continue
            if d is not None:
                data[obj] = d
        Mapper(fam, data, self.login_info).run()
        return len(data)

    def dump(self) -> dict:
        """All readable objects, raw (secrets masked) - used by `discover`."""
        if not self.token:
            self.login()
        self.objects = self._discover_objects()
        out = {}
        for obj, page in self.objects:
            try:
                out[f"{page}_{obj}"] = _mask(self.fetch(obj, page))
            except (requests.RequestException, ValueError) as exc:
                out[f"{page}_{obj}"] = f"error: {exc}"
        return out


def _mask(o):
    if isinstance(o, dict):
        return {k: ("***" if SECRET.search(k) and v not in ("", None) and not isinstance(v, bool) else _mask(v)) for k, v in o.items()}
    if isinstance(o, list):
        return [_mask(x) for x in o]
    return o


class Mapper:
    """Maps DZS objects to Prometheus metrics. Unknown objects get generic numeric metrics."""

    def __init__(self, fam, data: dict, login_info: dict):
        self.fam, self.data, self.login_info = fam, data, login_info

    def g(self, name, help_, labels, value, kind="gauge"):
        v = _num(value) if not isinstance(value, float) else value
        if v is not None:
            self.fam.add(name, help_, labels, v, kind)

    def run(self):
        handled = set()
        for obj in list(self.data):
            fn = getattr(self, f"m_{obj}", None)
            if fn:
                fn(self.data[obj])
                handled.add(obj)
        for obj, d in self.data.items():
            if obj not in handled:
                self.generic(obj, d)

    def generic(self, obj, data):
        for i, row in enumerate(_rows(data)):
            idx = str(row.get("iid", row.get("Index", i)))
            for k, v in row.items():
                if SECRET.search(k) or isinstance(v, (dict, list)):
                    continue
                n = _num(v)
                if n is not None:
                    self.g("modem_object_value", "Numeric value from a modem API object",
                           {"object": obj, "index": idx, "field": k}, n)

    # --- device ------------------------------------------------------------
    def m_DeviceInfo(self, d):
        info = {
            "model": d.get("modelName", ""), "software_version": d.get("softwareVersion", ""),
            "serial_number": d.get("serialNumber", ""), "mac_address": d.get("MACAddress", ""),
            "mac_oui": d.get("manufacturerOUI", ""), "hardware_version": d.get("hardwareVersion", ""),
            "manufacturer": d.get("manufacturer", ""), "customer": self.login_info.get("customer", ""),
        }
        pon = self.data.get("PonPortStatus") or {}
        if isinstance(pon, dict):
            info.update({"pon_mode": pon.get("ponMode", ""), "pon_version": pon.get("ponVerSion", ""),
                         "olt_type": pon.get("oltType", ""), "optical_type": pon.get("opticalType", "")})
        self.fam.add("modem_info", "Static modem information (value is always 1)",
                     {k: str(v) for k, v in info.items() if v not in ("", None)}, 1)
        self.g("modem_uptime_seconds", "Modem system uptime in seconds", {}, d.get("UpTime"))
        self.g("modem_cpu_usage_percent", "CPU usage (%)", {}, d.get("CPU_Load"))
        self.g("modem_memory_usage_percent", "Memory usage (%)", {}, d.get("MemoryUsage"))
        self.g("modem_temperature_celsius", "System temperature (°C)", {}, d.get("Temperature"))

    # --- PON / optics ------------------------------------------------------
    def m_PonPortStatus(self, d):
        self.g("modem_pon_link_up", "PON link state (1 = up)", {}, _up(d.get("ponLinkState")))
        self.g("modem_pon_link_uptime_seconds", "PON link uptime in seconds", {}, d.get("ponLinkUptime"))
        self.g("modem_optical_rx_power_dbm", "Received optical power (dBm)", {}, d.get("ponRxPower"))
        self.g("modem_optical_tx_power_dbm", "Transmitted optical power (dBm)", {}, d.get("ponTxPower"))
        self.g("modem_optical_bias_current_milliamps", "Laser bias current (mA)", {}, d.get("ponTxBiasCur"))
        self.g("modem_optical_supply_voltage_volts", "Transceiver supply voltage (V)", {}, d.get("ponSupplyVolt"))
        self.g("modem_optical_temperature_celsius", "Transceiver temperature (°C)", {}, d.get("ponTemp"))
        self.g("modem_pon_fec_enabled", "Forward error correction enabled", {}, _up(d.get("fecStatus")))
        self.g("modem_pon_ber", "PON bit error rate status as reported", {}, d.get("berStatus"))
        self.g("modem_pon_onu_id", "ONU ID assigned by the OLT", {}, d.get("onuID"))
        for metric, key in (("rx_power_dbm", "RxPower"), ("tx_power_dbm", "TxPower"), ("supply_voltage_volts", "Voltage"),
                            ("temperature_celsius", "Temperature"), ("bias_current_milliamps", "TxBias")):
            for bound in ("lower", "upper"):
                self.g(f"modem_optical_{metric}_threshold", "Optical alarm threshold", {"bound": bound},
                       d.get(f"{bound}{key}Threshold"))

    def m_StatisticsPonObj(self, d):
        for row in _rows(d):
            self._traffic("pon", "pon", row, {
                "rx_bytes": "RxTotalByte", "tx_bytes": "TxTotalByte", "rx_packets": "RxTotalPacket",
                "tx_packets": "TxTotalPacket", "rx_crc_errors": "RxCrcPacket",
                "rx_undersize_packets": "RxUnderSizePacket", "tx_undersize_packets": "TxUnderSizePacket",
                "tx_collisions": "TxCollisionPacket",
            }, casts=True)
            self.g("modem_pon_rx_rate_bytes_per_second", "PON receive rate as reported by the modem", {}, row.get("RxRateBytePerSecond"))
            self.g("modem_pon_tx_rate_bytes_per_second", "PON transmit rate as reported by the modem", {}, row.get("TxRateBytePerSecond"))

    # --- traffic helpers ---------------------------------------------------
    def _traffic(self, kind, iface, row, mapping, casts=False, extra=None):
        labels = {"interface": iface, "type": kind, **(extra or {})}
        for metric, key in mapping.items():
            v = _num(row.get(key))
            if v is not None and v >= 0:
                self.fam.add(f"modem_interface_{metric}", f"Interface {metric.replace('_', ' ')} counter", labels, v, "counter")
        if casts:
            for direction in ("Rx", "Tx"):
                for cast in ("Unicast", "Multicast", "Broadcast"):
                    v = _num(row.get(f"{direction}{cast}Packet"))
                    if v is not None and v >= 0:
                        self.fam.add(f"modem_interface_{direction.lower()}_packets_by_cast",
                                     "Packets by cast type", {**labels, "cast": cast.lower()}, v, "counter")
                    v = _num(row.get(f"{direction}{cast}Byte"))
                    if v is not None and v >= 0:
                        self.fam.add(f"modem_interface_{direction.lower()}_bytes_by_cast",
                                     "Bytes by cast type", {**labels, "cast": cast.lower()}, v, "counter")

    # --- LAN ---------------------------------------------------------------
    def m_LANStatistics(self, d):
        for row in _rows(d):
            self._traffic("lan", f"lan{row.get('iid')}", row, {
                "rx_bytes": "rxTotalBytes", "tx_bytes": "txTotalBytes", "rx_packets": "rxFrames",
                "tx_packets": "txFrames", "rx_crc_errors": "rxCrcErrFrames", "tx_errors": "txErrFrames",
                "tx_collisions": "txCollFrames", "rx_undersize_packets": "rxUnderSizeFrames",
            })
            for direction in ("rx", "tx"):
                for cast, key in (("unicast", "Ucast"), ("multicast", "Mcast"), ("broadcast", "Bcast")):
                    v = _num(row.get(f"{direction}{key}Frames"))
                    if v is not None and v >= 0:
                        self.fam.add(f"modem_interface_{direction}_packets_by_cast", "Packets by cast type",
                                     {"interface": f"lan{row.get('iid')}", "type": "lan", "cast": cast}, v, "counter")

    def m_LANPortStatus(self, d):
        for row in _rows(d):
            port = {"port": f"lan{row.get('iid')}"}
            self.g("modem_lan_port_up", "LAN port link state (1 = up)", port, _up(row.get("Status")))
            self.g("modem_lan_port_admin_up", "LAN port administratively enabled", port, _up(row.get("Admin")))
            duplex, _, speed = str(row.get("Mode", "")).partition("/")
            if speed.isdigit():
                self.g("modem_lan_port_speed_mbps", "LAN port negotiated speed (Mbit/s)", port, float(speed))
                self.g("modem_lan_port_full_duplex", "LAN port duplex (1 = full)", port, 1.0 if duplex.lower() == "full" else 0.0)

    # --- WAN ---------------------------------------------------------------
    def _wan_names(self) -> dict[str, dict]:
        names = {}
        for row in _rows(self.data.get("WANObject")):
            names[str(row.get("iid"))] = row
        return names

    def m_WANObject(self, d):
        pass  # used as metadata by the WAN mappers

    def m_StatisticsWanObj(self, d):
        wans = self._wan_names()
        for row in _rows(d):
            iid = str(row.get("iid"))
            svc = wans.get(iid, {}).get("ServiceList", "")
            self._traffic("wan", f"wan{iid}", row, {
                "rx_bytes": "RxTotalByte", "tx_bytes": "TxTotalByte",
                "rx_packets": "RxTotalPacket", "tx_packets": "TxTotalPacket",
            }, casts=True, extra={"service": svc})

    def _wan_conn(self, d, kind):
        wans = self._wan_names()
        for row in _rows(d):
            iid = str(row.get("iid"))
            meta = wans.get(iid, {})
            if not row.get("enable") and not meta.get("Active"):
                continue
            if kind == "ppp" and meta and meta.get("ConnectionType") not in (None, "PPPoE"):
                continue
            if kind == "ppp" and not meta and iid in self._routed_ip_iids():
                continue  # WAN not listed in WANObject: the IP entry is the real one
            if kind == "ip" and meta and meta.get("ConnectionType") == "PPPoE":
                continue
            labels = {"wan": f"wan{iid}", "kind": kind, "service": str(row.get("ServiceList", ""))}
            self.g("modem_wan_connection_up", "WAN connection status (1 = connected)", {**labels, "ip_version": "ipv4"},
                   _up(row.get("connectionStatus")))
            if "connectionStatus6" in row:
                self.g("modem_wan_connection_up", "WAN connection status (1 = connected)", {**labels, "ip_version": "ipv6"},
                       _up(row.get("connectionStatus6")))
            self.g("modem_wan_connection_uptime_seconds", "WAN connection uptime in seconds",
                   {**labels, "ip_version": "ipv4"}, row.get("ConnectionTimeIpv4"))
            self.g("modem_wan_connection_uptime_seconds", "WAN connection uptime in seconds",
                   {**labels, "ip_version": "ipv6"}, row.get("ConnectionTimeIpv6"))
            self.g("modem_wan_mtu_bytes", "WAN MTU", labels, row.get("MTU"))
            self.fam.add("modem_wan_info", "WAN connection details (value is always 1)", {
                **labels,
                "connection_type": str(meta.get("ConnectionType") or row.get("connectionType", "")),
                "external_ip": str(row.get("externalIPAddress", "")),
                "gateway": str(row.get("defaultGateway", "")),
                "dns_servers": str(row.get("DNSServers", "")),
                "ipv6_address": str(row.get("ip6AddrGlobal", "")),
                "ipv6_prefix": f"{row.get('ip6AddrPrefix', '')}/{row.get('PrefixLen6', '')}" if row.get("ip6AddrPrefix") else "",
                "vlan": str(row.get("VLANId", "")),
                "mac_address": str(row.get("MACAddress", "")),
            }, 1)

    def _routed_ip_iids(self) -> set[str]:
        return {str(r.get("iid")) for r in _rows(self.data.get("WANIPConnection"))
                if r.get("enable") and r.get("connectionType") not in ("", "Unconfigured", None)}

    def m_WANPPPConnection(self, d):
        self._wan_conn(d, "ppp")

    def m_WANIPConnection(self, d):
        self._wan_conn(d, "ip")

    # --- Wi-Fi -------------------------------------------------------------
    def _ssids(self, obj) -> dict[str, str]:
        return {str(r.get("iid")): str(r.get("SSID", "")) for r in _rows(self.data.get(obj))}

    def _wlan_stats(self, d, band, cfg_obj):
        ssids = self._ssids(cfg_obj)
        for row in _rows(d):
            if "TxByteCount" not in row and "TxTotalByte" not in row:
                continue
            iid = str(row.get("iid"))
            self._traffic("wlan", f"wlan{band}_{iid}", row, {
                "rx_bytes": "RxByteCount", "tx_bytes": "TxByteCount", "rx_packets": "RxCount",
                "tx_packets": "TxCount", "rx_errors": "RxErrorCount", "tx_errors": "TxErrorCount",
                "rx_drops": "RxDropCount", "tx_drops": "TxDropCount",
            }, casts=True, extra={"band": band, "ssid": ssids.get(iid, "")})

    def m_StatisticsWlanObj(self, d):
        self._wlan_stats(d, "2.4ghz", "WLANConfiguration")

    def m_StatisticsWlan11acObj(self, d):
        self._wlan_stats(d, "5ghz", "WLAN11acConfiguration")

    def _wlan_cfg(self, d, band):
        for row in _rows(d):
            labels = {"band": band, "index": str(row.get("iid")), "ssid": str(row.get("SSID", ""))}
            self.g("modem_wifi_ssid_enabled", "SSID enabled (1 = on)", labels, _num(row.get("RadioEnabled")))
            self.g("modem_wifi_ssid_max_stations", "Max stations for SSID", labels, row.get("MaxStaNum"))
            if row.get("RadioEnabled"):
                self.fam.add("modem_wifi_ssid_info", "SSID details (value is always 1)", {
                    **labels, "security": str(row.get("Security", "")), "mac_address": str(row.get("MACAddress", "")),
                    "hidden": str(not row.get("SSIDAdvertisementEnabled", True)).lower(), "mode": str(row.get("Mode", "")),
                }, 1)

    def m_WLANConfiguration(self, d):
        self._wlan_cfg(d, "2.4ghz")

    def m_WLAN11acConfiguration(self, d):
        self._wlan_cfg(d, "5ghz")

    def _wlan_common(self, d, band, onoff):
        for row in _rows(d):
            labels = {"band": band}
            self.g("modem_wifi_radio_enabled", "Wi-Fi radio enabled (1 = on)", labels, _num(row.get(onoff)))
            self.g("modem_wifi_channel", "Current Wi-Fi channel", labels, row.get("CurrentChannel"))
            self.g("modem_wifi_auto_channel", "Automatic channel selection enabled", labels, _num(row.get("AutoChannelEnable")))
            self.fam.add("modem_wifi_radio_info", "Wi-Fi radio details (value is always 1)", {
                **labels, "standard": str(row.get("Standard", "")), "bandwidth": str(row.get("MaxBitRate", "")),
                "tx_power": str(row.get("TransmitPower", "")), "country": str(row.get("Country", "")),
            }, 1)

    def m_WLANCommon(self, d):
        self._wlan_common(d, "2.4ghz", "WlanRadioOnOff")

    def m_WLAN11acCommon(self, d):
        self._wlan_common(d, "5ghz", "Wlan11acRadioOnOff")

    def m_WLANAssociatedDevice(self, d):
        rows = _rows(d)
        self.g("modem_wifi_clients", "Associated Wi-Fi clients", {}, float(len(rows)))
        for row in rows:
            mac = str(row.get("MACAddress") or row.get("MAC") or row.get("AssociatedDeviceMACAddress") or row.get("iid"))
            for k, v in row.items():
                n = _num(v)
                if n is not None and not isinstance(v, bool) and k not in ("iid",):
                    self.g("modem_wifi_client_value", "Numeric attribute of an associated Wi-Fi client",
                           {"mac": mac, "field": k}, n)

    def m_BandSteeringStatus(self, d):
        for band, sfx in (("2.4ghz", "2G"), ("5ghz", "5G")):
            self.g("modem_wifi_band_steering_threshold", "Band steering threshold", {"band": band}, d.get(f"Threshold{sfx}"))
            self.g("modem_wifi_band_steering_current", "Band steering current value", {"band": band}, d.get(f"CurThreshold{sfx}"))

    # --- LAN hosts ---------------------------------------------------------
    def m_DhcpLease(self, d):
        rows = _rows(d)
        self.g("modem_dhcp_leases", "Active DHCP leases", {"ip_version": "ipv4"}, float(len(rows)))
        for row in rows:
            labels = {"mac": str(row.get("MAC", "")).lower(), "ip": str(row.get("IP", "")),
                      "hostname": str(row.get("ClientName", ""))}
            secs = _hms(row.get("ExpireTime", ""))
            self.g("modem_dhcp_lease_expiry_seconds", "Seconds until the DHCP lease expires", labels,
                   secs if secs is not None else 0.0)

    def m_Dhcpv6Lease(self, d):
        self.g("modem_dhcp_leases", "Active DHCP leases", {"ip_version": "ipv6"}, float(len(_rows(d))))

    def m_ARPStatus(self, d):
        self.g("modem_neighbor_entries", "ARP / IPv6 neighbor table size", {"ip_version": "ipv4"}, float(len(_rows(d))))

    def m_ARP6Status(self, d):
        self.g("modem_neighbor_entries", "ARP / IPv6 neighbor table size", {"ip_version": "ipv6"}, float(len(_rows(d))))

    def m_RouteTable(self, d):
        self.g("modem_routes", "Entries in the routing table", {}, float(len(_rows(d))))

    def m_LANAddressConfiguration(self, d):
        for row in _rows(d):
            self.fam.add("modem_lan_info", "LAN interface addressing (value is always 1)", {
                "ip": str(row.get("IPAddress", "")), "netmask": str(row.get("Netmask", "")),
                "ipv6_address": str(row.get("Ipv6GlobalAddr", "")),
            }, 1)

    def m_CwmpStatus(self, d):
        status = str(d.get("Connection_status", ""))
        self.g("modem_tr069_connected", "TR-069 (ACS) connection OK", {},
               1.0 if status.lower() in ("connected", "success", "connectsuccess") else 0.0)
        self.fam.add("modem_tr069_info", "TR-069 status (value is always 1)", {"status": status}, 1)

    def m_WifiMeshTopo(self, d):
        nodes = d.get("nodes", []) if isinstance(d, dict) else []
        counts: dict[str, int] = {}
        for n in nodes:
            counts[str(n.get("type", "unknown"))] = counts.get(str(n.get("type", "unknown")), 0) + 1
        for t, c in counts.items():
            self.g("modem_mesh_nodes", "Nodes in the Wi-Fi mesh / LAN topology by type", {"type": t.lower()}, float(c))
        conns: dict[str, int] = {}
        for c in (d.get("connections", []) if isinstance(d, dict) else []):
            conns[str(c.get("type", ""))] = conns.get(str(c.get("type", "")), 0) + 1
        for t, c in conns.items():
            self.g("modem_mesh_links", "Links in the mesh topology by medium", {"medium": t.lower()}, float(c))

    def m_TrafficStatisticList(self, d):
        pass
