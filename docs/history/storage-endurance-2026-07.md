# Storage Endurance Note — SD Card at Current Volume, July 2026

**Purpose:** record what an ~85-hour continuous fixed-node run showed about the
entities database and its write path on SD storage, as a data point for the
durable-storage decision in #211. Configuration and hardware facts live in
`CLAUDE.md` / `CONTEXT.md` and are cross-referenced rather than repeated. Relates
to #211.

---

## What the run showed

Over roughly 85 hours of continuous operation at the current device density, the
entities database and its write-ahead log stayed well inside a safe envelope:

| Signal | Observed | Reading |
|---|---|---|
| Database size | grew ~705 MB → ~831 MB over the run | Steady, expected growth |
| Write-ahead log | stayed in the 0–6 MB band every health cycle | No persist backlog building |
| Persist path | no watchdog crash-loop, no failed units | Off-loop writer kept up |
| Root filesystem | ~34% used | Ample headroom |

The key observation is the write-ahead log. In the earlier full-disk incident it
was the WAL ballooning (tens of GB) that silently starved the node; here it drains
back down on every cycle and never accumulates. At present poll volume the
off-loop writer comfortably keeps pace with ingest.

## What this does — and does not — mean for #211

This is reassuring but **not** a resolution. The finding is bounded to the
*current* device density. #211 is fundamentally about what happens as poll volume
climbs: the off-loop writer's headroom shrinks as the retained population grows,
and the SD card's sustained write behaviour is the real ceiling. This run
demonstrates the safe envelope exists today; it does not raise the ceiling.

The durable fix remains migrating the entities store to USB-SSD, which removes the
SD-card write ceiling rather than staying under it. #211 should stay open until
that migration lands. Until then, the WAL-size health signal is the right early
warning to keep watching — a WAL that stops draining is the leading indicator that
ingest has outrun the writer.
