---
name: coach
description: Ask Luke's fitness coach for on-demand lifting or running advice. Use when Luke asks a training question (e.g. "should I deload bench this week?", "is 5:20/km too quick for easy runs?", "swap a routine for more pulling"). Returns coached advice grounded in his profile, benchmarks, recent training, weekly load, and his routine template library. Advice only — it does not change his plan.
---

# Fitness coach (on-demand advice)

This skill answers Luke's training questions using the same coaching brain that powers
the daily-email feedback flow, but on demand and without changing his plan.

## When to use

Invoke this whenever Luke asks a lifting, programming, or running question and wants an
answer — not a plan change. Examples:

- "Should I deload bench this week given my sleep?"
- "My RDL felt heavy on Tuesday, drop the weight?"
- "Is 5:20/km too quick for my easy runs?"
- "Which of my routines should I do tomorrow if I trained legs yesterday?"

If Luke instead wants tomorrow's session *changed*, that is NOT this skill — tell him to
reply to a daily email (the override flow), or edit the override directly.

## How to run it

The coach is a CLI in the project root. Run it from the `fitness-emails` directory:

```bash
python3 ask_coach.py "<Luke's question>"
```

Pick the domain so the right specialist context loads (default is strength):

```bash
python3 ask_coach.py --domain strength "should I deload bench this week?"
python3 ask_coach.py --domain run "is 5:20/km too quick for easy runs?"
```

Choose `--domain run` when the question is about running/pace/HR/mileage; otherwise use
`strength` (the default). Pass `--no-routines` only if Luke explicitly wants advice that
ignores his documented routine templates.

## Requirements

- `GEMINI_API_KEY` must be set in the environment (the coach calls Gemini). If it is not
  set, the CLI prints an error to stderr and exits non-zero — relay that to Luke rather
  than inventing an answer.
- Run from the repo root so the script's imports resolve.

## What it does under the hood

`ask_coach.py` calls `coach_orchestrator.answer_training_question()`, which assembles:
Luke's profile, the matching specialist (`specialists/lifting.py` or `running.py`), the
active taper block if applicable, his recent 14-day training summary, the deterministic
weekly load, and — for strength — the routine template library (`routine_library.py`).
It returns prose advice and writes no override.

## After running

Relay the coach's answer to Luke verbatim or lightly summarised. Do not fabricate weights,
paces, or sets the coach did not give. If he then wants the advice turned into a plan
change, point him to the daily-email reply flow.
