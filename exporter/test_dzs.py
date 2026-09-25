"""Unit tests for the DZS JSON-API driver (login + metric mapping).

Run with:  pytest exporter/
No modem or network access required: the fixtures below have the same shape as
responses captured from a real Viettel H646GM-V, with identifying values
(serials, MACs, IPs, SSIDs) replaced by dummy data.
"""
import base64

import pytest
from modem_exporter import dzs
from modem_exporter.client import LoginError
from modem_exporter.config import Config
from modem_exporter.metrics import _Families

DEVICE_INFO = {
    "manufacturerOUI": "00:11:22", "modelName": "H646GM-V", "serialNumber": "DSNW00000001",
    "softwareVersion": "VT5.2.2140138", "UpTime": 1022459, "CPU_Load": "13.7300",
    "MemoryUsage": "48", "MACAddress": "00:11:22:33:44:55",
}
PON = {
    "ponVerSion": "RP0201", "ponLinkState": "Up", "ponLinkUptime": 1022423, "ponMode": "GPON",
    "oltType": "ZTE", "onuID": 0, "opticalType": "B+", "ponRxPower": "-16", "ponTxPower": "3",
    "ponTxBiasCur": "13", "ponSupplyVolt": "3", "ponTemp": "53", "fecStatus": "Disabled", "berStatus": "0",
    "lowerRxPowerThreshold": "-127.000000", "upperRxPowerThreshold": "-0.000000",
}
LAN_PORTS = [
    {"iid": 1, "Admin": "Up", "Status": "Up", "Mode": "Full/1000"},
    {"iid": 2, "Admin": "Up", "Status": "Down", "Mode": "Na/Na"},
]
LAN_STATS = [{"iid": 1, "txFrames": 200, "txTotalBytes": 5000, "rxFrames": 100, "rxTotalBytes": 9000,
              "rxCrcErrFrames": 0, "txErrFrames": 1, "rxUcastFrames": 90, "rxMcastFrames": 6, "rxBcastFrames": 4}]
PON_STATS = [{"TxTotalPacket": 10, "TxTotalByte": 1000, "RxTotalPacket": 20, "RxTotalByte": 2000,
              "RxCrcPacket": 0, "RxUnicastPacket": 15, "RxRateBytePerSecond": 471011}]
WAN_OBJECT = [{"iid": 0, "IsDefault": True, "Active": True, "ConnectionType": "PPPoE", "ServiceList": "INTERNET"},
              {"iid": 1, "IsDefault": False, "Active": False, "ConnectionType": "Dynamic", "ServiceList": ""}]
WAN_PPP = [
    {"iid": 0, "enable": True, "connectionStatus": "Connected", "connectionStatus6": "Connected",
     "externalIPAddress": "203.0.113.10", "defaultGateway": "203.0.113.1", "DNSServers": "198.51.100.1",
     "MTU": 1492, "VLANId": 35, "ServiceList": "INTERNET", "ConnectionTimeIpv4": 3600, "ConnectionTimeIpv6": 3500,
     "username": "someone", "password": "secret"},
    {"iid": 1, "enable": False, "connectionStatus": "Disconnected"},
]
WAN_IP = [
    {"iid": 0, "enable": False, "connectionStatus": "Connected", "connectionType": "Unconfigured"},
    {"iid": 6, "enable": True, "connectionStatus": "Disconnected", "connectionType": "IP_Routed",
     "ServiceList": "TR069", "ConnectionTimeIpv4": 0},
]
WAN_STATS = [{"iid": 0, "RxTotalByte": 111, "TxTotalByte": 222, "RxTotalPacket": 3, "TxTotalPacket": 4}]
WLAN_CFG = [{"iid": 1, "RadioEnabled": True, "SSID": "HomeWiFi", "KeyPassphrase": "hunter22",
             "Security": "Wpa2Psk", "MaxStaNum": 22, "SSIDAdvertisementEnabled": True, "MACAddress": "00:11:22:33:44:56"}]
WLAN_STATS = [{"iid": 1, "TxCount": 5, "RxCount": 6, "TxByteCount": 700, "RxByteCount": 800, "RxDropCount": 9,
               "TxUnicastPacket": -1}, {"iid": 2}]
DHCP = [{"iid": 1, "MAC": "AA:BB:CC:00:00:01", "IP": "192.168.1.2", "ClientName": "laptop", "ExpireTime": "0:38:16"}]


def samples(fam, name):
    """{frozenset(label items): value} for one metric, without the constant `modem` label."""
    out = {}
    for key, value in fam.samples.get(name, {}).items():
        out[frozenset((k, v) for k, v in key if k != "modem")] = value
    return out


def one(fam, name, **labels):
    for key, value in samples(fam, name).items():
        if all((k, v) in key for k, v in labels.items()):
            return value
    raise AssertionError(f"{name}{labels} not found in {samples(fam, name)}")


@pytest.fixture
def fam():
    f = _Families("test")
    dzs.Mapper(f, {
        "DeviceInfo": DEVICE_INFO, "PonPortStatus": PON, "LANPortStatus": LAN_PORTS, "LANStatistics": LAN_STATS,
        "StatisticsPonObj": PON_STATS, "WANObject": WAN_OBJECT, "WANPPPConnection": WAN_PPP,
        "WANIPConnection": WAN_IP, "StatisticsWanObj": WAN_STATS, "WLANConfiguration": WLAN_CFG,
        "StatisticsWlanObj": WLAN_STATS, "DhcpLease": DHCP, "BandSteeringStatus": {"Threshold2G": 80},
        "SomethingNew": {"Counter": 7, "Password": "123"},
    }, {"customer": "VIETTEL"}).run()
    return f


