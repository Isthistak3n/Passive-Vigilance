#!/usr/bin/env python3
"""Phase-1 go/no-go spike for BLE advertisement capture (design-ble-advertisement-capture.md).

Listens **passively** (HCI LE scan_type=0x00 — listen-only, no SCAN_REQ, consistent
with the platform's no-transmit charter) for BLE advertisements and reports, per
device, the fingerprint-relevant fields the design note depends on:

    - manufacturer data company id(s)            -> vendor fingerprint
    - service UUIDs / service data                -> service / beacon fingerprint
    - local name, tx power                        -> coarse identity
    - per-advertisement RSSI                      -> proximity / approaching signal

VALIDATED ON THE NODE (2026-06-13, Edimax hci0, BlueZ 5.82):
    - bleak's *passive* path (BlueZ AdvertisementMonitor / or_patterns) returns
      nothing on this controller — it does not support offloaded advert monitoring.
    - Raw HCI passive scan (this script) WORKS: captured Apple adverts with a real
      RSSI (-58 dBm), which is the win over Kismet's flat 0. Verdict: GO, with raw
      HCI as the production capture primitive (not AdvertisementMonitor).

Prerequisites (the disruptive cutover — do NOT leave half-applied during a soak):
    1. Free hci0 from Kismet — close the linuxbluetooth source via the Kismet API
       (POST /datasource/by-uuid/<uuid>/close_source.cmd) so WiFi capture stays up,
       or comment out the source line in kismet_site.conf and reload.
    2. Make sure bluetoothd is NOT scanning on hci0 (stop it; it is disabled here).
    3. sudo hciconfig hci0 up
    4. sudo python3 scripts/ble_capture_spike.py --seconds 60

Restore afterwards: reopen the Kismet source (open_source.cmd) — WiFi never dropped.

Needs root (raw HCI socket / CAP_NET_RAW+ADMIN). No third-party dependencies.
"""
from __future__ import annotations

import argparse
import socket
import struct
import time

SOL_HCI = 0
HCI_FILTER = 2
OGF_LE = 0x08
OCF_SET_SCAN_PARAMS = 0x000B
OCF_SET_SCAN_ENABLE = 0x000C


def _send_cmd(sock: socket.socket, ocf: int, params: bytes, ogf: int = OGF_LE) -> None:
    opcode = (ogf << 10) | ocf
    sock.send(b"\x01" + struct.pack("<H", opcode) + bytes([len(params)]) + params)


def _parse_ad(data: bytes):
    """Walk the AD structures, returning (company_ids, n_service_uuids, n_service_data, name)."""
    company_ids: list[int] = []
    n_svc = n_svcdata = 0
    name = ""
    j = 0
    while j < len(data):
        length = data[j]
        if length == 0:
            break
        ad_type = data[j + 1] if j + 1 < len(data) else 0
        value = data[j + 2 : j + 1 + length]
        if ad_type == 0xFF and len(value) >= 2:        # manufacturer specific
            company_ids.append(value[0] | (value[1] << 8))
        elif ad_type in (0x02, 0x03):                  # 16-bit service UUIDs
            n_svc += 1
        elif ad_type == 0x16:                          # service data 16-bit
            n_svcdata += 1
        elif ad_type in (0x08, 0x09):                  # shortened / complete name
            name = value.decode("utf-8", "replace")
        j += length + 1
    return company_ids, n_svc, n_svcdata, name


def run(seconds: int) -> int:
    sock = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_RAW, socket.BTPROTO_HCI)
    # hci_filter is 4-byte aligned -> 16 bytes (the 14 logical bytes + 2 pad), else EINVAL.
    sock.setsockopt(SOL_HCI, HCI_FILTER, struct.pack("<IIIH2x", 0xFFFFFFFF, 0xFFFFFFFF, 0xFFFFFFFF, 0))
    sock.bind((0,))

    # LE Set Scan Parameters: scan_type=0x00 (PASSIVE), interval/window 0x0010,
    # own_addr_type public, filter_policy accept-all.
    _send_cmd(sock, OCF_SET_SCAN_PARAMS, struct.pack("<BHHBB", 0x00, 0x0010, 0x0010, 0x00, 0x00))
    time.sleep(0.2)
    _send_cmd(sock, OCF_SET_SCAN_ENABLE, struct.pack("<BB", 0x01, 0x00))  # enable, no dedup

    print(f"[spike] passive HCI LE scan (listen-only) for {seconds}s ...")
    sock.settimeout(seconds + 1)
    seen: dict[str, tuple] = {}
    adverts = 0
    t0 = time.time()
    while time.time() - t0 < seconds:
        try:
            pkt = sock.recv(260)
        except socket.timeout:
            break
        # HCI event (0x04), LE Meta (0x3E), subevent LE Advertising Report (0x02)
        if len(pkt) < 4 or pkt[0] != 0x04 or pkt[1] != 0x3E or pkt[3] != 0x02:
            continue
        i = 5  # skip type, event code, plen, subevent, num_reports
        addr = pkt[i + 2 : i + 8][::-1]
        dlen = pkt[i + 8]
        data = pkt[i + 9 : i + 9 + dlen]
        rssi = struct.unpack("b", pkt[i + 9 + dlen : i + 10 + dlen])[0]
        adverts += 1
        seen[":".join("%02X" % b for b in addr)] = (rssi, *_parse_ad(data))

    _send_cmd(sock, OCF_SET_SCAN_ENABLE, struct.pack("<BB", 0x00, 0x00))  # disable
    sock.close()

    real_rssi = sum(1 for v in seen.values() if v[0] not in (0, None))
    with_payload = sum(1 for v in seen.values() if v[1] or v[2] or v[3])
    print(f"\n[spike] distinct addresses: {len(seen)}   total adverts: {adverts}")
    print(f"[spike] adverts with a real (non-zero) RSSI: {real_rssi}")
    print(f"[spike] devices carrying payload to fingerprint: {with_payload}")
    print("-" * 72)
    for addr, (rssi, cids, n_svc, n_svcdata, name) in sorted(seen.items(), key=lambda kv: kv[1][0], reverse=True)[:40]:
        vendor = ",".join(f"0x{c:04x}" for c in cids) or "-"
        print(
            f"{addr}  rssi={rssi!s:>4}  vendor={vendor:<14} "
            f"svc={n_svc} svcdata={n_svcdata} name={name!r}"
        )
    print("-" * 72)
    ok = real_rssi > 0 and with_payload > 0
    print(
        "[spike] VERDICT:",
        "GO — passive payload + real RSSI captured; software path is viable."
        if ok else
        "NO-GO — saw no payload or no real RSSI in this window (try a longer scan / busier area).",
    )
    return 0 if ok else 2


def main() -> int:
    ap = argparse.ArgumentParser(description="Passive BLE advertisement capture spike (raw HCI)")
    ap.add_argument("--seconds", type=int, default=60, help="scan duration (default 60)")
    args = ap.parse_args()
    try:
        return run(args.seconds)
    except PermissionError:
        raise SystemExit("needs root for the raw HCI socket — run with sudo.")
    except OSError as exc:
        raise SystemExit(f"HCI error ({exc}). Is hci0 up and free of Kismet/bluetoothd?")


if __name__ == "__main__":
    raise SystemExit(main())
