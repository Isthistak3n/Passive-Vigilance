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

## Additional sections

> TODO: HackRF tools, Wi-Fi monitor mode (Alfa AWUS036ACH),
> Bluetooth setup, Python virtual environment.
