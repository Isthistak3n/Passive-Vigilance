# Setup

> This guide covers Raspberry Pi OS / Debian Bookworm and Trixie (64-bit, ARM64/ARM32).

---

## Quick Install (recommended)

```bash
git clone https://github.com/Isthistak3n/Passive-Vigilance.git
cd Passive-Vigilance
sudo bash deploy/install.sh
```

`install.sh` auto-detects your OS codename, installs all system packages, configures
gpsd and Kismet as systemd services, and prints next-step instructions when done.

---

## Boot Sequence

```
gpsd  ──►  kismet  ──►  passive-vigilance
```

`gpsd` and `kismet` are enabled at boot by the installer.
`passive-vigilance` must be enabled manually after `.env` is configured.

---

## GPS

### Hardware

Connect a USB GPS dongle (e.g. u-blox 7 or 8 based) to any USB port on the Pi.
Once plugged in, confirm the device node appears:

```bash
ls /dev/ttyUSB*
# expected: /dev/ttyUSB0
```

### Install gpsd and the Python bindings

```bash
sudo apt install -y gpsd gpsd-clients python3-gps
```

> **Important:** The Python GPS bindings (`import gps`) are provided by the
> `python3-gps` apt package. **Do not** install `gpsd-py3` via pip — it is a
> different package with an incompatible API and is not used by this project.
> `python3-gps` is intentionally absent from `requirements.txt` because pip
> cannot install it correctly on Raspberry Pi OS / Debian.

### Configure gpsd

Edit `/etc/default/gpsd`:

```bash
sudo nano /etc/default/gpsd
```

Set the following values:

```ini
DEVICES="/dev/ttyUSB0"
GPSD_OPTIONS="-n"
```

The `-n` flag tells gpsd to start polling the receiver immediately without
waiting for a client to connect — important for cold-start time-to-first-fix.

If your dongle appears on a different node (e.g. `/dev/ttyACM0`) update
`DEVICES` accordingly, or set `GPS_DEVICE` in `.env` to match.

### Install the systemd drop-in override

```bash
sudo mkdir -p /etc/systemd/system/gpsd.service.d
sudo cp deploy/gpsd.override.conf /etc/systemd/system/gpsd.service.d/override.conf
sudo systemctl daemon-reload
```

### Enable and start gpsd

```bash
sudo systemctl enable gpsd
sudo systemctl start gpsd
```

Verify it is running:

```bash
sudo systemctl status gpsd
```

### Test the GPS feed

```bash
cgps -s
```

You should see satellite data and, once a fix is acquired, live coordinates.

> **Note:** On first power-on (cold start) a GPS fix can take **30–90 seconds**
> outdoors with a clear sky view. Subsequent starts with a warm receiver are
> typically under 10 seconds.

---

## Kismet

### Supported OS versions

Kismet is installed from the official Kismet apt repository.
Supported distros: **Debian Bookworm** and **Debian Trixie** (including Raspberry Pi OS).

### Install

```bash
wget -O - https://www.kismetwireless.net/repos/kismet-release.gpg.key \
  --quiet | gpg --dearmor | sudo tee /usr/share/keyrings/kismet.gpg > /dev/null

# Replace "trixie" with your distro codename (bookworm, trixie, etc.)
DISTRO=$(lsb_release -cs)
echo "deb [signed-by=/usr/share/keyrings/kismet.gpg] \
https://www.kismetwireless.net/repos/apt/release/${DISTRO} \
${DISTRO} main" | sudo tee /etc/apt/sources.list.d/kismet.list

sudo apt update
sudo apt install -y kismet
```

### Add user to kismet group

```bash
sudo usermod -aG kismet $USER
```

Log out and back in for the group change to take effect.

### Generate an API key

1. Start Kismet:
   ```bash
   kismet --no-ncurses-wrapper &
   ```
