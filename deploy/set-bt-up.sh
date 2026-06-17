#!/bin/bash
# Prepare the USB Bluetooth controller for Passive Vigilance's passive BLE
# advertisement scanner (modules/ble_scanner.py), which owns the controller
# directly through a raw HCI socket. bluetoothd is intentionally NOT running, so
# nothing else prepares the controller — this script is the single place that does:
#   - rfkill soft-blocks a fresh dongle until unblocked,
#   - after a reboot/re-plug the controller comes up BR/EDR-only (LE disabled),
#     which silently starves the advert scanner (it binds but gets ~no reports),
#   - a USB dongle can enumerate late at boot.
# So: unblock rfkill, bring the lowest present hci controller up, and enable LE.
# Invoked from passive-vigilance.service (ExecStartPre) and the BT-add udev rule
# (deploy/99-bt-hci-up.rules) so it covers both boot and hot-plug.
# Always exits 0 — BLE is best-effort and must never block startup.

/usr/sbin/rfkill unblock bluetooth 2>/dev/null || true

# Lowest-numbered present controller — the dongle may re-enumerate to hci1 after a
# USB reset, matching ble_scanner's own index resolution. Wait briefly for a late
# dongle at boot.
_hci_index() {
  ls /sys/class/bluetooth/ 2>/dev/null \
    | sed -n 's/^hci\([0-9][0-9]*\)$/\1/p' | sort -n | head -1
}
idx=$(_hci_index)
for _ in $(seq 1 15); do
  [ -n "$idx" ] && break
  sleep 1
  idx=$(_hci_index)
done
[ -n "$idx" ] || exit 0

# Every controller call is timeout-bounded: btmgmt can block indefinitely on a
# wedged controller (observed post-reboot), and as a blocking ExecStartPre that
# would hang the whole service into its start timeout and crash-loop it. LE still
# works without these — ble_scanner enables LE scan over its own raw HCI socket —
# so a timeout here is harmless. Best-effort, always exit 0.
timeout 5 /usr/bin/hciconfig "hci${idx}" up >/dev/null 2>&1 || true
timeout 5 /usr/bin/btmgmt --index "$idx" power on >/dev/null 2>&1 || true
timeout 5 /usr/bin/btmgmt --index "$idx" le on >/dev/null 2>&1 || true
exit 0
