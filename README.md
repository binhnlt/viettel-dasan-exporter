# Viettel / DASAN GPON Modem — Prometheus Exporter

A Prometheus exporter for the **Viettel H646GM-V** GPON modem (a DASAN / DZS
ONT) and other DZS ONTs that ship the same Vue-based web UI.

ISP modems like this give customers **no SNMP and no TR-069 access**. This
exporter logs in to the modem's web API the same way the browser does, reads
the status objects (device, fibre optics, WAN, LAN, Wi-Fi, DHCP), and
republishes them as Prometheus metrics on `/metrics`. Point your own
Prometheus at it and graph it in Grafana.

```
GPON modem ──HTTPS JSON API──► modem_exporter :9877/metrics ──► your Prometheus ──► your Grafana
            (optionally via SOCKS5)
```

- Runs as a container or a bare Python process (3 small dependencies).
- Device identity, uptime, CPU and memory; fibre Rx/Tx power, temperature, voltage and bias.
- Byte, packet, error and drop counters for the fibre, WAN, LAN 1-4 and each Wi-Fi SSID, so traffic can be shown in **bits/sec**.
- WAN (PPPoE/IP) status, uptime and public IP; LAN port link and speed; Wi-Fi radios; DHCP leases.
- Read-only: after login it only sends `GET` requests. It stops for a while after a failed login so the account doesn't get locked. Secrets are never exported.
- Can reach the modem through a SOCKS5/HTTP proxy, for monitoring a modem at another site.

---

## Metrics

| Metric | Type | Description |
|--------|------|-------------|
| `modem_up` | gauge | `1` if login + scrape succeeded this cycle, else `0` |
| `modem_info{model,software_version,serial_number,mac_address,mac_oui,pon_mode,pon_version,olt_type,optical_type,customer}` | gauge | Device identity. The value is always `1`; the data is in the labels |
| `modem_uptime_seconds` | gauge | Modem uptime in seconds |
| `modem_cpu_usage_percent` / `modem_memory_usage_percent` | gauge | CPU load and memory usage |
| `modem_pon_link_up` / `modem_pon_link_uptime_seconds` | gauge | Fibre (PON) link state and how long it has been up |
| `modem_optical_rx_power_dbm` / `modem_optical_tx_power_dbm` | gauge | Received / transmitted optical power |
| `modem_optical_temperature_celsius` / `_supply_voltage_volts` / `_bias_current_milliamps` | gauge | Transceiver readings (`*_threshold{bound}` gives the alarm limits) |
| `modem_pon_fec_enabled` / `modem_pon_ber` | gauge | FEC state and bit-error-rate status |
| `modem_interface_rx_bytes_total{interface,type}` / `modem_interface_tx_bytes_total` | counter | Bytes per interface: `pon`, `wan<N>`, `lan1-4`, `wlan2.4ghz_<N>`, `wlan5ghz_<N>` |
| `modem_interface_{rx,tx}_packets_total` | counter | Packets per interface |
| `modem_interface_{rx,tx}_{errors,drops,crc_errors,collisions,undersize_packets}_total` | counter | Error counters (whichever the interface provides) |
| `modem_interface_{rx,tx}_{packets,bytes}_by_cast_total{cast}` | counter | Unicast / multicast / broadcast breakdown |
| `modem_pon_{rx,tx}_rate_bytes_per_second` | gauge | Rate as computed by the modem itself |
| `modem_wan_connection_up{wan,kind,service,ip_version}` | gauge | `1` if the WAN connection is up (IPv4 and IPv6 separately) |
| `modem_wan_connection_uptime_seconds{...}` / `modem_wan_mtu_bytes` | gauge | WAN session uptime, MTU |
| `modem_wan_info{external_ip,gateway,dns_servers,ipv6_address,ipv6_prefix,vlan,...}` | gauge | WAN addressing; the value is always `1` |
| `modem_lan_port_up{port}` / `_admin_up` / `_speed_mbps` / `_full_duplex` | gauge | LAN port link, admin state and negotiated speed |
| `modem_wifi_radio_enabled{band}` / `modem_wifi_channel` / `modem_wifi_radio_info` | gauge | Wi-Fi radio state, channel, standard and bandwidth |
| `modem_wifi_ssid_enabled{band,index,ssid}` / `modem_wifi_ssid_info` / `modem_wifi_clients` | gauge | SSIDs and associated clients |
| `modem_dhcp_leases{ip_version}` / `modem_dhcp_lease_expiry_seconds{mac,ip,hostname}` | gauge | DHCP clients |
| `modem_neighbor_entries{ip_version}` / `modem_routes` / `modem_mesh_nodes{type}` | gauge | ARP/NDP table size, routes, mesh topology |
| `modem_tr069_connected` | gauge | `1` if the ISP's ACS (TR-069) connection is OK |
| `modem_object_value{object,index,field}` | gauge | Numeric fields of any other readable object, so nothing is lost |
| `modem_scrape_duration_seconds` / `modem_scrape_objects` / `modem_login_backoff_seconds` | gauge | Exporter self-monitoring |