2. Open the web UI: `http://<pi-ip>:2501`
3. Log in (first run generates credentials at `~/.kismet/kismet_httpd.conf`)
4. Go to **Settings → API Keys → Create API Key**
5. Name it `passive-vigilance`, click **Create**
6. Copy the key and add it to `.env`:
   ```ini
   KISMET_API_KEY=<your key here>
   ```

### Test the REST API

```bash
curl -s -H "KISMET-API-Key: <your-api-key>" \
  http://localhost:2501/system/status.json | python3 -m json.tool | head -20
```

A `200` response with JSON confirms the API is working.

### Check service status

```bash
sudo systemctl status kismet
journalctl -u kismet -n 50
```

### Install as a systemd service

```bash
sudo cp deploy/kismet.service /etc/systemd/system/kismet.service
sudo systemctl daemon-reload
sudo systemctl enable kismet
sudo systemctl start kismet
```

---

## Troubleshooting

### Kismet won't start

```bash
journalctl -u kismet -n 50 --no-pager
```

Common causes:
- No Wi-Fi interface in monitor mode — check `ip link`
- Port 2501 already in use — `sudo lsof -i :2501`
- Permissions — ensure user is in `kismet` group

### API key invalid (401)

- Regenerate key in the web UI: **Settings → API Keys**
- Confirm `KISMET_API_KEY` in `.env` matches exactly (no trailing whitespace)
- Confirm Kismet is running: `systemctl is-active kismet`

### No devices seen

- Confirm a monitor-mode interface is configured as a datasource in Kismet
- Check Kismet web UI → **Interfaces** for active sources
- Some adapters require explicit monitor mode: `sudo ip link set wlan1 down && sudo iw wlan1 set monitor none && sudo ip link set wlan1 up`

### libwebsockets version mismatch on apt install

This occurs when installing the `bookworm` Kismet package on a `trixie` system
(or vice versa). Fix: use the correct repo codename.

```bash
DISTRO=$(lsb_release -cs)  # must match the repo URL
```

### dpkg debconf prompt getting stuck during install

