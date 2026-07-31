#!/usr/bin/env bash
#
# sdr-wedge-watch.sh — auto-recover the RTL-SDR (0bda:2838, NESDR SMArt v5)
# when readsb enters the "SDR wedged" crash loop.
#
# WHY: the dongle occasionally wedges at the USB level. readsb then dies ~16s
# after every start with "SDR wedged, exiting!" and systemd restart-loops it
# forever (22+ restarts observed 2026-07-03) — aircraft detection is silently
# down until someone notices. A plain service restart never clears it; only a
# USB-level reset (`usbreset 0bda:2838`) does. This watchdog closes that gap.
#
# TRIGGER: >= 2 "SDR wedged" lines in the readsb journal within the last 3
# minutes. One line could be a transient (systemd's restart may ride through);
# two means the restart already failed to clear it, i.e. a genuine loop. Clean
# coordinator slice stops (systemctl stop by the SDR time-share) never log this
# line, so normal ADS-B/AIS handoffs can never false-trigger a reset.
#
# COOLDOWN: 10 min between resets, so a reset that doesn't take can't turn
# into a reset storm (and stale journal lines inside the 3-min window right
# after a reset can't double-fire).
#
# Runs as root every 2 min from /etc/cron.d/sdr-wedge-watch (installed by
# deploy/install.sh). Logs only when it acts (or errors) so the log stays small.
# Proven over a 3-day fixed-node run (2026-07-27..31): 2-4 auto-recovered
# episodes/day, ADS-B never down >~2 min, and `vcgencmd get_throttled=0x0`
# throughout — ruling out the power supply / heat and isolating the wedge to the
# SDR/USB link itself.
set -uo pipefail

USB_ID="0bda:2838"
HERE="/home/${PI_USER}/sdr-watch"
LOG="$HERE/sdr-wedge-watch.log"
STATE="$HERE/last-reset-epoch"
COOLDOWN_S=600
USBRESET="/usr/bin/usbreset"

log() { echo "$(date -u +%FT%TZ)  $*" >>"$LOG"; }

# 1) look for the wedge signature in the last 3 minutes
count=$(journalctl -u readsb --since '3 min ago' --no-pager 2>/dev/null | grep -c 'SDR wedged')
[ "$count" -ge 2 ] || exit 0

# 2) cooldown guard
now=$(date +%s)
last=$(cat "$STATE" 2>/dev/null || echo 0)
if [ $((now - last)) -lt "$COOLDOWN_S" ]; then
    log "wedge loop detected ($count lines/3min) but within ${COOLDOWN_S}s cooldown — skipping"
    exit 0
fi

# 3) act: USB-level reset (the only thing that clears a wedge)
log "WEDGE LOOP: $count 'SDR wedged' lines in 3 min (readsb restarts: $(systemctl show readsb -p NRestarts --value)) — resetting $USB_ID"
if ! "$USBRESET" "$USB_ID" >>"$LOG" 2>&1; then
    log "ERROR: usbreset failed — dongle may need a power cycle"
    echo "$now" >"$STATE"   # still start cooldown so we don't hammer a dead bus
    exit 1
fi
echo "$now" >"$STATE"

# 4) verify recovery: readsb should start and SURVIVE past the ~16s wedge point.
#    (systemd/the coordinator handles the actual start; we just observe.)
sleep 45
sub=$(systemctl show readsb -p SubState --value 2>/dev/null)
fresh=$(journalctl -u readsb --since '40 seconds ago' --no-pager 2>/dev/null | grep -c 'SDR wedged')
if [ "$sub" = "running" ] && [ "$fresh" -eq 0 ]; then
    log "RECOVERED: readsb running, no new wedge lines after reset"
elif [ "$fresh" -gt 0 ]; then
    log "NOT RECOVERED: wedge lines continue after reset — will retry after cooldown"
else
    log "post-reset state: readsb=$sub, no wedge lines (may be an AIS/ACARS slice — OK)"
fi
