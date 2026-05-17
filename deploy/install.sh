#!/bin/bash
set -e

# ── Passive Vigilance — One-command installer ──────────────────────────────
# Usage: sudo bash deploy/install.sh
# Tested on: Raspberry Pi OS / Debian Bookworm and Trixie, ARM64/ARM32

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PI_USER="${SUDO_USER:-$(whoami)}"
DISTRO=$(lsb_release -cs 2>/dev/null || echo "bookworm")
LOG="[Passive Vigilance]"

# ── 1. Checks ──────────────────────────────────────────────────────────────
echo "$LOG Checking environment..."
[ "$EUID" -eq 0 ] || { echo "Please run with sudo"; exit 1; }
ping -c1 -W2 8.8.8.8 &>/dev/null || { echo "No internet connection"; exit 1; }
echo "$LOG Detected OS: $DISTRO"

# ── 2. Apt dependencies ────────────────────────────────────────────────────
echo "$LOG Installing system packages..."
apt update -qq

wget -O - https://www.kismetwireless.net/repos/kismet-release.gpg.key \
  --quiet | gpg --dearmor | \
  tee /usr/share/keyrings/kismet.gpg > /dev/null

echo "deb [signed-by=/usr/share/keyrings/kismet.gpg] \
https://www.kismetwireless.net/repos/apt/release/${DISTRO} \
${DISTRO} main" | tee /etc/apt/sources.list.d/kismet.list

apt update -qq
DEBIAN_FRONTEND=noninteractive apt install -y \
  gpsd gpsd-clients python3-gps python3-pip python3-venv \
  kismet \
  rtl-sdr librtlsdr0 librtlsdr-dev

# ── 2b. readsb (wiedehopf build — RTL-SDR enabled) ────────────────────────
# The Debian Trixie readsb package is compiled without ENABLE_RTLSDR.
# wiedehopf's build supports --device-type rtlsdr and includes tar1090
# (lighttpd-based web map) which serves /data/aircraft.json on port 8080.
echo "$LOG Installing readsb (wiedehopf — RTL-SDR + tar1090 included)..."
bash -c "$(wget -O - https://raw.githubusercontent.com/wiedehopf/adsb-scripts/master/readsb-install.sh)"

# Align tar1090's alternate HTTP port with PV's READSB_URL default (port 8080).
# readsb-install.sh creates 95-tar1090-otherport.conf on port 8504 by default.
LIGHTTPD_OTHERPORT="/etc/lighttpd/conf-enabled/95-tar1090-otherport.conf"
if [ -f "$LIGHTTPD_OTHERPORT" ]; then
    sed -i 's/":8504"/":8080"/' "$LIGHTTPD_OTHERPORT"
    systemctl restart lighttpd
    echo "$LOG tar1090 configured on port 8080 (/data/aircraft.json)"
fi

# ── 3. Python dependencies ─────────────────────────────────────────────────
# Install GDAL and GIS system dependencies first
# (required for geopandas/fiona to install without building from source on ARM)
DEBIAN_FRONTEND=noninteractive apt-get install -y \
    gdal-bin libgdal-dev python3-gdal \
    python3-geopandas python3-fiona python3-numpy python3-shapely

# Create virtualenv with access to apt-installed system packages.
# --system-site-packages exposes python3-gps, python3-geopandas, python3-fiona,
# python3-numpy, and python3-gdal without copying them into the venv.
# pip then only installs the remaining lightweight packages (aiohttp, pyrtlsdr, etc.)
VENV_DIR="/opt/passive-vigilance/venv"
echo "$LOG Creating Python virtualenv at $VENV_DIR..."
mkdir -p "$(dirname "$VENV_DIR")"
python3 -m venv --system-site-packages "$VENV_DIR"
chown -R "$PI_USER:$PI_USER" /opt/passive-vigilance

echo "$LOG Installing Python packages into virtualenv..."
"$VENV_DIR/bin/pip" install --upgrade pip -q
"$VENV_DIR/bin/pip" install -r "$REPO_DIR/requirements.txt" -q
# geopy: geodesic distance fallback for persistence engine location clustering
"$VENV_DIR/bin/pip" install geopy -q

# Leaflet.js for offline use — only download if web GUI is enabled
if grep -qE "^\s*GUI_ENABLED\s*=\s*true" "$REPO_DIR/.env" 2>/dev/null; then
  echo "$LOG Downloading Leaflet.js for offline use (field deployments)..."
  LEAFLET_VERSION="1.9.4"
  LEAFLET_DIR="$REPO_DIR/gui/static/leaflet"
  mkdir -p "$LEAFLET_DIR"
  curl -sL "https://unpkg.com/leaflet@${LEAFLET_VERSION}/dist/leaflet.js" \
      -o "$LEAFLET_DIR/leaflet.js"
  curl -sL "https://unpkg.com/leaflet@${LEAFLET_VERSION}/dist/leaflet.css" \
      -o "$LEAFLET_DIR/leaflet.css"
  curl -sL "https://unpkg.com/leaflet@${LEAFLET_VERSION}/dist/images/marker-icon.png" \
      -o "$LEAFLET_DIR/marker-icon.png"
  curl -sL "https://unpkg.com/leaflet@${LEAFLET_VERSION}/dist/images/marker-shadow.png" \
      -o "$LEAFLET_DIR/marker-shadow.png"
  echo "$LOG Leaflet downloaded for offline use"
