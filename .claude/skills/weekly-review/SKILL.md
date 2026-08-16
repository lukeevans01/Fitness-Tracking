---
name: weekly-review
description: Review Luke's last week of training and calibrate the coming week. Use when Luke wants his plan adjusted rather than a question answered: "review my week", "recalibrate next week", "I've been overtraining, sort next week out", "what should next week look like?", or when he mentions his volume or anchor drifting. Acts as an expert in marathon running, strength training and nutrition, assesses under/overtraining from real data, and can write the chosen week into his plan through the safety guardrails.
---

# Weekly review and calibration

Reviews what Luke actually trained, proposes three options for the coming week, and can
write the chosen one into his plan. This is the plan-changing counterpart to the
`coach` skill, which is advice only.

## When to use

Use this when Luke wants the **plan changed** on the basis of how last week went:

- "Review my week and sort out next week"
- "I've been undertraining, push next week up"
- "Recalibrate my volume, I've been ill"
- "What should next week look like?"

Do **not** use it for a single question ("is 5:20/km too quick for easy runs?"). That is
the `coach` skill. Do not use it to change one specific day; that is the daily-email reply
flow or the dashboard plan editor.

## Critical: do not reinvent the coaching

The expertise already exists in the repo and is injected into the prompt automatically:

- `specialists/running.py`: pace and HR zones, the 80/20 distribution, Luke's documented
  failure mode of drifting into the moderate grey zone
- `specialists/lifting.py`: RIR defaults, benchmarks, progression
- `specialists/nutrition.py`: targets and practical constraints
- `plan_guardrails.py`: the deterministic safety envelope
- `progression.py`: block phasing and the self-calibrating volume anchor

**Never hand-write a training week into the store yourself, and never quote paces, weights
or volumes you invented.** Run the CLI and relay what it produced. If Luke wants the
underlying expertise changed, edit the specialist module so the automated Sunday review
picks the change up too, otherwise the two paths drift apart.

## How to run it

From the `fitness-emails` directory:

```bash
python3 review_week.py
```

That reviews last week, prints the coach's three options with each one's structured week
already checked against the guardrails, and writes nothing.

To put a week into the plan once Luke has chosen:

```bash
python3 review_week.py --apply B
```

Other flags:

- `--recalibrate-anchor` also persists the recalculated volume anchor. The Sunday job does
  this automatically, so only pass it if Luke wants the anchor moved now.
- `--week-start YYYY-MM-DD` plans a week other than the next one.
- `--json` prints the raw coach response, for debugging.

## Requirements

- `GEMINI_API_KEY` must be set. Without it the CLI prints the error and exits non-zero, so
  relay that rather than inventing a plan.
- Run from the repo root so imports resolve.

## Reading the output

Each option shows its label, rationale, weekly running total and a day-by-day list. Watch
for two markers:

- `adjusted: ...` means the guardrails clamped something (usually a volume jump or a long run
  taking too large a share of the week). The option is still applicable; tell Luke what was
  trimmed and why.
- `REJECTED by guardrails` means the option cannot be applied. Report the reason. Do not try to
  patch it by hand; if all three are rejected, something is wrong with the proposal and it
  is worth re-running.

An option may also have no structured plan, in which case choosing it simply leaves the
standard cycle in place.

## After applying

The CLI writes per-date overrides into `data/app.db`. Those are picked up by the daily
email and the dashboard, but only once committed:

```bash
git add data/app.db && git commit -m "plan: weekly review applied"
```

Ask Luke before committing and pushing. Confirm what was written and what the guardrails
changed, and do not claim a week is live until the commit is pushed.

## Guardrails you must not work around

`plan_guardrails.py` enforces these deterministically, and they exist because the daily
sender has no LLM available to sanity-check anything:

- at least one rest day per week
- a long run cannot exceed 60 percent of the week's running, nor 32 km
- weekly running cannot jump more than 20 percent over last week's actual
- load cannot rise inside the four-week taper
- clamping only ever reduces load, never scales a light week up

If Luke wants one of these limits changed, that is a deliberate edit to
`plan_guardrails.py` with a reason, not something to bypass for one week.
