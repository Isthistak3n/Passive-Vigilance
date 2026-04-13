# Setup

> This guide covers Raspberry Pi OS (Bookworm/Bullseye, 64-bit).

---

## GPS

### Hardware

Connect a USB GPS dongle (e.g. u-blox 7 or 8 based) to any USB port on the Pi.
Once plugged in, confirm the device node appears:

```bash
ls /dev/ttyUSB*
# expected: /dev/ttyUSB0
```

### Install gpsd

```bash
sudo apt install -y gpsd gpsd-clients python3-gps
```

### Configure gpsd

Edit `/etc/default/gpsd`:

```bash
sudo nano /etc/default/gpsd
```

Set the following values:

```ini
DEVICES="/dev/ttyUSB0"
GPSD_OPTIONS="-n"
```

The `-n` flag tells gpsd to start polling the receiver immediately without
waiting for a client to connect — important for cold-start time-to-first-fix.

If your dongle appears on a different node (e.g. `/dev/ttyACM0`) update
`DEVICES` accordingly, or set `GPS_DEVICE` in `.env` to match.

### Enable and start gpsd

```bash
sudo systemctl enable gpsd
sudo systemctl start gpsd
```

Verify it is running:

```bash
sudo systemctl status gpsd
```

### Test the GPS feed

```bash
cgps -s
```

You should see satellite data and, once a fix is acquired, live coordinates.

> **Note:** On first power-on (cold start) a GPS fix can take **30–90 seconds**
> outdoors with a clear sky view. Subsequent starts with a warm receiver are
> typically under 10 seconds.

---

## Additional sections

> TODO: Kismet, dump1090, RTL-SDR drivers, HackRF tools, Wi-Fi monitor mode,
> Bluetooth setup, Python virtual environment.