fi

ln -sf "$VENV_DIR/bin/python3" /usr/local/bin/pv-python
echo "$LOG Virtualenv ready. To run manually: $VENV_DIR/bin/python3 main.py"

# Create session output directory
mkdir -p "/home/$PI_USER/Passive-Vigilance/data/sessions"
chown "$PI_USER:$PI_USER" "/home/$PI_USER/Passive-Vigilance/data/sessions"

# ── 3b. WiFi monitor mode setup ────────────────────────────────────────────
echo "$LOG Configuring WiFi monitor mode..."
# Detect USB WiFi interface (wlan1 if built-in WiFi is wlan0)
WIFI_IFACE=$(ip link show | grep -E "^[0-9]+: wlan[1-9]" | \
  head -1 | awk '{print $2}' | tr -d ':')
if [ -z "$WIFI_IFACE" ]; then
  WIFI_IFACE="wlan1"
  echo "$LOG Warning: could not detect USB WiFi interface, defaulting to wlan1"
fi
echo "$LOG USB WiFi interface detected as: $WIFI_IFACE"

# Tell NetworkManager to ignore it
cat > /etc/NetworkManager/conf.d/99-unmanaged-wifi-monitor.conf << EOF
[keyfile]
unmanaged-devices=interface-name:${WIFI_IFACE}
EOF

# Install udev rule
sed "s/wlan1/${WIFI_IFACE}/g" \
  "$REPO_DIR/deploy/99-wlan1-monitor.rules" > \
  /etc/udev/rules.d/99-wifi-monitor.rules

# Install monitor mode script
sed "s/wlan1/${WIFI_IFACE}/g" \
  "$REPO_DIR/deploy/set-monitor-mode.sh" > \
  /usr/local/bin/set-monitor-mode.sh
chmod +x /usr/local/bin/set-monitor-mode.sh

# Store interface name in .env
if grep -q "WIFI_MONITOR_INTERFACE" "$REPO_DIR/.env" 2>/dev/null; then
  sed -i "s/WIFI_MONITOR_INTERFACE=.*/WIFI_MONITOR_INTERFACE=${WIFI_IFACE}/" \
    "$REPO_DIR/.env"
else
  echo "WIFI_MONITOR_INTERFACE=${WIFI_IFACE}" >> "$REPO_DIR/.env"
fi

# Reload udev and NetworkManager
udevadm control --reload-rules
systemctl restart NetworkManager

# ── 3d. RTL-SDR kernel module blacklist ────────────────────────────────────
# Prevent DVB-T drivers from claiming the dongle before rtlsdr can.
# IMPORTANT: must be a .conf file — update-initramfs ignores .rules files
# when building the initramfs, so a .rules blacklist never applies at boot.
# The install directives are stronger than blacklist alone: they block
# explicit modprobe calls too, not just automatic USB hotplug loading.
echo "$LOG Blacklisting conflicting RTL-SDR kernel modules..."
# Remove any legacy .rules file from prior installs to avoid duplicate config.
rm -f /etc/modprobe.d/rtlsdr.rules
cat > /etc/modprobe.d/blacklist-rtlsdr.conf << 'BLACKLIST'
blacklist dvb_usb_rtl28xxu
blacklist rtl2832
blacklist rtl2830
install dvb_usb_rtl28xxu /bin/false
install rtl2832 /bin/false
install rtl2830 /bin/false
BLACKLIST
echo "$LOG Rebuilding initramfs (required for blacklist to apply at boot)..."
update-initramfs -u

# ── 4. Groups ──────────────────────────────────────────────────────────────
echo "$LOG Configuring user groups..."
usermod -aG kismet "$PI_USER"
usermod -aG dialout "$PI_USER"

# Allow the PV service user to start/stop readsb without interactive auth.
# The SDR coordinator time-shares the dongle between readsb and DroneRF;
# it needs to start/stop readsb as a system service during each slice.
echo "$LOG Writing sudoers rule for readsb management..."
cat > /etc/sudoers.d/passive-vigilance << EOF
# Allow $PI_USER to start/stop readsb for SDR coordinator time-sharing
$PI_USER ALL=(ALL) NOPASSWD: /usr/bin/systemctl start readsb.service, /usr/bin/systemctl stop readsb.service
EOF
chmod 0440 /etc/sudoers.d/passive-vigilance

# ── 5. gpsd config ─────────────────────────────────────────────────────────
echo "$LOG Configuring gpsd..."

# Auto-detect GPS device
# Priority: 1) GPS_DEVICE from .env, 2) UART HAT, 3) USB dongle
if [ -f "$REPO_DIR/.env" ]; then
    GPS_DEVICE_ENV=$(grep "^GPS_DEVICE=" "$REPO_DIR/.env" \
        | cut -d= -f2 | tr -d ' "')
fi

