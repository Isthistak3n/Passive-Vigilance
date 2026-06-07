# Multi-day soak plan (2026-06)

**Status:** Draft. The validation gate for the working prototype, per
[roadmap-fixed-node-prototype.md](roadmap-fixed-node-prototype.md). Run it after
P0 (done) and the P1/P2 stack, before declaring the prototype validated.

## Goal

Demonstrate **learn-then-detect, end to end, on real hardware**: chase learns
its location's RF pattern of life, freezes, and then surfaces genuine anomalies
across several days at a false-positive rate the operator can live with — while
staying up and bounded in memory and disk for the whole run. This is an
endurance-and-correctness test, not a usability one.

## The dependency that shapes the schedule

The P1 approaching walk-test and the P2 egregious check can only be exercised by
the **P1+P2 code actually running on chase** — and P1's approaching trigger
needs a *frozen* baseline with real per-device RSSI banked under that code. So
the soak, the walk-test, and the real learning window are not three separate
runs; they are one window run under the stack:

1. Deploy the stack to chase (check out `feat/egregious-during-baseline`, which
   contains P0+P1+P2, and restart the service). This runs unmerged validation
   code on the test node — acceptable for a prototype validation run; the PRs
   still merge only after the walk-test passes.
2. Wipe the baseline so a fresh window starts under the stack.
3. Learn → freeze → observe, as below.

The PRs stay held (per decision) until the walk-test inside this soak passes;
then merge the stack and, if desired, re-run on the merged `main` for the record.

## Configuration

| Knob | Soak value | Why |
|---|---|---|
| Code on chase | `feat/egregious-during-baseline` (P0+P1+P2) | Exercises the whole prototype, not just P0 |
| `NODE_MODE` | `fixed` | Required; absence crash-loops the service |
| `FIXED_BASELINE_HOURS` | `48` | Freeze lands *inside* the soak; 48h clears the 12-distinct-hour off-schedule guard with margin (real deployment would use 72h) |
| `BASELINE_DB_PATH` | fresh / wiped | A clean window banking RSSI from minute one |
| `EGREGIOUS_SIGNAL_DBM` | `-45` (default) | The P2 knob; tune here if the deliberately-close test floods or misses |
| `APPROACHING_*` | defaults | The P1 knobs; tune only if the walk-test mis-fires |
| SDR / DroneRF | as deployed | Note stability with the SDR path's behaviour; a disabled SDR path is the simplest first soak |

## Timeline (~4 days)

- **Hours 0–48 — learning.** Watch P2: a deliberately-close device of yours
  should flag during this window; ordinary nearby traffic should NOT flood. This
  is where `EGREGIOUS_SIGNAL_DBM` gets calibrated. Confirm RSSI and hour-of-day
  are banking (the gap that sank the last baseline).
- **~Hour 48 — freeze.** Verify a clean freeze: `is_learning` false, a healthy
  fraction of profiles carry non-null `signal_mean`/`signal_var` and ≥12 distinct
  baseline hours.
- **Hours 48–96 — post-freeze.** Run the P1 **walk-test**: carry a known device
  toward the node; the approaching signal must trip while stationary devices stay
  quiet. Then let it observe for the rest of the window.

## What to measure, and the pass bar

| Phase | Measure | Pass |
|---|---|---|
| P0 | Process RSS sampled across the freeze boundary and over days | Flat across freeze; no unbounded growth |
| P0 | `observations` row count / disk vs the retention sweep | Bounded; disk within budget |
| P1 | Walk-toward a known device | Approaching fires on the real approach |
| P1 | Ambient stationary devices post-freeze | Approaching false-positive rate low |
| P2 | Deliberately-close device during learning | Flags during baseline, without flooding |
| Overall | Post-freeze novelty/off-schedule anomaly + FP rate | A sane anomaly stream with tolerable FP |
| Overall | Uptime / counter advancement over days | Stays up; sensor counters keep advancing (trust counters, not the ✓ health flags) |

## Safety / isolation

This soak **is** the live run on chase, so the usual "operate on copies" rule is
inverted — it is the production-shaped exercise. Guard rails: confirm
`NODE_MODE=fixed` is set before restart (or the service crash-loops); keep the
retired baseline backup; and read counter advancement, not the health-banner
flags, to judge liveness — the node has silently stalled green before.

## Open decisions for the operator

1. **When to start.** Restarting chase onto the stack now sacrifices only the few
   hours of learning done since today's clean restart, and gets the soak +
   walk-test moving sooner under the full prototype. Recommended.
2. **Baseline length.** 48h (freeze inside a 4-day soak) vs 72h (matches real
   deployment but needs a longer total run for meaningful post-freeze time).
3. **SDR path.** Run the first soak with the DroneRF/SDR path disabled for a
   cleaner endurance read, or include it to test the time-share under load.
