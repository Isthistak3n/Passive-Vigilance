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
  gpsd gpsd-clients python3-gps python3-pip \
  kismet readsb \
  librtlsdr0 librtlsdr-dev

# ── 3. Python dependencies ─────────────────────────────────────────────────
echo "$LOG Installing Python packages..."
sudo -u "$PI_USER" pip3 install -r "$REPO_DIR/requirements.txt" \
  --break-system-packages -q
# geopy: geodesic distance fallback for persistence engine location clustering
sudo -u "$PI_USER" pip3 install geopy --break-system-packages -q
# GIS output — shapefile and GeoJSON session export
apt install -y python3-geopandas python3-fiona python3-shapely

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
# Prevent DVB-T drivers from claiming the dongle before rtlsdr can
echo "$LOG Blacklisting conflicting RTL-SDR kernel modules..."
echo "blacklist dvb_usb_rtl28xxu" | tee /etc/modprobe.d/rtlsdr.rules > /dev/null
echo "blacklist rtl2832"          | tee -a /etc/modprobe.d/rtlsdr.rules > /dev/null
echo "blacklist rtl2830"          | tee -a /etc/modprobe.d/rtlsdr.rules > /dev/null

# ── 4. Groups ──────────────────────────────────────────────────────────────
echo "$LOG Configuring user groups..."
usermod -aG kismet "$PI_USER"
usermod -aG dialout "$PI_USER"

# ── 5. gpsd config ─────────────────────────────────────────────────────────
echo "$LOG Configuring gpsd..."
cat > /etc/default/gpsd << 'EOF'
START_DAEMON="true"
USBAUTO="true"
DEVICES="/dev/ttyUSB0"
GPSD_OPTIONS="-n"
EOF
mkdir -p /etc/systemd/system/gpsd.service.d
cp "$REPO_DIR/deploy/gpsd.override.conf" \
  /etc/systemd/system/gpsd.service.d/override.conf

# ── 6. Kismet service ──────────────────────────────────────────────────────
echo "$LOG Installing Kismet systemd service..."
cp "$REPO_DIR/deploy/kismet.service" /etc/systemd/system/kismet.service

# ── 7. Passive Vigilance service ───────────────────────────────────────────
echo "$LOG Installing Passive Vigilance service..."
cp "$REPO_DIR/deploy/passive-vigilance.service" \
  /etc/systemd/system/passive-vigilance.service
sed -i "s|/home/survkis|/home/$PI_USER|g" \
  /etc/systemd/system/passive-vigilance.service

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
echo "════════════════════════════════════════"