# --- device / optics ---------------------------------------------------------
def test_device_info_labels(fam):
    (labels,) = samples(fam, "modem_info")
    labels = dict(labels)
    assert labels["model"] == "H646GM-V"
    assert labels["software_version"] == "VT5.2.2140138"
    assert labels["pon_mode"] == "GPON"
    assert labels["customer"] == "VIETTEL"


def test_device_gauges(fam):
    assert one(fam, "modem_uptime_seconds") == 1022459
    assert one(fam, "modem_cpu_usage_percent") == pytest.approx(13.73)
    assert one(fam, "modem_memory_usage_percent") == 48


def test_optics(fam):
    assert one(fam, "modem_optical_rx_power_dbm") == -16
    assert one(fam, "modem_optical_tx_power_dbm") == 3
    assert one(fam, "modem_optical_temperature_celsius") == 53
    assert one(fam, "modem_pon_link_up") == 1
    assert one(fam, "modem_optical_rx_power_dbm_threshold", bound="lower") == -127


# --- ports / traffic ---------------------------------------------------------
def test_lan_ports(fam):
    assert one(fam, "modem_lan_port_up", port="lan1") == 1
    assert one(fam, "modem_lan_port_up", port="lan2") == 0
    assert one(fam, "modem_lan_port_speed_mbps", port="lan1") == 1000
    assert ("port", "lan2") not in {kv for key in samples(fam, "modem_lan_port_speed_mbps") for kv in key}


def test_interface_counters(fam):
    assert one(fam, "modem_interface_rx_bytes", interface="lan1") == 9000
    assert one(fam, "modem_interface_tx_errors", interface="lan1") == 1
    assert one(fam, "modem_interface_rx_bytes", interface="pon") == 2000
    assert one(fam, "modem_interface_rx_bytes", interface="wan0", service="INTERNET") == 111
    assert one(fam, "modem_interface_rx_packets_by_cast", interface="lan1", cast="unicast") == 90
    assert fam.defs["modem_interface_rx_bytes"][0] == "counter"


def test_wlan_stats_use_ssid_and_skip_placeholders(fam):
    assert one(fam, "modem_interface_tx_bytes", interface="wlan2.4ghz_1", ssid="HomeWiFi") == 700
    # -1 means "not supported" on this firmware and must not be exported
    assert not samples(fam, "modem_interface_tx_packets_by_cast")
    # empty rows (iid 2 without counters) are ignored
    assert all(("interface", "wlan2.4ghz_2") not in k for k in samples(fam, "modem_interface_rx_bytes"))


# --- WAN ---------------------------------------------------------------------
def test_wan_connections(fam):
    assert one(fam, "modem_wan_connection_up", wan="wan0", kind="ppp", ip_version="ipv4") == 1
    assert one(fam, "modem_wan_connection_uptime_seconds", wan="wan0", ip_version="ipv4") == 3600
    info = dict(next(iter(samples(fam, "modem_wan_info", ))))
    assert info["external_ip"] == "203.0.113.10"
    # WAN6 is not in WANObject: exported once, as the IP connection
    wan6 = [dict(k) for k in samples(fam, "modem_wan_connection_up") if ("wan", "wan6") in k]
    assert {w["kind"] for w in wan6} == {"ip"}
    # disabled / inactive connections are skipped
    assert all(("wan", "wan1") not in k for k in samples(fam, "modem_wan_connection_up"))


# --- secrets / generic -------------------------------------------------------
def test_no_secrets_exported(fam):
    everything = str(fam.samples)
    for secret in ("hunter22", "secret", "someone"):
        assert secret not in everything


def test_generic_object_values(fam):
    assert one(fam, "modem_object_value", object="SomethingNew", field="Counter") == 7
    assert all(("field", "Password") not in k for k in samples(fam, "modem_object_value"))


def test_dhcp_lease_expiry(fam):
    assert one(fam, "modem_dhcp_lease_expiry_seconds", hostname="laptop") == 38 * 60 + 16


# --- login -------------------------------------------------------------------
class FakeResp:
    def __init__(self, payload, status=200):
        self.payload, self.status_code, self.ok = payload, status, status < 400

    def json(self):
        return self.payload


class FakeSession:
    def __init__(self, login_result):
        self.login_result = login_result
        self.posted = None

    def get(self, url, **kw):
        if "cmd=Login" in url:
            return FakeResp({"Login": {"status_code": 200, "data": {"mode": "image", "value": "", "encodeEnable": True}}})
        return FakeResp({"X": {"status_code": 200, "data": {}}})

    def post(self, url, json=None, **kw):
        self.posted = json
        return FakeResp(self.login_result)


def make_driver(result):
    session = FakeSession(result)
    cfg = Config(url="https://modem", username="admin", password="pässword")
    return dzs.DzsDriver(cfg, lambda: session), session


def test_login_encodes_password_base64():
    drv, session = make_driver({"Login": {"data": {"login": {"authenticatedToken": "tok", "timeout": 300}}}})
    drv.login()
    sent = session.posted["Login"]["data"]
    assert sent["username"] == "admin"
    assert base64.b64decode(sent["password"]).decode() == "pässword"
    assert drv.token == "tok"


def test_login_wrong_password():
    drv, _ = make_driver({"Login": {"status_code": 200, "error": {"code": 9895, "message": ""}}})
    with pytest.raises(LoginError, match="wrong username or password"):
        drv.login()


def test_login_locked_sets_retry_after():
    drv, _ = make_driver({"Login": {"error": {"code": 9897, "BannedTime": 60, "MaxTrial": 5}}})
    with pytest.raises(LoginError) as exc:
        drv.login()
    assert exc.value.retry_after == 60
