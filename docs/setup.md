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

## Additional sections

> TODO: RTL-SDR drivers, HackRF tools, Wi-Fi monitor mode (Alfa AWUS036ACH),
> Bluetooth setup, dump1090, Python virtual environment.