Throughput is derived at query time, e.g.
`rate(modem_interface_rx_bytes_total{interface="wan0"}[5m]) * 8` gives WAN download in bit/s.

---

## Quick start

```bash
git clone https://github.com/binhnlt/viettel-dasan-exporter.git
cd viettel-dasan-exporter
cp .env.example .env
nano .env # set MODEM_URL, MODEM_USERNAME, MODEM_PASSWORD (and MODEM_PROXY if needed)
docker compose up -d
```

Metrics are then served at `http://<host>:9877/metrics`. Check with:

```bash
curl -s localhost:9877/metrics | grep -E '^modem_(up|info|uptime)'
```

To run without Docker: `pip install -r exporter/requirements.txt && cd exporter && python -m modem_exporter --env-file ../.env`.

### Scrape it with Prometheus

Add a job to your Prometheus config (full snippet in
[`examples/prometheus-scrape.yml`](examples/prometheus-scrape.yml)):

```yaml
scrape_configs:
  - job_name: modem
    scrape_interval: 60s
    scrape_timeout: 50s
    static_configs:
      - targets: ['EXPORTER_HOST:9877']
```

A scrape reads about 25 API objects one after another. Through a remote SOCKS5
proxy that took ~19 s during development, which is why the timeout above is generous.

### Grafana dashboard (optional)

Import [`examples/grafana-dashboard.json`](examples/grafana-dashboard.json) into
Grafana (Dashboards → Import). Variables at the top of the dashboard let you choose:

| Variable | Purpose |
|----------|---------|
| **Data source** | Any Prometheus data source; no editing of the JSON needed |
| **Job** | Prometheus `job` label(s) to show (multi-select, default *All*) |
| **Modem** | `modem` label(s), i.e. `MODEM_NAME`, when you monitor several modems |
| **Filters** | Ad-hoc `label = value` filters for any other label, e.g. the `device` / `location` labels you add in the scrape config |

The dashboard's own tags (`viettel`, `dasan`, `gpon`, `modem`, `network`) can be changed under Dashboard settings.

It shows:
- reachability, uptime, fibre and internet status, CPU and memory
- the fibre Rx power, colour-coded against the GPON B+ range
- WAN and fibre throughput in bit/s
- optical history
- LAN and Wi-Fi traffic per port/SSID, and errors
- LAN port speeds, Wi-Fi radios and DHCP leases

![alt text](image-1.png)
![alt text](image.png)
![alt text](image-2.png)
---

## Configuration

All configuration is via environment variables (usually the `.env` file):

| Variable | Default | Description |
|----------|---------|-------------|
| `MODEM_URL` | *(required)* | Modem web UI URL, e.g. `https://192.168.1.1` |
| `MODEM_USERNAME` | *(required)* | Web UI username |
| `MODEM_PASSWORD` | *(required)* | Web UI password (plaintext; the exporter base64-encodes it when the firmware asks for that) |
| `MODEM_PROXY` | *(none)* | `socks5://user:pass@host:port`, `socks5h://…` (DNS via proxy) or `http://…` |
| `MODEM_NAME` | host of `MODEM_URL` | Value of the `modem` label on every metric |
| `DRIVER` | `auto` | `dzs` (JSON API), `html` (generic page scraper), or `auto`-detect |
| `LISTEN_PORT` | `9877` | Port to serve `/metrics` on |
| `SCRAPE_TIMEOUT` | `10` | Per-HTTP-request timeout (seconds) |
| `CACHE_SECONDS` | `30` | Reuse the last result if Prometheus scrapes again within this window |
| `VERIFY_TLS` | `false` | The modem uses a self-signed certificate |
| `LOGOUT_AFTER_SCRAPE` | `true` | Log out after each scrape so the web UI stays usable from a browser |
| `LOGIN_BACKOFF_SECONDS` | `300` | Wait this long after a failed login before trying again |
| `LOG_LEVEL` | `INFO` | `DEBUG` shows every request |

`URL`, `USERNAME`, `PASSWORD` and `PROXY` (without the `MODEM_` prefix) are accepted too.
To monitor several modems, run one container per modem, each with its own env file and `MODEM_NAME`.

---

## How it works

**1. Authentication.** The web UI is a single-page Vue app, and all data comes
from a JSON API under `/dm/`. The exporter first calls `GET /dm/sys/?cmd=Login`.
That response says whether a captcha is shown (on the LAN the "captcha" is
returned in plain text) and whether `encodeEnable` is set. If it is, the password
is base64-encoded, as the browser does. The exporter then `POST`s
`{"Login":{"data":{"username","password","captcha"}}}` and gets back an
`authenticatedToken`. Every later request carries it as `Authorization: Bearer <token>`.
Error codes are mapped to readable messages: `9895`/`9896` wrong credentials,
`9897` account locked, with the lock time.

**2. Scraping.** The exporter reads `GET /dm/sys/?objs=Permission` to learn
which objects this account may read. It then fetches every readable
`StatusPage-*` object with `GET /dm/tr98/?objs=<Object>&page=<Page>`:

