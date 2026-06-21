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
  rtl-sdr librtlsdr0 librtlsdr-dev \
  bluez usbutils
# bluez   -> btmgmt, used by the service ExecStartPre to enable LE on the BLE
#            controller at boot (BR/EDR-only otherwise starves the raw-HCI scanner).
# usbutils -> lsusb (DroneRF hardware probe) and usbreset (optional SDR-handoff
#            wedge recovery, SDR_HANDOFF_USB_RESET=true).

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

# ── 2c. AIS-catcher (optional — marine AIS decoder, VHF ~162 MHz) ──────────
# OFF by default: AIS is VHF and won't receive on a 1090 ADS-B antenna. Build it
# only when adding VHF hardware. Gate: INSTALL_AIS=true ./install.sh. No Debian
# Trixie/ARM package, so build from source (like readsb). The coordinator manages
# the ais-catcher.service start/stop (see the sudoers rule below).
if [ "${INSTALL_AIS:-false}" = "true" ]; then
    echo "$LOG Installing AIS-catcher (marine AIS decoder) from source..."
    DEBIAN_FRONTEND=noninteractive apt install -y \
        git cmake build-essential pkg-config libusb-1.0-0-dev librtlsdr-dev zlib1g-dev
    AISC_DIR=/opt/AIS-catcher
    if [ ! -d "$AISC_DIR" ]; then
        git clone --depth 1 https://github.com/jvde-github/AIS-catcher.git "$AISC_DIR"
    fi
    ( cd "$AISC_DIR" && mkdir -p build && cd build && cmake .. && make -j"$(nproc)" && make install )
    install -m 0644 "$REPO_DIR/deploy/ais-catcher.service" /etc/systemd/system/ais-catcher.service
    systemctl daemon-reload
    # Disabled at boot — the SDR coordinator starts it on the AIS slice.
    systemctl disable ais-catcher.service 2>/dev/null || true
    echo "$LOG AIS-catcher installed; ais-catcher.service is coordinator-managed (disabled at boot)."
else
    echo "$LOG Skipping AIS-catcher (set INSTALL_AIS=true to build it for VHF/AIS)."
fi

# ── 2d. acarsdec (optional — aviation ACARS decoder, VHF ~131 MHz) ─────────
# OFF by default (VHF, antenna-limited). Gate: INSTALL_ACARS=true ./install.sh.
# Build acarsdec (and its libacars dependency) from source; emits JSON over UDP to
# the ACARSModule. The coordinator manages acarsdec.service (sudoers rule above).
if [ "${INSTALL_ACARS:-false}" = "true" ]; then
    echo "$LOG Installing acarsdec (ACARS decoder) from source..."
    DEBIAN_FRONTEND=noninteractive apt install -y \
        git cmake build-essential pkg-config libusb-1.0-0-dev librtlsdr-dev zlib1g-dev libxml2-dev
    LIBACARS_DIR=/opt/libacars
    if [ ! -d "$LIBACARS_DIR" ]; then
        git clone --depth 1 https://github.com/szpajder/libacars.git "$LIBACARS_DIR"
    fi
    ( cd "$LIBACARS_DIR" && mkdir -p build && cd build && cmake .. && make -j"$(nproc)" && make install && ldconfig )
    ACARSDEC_DIR=/opt/acarsdec
    if [ ! -d "$ACARSDEC_DIR" ]; then
        git clone --depth 1 https://github.com/TLeconte/acarsdec.git "$ACARSDEC_DIR"
    fi
    ( cd "$ACARSDEC_DIR" && mkdir -p build && cd build && cmake .. -Drtl=ON && make -j"$(nproc)" && make install )
    install -m 0644 "$REPO_DIR/deploy/acarsdec.service" /etc/systemd/system/acarsdec.service
    systemctl daemon-reload
    systemctl disable acarsdec.service 2>/dev/null || true
    echo "$LOG acarsdec installed; acarsdec.service is coordinator-managed (disabled at boot)."
else
    echo "$LOG Skipping acarsdec (set INSTALL_ACARS=true to build it for VHF/ACARS)."
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

# Leaflet assets ship vendored in-repo (gui/static/leaflet/) — no download needed.
# The GUI serves them locally and works on a fresh clone with no internet.

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

# ── 3c. Bluetooth controller bring-up (for PV's raw-HCI BLE scanner) ────────
# PV's BLE scanner owns the controller directly (no bluetoothd), so nothing else
# unblocks rfkill, raises the controller, or enables LE — and it can enumerate
# late at boot. Install a helper + udev rule so the controller is prepared on boot
# and hot-plug; passive-vigilance.service also calls it via ExecStartPre.
echo "$LOG Configuring Bluetooth controller bring-up..."
cp "$REPO_DIR/deploy/set-bt-up.sh" /usr/local/bin/set-bt-up.sh
chmod +x /usr/local/bin/set-bt-up.sh
cp "$REPO_DIR/deploy/99-bt-hci-up.rules" /etc/udev/rules.d/99-bt-hci-up.rules
udevadm control --reload-rules

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

