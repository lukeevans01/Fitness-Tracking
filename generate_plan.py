#!/usr/bin/env python3
"""
One-shot script: ask the specialists to design Luke's optimal weekly training plan,
then write the result to plan_template.json.

Run locally with GEMINI_API_KEY set:
    GEMINI_API_KEY=... python3 generate_plan.py
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

import gemini_client
from profile import default_profile
from specialists import lifting, mobility, running

_DAY_SCHEMA = """\
{
  "day_num": integer (1–7),
  "day_label": "Monday" | "Tuesday" | ... | "Sunday",
  "session_type": "descriptive session name",
  "session_kind": "run" | "strength" | "rest",
  "duration_min": integer,
  "warm_up": "string — for strength/run only, omit key for rest",
  "exercises": [
    {"name": "string", "sets_reps": "string", "weight": "string", "rest": "string"}
  ],
  "run_details": {
    "pace": "string", "hr_target": "string",
    "duration": "string", "distance": "string", "effort": "string"
  },
  "details": "string — rest/mobility notes only",
  "extras": "string — optional day-specific notes",
  "short_version": "string — what to do if tired or short on time",
  "purpose": "string — one sentence"
}\
"""

_PROMPT_TEMPLATE = """\
{shared_profile}

--- RUNNING SPECIALIST ---
{running_context}

--- STRENGTH SPECIALIST ---
{lifting_context}

--- MOBILITY SPECIALIST ---
{mobility_context}

---

TASK: Design Luke's optimal 7-day repeating weekly training plan.

Hard constraints:
- Exactly 3 run sessions + 3 full body strength sessions + 1 rest day = 7 days
- Tuesday evenings: squash (treat as intensity — do not schedule a hard run OR a hard strength \
morning on Tuesday; keep Tuesday manageable so squash still happens)
- Day 1 = Monday (cycle start: 2026-05-25)
- Use Luke's actual strength benchmarks for all exercise weights

Optimisation goals (in priority order):
1. Sub-3:25 at San Sebastián 22 Nov 2026 — running volume and quality take precedence
2. Continue strength progress (bench 100 kg trajectory, squat/RDL maintenance)
3. Minimise injury risk given sleep deprivation and new-baby context

Running structure to embed (exactly 3 run sessions):
- One long run per week (Sunday is ideal — 12–16 km, escalates through training block)
- One quality session per week — currently label as "easy + optional MP segment" \
(becomes proper tempo/intervals in the marathon build from Aug 2026)
- One easy run (HR <150, 5:35–6:00/km)
- Do not put a hard run the day before or after the long run

Strength structure to embed (exactly 3 sessions, ALL full body):
- Every strength session is full body — compound movements only, no isolation-only days
- Each session must include: a squat pattern, a hinge pattern, a push, a pull, and core
- Bench press is the priority push movement (100 kg trajectory)
- Vary the primary compound each session (e.g. squat-led / hinge-led / athletic)
- RIR 3 on all working sets
- Place strength days where they don't compromise the key run sessions

Mobility:
- Do not add a standalone mobility day — weave it into warm-ups and short_versions
- Note dynamic mobility before runs, static after runs in the relevant sessions

For each of the 7 days output the full session detail:
- Strength days: specific exercises, sets/reps, weights (kg), rest periods
- Run days: pace zone, HR target, distance, effort description
- Rest day: brief optional mobility note

Output ONLY a JSON array of exactly 7 objects, each matching this schema:
{day_schema}

No surrounding text. No markdown fences. Just the raw JSON array.\
"""


def _build_prompt() -> str:
    return _PROMPT_TEMPLATE.format(
        shared_profile=default_profile().profile_text,
        running_context=running.system_context(),
        lifting_context=lifting.system_context(),
        mobility_context=mobility.system_context(),
        day_schema=_DAY_SCHEMA,
    )


def _validate(days: list) -> None:
    if not isinstance(days, list):
        raise ValueError(f"Expected list, got {type(days).__name__}")
    if len(days) != 7:
        raise ValueError(f"Expected 7 days, got {len(days)}")
    for d in days:
        for key in ("day_num", "day_label", "session_type", "session_kind", "duration_min", "short_version", "purpose"):
            if key not in d:
                raise ValueError(f"Day {d.get('day_num','?')} missing key: {key!r}")
        if d["session_kind"] not in ("run", "strength", "rest"):
            raise ValueError(f"Day {d['day_num']}: invalid session_kind {d['session_kind']!r}")


def main():
    prompt = _build_prompt()

    print("[gemini] Generating optimal weekly plan from specialists...")
    response_text = gemini_client.call_gemini(prompt)

    try:
        days = json.loads(response_text)
    except json.JSONDecodeError as exc:
        sys.exit(f"Gemini returned invalid JSON: {exc}\n\n{response_text[:800]}")

    try:
        _validate(days)
    except ValueError as exc:
        sys.exit(f"Plan validation failed: {exc}")

    plan_path = ROOT / "plan_template.json"
    with open(plan_path) as f:
        plan = json.load(f)

    plan["_comment"] = (
        "Luke's fitness plan. Repeating 7-day training cycle (Day 1 = Monday). "
        "Generated by coach specialists via generate_plan.py. "
        "Edit individual sessions directly or re-run generate_plan.py to regenerate."
    )
    plan["cycle_length_days"] = 7
    plan["cycle_days"] = days

    with open(plan_path, "w") as f:
        json.dump(plan, f, indent=2)
        f.write("\n")

    print("[ok] plan_template.json updated.\n")
    print("Generated plan:")
    for day in days:
        kind = day["session_kind"].upper()
        label = day.get("day_label", f"Day {day['day_num']}")
        name = day["session_type"]
        dur = day["duration_min"]
        print(f"  {label:10} [{kind:8}] {name} ({dur} min)")


if __name__ == "__main__":
    main()
