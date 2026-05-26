---
schema_version: 1
last_updated: 2026-05-25
---

# Adaptation state

## Mode

```
mode: normal
```

Valid values: `normal` | `survival` | `paused`

- **normal** — daily emails active, full feedback loop running
- **survival** — training paused (baby, illness, travel, etc.). Daily emails stop. Weekly check-in continues. On resume, training picks up toward the same goal.
- **paused** — all emails stopped manually

### Survival mode log

| Started | Ended | Reason |
|---|---|---|
| — | — | — |

## Current training phase

```
phase: maintenance
cycle_start_date: 2026-05-25
cycle_length_days: 10
```

Phase becomes `marathon_build` when Phase 3 starts (~2026-08-04).

## Weekly load counters (current week)

Reset each Monday. Gemini uses these to flag overload.

```
week_start: 2026-05-25
strength_sessions: 0
run_sessions: 0
run_km_total: 0.0
squash_sessions: 0
```

## Taper

```
taper_active: false
taper_start_date: null
```

Taper triggers ~4 weeks before race day (rule-based, not LLM). Once active, Gemini prompt receives a taper flag and volume/intensity caps.

## Goal

```
goal_race: San Sebastián marathon
goal_date: 2026-11-22
goal_time: 3:25:00
goal_pace_per_km: 4:51
```