# Allow the PV service user to start/stop the SDR decoder services without
# interactive auth. The SDR coordinator time-shares the single dongle across bands
# (readsb for ADS-B, ais-catcher for AIS, acarsdec for ACARS); it starts/stops each
# service on its slice. The scope MUST list every managed service or the handoff
# fails silently. (acarsdec is Phase 2; listed now so the rule is forward-ready.)
echo "$LOG Writing sudoers rule for SDR decoder service management..."
cat > /etc/sudoers.d/passive-vigilance << EOF
# Allow $PI_USER to start/stop SDR decoder services for the coordinator's time-share
$PI_USER ALL=(ALL) NOPASSWD: /usr/bin/systemctl start readsb.service, /usr/bin/systemctl stop readsb.service, /usr/bin/systemctl start ais-catcher.service, /usr/bin/systemctl stop ais-catcher.service, /usr/bin/systemctl start acarsdec.service, /usr/bin/systemctl stop acarsdec.service
EOF
chmod 0440 /etc/sudoers.d/passive-vigilance

# ── 5. gpsd config ─────────────────────────────────────────────────────────
echo "$LOG Configuring gpsd..."

# Auto-detect GPS device(s) — a node may have a UART HAT (ttyAMA0) and one
# or more USB GNSS receivers (u-blox CDC-ACM on ttyACM*, USB-serial bridges
# on ttyUSB*) at once; pass all present devices to gpsd so it uses whichever
# is plugged in. Mirrors the candidate list in modules/gps.py.
DEVICES=""
add_device() {
    [ -e "$1" ] || return
    case " $DEVICES " in *" $1 "*) return ;; esac
    DEVICES="${DEVICES:+$DEVICES }$1"
}

# 1) GPS_DEVICE from .env — single path or space-separated list; takes
#    priority so an explicit operator choice is listed first.
if [ -f "$REPO_DIR/.env" ]; then
    GPS_DEVICE_ENV=$(grep "^GPS_DEVICE=" "$REPO_DIR/.env" \
        | cut -d= -f2- | tr -d '"')
fi
for dev in $GPS_DEVICE_ENV; do
    add_device "$dev"
done

# 2) UART HAT (Waveshare L76K etc.)
add_device "/dev/ttyAMA0"

# 3) USB GNSS receivers — u-blox CDC-ACM dongles and USB-serial bridges
for dev in /dev/ttyACM* /dev/ttyUSB*; do
    add_device "$dev"
done

if [ -z "$DEVICES" ]; then
    DEVICES="/dev/ttyUSB0"
    echo "$LOG WARNING: No GPS device found, defaulting to $DEVICES"
else
    echo "$LOG GNSS device(s) detected: $DEVICES"
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
PI_USER="$PI_USER" envsubst '$PI_USER' \
  < "$REPO_DIR/deploy/kismet.service" \
  > /etc/systemd/system/kismet.service

# ── 7. Passive Vigilance service ───────────────────────────────────────────
echo "$LOG Installing Passive Vigilance service..."
PI_USER="$PI_USER" envsubst '$PI_USER' \
  < "$REPO_DIR/deploy/passive-vigilance.service" \
  > /etc/systemd/system/passive-vigilance.service

# ── 7b. Persistent journald (so a crash window survives) ───────────────────
# Raspberry Pi OS ships /usr/lib/systemd/journald.conf.d/40-rpi-volatile-storage.conf
# which forces Storage=volatile — logs live in RAM and are lost on reboot, to spare
# the SD card. That drop-in overrides /etc/systemd/journald.conf, so editing the main
# file has no effect. We add a higher-priority /etc drop-in (99- sorts last) so logs
# persist across reboots for crash diagnosis, capped at 500M to bound SD-card wear.
echo "$LOG Enabling persistent journald (capped at 500M)..."
mkdir -p /etc/systemd/journald.conf.d
cat > /etc/systemd/journald.conf.d/99-persistent.conf << 'JOURNALD'
[Journal]
Storage=persistent
SystemMaxUse=500M
JOURNALD
mkdir -p /var/log/journal
# Set correct ownership/ACLs on the journal dir, then apply the new config.
systemd-tmpfiles --create --prefix /var/log/journal 2>/dev/null || true
systemctl restart systemd-journald
journalctl --flush 2>/dev/null || true

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
