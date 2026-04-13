#!/usr/bin/env python3
"""CLI tool for managing the Passive Vigilance ignore lists.

Usage examples
--------------
  # Add a device by MAC
  python3 scripts/manage_ignore_list.py --add-mac aa:bb:cc:dd:ee:ff --label "home router"

  # Add an OUI (vendor prefix)
  python3 scripts/manage_ignore_list.py --add-oui aa:bb:cc --label "Raspberry Pi Foundation"

  # Add an SSID
  python3 scripts/manage_ignore_list.py --add-ssid "MyHomeNetwork" --label "home AP"

  # Remove a MAC
  python3 scripts/manage_ignore_list.py --remove-mac aa:bb:cc:dd:ee:ff

  # List everything
  python3 scripts/manage_ignore_list.py --list

  # Show counts
  python3 scripts/manage_ignore_list.py --stats

  # Import current Kismet device list
  python3 scripts/manage_ignore_list.py --import-kismet
"""

import argparse
import asyncio
import json
import os
import sys

# Allow running from repo root without installing the package
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from dotenv import load_dotenv

load_dotenv()

DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data", "ignore_lists")


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Manage Passive Vigilance ignore lists",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    group = p.add_mutually_exclusive_group(required=True)
    group.add_argument("--add-mac",    metavar="MAC",  help="Add a full MAC address")
    group.add_argument("--add-oui",    metavar="OUI",  help="Add an OUI prefix (first 3 octets)")
    group.add_argument("--add-ssid",   metavar="SSID", help="Add an SSID")
    group.add_argument("--remove-mac", metavar="MAC",  help="Remove a MAC or OUI prefix")
    group.add_argument("--remove-ssid",metavar="SSID", help="Remove an SSID")
    group.add_argument("--list",       action="store_true", help="Print all entries")
    group.add_argument("--stats",      action="store_true", help="Print entry counts")
    group.add_argument("--import-kismet", action="store_true",
                       help="Bulk-import current Kismet device list")

    p.add_argument("--label", default="", help="Human-readable label (optional)")
    p.add_argument("--data-dir", default=DATA_DIR,
                   help="Directory for ignore list JSON files (default: data/ignore_lists)")
    return p


def _print_list(il) -> None:
    from modules.ignore_list import _MAC_FILE, _SSID_FILE
    print(f"\n{'─'*60}")
    print(f"  MACs ({len(il._macs)} full / {len(il._ouis)} OUI prefixes)")
    print(f"{'─'*60}")
    for entry in sorted(il._macs.values(), key=lambda e: e["mac"]):
        label = f"  {entry['label']}" if entry.get("label") else ""
        print(f"  {entry['mac']}{label}  [{entry['added']}]")
    for entry in sorted(il._ouis.values(), key=lambda e: e["mac"]):
        label = f"  {entry['label']}" if entry.get("label") else ""
        print(f"  {entry['mac']} (OUI){label}  [{entry['added']}]")

    print(f"\n{'─'*60}")
    print(f"  SSIDs ({len(il._ssids)})")
    print(f"{'─'*60}")
    for entry in sorted(il._ssids.values(), key=lambda e: e["ssid"].lower()):
        label = f"  {entry['label']}" if entry.get("label") else ""
        print(f"  {entry['ssid']!r}{label}  [{entry['added']}]")
    print()


async def _import_kismet(il) -> None:
    """Connect to Kismet, poll current devices, bulk-add to ignore list."""
    from modules.kismet import KismetModule

    km = KismetModule()
    try:
        await km.connect()
    except ConnectionError as exc:
        print(f"[ERROR] Could not connect to Kismet: {exc}", file=sys.stderr)
        sys.exit(1)

    devices = await km.poll_devices()
    await km.close()

    if not devices:
        print("No devices returned from Kismet.")
        return

    added = il.add_from_kismet(devices)
    il.save()
    print(f"Imported {added} new devices from Kismet ({len(devices)} total seen).")


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()

    from modules.ignore_list import IgnoreList
    il = IgnoreList(data_dir=args.data_dir)

    if args.add_mac:
        il.add_mac(args.add_mac, label=args.label)
        il.save()
        print(f"Added MAC: {args.add_mac}")

    elif args.add_oui:
        il.add_oui(args.add_oui, label=args.label)
        il.save()
        print(f"Added OUI: {args.add_oui}")

    elif args.add_ssid:
        il.add_ssid(args.add_ssid, label=args.label)
        il.save()
        print(f"Added SSID: {args.add_ssid!r}")

    elif args.remove_mac:
        removed = il.remove_mac(args.remove_mac)
        if removed:
            il.save()
            print(f"Removed: {args.remove_mac}")
        else:
            print(f"Not found in ignore list: {args.remove_mac}", file=sys.stderr)
            sys.exit(1)

    elif args.remove_ssid:
        removed = il.remove_ssid(args.remove_ssid)
        if removed:
            il.save()
            print(f"Removed SSID: {args.remove_ssid!r}")
        else:
            print(f"Not found in ignore list: {args.remove_ssid!r}", file=sys.stderr)
            sys.exit(1)

    elif args.list:
        _print_list(il)

    elif args.stats:
        s = il.stats()
        print(f"MACs:  {s['mac_count']}")
        print(f"OUIs:  {s['oui_count']}")
        print(f"SSIDs: {s['ssid_count']}")
        print(f"Total: {sum(s.values())}")

    elif args.import_kismet:
        asyncio.run(_import_kismet(il))


if __name__ == "__main__":
    main()
