# Passive Vigilance

> A passive RF/WiFi/BT/ADS-B sensor platform for counter-surveillance,
> situational awareness, and open-source RF intelligence.

---

## What is this?

Passive Vigilance is a field-deployable sensor platform built on a Raspberry Pi
that helps you understand the RF environment around you — without ever
transmitting a single packet. It listens. It logs. It alerts.

Originally inspired by [Chasing Your Tail NG](https://github.com/ArgeliusLabs/Chasing-Your-Tail-NG),
Passive Vigilance extends the counter-surveillance concept into a unified,
always-on sensor platform covering WiFi, Bluetooth, ADS-B aircraft, and
drone command links simultaneously — all GPS-stamped and GIS-ready.

If you've ever wanted to know whether you're being followed, whether there's
a drone overhead, or who's flying above your location and where they came
from — this is built for that.

**It is entirely passive. It never connects to, transmits to, or interferes
with any device or network.**

---

## Use cases

- **Counter-surveillance** — detect devices that follow you across multiple
  locations using WiFi and Bluetooth beacon persistence scoring
- **Drone detection** — alert when drone command link frequencies
  (433 MHz, 868 MHz, 915 MHz, 2.4 GHz) are active in your area
- **Aircraft awareness** — track aircraft overhead with full registration,
  operator, and origin data via ADS-B and adsb.lol enrichment
- **Wardriving** — automatically upload session data to WiGLE.net to
  contribute to the global RF database
- **GIS analysis** — all detections are GPS-stamped and exported as
  shapefiles for post-session analysis in QGIS or ArcGIS
- **Field security** — deploy as a standalone sensor at events, locations,
  or during travel for passive RF situational awareness

---

## How it works

Every detection from every sensor is tagged with a GPS fix (lat, lon, UTC)
before being written to disk or triggering an alert. The platform runs
entirely as background systemd services — plug in power and it starts
capturing automatically.

---

## Project status

| Module | Status | Description |
|--------|--------|-------------|
| GPS daemon | ✅ Complete | gpsd integration, fix quality — 9 tests |
| Kismet integration | ✅ Complete | REST API, API key auth, WiGLE CSV — 10 tests |
| ADS-B + drone RF | ✅ Complete | readsb + adsb.lol enrichment — 20 tests |
| WiFi monitor mode | ✅ Complete | MT7610U/RTL8811AU udev + NM unmanaged — 15 tests |
| Ignore lists | ✅ Complete | MAC/OUI/SSID filtering, CLI tool — 22 tests |
| Persistence engine | 🔄 In progress | Time-window scoring, surveillance detection |
| Alert engine | ⏳ Planned | Pluggable Ntfy/Telegram/Signal/Discord |
| Shapefile writer | ⏳ Planned | geopandas/fiona GIS output |
| WiGLE uploader | ⏳ Planned | Session-end Kismet CSV upload |
| Orchestrator | ⏳ Planned | asyncio event loop, all modules wired |

**76 tests passing** across completed modules.

---

## Hardware Requirements

| Component | Notes |
|---|---|
| Raspberry Pi 4 | 4 GB RAM recommended |
| RTL-SDR or HackRF | ADS-B reception and drone RF scanning |
| Wi-Fi dongle (monitor mode) | Alfa AWUS036ACH recommended |
| Bluetooth dongle | BLE scanning via Bleak |
| GPS dongle | NMEA over USB (e.g. u-blox 7/8) |

---

## Software Dependencies

See [requirements.txt](requirements.txt) for the full Python dependency list.

Key runtime services:

- **Kismet** — passive Wi-Fi / Bluetooth capture daemon
- **dump1090** — ADS-B decoder (Mode S / SBS-1 output)
- **gpsd** — GPS daemon

---

## Setup

See [docs/setup.md](docs/setup.md) for full installation and configuration instructions.

Quick start:

```bash
cp .env.example .env
# Edit .env and fill in your credentials
pip install -r requirements.txt
python main.py
```

---

## Legal / Responsible Use Notice

This tool is intended **for lawful passive monitoring and research only**.  
You are responsible for ensuring your use complies with all applicable local, national, and
international laws, including but not limited to radio spectrum regulations and privacy
legislation. The authors accept no liability for unlawful or unethical use.

---

## License

MIT — see [LICENSE](LICENSE).
