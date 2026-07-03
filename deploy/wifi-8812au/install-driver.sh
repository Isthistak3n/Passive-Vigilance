#!/usr/bin/env bash
#
# install-driver.sh — build & install the out-of-tree rtl88XXau (aircrack-ng)
# driver for the RTL8812AU WiFi adapter, patched to build on Linux 6.15-6.18,
# and blacklist the in-tree rtw88_8812au.
#
# WHY: the in-tree rtw88_8812au driver has incomplete 5 GHz TX-power tables.
# When Kismet hops the monitor interface onto a 5 GHz channel with no table
# entry, rtw_get_tx_power_params throws a WARN, the channel-set fails, the
# capture helper dies, and Kismet floods SOURCEERROR "IPC connection closed".
# The aircrack-ng 88XXau driver has complete 2.4 + 5 GHz (incl. DFS) tables and
# handles full-spectrum monitor-mode hopping. See docs/wifi-driver-8812au.md.
#
# Idempotent. Requires sudo. Run once per node (survives kernel updates via DKMS).
#
#   sudo deploy/wifi-8812au/install-driver.sh
#
# After it finishes, also apply the Kismet 20 MHz setting (see kismet-88XXau-
# 20mhz.conf in this dir) and reboot — or do a live modprobe swap.
set -euo pipefail

DRIVER_REPO="https://github.com/aircrack-ng/rtl8812au.git"
DRIVER_COMMIT="734485506a30d6237c2deaad666a19f8ca5379f2"  # 2026-03-17, "fix-6.19"
DKMS_NAME="realtek-rtl88xxau"
DKMS_VER="5.6.4.2~20230501"                                 # from the repo's dkms.conf
BUILD_DIR="${BUILD_DIR:-/usr/src/${DKMS_NAME}-${DKMS_VER}}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ $EUID -ne 0 ]]; then echo "run with sudo" >&2; exit 1; fi

echo "==> prerequisites (dkms + build tooling + headers)"
apt-get install -y dkms git build-essential "linux-headers-$(uname -r)"

echo "==> fetch driver source @ ${DRIVER_COMMIT:0:7} into ${BUILD_DIR}"
rm -rf "$BUILD_DIR"
git clone "$DRIVER_REPO" "$BUILD_DIR"
git -C "$BUILD_DIR" checkout -q "$DRIVER_COMMIT"

echo "==> patch for Linux 6.15-6.18"
# 1) build-system: kernel 6.18 ignores the deprecated EXTRA_CFLAGS; use ccflags-y
find "$BUILD_DIR" \( -name Makefile -o -name '*.mk' \) \
    -exec sed -i 's|EXTRA_CFLAGS|ccflags-y|g' {} +
# 2) C sources: timer API compat shim + cfg80211 radio_idx parameter
python3 "$HERE/apply-linux-patches.py" "$BUILD_DIR"

echo "==> dkms build + install"
dkms remove "${DKMS_NAME}/${DKMS_VER}" --all 2>/dev/null || true
dkms add "$BUILD_DIR"
dkms build "${DKMS_NAME}/${DKMS_VER}" --force
dkms install "${DKMS_NAME}/${DKMS_VER}" --force

echo "==> blacklist the in-tree driver + refresh module deps"
install -m 0644 "$HERE/pv-8812au.conf" /etc/modprobe.d/pv-8812au.conf
depmod -a

cat <<EOF

==> done. Module installed for kernel $(uname -r).

Next:
  * Kismet: append the two lines from
      $HERE/kismet-88XXau-20mhz.conf
    to /etc/kismet/kismet_site.conf (hop at 20 MHz so the driver does not
    warn on HT40/VHT width sets — still visits every frequency).
  * Reboot to activate (the blacklist + DKMS autoload bring the adapter up
    on rtl88XXau), then confirm:
      basename \$(readlink -f /sys/class/net/wlan1/device/driver)   # -> rtl88XXau
EOF
