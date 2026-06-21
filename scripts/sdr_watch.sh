#!/usr/bin/env bash
# sdr_watch.sh — periodic watch for a wedged RTL-SDR during the time-share cycle.
#
# The single-dongle SDR cycle hands the radio between readsb (ADS-B, 1090 MHz) and the
# AIS/ACARS decoders (VHF). The known failure (2026-06-21) is readsb crash-looping
# "SDR wedged" on a handoff and ADS-B flatlining. This watch flags that early:
#
#   * readsb crash-looping        — its systemd NRestarts counter climbing
#   * readsb wedge/claim errors   — its journal in the last interval
#   * PV-side handoff failures     — passive-vigilance journal
#   * ADS-B recovery              — aircraft count (0 is normal *during* a VHF slice;
#                                    sustained 0 across many checks while readsb is
#                                    "active" is the wedge tell)
#   * PV memory                   — RSS, to catch a leak over a long soak
#
# Usage:  ./scripts/sdr_watch.sh [interval_seconds]   (default 60)
# Logs to $SDR_WATCH_LOG (default /tmp/sdr_watch.log) and stdout. Ctrl-C to stop.

INTERVAL="${1:-60}"
LOG="${SDR_WATCH_LOG:-/tmp/sdr_watch.log}"
READSB_URL="${READSB_URL:-http://localhost/tar1090/data/aircraft.json}"

prev_restarts="$(systemctl show -p NRestarts --value readsb 2>/dev/null || echo 0)"
zero_streak=0

echo "# sdr_watch started $(date -u +%FT%TZ) interval=${INTERVAL}s log=$LOG" | tee -a "$LOG"

while true; do
    ts="$(date -u +%H:%M:%S)"
    warns=""

    # readsb crash-loop: NRestarts climbing between checks is the clearest wedge tell.
    restarts="$(systemctl show -p NRestarts --value readsb 2>/dev/null || echo 0)"
    if [ "$restarts" -gt "$prev_restarts" ] 2>/dev/null; then
        warns="$warns readsb-restarted(+$((restarts - prev_restarts)))"
    fi
    prev_restarts="$restarts"

    # Wedge / claim signatures in the last interval of logs.
    if journalctl -u readsb --since "${INTERVAL}s ago" --no-pager 2>/dev/null \
        | grep -qiE "wedged|couldn't claim|usb_claim|reattach|No supported devices"; then
        warns="$warns readsb-wedge-log"
    fi
    if journalctl -u passive-vigilance --since "${INTERVAL}s ago" --no-pager 2>/dev/null \
        | grep -qiE "SDR handoff .* failed|couldn't claim|exitcode=0|marking unhealthy"; then
        warns="$warns pv-handoff-fail"
    fi

    rstate="$(systemctl is-active readsb 2>/dev/null)"
    ac="$(curl -s --max-time 3 "$READSB_URL" 2>/dev/null \
        | python3 -c "import sys,json;print(len(json.load(sys.stdin).get('aircraft',[])))" 2>/dev/null || echo '?')"

    # Sustained 0 aircraft while readsb is "active" across many checks = not recovering.
    if [ "$rstate" = "active" ] && [ "$ac" = "0" ]; then
        zero_streak=$((zero_streak + 1))
        [ "$zero_streak" -ge 5 ] && warns="$warns adsb-not-recovering(${zero_streak}x)"
    else
        zero_streak=0
    fi

    pid="$(systemctl show -p MainPID --value passive-vigilance 2>/dev/null)"
    rss="$(awk '/VmRSS/{print int($2/1024)}' "/proc/$pid/status" 2>/dev/null || echo '?')"
    owner="$(journalctl -u passive-vigilance --since "180s ago" --no-pager 2>/dev/null \
        | grep -oiE "handing off to [a-z_]+" | tail -1)"

    status="OK"; [ -n "$warns" ] && status="WARN:$warns"
    printf '%s readsb=%s aircraft=%s rss=%sMB restarts=%s [%s] %s\n' \
        "$ts" "$rstate" "$ac" "$rss" "$restarts" "${owner:-no-handoff-seen}" "$status" | tee -a "$LOG"

    sleep "$INTERVAL"
done
