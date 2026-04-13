# Passive Vigilance

Passive RF/WiFi/BT/ADS-B sensor platform on Raspberry Pi using RTL-SDR or HackRF, Kismet,
dump1090, GPS — with drone detection alerts, WiGLE wardriving uploads, adsb.lol flight
enrichment, and shapefile output for GIS analysis.

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
