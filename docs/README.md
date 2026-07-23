# Documentation

Start here. This folder holds the full documentation for Passive Vigilance;
the table below is the map.

## If you want to…

| Your goal | Read this |
|-----------|-----------|
| **Install and run a node** | [setup.md](setup.md) — full install, configuration reference, and troubleshooting |
| **Understand the whole design and what has shipped** | [design-and-roadmap.md](design-and-roadmap.md) — the two threat models, fixed vs. mobile scoring, the phased roadmap, and soak validation |
| **Find your way around the code** | [architecture.md](architecture.md) — the source-tree map and runtime shape |
| **Set up the fixed + mobile "recon pair"** | [design-recon-pair.md](design-recon-pair.md) — how a fixed base node tasks a roaming node to find where a device beds down |
| **Understand the aircraft-of-interest work** | [design-aircraft-of-interest.md](design-aircraft-of-interest.md) — scoring the air picture and hooking in ACARS |
| **Swap in the RTL8812AU WiFi adapter** | [wifi-driver-8812au.md](wifi-driver-8812au.md) — the driver swap and its trade-offs |

## Reference

- **[architecture.md](architecture.md)** — source-tree map, module responsibilities, runtime shape.
- **[setup.md](setup.md)** — the operator's manual: install, per-sensor bring-up, the full `.env` configuration table, and troubleshooting.

## Design

- **[design-and-roadmap.md](design-and-roadmap.md)** — the consolidated design and phased roadmap (P0–P7), including the multi-day soak validation.
- **[design-recon-pair.md](design-recon-pair.md)** — the multi-node reconnaissance-pair design and its field-test plan.
- **[design-aircraft-of-interest.md](design-aircraft-of-interest.md)** — persistence scoring for the air picture.

## History

Archival records of how the system was built and hardened — useful for context,
not needed to run a node. See [history/](history/):

- **[history/retrospective-2026-07.md](history/retrospective-2026-07.md)** — the project journey, April → July 2026, narrated.
- **[history/field-findings-2026-06.md](history/field-findings-2026-06.md)** — fixed-node field-test findings and memory-soak numbers.
- **[history/field-test-2026-06.md](history/field-test-2026-06.md)** — the June 2026 field test and soak report.
- **[history/rollup-investigation.md](history/rollup-investigation.md)** — the sighting-rollup investigation that led to the bounded state table.
