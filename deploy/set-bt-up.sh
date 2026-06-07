#!/bin/bash
# Raise the USB Bluetooth controller (hci0) for Kismet's linuxbluetooth source.
#
# Kismet talks to the BT controller directly and does NOT run bluetoothd, so
# nothing else raises hci0. The USB dongle can also enumerate AFTER Kismet
# starts at boot, which leaves the linuxbluetooth source failing and hci0 DOWN.
# This unblocks rfkill and brings hci0 up, waiting briefly for a late dongle.
#
# Always exits 0 — Bluetooth is best-effort and must never block WiFi capture.
/usr/sbin/rfkill unblock bluetooth 2>/dev/null || true
for _ in $(seq 1 15); do
  if /usr/bin/hciconfig hci0 >/dev/null 2>&1; then
    /usr/bin/hciconfig hci0 up 2>/dev/null && break
  fi
  sleep 1
done
exit 0