| Object | Provides |
|--------|----------|
| `DeviceInfo` | model, firmware, serial, MAC, uptime, CPU, memory |
| `PonPortStatus` | PON link, Rx/Tx power, temperature, voltage, bias, OLT type, thresholds |
| `StatisticsPonObj`, `StatisticsWanObj`, `LANStatistics`, `StatisticsWlanObj`, `StatisticsWlan11acObj` | traffic counters |
| `WANObject`, `WANPPPConnection`, `WANIPConnection` | WAN connections, status, uptime, addressing |
| `LANPortStatus`, `LANAddressConfiguration` | LAN port link/speed, LAN addressing |
| `WLANCommon`, `WLAN11acCommon`, `WLANConfiguration`, `WLAN11acConfiguration`, `WLANAssociatedDevice` | radios, SSIDs, clients |
| `DhcpLease`, `Dhcpv6Lease`, `ARPStatus`, `ARP6Status`, `RouteTable`, `WifiMeshTopo`, `CwmpStatus` | hosts, routes, topology, TR-069 |

The API makes changes only via `POST`/`DELETE`. The exporter only sends `GET`s
and never touches objects such as `Reboot`, `RestoreFactory` or `FirmwareUpgrade`.
Objects the account isn't allowed to read (HTTP 403 "Unauthorized URL") are
skipped for the rest of the session.

**3. Publishing.** Values are exposed through a `prometheus_client` custom
collector, so every scrape reads live data. The result is cached for
`CACHE_SECONDS` so several Prometheus servers or quick reloads don't overload
the modem. If a scrape fails, `modem_up` goes to `0` and the other series are
omitted for that cycle rather than showing stale data.

### Other modems (html driver)

If `/dm/sys/?cmd=Login` doesn't exist, the exporter falls back to a generic
HTML driver. It finds the login form on its own, crawls the status pages
(skipping anything that looks like reboot/reset/apply/save), and turns
label/value rows, tables and JavaScript variables into `modem_field_*` /
`modem_table_*` metrics. Where it recognises values, it also exports the same
uptime, optical and interface metrics as above. To see what it finds on a new
modem:

```bash
mkdir -p dump && chmod 777 dump
docker compose run --rm -v "$PWD/dump:/app/dump" exporter discover
```

This saves the raw pages or API objects (secrets masked) plus a summary to `./dump`.
`docker compose run --rm exporter once` prints one scrape to stdout.

---

## Limitations

- **Coarse optical readings.** This firmware reports Rx/Tx power, temperature
  and voltage as whole numbers (e.g. `-16` dBm, `3` V), so changes smaller than
  1 dBm aren't visible.
- **Account permissions.** The customer `admin` account can't read some objects,
  for example `HWInfo`, `TimeServer` and `IGMPSnoopObject`. What you get depends
  on what your ISP allows.
- **`modem_pon_*_rate_bytes_per_second`** is the modem's own calculation and
  can look implausible. Prefer `rate()` over the byte counters.
- **Counters reset** when the modem reboots or someone clicks *Clear* on the
  statistics page. Prometheus' `rate()` handles this.

---

## Troubleshooting

**`modem_up` is `0`**: check the exporter logs with `docker logs modem-exporter`.

- **`Login rejected (code 9895): wrong username or password`**: the password
  is case-sensitive. Check it by logging in with a browser. The exporter now
  waits `LOGIN_BACKOFF_SECONDS` before trying again (see `modem_login_backoff_seconds`).
- **`Account locked by modem for Ns`**: too many failed logins, from the
  exporter or a browser. Wait it out.
- **`Modem unreachable`**: the exporter host can't reach `MODEM_URL`. If the
  modem is at another site, set `MODEM_PROXY`. With WSL2 or Docker Desktop,
  check that the container can route to the modem's subnet.
- **`requires an image captcha`**: the modem shows a real captcha, typically
  when accessed from the WAN side. Scrape it from the LAN instead.
- **Scrapes time out in Prometheus**: increase `scrape_timeout`. Through a
  slow proxy one scrape can take ~20 s.

**No data in Grafana**: confirm the Prometheus target is `UP`
(`Status → Targets`) and that `modem_up` exists in Prometheus first.

---

## Security notes

- `.env` holds the modem password (and proxy credentials) and is git-ignored.
  Keep it `chmod 600`.
- Wi-Fi keys, PPPoE credentials, RADIUS secrets and tokens are never exported,
  and are masked in `discover` output.
- Metrics include your public IP, SSIDs and DHCP hostnames. `/metrics` has no
  authentication, so restrict it to your monitoring network.
- TLS verification is off by default because the modem uses a self-signed
  certificate. Keep the management interface on a trusted network.

---

## Compatibility

Developed and tested against a **Viettel H646GM-V** (DASAN/DZS GPON ONT),
firmware `VT5.2.2140138`, on a ZTE OLT. It should work unchanged on other
DZS ONTs that use the same web UI, i.e. where `/dm/sys/?cmd=Login` returns JSON.
The objects actually read come from the modem's own permission list. Other
modems fall back to the generic HTML driver, which may need `PAGES` / `LOGIN_*`
tweaks (see `.env.example`).

## License

MIT