if [ -n "$GPS_DEVICE_ENV" ]; then
    DEVICES="$GPS_DEVICE_ENV"
    echo "$LOG GPS device from .env: $DEVICES"
elif [ -e "/dev/ttyAMA0" ]; then
    DEVICES="/dev/ttyAMA0"
    echo "$LOG GPS HAT detected: using $DEVICES"
elif DEVICES=$(ls /dev/ttyUSB* /dev/ttyACM* 2>/dev/null | head -1) && [ -n "$DEVICES" ]; then
    echo "$LOG USB GPS detected: using $DEVICES"
else
    DEVICES="/dev/ttyUSB0"
    echo "$LOG WARNING: No GPS device found, defaulting to $DEVICES"
fi

cat > /etc/default/gpsd << EOF
START_DAEMON="true"
USBAUTO="true"
DEVICES="$DEVICES"
GPSD_OPTIONS="-n"
EOF
mkdir -p /etc/systemd/system/gpsd.service.d
cat > /etc/systemd/system/gpsd.service.d/override.conf << EOF
[Service]
TimeoutSec=60
ExecStart=
ExecStart=/usr/sbin/gpsd -n $DEVICES
EOF

# ── 6. Kismet service ──────────────────────────────────────────────────────
echo "$LOG Installing Kismet systemd service..."
cp "$REPO_DIR/deploy/kismet.service" /etc/systemd/system/kismet.service

# ── 7. Passive Vigilance service ───────────────────────────────────────────
echo "$LOG Installing Passive Vigilance service..."
PI_USER="$PI_USER" envsubst '$PI_USER' \
  < "$REPO_DIR/deploy/passive-vigilance.service" \
  > /etc/systemd/system/passive-vigilance.service

# ── 8. Enable services ─────────────────────────────────────────────────────
echo "$LOG Enabling services..."
systemctl daemon-reload
systemctl enable gpsd kismet
# passive-vigilance enabled separately after .env is configured

# ── 9. .env setup ──────────────────────────────────────────────────────────
if [ ! -f "$REPO_DIR/.env" ]; then
  cp "$REPO_DIR/.env.example" "$REPO_DIR/.env"
  chown "$PI_USER:$PI_USER" "$REPO_DIR/.env"
  echo "$LOG Created .env from template"
fi

# ── 10. Start services ─────────────────────────────────────────────────────
echo "$LOG Starting gpsd and Kismet..."
systemctl start gpsd
systemctl start kismet
sleep 8

# ── 11. Verify ─────────────────────────────────────────────────────────────
KISMET_STATUS=$(curl -s -o /dev/null -w "%{http_code}" \
  http://localhost:2501/system/status.json 2>/dev/null || echo "000")

echo ""
echo "════════════════════════════════════════"
echo " Passive Vigilance — Installation Complete"
echo "════════════════════════════════════════"
echo ""
echo " Detected OS: $DISTRO"
echo " Install path: $REPO_DIR"
echo " Pi user: $PI_USER"
echo ""
echo " Services enabled at boot:"
echo "   ✓ gpsd"
echo "   ✓ kismet"
echo "   ✓ readsb (ADS-B decoder — activates when RTL-SDR dongle is plugged in)"
echo ""
if [ "$KISMET_STATUS" = "200" ] || [ "$KISMET_STATUS" = "401" ]; then
  echo "   ✓ Kismet REST API responding on :2501"
else
  echo "   ✗ Kismet API not yet responding — may still be starting"
fi
echo ""
echo " NEXT STEPS (required before starting sensor):"
echo ""
echo " 1. Log out and back in (group membership changes)"
echo ""
echo " 2. Generate Kismet API key:"
echo "    Open: http://$(hostname -I | awk '{print $1}'):2501"
echo "    Go to: Settings -> API Keys -> Create"
echo "    Name it: passive-vigilance"
echo "    Copy the key"
echo ""
echo " 3. Add credentials to .env:"
echo "    nano $REPO_DIR/.env"
echo "    Set: KISMET_API_KEY=<your key>"
echo "    Set: WIGLE_API_NAME, WIGLE_API_KEY"
echo "    Set: ADSBXLOL_API_KEY"
echo "    Set: ALERT_BACKEND + relevant config"
echo ""
echo " 4. Enable and start the sensor:"
echo "    sudo systemctl enable passive-vigilance"
echo "    sudo systemctl start passive-vigilance"
echo ""
echo " 5. Plug in RTL-SDR dongle — readsb activates automatically"
echo "    Test ADS-B: curl http://localhost:8080/data/aircraft.json"
echo "    Test RTL-SDR: rtl_test -t"
echo ""
echo " 6. Take the GPS dongle outside for first fix (30-90 seconds)"
echo ""
echo " Session output per run (data/sessions/<session_id>/):"
echo "   detections.kml     — open in Google Earth or import to Google Maps"
echo "   detections.geojson — open in QGIS or any GIS tool"
echo "   detections_wifi.shp / detections_aircraft.shp / detections_drone.shp"
echo "   summary.json       — session stats and file paths"
echo ""
echo "════════════════════════════════════════"