If `apt install kismet` hangs on a debconf dialog ("Should Kismet be installed
with suid-root helpers?"):

```bash
# In another terminal:
echo "kismet-capture-common kismet-common/suid-root boolean true" | \
  sudo debconf-set-selections
sudo kill $(pgrep apt)
sudo dpkg --configure -a
```

---

---

## WiFi Monitor Mode

### Hardware

This project uses the **MediaTek MT7610U** USB dongle (`0e8d:7610`) on interface `wlan1`.
The driver (`mt76` series) is built into the Linux kernel — no DKMS or out-of-tree driver needed.

> **Do not touch `wlan0`** — that is the Pi's built-in WiFi used for network connectivity.

### Verifying monitor mode support

```bash
iw phy phy6 info | grep monitor
# Expected: * monitor
```

### Setting monitor mode manually

```bash
sudo ip link set wlan1 down
sudo iw wlan1 set monitor none
sudo ip link set wlan1 up
iw dev wlan1 info   # should show: type monitor
```

### Automatic monitor mode via udev (installed by install.sh)

A udev rule triggers a script whenever `wlan1` appears (boot or plug-in):

```
/etc/udev/rules.d/99-wifi-monitor.rules
/usr/local/bin/set-monitor-mode.sh
```

To reload rules manually:
```bash
sudo udevadm control --reload-rules
sudo udevadm trigger
```

### Preventing NetworkManager from reclaiming wlan1

NetworkManager will reset the interface to managed mode unless told to ignore it:

```bash
# File: /etc/NetworkManager/conf.d/99-unmanaged-wlan1.conf
[keyfile]
unmanaged-devices=interface-name:wlan1
```

After writing the file:
```bash
sudo systemctl restart NetworkManager
nmcli dev show wlan1 | grep GENERAL.STATE
# Expected: 10 (unmanaged)
```

Then re-apply monitor mode (NM resets it once on restart):
```bash
sudo ip link set wlan1 down
sudo iw wlan1 set monitor none
sudo ip link set wlan1 up
```

### Troubleshooting: interface reverts to managed

If `wlan1` reverts to managed mode after reboot:
1. Check udev rule is installed: `cat /etc/udev/rules.d/99-wifi-monitor.rules`
2. Check script exists and is executable: `ls -la /usr/local/bin/set-monitor-mode.sh`
3. Check NM unmanaged config: `cat /etc/NetworkManager/conf.d/99-unmanaged-wlan1.conf`
4. Check logs: `journalctl | grep "Passive Vigilance"`

---

## ADS-B (readsb)

### About readsb

`readsb` is a drop-in replacement for `dump1090-fa` and is the ADS-B decoder used by
this project. It is available in the Debian Trixie repos and serves data on the same
ports as dump1090-fa.

### Install

```bash
sudo apt install -y readsb
```

readsb is enabled as a systemd service automatically and starts when an RTL-SDR dongle is connected.

### Test ADS-B output

```bash
# JSON aircraft data (HTTP)
curl http://localhost:8080/data/aircraft.json | python3 -m json.tool | head -30

# SBS-1 BaseStation stream (TCP, used by some tools)
nc localhost 30003
```

### adsb.lol API enrichment

The `ADSBModule` optionally enriches aircraft records (registration, type, operator,
military flag) via the adsb.lol / ADSBExchange API.

1. Register as a feeder at https://www.adsb.lol/docs/
2. Obtain your RapidAPI key
3. Add to `.env`:
   ```ini
   ADSBXLOL_API_KEY=your-key-here
   ```

---

## RTL-SDR

### Kernel module blacklist

The DVB-T kernel drivers claim RTL-SDR hardware before the rtlsdr library can.
Blacklist them:

```bash
echo "blacklist dvb_usb_rtl28xxu" | sudo tee /etc/modprobe.d/rtlsdr.rules
echo "blacklist rtl2832"          | sudo tee -a /etc/modprobe.d/rtlsdr.rules
echo "blacklist rtl2830"          | sudo tee -a /etc/modprobe.d/rtlsdr.rules
sudo modprobe -r dvb_usb_rtl28xxu rtl2832 rtl2830 2>/dev/null || true
```

`deploy/install.sh` does this automatically.

### Test the RTL-SDR dongle

```bash
rtl_test -t
```

Expected output: device found, gain values listed, sample rate test passes.

### pyrtlsdr version note

`pyrtlsdr` is pinned to `0.2.93` in `requirements.txt`.  Do **not** upgrade without
testing — `pyrtlsdr >= 0.3.0` calls `rtlsdr_set_dithering()` which is absent from
`librtlsdr 2.0.2` (the Osmocom fork in Debian Trixie), causing an `AttributeError` at
import time.

---

## Drone RF Detection

The `DroneRFModule` passively scans these frequencies for drone command-link and video
link signals:

| Frequency | Use |
|---|---|
| 433 MHz | Hobbyist RC (worldwide) |
| 868 MHz | EU DJI / drone telemetry |
| 915 MHz | US DJI / drone telemetry |
| 2.4 GHz | DJI OcuSync, FPV video — at edge of R820T range |
| 5.8 GHz | FPV video — **beyond RTL-SDR range, best effort** |

**Note:** Most RTL-SDR dongles (R820T/R820T2 chip) top out around 1750 MHz.
2.4 GHz may work with an E4000-chip dongle; 5.8 GHz is beyond all common RTL-SDR
hardware. The module skips frequencies above hardware range gracefully.

Set the detection threshold in `.env`:
```ini
DRONE_POWER_THRESHOLD_DB=-20
```

Lower values (e.g. `-30`) increase sensitivity but also false positives.

**Hardware conflict:** `readsb` and `DroneRFModule` both require an RTL-SDR dongle.
With a single dongle, stop readsb before running a drone scan, or use two dongles.

---

## Ignore Lists

Ignore lists let you suppress known-benign devices (your own phone, home AP, etc.)
from appearing in sensor output and alerts.

### Data files

Ignore list JSON files live in `data/ignore_lists/` and are **git-ignored** so
personal device data is never committed.

```
data/ignore_lists/mac_ignore.json   — full MACs and OUI prefixes
data/ignore_lists/ssid_ignore.json  — SSIDs (case-insensitive)
```

### Managing entries

Use the CLI tool at `scripts/manage_ignore_list.py`:

```bash
# Add a device by MAC
python3 scripts/manage_ignore_list.py --add-mac aa:bb:cc:dd:ee:ff --label "home router"

# Add an OUI (vendor prefix — matches all MACs with that prefix)
python3 scripts/manage_ignore_list.py --add-oui b8:27:eb --label "Raspberry Pi Foundation"

# Add a known-benign SSID
python3 scripts/manage_ignore_list.py --add-ssid "MyHomeNetwork" --label "home AP"

# Remove a MAC
python3 scripts/manage_ignore_list.py --remove-mac aa:bb:cc:dd:ee:ff

# List all entries
python3 scripts/manage_ignore_list.py --list

# Show counts
python3 scripts/manage_ignore_list.py --stats

# Bulk-import everything Kismet currently sees (useful for initial seeding)
python3 scripts/manage_ignore_list.py --import-kismet
```

### Using the ignore list in code

Pass an `IgnoreList` instance to `KismetModule`:

```python
from modules.ignore_list import IgnoreList
from modules.kismet import KismetModule

il = IgnoreList(data_dir="data/ignore_lists")
km = KismetModule(gps_module=gps, ignore_list=il)
await km.connect()
devices = await km.poll_devices()  # ignored devices are silently filtered out
```

### MAC normalization

All MACs are normalized to lowercase colon-separated form (`aa:bb:cc:dd:ee:ff`)
before storage and lookup. Dashes and compact forms are accepted on input.

### OUI matching

An OUI entry stores the first three octets (`aa:bb:cc`). Any MAC whose first
three octets match is treated as ignored.

---

## Persistence Engine

The persistence engine is the counter-surveillance intelligence layer. It scores
every detected device on four weighted criteria across four overlapping time
windows to produce a 0.0–1.0 surveillance confidence score.

### How scoring works

| Component | Weight | Description |
|-----------|--------|-------------|
| Temporal | 35% | Fraction of the four time windows (5/10/15/20 min) in which the device was seen |
| Location | 35% | Number of distinct GPS clusters (100 m threshold) the device appeared at |
| Frequency | 20% | How consistently the device appears within the largest window |
| Signal | 10% | Average signal strength — stronger = device is physically nearby |

A device seen in all four windows at two GPS locations with strong signal will
score approximately 0.70–0.80, which crosses the default threshold of 0.7.

### Alert levels

| Score range | Level | Meaning |
|-------------|-------|---------|
| 0.5 – 0.7 | `suspicious` | Possibly following — monitor |
| 0.7 – 0.9 | `likely` | Probable surveillance — consider action |
| 0.9+ | `high` | High-confidence tracking behaviour |

### Tuning the threshold

The default threshold of **0.7** is calibrated for typical suburban/rural use.

- **Urban environment** (many ambient devices): raise to `0.8` to reduce noise
- **Rural environment** (fewer devices): lower to `0.6` for earlier alerts
- Adjust in `.env`:
  ```ini
  PERSISTENCE_ALERT_THRESHOLD=0.8
  ```

### First-run false positives

On first run, any device that has been near you for a while (ISP router vans,
delivery drivers, neighbours) may trigger alerts. Suppress them:

```bash
# Bulk-import everything Kismet currently sees into the ignore list
python3 scripts/manage_ignore_list.py --import-kismet
```

Run this after arriving at a new location before the session starts. Devices
added to the ignore list are silently filtered before the persistence engine
ever scores them.

### GPS requirement

Location clustering (35% of the score) requires a GPS fix. Without GPS:

- Temporal, frequency, and signal components still work (65% of max score)
- Maximum achievable score without GPS: ~0.65
- With default threshold of 0.7, GPS is effectively required to trigger alerts
- To use temporal-only detection without GPS, lower the threshold to `0.6`

### Poll interval

Set `PERSISTENCE_POLL_INTERVAL_SECONDS` to match your `main.py` polling rate
(default 30 s). The frequency score uses this to calculate expected vs actual
observation density — a mismatch degrades frequency scoring accuracy.

---

## Alert Engine

The alert engine delivers sensor detections to your phone or chat platform.
It is pluggable — swap backends by changing `ALERT_BACKEND` in `.env`.

### Choosing a backend

| Backend | When to use |
|---------|-------------|
| `ntfy` | Recommended — free, no account required, phone app available |
| `telegram` | If you already have a Telegram bot |
| `discord` | If you monitor a Discord server |
| `console` | Testing and development without external services |

### Ntfy (recommended)

Ntfy is a simple push notification service. No account required for the public
server. Install the ntfy app on your phone (Android / iOS), choose a unique
topic name, and you're done.

1. Install the ntfy app on your phone from ntfy.sh
2. Choose a unique topic name (treat it like a password — anyone who knows it
   can read your alerts)
3. Add to `.env`:
   ```ini
   ALERT_BACKEND=ntfy
   NTFY_TOPIC=my-unique-topic-name
   NTFY_SERVER=https://ntfy.sh
   ```

#### Self-hosting Ntfy for privacy

If your detections are sensitive, self-host ntfy on a VPS or your LAN:

```bash
# Install on a Debian/Ubuntu server
apt install ntfy
systemctl enable --now ntfy
```

Then set `NTFY_SERVER=http://your-server:80` in `.env`.
Self-hosted ntfy supports auth tokens for additional security.

### Telegram

1. Message `@BotFather` on Telegram: `/newbot` → follow prompts → copy token
2. Message your new bot to create the chat, then get your chat ID:
   ```bash
   curl "https://api.telegram.org/bot<TOKEN>/getUpdates"
   # Look for "chat": {"id": <your-chat-id>}
   ```
3. Add to `.env`:
   ```ini
   ALERT_BACKEND=telegram
   TELEGRAM_BOT_TOKEN=123456:ABCdef...
   TELEGRAM_CHAT_ID=987654321
   ```

### Rate limiting

In high-density RF environments (city centres, airports, events) the same
devices or signals may trigger repeatedly within seconds. Rate limiting
prevents notification spam while ensuring you are still alerted:

| Event type | Default cooldown | Rationale |
|------------|-----------------|-----------|
| Drone RF | 600 s (10 min) | Drone presence is persistent — one alert per encounter is enough |
| Persistent device | 300 s (5 min) | Score updates gradually — re-alert when behaviour continues |
| Aircraft | 60 s (1 min) | Aircraft move through quickly — per-ICAO cooldown avoids floods |

Adjust in `.env`:
```ini
DRONE_ALERT_COOLDOWN_SECONDS=600
PERSISTENCE_ALERT_COOLDOWN_SECONDS=300
AIRCRAFT_ALERT_COOLDOWN_SECONDS=60
```

Rate limiter state is in-memory and resets on restart (intentional — after a
restart you want to know what is currently in range).

### Testing alerts without hardware

Use the console backend to verify the alert pipeline without any hardware or
external services:

```python
python3 -c "
from modules.alerts import AlertFactory
backend = AlertFactory.get_backend('console')
backend.send('Test', 'Alert engine working', 'high')
"
```

Or test a specific event type:

```python
python3 -c "
from modules.alerts import AlertFactory
backend = AlertFactory.get_backend('console')
backend.send_drone_alert({'freq_mhz': 915.0, 'power_db': -18.5, 'lat': 51.5, 'lon': -0.1})
"
```

---

## Orchestrator

The asyncio orchestrator (`main.py`) wires all sensor modules into a unified
always-on event loop. It is managed as a systemd service on the Pi.

### Starting and stopping

```bash
# Enable at boot (do this once after .env is fully configured)
sudo systemctl enable passive-vigilance

# Start / stop
sudo systemctl start passive-vigilance
sudo systemctl stop passive-vigilance   # triggers clean shutdown (up to 30 s)

# Restart after config change
sudo systemctl restart passive-vigilance
```

### Watching live logs

```bash
journalctl -fu passive-vigilance
```

Startup banner confirms which modules connected and where session output will be written:

```
Passive Vigilance v0.1.0 — Session 20260101_120000
Active modules : GPS, Kismet, ADS-B, DroneRF (active)
Alert backend  : Ntfy
Output         : data/sessions/20260101_120000/
```

### Manual run (testing without systemd)

```bash
cd /home/survkis/Passive-Vigilance
python3 main.py
```

Ctrl+C triggers a clean shutdown — the same sequence as `systemctl stop`.

### Poll intervals

| Module | Interval | Notes |
|--------|----------|-------|
| GPS | 1 s | Blocking `gpsd.read()` runs in thread executor |
| readsb (ADS-B) | 5 s | HTTP JSON poll to `localhost:8080` |
| Kismet (WiFi/BT) | 30 s | REST API — devices scored by PersistenceEngine |
| DroneRF | Continuous | Background asyncio task; detections drained every 5 s |

### Session output

Each session writes to its own directory:

```
data/sessions/YYYYMMDD_HHMMSS/
├── summary.json            — session metadata and event counts
├── detections_wifi.shp     — WiFi/BT persistence events (Point geometry)
├── detections_aircraft.shp — ADS-B aircraft detections
├── detections_drone.shp    — Drone RF hits
└── detections.geojson      — All events in a single FeatureCollection
```

`summary.json` structure:

```json
{
  "session_id": "20260101_120000",
  "start_time": "2026-01-01T12:00:00+00:00",
  "end_time":   "2026-01-01T13:00:00+00:00",
  "duration_seconds": 3600,
  "gps_fixes_received": 3597,
  "unique_devices_tracked": 4,
  "persistent_detections": 7,
  "aircraft_detected": 23,
  "drone_detections": 0,
  "modules_active": { "gps": true, "kismet": true, "adsb": true, "drone_rf": false }
}
```

### Graceful shutdown sequence

On SIGINT/SIGTERM or Ctrl+C:

1. DroneRF scan cancelled
2. Kismet and readsb sessions closed
3. GPS disconnected
4. `summary.json` written
5. Shapefile and GeoJSON written (if any events)
6. WiGLE CSV uploaded (if configured and CSV found)

Allow up to 30 seconds for step 5–6 — `TimeoutStopSec=30` is set in the
service unit.

### First-run checklist

Before starting the sensor for the first time:

- [ ] GPS dongle is connected and gpsd is running (`cgps -s` shows data)
- [ ] Kismet is running and API key is set in `.env`
- [ ] RTL-SDR dongle is connected (for ADS-B + drone RF)
- [ ] Alert backend is configured and tested
- [ ] Ignore list is populated with your own devices:
  ```bash
  python3 scripts/manage_ignore_list.py --import-kismet
  ```
- [ ] `.env` credentials are complete

---

## Operational Resilience

### Health banner

Every 5 minutes (default) Passive Vigilance logs a structured health summary to
`journalctl`. It shows sensor health, cumulative event counts, and alert stats
for the current session — useful for confirming the platform is working without
tailing every log line.

**View live:**
```bash
journalctl -fu passive-vigilance
```

Example banner:
```
── Passive Vigilance Health ──────────────────────────
Session: 20260101_120000 | Uptime: 0h 05m 00s
GPS:     ✓ Fixed | Lat: 51.5074 Lon: -0.1278
Kismet:  ✓ Active | Devices seen: 142
ADS-B:   ✓ Active | Aircraft: 7
DroneRF: ✓ Active | Detections: 0
Alerts:  Ntfy | Sent: 3 | Rate-limited: 1
Events:  2 persistent | 7 aircraft | 0 drone
──────────────────────────────────────────────────────
```

**Tuning the interval** (default 300 s):
```ini
# .env
HEALTH_BANNER_INTERVAL_SECONDS=300
```

Set to `60` for verbose monitoring; `600` to reduce log noise on long sessions.

### Automatic sensor reconnection

When a sensor poll fails for the first time after a successful run, Passive
Vigilance immediately attempts to close and reopen the module connection before
declaring it degraded. This handles transient failures (USB device reset, network
hiccup, Kismet restart) without requiring a full service restart.

**Behaviour:**
- Only triggers on the first failure (health `True → False` transition)
- Does **not** retry on every subsequent failure — the sensor stays in degraded
  state until either the reconnect succeeds or the service is restarted manually
- Up to 3 attempts with 5 s between each (configurable)
- GPS, Kismet, and ADS-B (readsb) support reconnection
- DroneRF does not (hardware scan — restart the service if it fails)

**Configuring reconnect behaviour:**
```ini
# .env
MAX_RECONNECT_ATTEMPTS=3
RECONNECT_INTERVAL_SECONDS=5
```

**What to do if a sensor fails to reconnect:**
1. Check the journal: `journalctl -u passive-vigilance -n 50 --no-pager`
2. Verify hardware: `lsusb`, `ls /dev/ttyUSB*`, `systemctl status kismet`
3. Restart the service: `sudo systemctl restart passive-vigilance`

A sensor that repeatedly fails reconnect within the first few cycles usually
indicates a hardware problem (unplugged dongle, dead USB port) rather than a
software fault.

---

## KML Output

Every session automatically produces a `detections.kml` file alongside the
shapefiles and GeoJSON.  KML (Keyhole Markup Language) is readable by Google
Earth, Google Maps, and most GIS tools.

### File location

```
data/sessions/{session_id}/detections.kml
```

The file is written at session end by `ShapefileWriter`, which calls
`KMLWriter` internally — no extra configuration is needed.

### Opening the file

**Google Earth Desktop:**
1. File → Open → select `detections.kml`
2. Detections appear as pushpins in three folders: WiFi/BT, Aircraft, Drone RF

**Google Maps (My Maps):**
1. maps.google.com → Menu → Your places → Maps → Create Map
2. Import → upload `detections.kml`

**QGIS:**
- Layer → Add Layer → Add Vector Layer → select `detections.kml`

### Color coding

| Color | Meaning |
|-------|---------|
| White pushpin | Device first seen (no score yet / default) |
| Yellow pushpin | Suspicious (score 0.5–0.7) |
| Orange pushpin | Likely surveillance (score 0.7–0.9) |
| Red pushpin | High confidence surveillance (score 0.9+) |
| Blue airplane | Normal aircraft |
| Red pushpin | Emergency aircraft |
| Orange radio tower | Drone RF detection |

### Track lines

For any WiFi/BT device seen at two or more distinct GPS locations, a dashed
LineString is drawn connecting the cluster centroids.  Track line color
matches the device's alert level (yellow/orange/red).  This makes movement
patterns immediately visible in Google Earth.

### Aircraft altitude

Aircraft Placemarks are placed at their actual altitude (feet converted to
metres) in the KML Point coordinates.  In Google Earth, enable
**View → Show Terrain** to see aircraft at realistic height above ground.

### Sharing a session

Zip the session folder and share the archive — the `.kml` file is
self-contained and opens without any additional software beyond Google Earth:

```bash
zip -r session_20240101.zip data/sessions/20240101_120000/
```

---

## MAC Randomization

Modern mobile devices — iOS 14+, Android 10+, Windows 10+ — randomize their
MAC addresses to prevent passive tracking.  Passive Vigilance detects
randomized MACs automatically and can optionally fingerprint likely-same devices
across MAC changes.

### How randomization is detected

A MAC address has its **locally administered bit** (bit 1 of the first octet)
set to `1` when it was not assigned by the hardware vendor.  This is the
standard indicator used by all major platforms.

Equivalently: the second hex digit of the first octet is `2`, `6`, `A`, or `E`.

```
02:ab:cd:ef:01:23  →  randomized  (second digit = 2)
a4:c3:f0:11:22:33  →  static      (second digit = 4)
```

Each device record from Kismet is stamped with two extra fields:
- `mac_type`: `"randomized"` or `"static"`
- `is_randomized`: `True` / `False`

Persistence engine `DetectionEvent` objects carry `mac_type` so alert bodies
include the information.

### Fingerprint grouping

When `HANDLE_MAC_RANDOMIZATION=true` (the default), `PersistenceEngine` can
group randomized MACs that are likely the same physical device.  Grouping is
based on shared probe SSIDs seen across MAC changes.

Call `persistence_engine.get_fingerprint_summary()` to get the current list of
`MACFingerprint` clusters.  Each cluster contains:
- `canonical_mac` — representative MAC for the group
- `all_macs` — every MAC seen in the cluster
- `probe_ssids` — union of probe SSIDs across all MACs
- `avg_rssi` — mean signal strength
- `device_count` — number of distinct MACs grouped

### Dropping randomized MACs

If randomized devices generate too much noise in your environment (e.g. a
crowded public space), you can silently drop all of them:

```ini
# .env
IGNORE_RANDOMIZED_MACS=true
```

With this set, `IgnoreList.is_ignored_randomized()` returns `True` for every
MAC with the locally administered bit set, and the persistence engine never
tracks them.  Static (OUI-assigned) MACs are unaffected.

### Configuration reference

| Variable | Default | Description |
|----------|---------|-------------|
| `HANDLE_MAC_RANDOMIZATION` | `true` | Detect and fingerprint randomized MACs |
| `IGNORE_RANDOMIZED_MACS` | `false` | Drop all randomized MACs from tracking |

---

## Web GUI (optional)

Passive Vigilance includes a live browser dashboard for monitoring detections
in real time without `journalctl`. It is disabled by default — zero overhead
when `GUI_ENABLED=false`.

### Enable

1. Install Flask:
   ```bash
   pip install flask --break-system-packages
   ```

2. Set in `.env`:
   ```ini
   GUI_ENABLED=true
   GUI_HOST=0.0.0.0   # bind to all interfaces (accessible from other devices on LAN)
   GUI_PORT=8080
   ```

3. Start the orchestrator normally:
   ```bash
   python3 main.py
   # or: sudo systemctl start passive-vigilance
   ```

4. Open `http://<pi-ip>:8080` in a browser.

### Features

| Tab | Contents |
|-----|----------|
| Map | Live Leaflet map; WiFi, aircraft, and drone RF markers color-coded by alert level |
| WiFi/BT | Table of all persistence detections with score, MAC type, manufacturer |
| Aircraft | ADS-B aircraft table with callsign, altitude, speed; emergency rows highlighted |
| Drone RF | Drone RF detection table with frequency and power |
| Alerts | Live alert feed — WiFi, aircraft, drone, and system health events |

Sensor health indicators (GPS / WiFi / ADS-B / Drone) turn green or red in the
header bar based on `/api/status` polled every 5 seconds.

New events appear instantly via Server-Sent Events (SSE) — no page refresh needed.

### API endpoints

The GUI exposes a REST API alongside the SSE stream:

| Endpoint | Description |
|----------|-------------|
| `GET /api/status` | Session ID, sensor health, cumulative stats, GPS fix |
| `GET /api/wifi` | All WiFi/BT persistence events this session |
| `GET /api/aircraft` | All ADS-B aircraft events this session |
| `GET /api/drone` | All drone RF detections this session |
| `GET /api/alerts` | All alert events this session |
| `GET /stream` | SSE stream — real-time events as they occur |

### Security note

The GUI has no authentication. **Do not expose it to the public internet.**
Bind to `127.0.0.1` or use a VPN / SSH tunnel if remote access is needed:

```bash
ssh -L 8080:localhost:8080 survkis@<pi-ip>
# then open http://localhost:8080 locally
```

---

## Additional sections

> TODO: HackRF tools, Wi-Fi monitor mode (Alfa AWUS036ACH),
> Bluetooth setup, Python virtual environment.
