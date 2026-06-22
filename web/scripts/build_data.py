#!/usr/bin/env python3
"""Build the static JSON the dashboard reads, from the committed training data.

This is the read-side of the UI pipeline: it reuses the existing ingest adapters to
load the real CSVs and emits pre-aggregated JSON (no raw rows shipped to the browser).
A GitHub Action runs this whenever data is committed, then Cloudflare Pages serves the
static site.

Run from the fitness-emails dir:  python3 web/scripts/build_data.py
Outputs (web/public/data/): running.json, lifting.json, nutrition.json, home.json
"""

from __future__ import annotations

import json
import re
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

# web/scripts/ -> web/ -> fitness-emails/
FITNESS_DIR = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(FITNESS_DIR))

from zoneinfo import ZoneInfo  # noqa: E402

import plan_cycle  # noqa: E402
import progression  # noqa: E402
import store  # noqa: E402
from ingest import strava_csv, strong_csv  # noqa: E402

# All "what block am I in" date logic uses the coach's timezone, matching the rest of
# the system (never naive date.today() for progression).
TZ = ZoneInfo("Europe/Amsterdam")

# Pace-zone thresholds (min/km), mirroring training_summary so the dashboard and the
# coach agree on what "easy / moderate / quality" mean.
EASY_MIN_PACE = 6.0       # >= 6:00/km = easy
MODERATE_MIN_PACE = 5.1   # 5:06-5:59/km = moderate; below = quality/MP+

DATA_DIR = FITNESS_DIR / "data"
STRAVA_CSV = DATA_DIR / "strava.csv"
STRONG_CSV = DATA_DIR / "strong.csv"
NUTRITION_LOG_DIR = FITNESS_DIR / "nutrition_log"
PLAN_TEMPLATE = FITNESS_DIR / "plan_template.json"
PROFILE_JSON = FITNESS_DIR / "profiles" / "luke.json"
OUT_DIR = FITNESS_DIR / "web" / "public" / "data"

# Marathon target, mirrored from the coach context so the dashboard narrative and the
# email coach point at the same goal.
MARATHON_DATE = date(2026, 11, 22)   # San Sebastian
MARATHON_GOAL = "sub-3:25"

# Easy-running share we want for marathon base building (the 80/20 idea).
EASY_TARGET_PCT = 70

# Strength: map a normalised exercise name (as emitted by strong_csv) to a short display
# name for the e1RM chart. Strong exports use names like "squat (barbell)" and
# "bench press (barbell)", so we match by substring and exclude accessory variants.
# Pull-ups are excluded from e1RM (added weight only; bodyweight is not in the export, so
# e1RM would be misleading).
KEY_LIFT_ORDER = ["Squat", "Bench", "RDL", "OHP"]


def _key_lift_display(exercise: str) -> str | None:
    name = exercise.lower()
    if "squat" in name and "barbell" in name and "split" not in name and "front" not in name:
        return "Squat"
    if "bench press" in name and "barbell" in name and "incline" not in name and "decline" not in name:
        return "Bench"
    if "romanian deadlift" in name:
        return "RDL"
    if "overhead press" in name and "dumbbell" not in name and "seated" not in name:
        return "OHP"
    return None


# Current working benchmarks and targets (kg), mirrored from the coach profile context.
LIFT_BENCHMARKS = {
    "Squat": {"current": 120, "target": 130},
    "Bench": {"current": 85, "target": 96},
    "RDL": {"current": 108, "target": 120},
    "OHP": {"current": 49, "target": 55},
}
# Reps cap for a trustworthy Epley one-rep-max estimate.
E1RM_MAX_REPS = 12


def _week_start(d: date) -> date:
    """Monday of the ISO week containing d."""
    return d - timedelta(days=d.weekday())


def _zone(pace_min_km: float | None) -> str | None:
    if pace_min_km is None:
        return None
    if pace_min_km >= EASY_MIN_PACE:
        return "easy"
    if pace_min_km >= MODERATE_MIN_PACE:
        return "moderate"
    return "quality"


def _fmt_km(value: float) -> str:
    """Whole number for tidy prose, e.g. 42 km not 42.3 km."""
    return f"{round(value):d}"


def _window_stats(runs: list, start: date, end: date) -> dict:
    """Aggregate runs with start <= date <= end (inclusive)."""
    sel = [r for r in runs if start <= r.date <= end]
    out = {
        "runs": len(sel), "km": 0.0, "longest_km": 0.0,
        "easy_km": 0.0, "moderate_km": 0.0, "quality_km": 0.0, "unzoned_km": 0.0,
        "active_weeks": 0,
    }
    weeks_seen = set()
    for r in sel:
        out["km"] += r.distance_km
        out["longest_km"] = max(out["longest_km"], r.distance_km)
        zone = _zone(r.pace_min_km)
        out[f"{zone}_km" if zone else "unzoned_km"] += r.distance_km
        weeks_seen.add(_week_start(r.date))
    out["active_weeks"] = len(weeks_seen)
    return out


def build_recent(runs: list, weeks: int = 4) -> dict | None:
    """Trailing-`weeks` analysis anchored on the most recent run, plus a grounded,
    rules-based narrative. Anchoring on the last run (not today) keeps the summary
    meaningful even if the Strava export lags by a few days."""
    if not runs:
        return None

    last_run = max(r.date for r in runs)
    anchor_week = _week_start(last_run)
    window_start = anchor_week - timedelta(weeks=weeks - 1)
    window_end = anchor_week + timedelta(days=6)
    prev_start = window_start - timedelta(weeks=weeks)
    prev_end = window_start - timedelta(days=1)

    cur = _window_stats(runs, window_start, window_end)
    prev = _window_stats(runs, prev_start, prev_end)

    classified = cur["easy_km"] + cur["moderate_km"] + cur["quality_km"]
    easy_pct = round(100 * cur["easy_km"] / classified) if classified else None
    moderate_pct = round(100 * cur["moderate_km"] / classified) if classified else None
    quality_pct = round(100 * cur["quality_km"] / classified) if classified else None

    trend_pct = None
    if prev["km"] > 0:
        trend_pct = round(100 * (cur["km"] - prev["km"]) / prev["km"])

    weeks_to_race = max(0, (MARATHON_DATE - last_run).days // 7)

    narrative = _narrative(
        weeks=weeks, cur=cur, easy_pct=easy_pct, moderate_pct=moderate_pct,
        quality_pct=quality_pct, trend_pct=trend_pct, weeks_to_race=weeks_to_race,
    )

    return {
        "weeks": weeks,
        "window_start": window_start.isoformat(),
        "window_end": window_end.isoformat(),
        "runs": cur["runs"],
        "km": round(cur["km"], 1),
        "avg_km_per_week": round(cur["km"] / weeks, 1),
        "longest_km": round(cur["longest_km"], 1),
        "active_weeks": cur["active_weeks"],
        "easy_pct": easy_pct,
        "moderate_pct": moderate_pct,
        "quality_pct": quality_pct,
        "trend_pct": trend_pct,
        "weeks_to_race": weeks_to_race,
        "narrative": narrative,
    }


def _narrative(weeks, cur, easy_pct, moderate_pct, quality_pct,
               trend_pct, weeks_to_race) -> list:
    """Return a list of {heading, text} blocks. British spelling, no em-dashes."""
    recap_bits = []
    if cur["runs"] == 0:
        return [{
            "heading": "What you have been up to",
            "text": (f"No runs are recorded in the last {weeks} weeks. "
                     "Upload your latest Strava export to refresh this summary."),
        }]

    avg_week = cur["km"] / weeks
    recap_bits.append(
        f"Over the last {weeks} weeks you logged {cur['runs']} runs totalling "
        f"{_fmt_km(cur['km'])} km, averaging {_fmt_km(avg_week)} km per week "
        f"across {cur['active_weeks']} of {weeks} weeks."
    )
    if trend_pct is not None:
        if trend_pct >= 8:
            recap_bits.append(f"That is up {trend_pct}% on the previous {weeks} weeks.")
        elif trend_pct <= -8:
            recap_bits.append(
                f"That is down {abs(trend_pct)}% on the previous {weeks} weeks.")
        else:
            recap_bits.append("Volume is roughly level with the previous block.")
    if cur["longest_km"] > 0:
        recap_bits.append(f"Your longest run was {_fmt_km(cur['longest_km'])} km.")
    if easy_pct is not None:
        recap_bits.append(
            f"By pace, your classified running split {easy_pct}% easy, "
            f"{moderate_pct}% moderate and {quality_pct}% quality.")

    push_bits = []
    # 1) Zone balance: the core 80/20 signal.
    if easy_pct is not None and easy_pct < EASY_TARGET_PCT:
        push_bits.append(
            f"Most of your volume sits in the moderate zone. Aim to slow your easy "
            f"runs until at least {EASY_TARGET_PCT}% of weekly km is genuinely easy "
            f"(6:00/km or slower). Easy running you are currently at {easy_pct}%, so "
            "the grey-zone moderate work is the first thing to trim.")
    elif easy_pct is not None:
        push_bits.append(
            f"Your easy share ({easy_pct}%) is in a good place for base building. "
            "Hold that discipline as volume rises.")

    # 2) Quality work toward the time goal.
    if quality_pct is not None and quality_pct < 8:
        push_bits.append(
            "You are doing very little quality work. One structured session a week, "
            "a tempo or intervals, would build the speed you need to hold "
            f"{MARATHON_GOAL} pace.")

    # 3) Consistency.
    if cur["active_weeks"] < weeks:
        push_bits.append(
            f"You ran in {cur['active_weeks']} of the last {weeks} weeks. Consistent "
            "weekly frequency matters more than any single big session, so protect a "
            "minimum run count even on busy weeks.")

    # 4) Long run progression toward the marathon.
    if weeks_to_race > 0:
        if cur["longest_km"] < 25:
            push_bits.append(
                f"With about {weeks_to_race} weeks to the marathon, your long run has "
                "room to grow. Extend it gradually, by no more than 1 to 2 km a week.")
        else:
            push_bits.append(
                f"About {weeks_to_race} weeks to race day. Keep the long run "
                "progressing and start rehearsing marathon-pace segments within it.")

    return [
        {"heading": "What you have been up to", "text": " ".join(recap_bits)},
        {"heading": "Where to push next", "text": " ".join(push_bits)},
    ]


def build_efficiency(runs: list) -> list:
    """Monthly aerobic efficiency factor (EF) = speed (m/min) / average HR.

    EF rises as the aerobic base improves: the runner covers more ground per heartbeat.
    We restrict to aerobic runs (easy + moderate, i.e. pace >= MODERATE_MIN_PACE) with a
    sane HR, so interval speed does not inflate the trend, and aggregate by month to smooth
    run-to-run noise. Only months with at least two qualifying runs are emitted.
    """
    monthly: dict[str, dict] = defaultdict(
        lambda: {"ef_sum": 0.0, "pace_sum": 0.0, "hr_sum": 0.0, "runs": 0}
    )
    for r in runs:
        if not r.avg_hr or r.avg_hr <= 100:
            continue
        if not r.moving_s or r.moving_s <= 0:
            continue
        if not r.pace_min_km or r.pace_min_km < MODERATE_MIN_PACE:
            continue  # drop quality/interval runs so EF reflects the aerobic base
        speed_m_per_min = (r.distance_km * 1000.0) / (r.moving_s / 60.0)
        ef = speed_m_per_min / r.avg_hr
        key = f"{r.date.year:04d}-{r.date.month:02d}"
        m = monthly[key]
        m["ef_sum"] += ef
        m["pace_sum"] += r.pace_min_km
        m["hr_sum"] += r.avg_hr
        m["runs"] += 1

    out = []
    for key, m in sorted(monthly.items()):
        if m["runs"] < 2:
            continue
        out.append({
            "month": key,
            "ef": round(m["ef_sum"] / m["runs"], 3),
            "avg_pace": round(m["pace_sum"] / m["runs"], 2),
            "avg_hr": round(m["hr_sum"] / m["runs"]),
            "runs": m["runs"],
        })
    return out


def build_zone_trend(weekly: list, window: int = 4) -> list:
    """Rolling `window`-week share of easy / moderate / quality running, as percentages
    that sum to 100. Smoothing over 4 weeks turns the noisy per-week split into a
    readable discipline trend against the 80/20 easy target. Weeks with no classified
    running in the trailing window are skipped."""
    out = []
    for i in range(len(weekly)):
        chunk = weekly[max(0, i - window + 1): i + 1]
        easy = sum(w["easy_km"] for w in chunk)
        mod = sum(w["moderate_km"] for w in chunk)
        qual = sum(w["quality_km"] for w in chunk)
        classified = easy + mod + qual
        if classified <= 0:
            continue
        easy_pct = round(100 * easy / classified)
        mod_pct = round(100 * mod / classified)
        out.append({
            "week_start": weekly[i]["week_start"],
            "easy_pct": easy_pct,
            "moderate_pct": mod_pct,
            "quality_pct": max(0, 100 - easy_pct - mod_pct),
        })
    return out


def build_running() -> dict:
    runs = [a for a in strava_csv.read_activities(STRAVA_CSV)
            if a.kind == "run" and a.distance_km > 0]

    weekly: dict[date, dict] = defaultdict(
        lambda: {"km": 0.0, "runs": 0, "easy_km": 0.0,
                 "moderate_km": 0.0, "quality_km": 0.0, "unzoned_km": 0.0}
    )
    yearly: dict[int, dict] = defaultdict(lambda: {"km": 0.0, "runs": 0})

    runs_with_hr = 0
    total_km = 0.0
    for r in runs:
        wk = _week_start(r.date)
        bucket = weekly[wk]
        bucket["km"] += r.distance_km
        bucket["runs"] += 1
        zone = _zone(r.pace_min_km)
        bucket[f"{zone}_km" if zone else "unzoned_km"] += r.distance_km

        y = yearly[r.date.year]
        y["km"] += r.distance_km
        y["runs"] += 1

        total_km += r.distance_km
        if r.avg_hr:
            runs_with_hr += 1

    weekly_out = [
        {
            "week_start": wk.isoformat(),
            "km": round(b["km"], 1),
            "runs": b["runs"],
            "easy_km": round(b["easy_km"], 1),
            "moderate_km": round(b["moderate_km"], 1),
            "quality_km": round(b["quality_km"], 1),
            "unzoned_km": round(b["unzoned_km"], 1),
        }
        for wk, b in sorted(weekly.items())
    ]
    yearly_out = [
        {"year": y, "km": round(b["km"], 1), "runs": b["runs"]}
        for y, b in sorted(yearly.items())
    ]

    recent = build_recent(runs, weeks=4)
    return {
        "generated_at": date.today().isoformat(),
        "recent": recent,
        "efficiency": build_efficiency(runs),
        "weekly": weekly_out,
        "zone_trend": build_zone_trend(weekly_out),
        "easy_target_pct": EASY_TARGET_PCT,
        "yearly": yearly_out,
        "summary": {
            "total_runs": len(runs),
            "total_km": round(total_km, 1),
            "runs_with_hr": runs_with_hr,
            "first_run": runs[-1].date.isoformat() if runs else None,
            "last_run": max((r.date for r in runs), default=None) and
                        max(r.date for r in runs).isoformat(),
        },
        "headline": _running_headline(recent),
    }


def _running_headline(recent: dict | None) -> dict:
    if not recent or recent["runs"] == 0:
        return {"kpis": [], "narrative": "No recent runs logged."}
    kpis = [
        {"value": f"{round(recent['avg_km_per_week'])} km", "label": "per week"},
        {"value": f"{recent['easy_pct']}%", "label": "easy pace"},
        {"value": recent["weeks_to_race"], "label": "weeks to race"},
    ]
    if recent["easy_pct"] is not None and recent["easy_pct"] < EASY_TARGET_PCT:
        push = (f"Only {recent['easy_pct']}% of your running is genuinely easy, so the "
                "priority is slowing easy days out of the moderate grey zone.")
    else:
        push = "Easy/quality balance looks healthy; keep building volume."
    narrative = (f"{recent['runs']} runs and {_fmt_km(recent['km'])} km over the last "
                 f"{recent['weeks']} weeks. " + push)
    return {"kpis": kpis, "narrative": narrative}


# --------------------------------------------------------------------------- lifting

def _e1rm(weight: float, reps: int) -> float | None:
    """Epley estimated one-rep max. None if the set is not a usable strength effort."""
    if weight <= 0 or reps < 1 or reps > E1RM_MAX_REPS:
        return None
    return weight * (1 + reps / 30.0)


def build_lifting() -> dict:
    try:
        lifts = strong_csv.read_lifts(STRONG_CSV)
    except FileNotFoundError:
        return {"generated_at": date.today().isoformat(), "empty": True,
                "headline": {"kpis": [], "narrative": "No strength data found."}}

    # Monthly best e1RM per key lift, plus a union of months for the chart.
    months: set[str] = set()
    best: dict[str, dict[str, float]] = defaultdict(dict)  # display -> {month: e1rm}
    # Monthly tonnage (kg lifted) and session dates across all exercises.
    monthly_tonnage: dict[str, float] = defaultdict(float)
    session_dates: set[date] = set()
    all_dates: list[date] = []

    for ls in lifts:
        session_dates.add(ls.date)
        all_dates.append(ls.date)
        month = f"{ls.date.year:04d}-{ls.date.month:02d}"
        monthly_tonnage[month] += max(ls.weight_kg, 0) * max(ls.reps, 0)
        display = _key_lift_display(ls.exercise)
        if not display:
            continue
        est = _e1rm(ls.weight_kg, ls.reps)
        if est is None:
            continue
        months.add(month)
        cur = best[display].get(month)
        if cur is None or est > cur:
            best[display][month] = est

    sorted_months = sorted(months)
    e1rm_series = []
    for month in sorted_months:
        row = {"month": month}
        for display in KEY_LIFT_ORDER:
            val = best.get(display, {}).get(month)
            row[display] = round(val, 1) if val is not None else None
        e1rm_series.append(row)

    tonnage_series = [
        {"month": m, "tonnes": round(monthly_tonnage[m] / 1000.0, 1)}
        for m in sorted(monthly_tonnage)
    ]

    # Current best e1RM per lift over the trailing 90 days (so a recent value exists).
    last_lift = max(all_dates) if all_dates else None
    current_e1rm = {}
    if last_lift:
        cutoff = last_lift - timedelta(days=90)
        for ls in lifts:
            if ls.date < cutoff:
                continue
            display = _key_lift_display(ls.exercise)
            if not display:
                continue
            est = _e1rm(ls.weight_kg, ls.reps)
            if est is None:
                continue
            if display not in current_e1rm or est > current_e1rm[display]:
                current_e1rm[display] = round(est, 1)

    # Sessions in the trailing 4 weeks.
    sessions_4w = 0
    if last_lift:
        win = last_lift - timedelta(weeks=4)
        sessions_4w = len({d for d in session_dates if d > win})

    return {
        "generated_at": date.today().isoformat(),
        "empty": False,
        "e1rm": e1rm_series,
        "tonnage": tonnage_series,
        "benchmarks": LIFT_BENCHMARKS,
        "current_e1rm": current_e1rm,
        "summary": {
            "total_sessions": len(session_dates),
            "first_session": min(all_dates).isoformat() if all_dates else None,
            "last_session": last_lift.isoformat() if last_lift else None,
            "sessions_4w": sessions_4w,
        },
        "recap": _lifting_recap(current_e1rm, sessions_4w, len(session_dates),
                                tonnage_series),
        "headline": _lifting_headline(current_e1rm, sessions_4w),
    }


def _lifting_recap(current_e1rm: dict, sessions_4w: int, total_sessions: int,
                   tonnage_series: list) -> list:
    """Grounded, rules-based strength summary as {heading, text} blocks."""
    recap = [
        f"You have logged {sessions_4w} strength sessions in the last 4 weeks, "
        f"and {total_sessions} sessions all-time."
    ]
    bests = [f"{lift} {round(current_e1rm[lift])} kg"
             for lift in KEY_LIFT_ORDER if lift in current_e1rm]
    if bests:
        recap.append("Your current best estimated one-rep maxes are "
                     + ", ".join(bests) + ".")
    if len(tonnage_series) >= 2:
        last_t = tonnage_series[-1]["tonnes"]
        prev_t = tonnage_series[-2]["tonnes"]
        if prev_t > 0:
            diff = round(100 * (last_t - prev_t) / prev_t)
            if diff >= 8:
                recap.append(f"Training volume rose about {diff}% last month, to "
                             f"{last_t} tonnes lifted.")
            elif diff <= -8:
                recap.append(f"Training volume eased about {abs(diff)}% last month, to "
                             f"{last_t} tonnes lifted.")
            else:
                recap.append(f"Training volume held steady at about {last_t} tonnes "
                             "lifted last month.")

    push = []
    gaps, at_target = [], []
    for lift, marks in LIFT_BENCHMARKS.items():
        cur = current_e1rm.get(lift)
        if cur is None:
            continue
        if marks["target"] > cur:
            gaps.append((marks["target"] - cur, lift, cur, marks["target"]))
        else:
            at_target.append(lift)
    if gaps:
        gaps.sort(reverse=True)
        _, lift, cur, target = gaps[0]
        push.append(f"Your {lift} is the biggest gap to target, about {round(cur)} kg "
                    f"against {target} kg, so give it priority in the next block.")
    if at_target:
        if len(at_target) == 1:
            push.append(f"Your {at_target[0]} is already at or above its target, so "
                        "hold it there.")
        else:
            push.append(", ".join(at_target) + " are already at or above target, so "
                        "maintain them.")
    if sessions_4w < 8:
        push.append("Aim for two to three quality strength sessions a week to keep "
                    "progressing alongside the marathon training.")
    if not push:
        push.append("Keep the key lifts progressing with small, steady load increases.")

    return [
        {"heading": "What you have been up to", "text": " ".join(recap)},
        {"heading": "Where to push next", "text": " ".join(push)},
    ]


def _lifting_headline(current_e1rm: dict, sessions_4w: int) -> dict:
    kpis = [{"value": sessions_4w, "label": "sessions / 4wk"}]
    for lift in ["Squat", "Bench"]:
        if lift in current_e1rm:
            kpis.append({"value": f"{round(current_e1rm[lift])} kg",
                         "label": f"{lift} e1RM"})
    # Pick the lift with the largest gap to target as the headline focus.
    push = "Keep the key lifts progressing."
    gaps = []
    for lift, marks in LIFT_BENCHMARKS.items():
        cur = current_e1rm.get(lift)
        if cur is not None and marks["target"] > cur:
            gaps.append((marks["target"] - cur, lift, cur, marks["target"]))
    if gaps:
        gaps.sort(reverse=True)
        _, lift, cur, target = gaps[0]
        push = (f"Your {lift} e1RM is about {round(cur)} kg against a {target} kg target, "
                f"the biggest gap right now, so prioritise it.")
    narrative = f"{sessions_4w} strength sessions in the last 4 weeks. " + push
    return {"kpis": kpis, "narrative": narrative}


# ------------------------------------------------------------------------- nutrition

_TOTAL_RE = {
    "kcal": re.compile(r"Calories:\*\*\s*([\d.]+)\s*/\s*([\d.]+)", re.IGNORECASE),
    "protein_g": re.compile(r"Protein:\*\*\s*([\d.]+)g?\s*/\s*([\d.]+)", re.IGNORECASE),
    "carbs_g": re.compile(r"Carbs:\*\*\s*([\d.]+)g?\s*/\s*([\d.]+)", re.IGNORECASE),
    "fat_g": re.compile(r"Fat:\*\*\s*([\d.]+)g?\s*/\s*([\d.]+)", re.IGNORECASE),
}
_DATE_RE = re.compile(r"log_date:\s*(\d{4}-\d{2}-\d{2})")


def _profile_targets() -> dict:
    try:
        from profile import default_profile
        t = default_profile().daily_targets
        return {k: t[k] for k in ("kcal", "protein_g", "carbs_g", "fat_g") if k in t}
    except Exception:
        return {"kcal": 2800, "protein_g": 130, "carbs_g": 432, "fat_g": 72}


def build_nutrition() -> dict:
    targets = _profile_targets()
    days = []
    if NUTRITION_LOG_DIR.exists():
        for path in sorted(NUTRITION_LOG_DIR.glob("*.md")):
            text = path.read_text(encoding="utf-8")
            dm = _DATE_RE.search(text)
            day_iso = dm.group(1) if dm else path.stem
            row = {"date": day_iso}
            ok = False
            for key, rx in _TOTAL_RE.items():
                m = rx.search(text)
                if m:
                    row[key] = round(float(m.group(1)), 1)
                    targets.setdefault(key, round(float(m.group(2))))
                    ok = True
                else:
                    row[key] = None
            if ok:
                days.append(row)
    days.sort(key=lambda d: d["date"])

    logged = [d for d in days if d.get("kcal") is not None]
    avg = {}
    if logged:
        for key in ("kcal", "protein_g", "carbs_g", "fat_g"):
            vals = [d[key] for d in logged if d.get(key) is not None]
            avg[key] = round(sum(vals) / len(vals), 1) if vals else None

    return {
        "generated_at": date.today().isoformat(),
        "empty": len(days) == 0,
        "targets": targets,
        "days": days,
        "averages": avg,
        "summary": {"days_logged": len(days),
                    "last_day": days[-1]["date"] if days else None},
        "recap": _nutrition_recap(days, avg, targets),
        "headline": _nutrition_headline(days, avg, targets),
    }


def _nutrition_recap(days: list, avg: dict, targets: dict) -> list:
    """Grounded, rules-based nutrition summary as {heading, text} blocks."""
    if not days:
        return [{
            "heading": "What you have been up to",
            "text": ("No nutrition is logged yet. Reply to one of your daily coaching "
                     "emails with what you ate and it will be parsed into macros and "
                     "tracked here against your targets."),
        }]

    n = len(days)
    recap = [f"You have {n} day(s) logged, most recently {days[-1]['date']}."]
    if avg.get("kcal") is not None:
        recap.append(f"On the days logged you are averaging {round(avg['kcal'])} kcal "
                     f"and {round(avg.get('protein_g') or 0)}g protein.")
    if n < 3:
        recap.append("Log a few more days to build a reliable trend.")

    push = []
    prot_t = targets.get("protein_g")
    kcal_t = targets.get("kcal")
    if prot_t and avg.get("protein_g") is not None and avg["protein_g"] < prot_t:
        short = round(prot_t - avg["protein_g"])
        push.append(f"Protein is averaging {round(avg['protein_g'])}g against a "
                    f"{round(prot_t)}g target, about {short}g short, so that is the main "
                    "thing to push.")
    if kcal_t and avg.get("kcal") is not None and avg["kcal"] < kcal_t:
        push.append(f"Calories are running under your {round(kcal_t)} target, worth "
                    "watching given the marathon training load.")
    if not push:
        push.append("Macros are tracking close to target, so keep the consistency up.")

    return [
        {"heading": "What you have been up to", "text": " ".join(recap)},
        {"heading": "Where to push next", "text": " ".join(push)},
    ]


def _nutrition_headline(days: list, avg: dict, targets: dict) -> dict:
    if not days:
        return {"kpis": [],
                "narrative": ("No nutrition logged yet. Reply to a daily email with what "
                              "you ate and it will start tracking here.")}
    latest = days[-1]
    kcal_t = targets.get("kcal")
    prot_t = targets.get("protein_g")
    kpis = [
        {"value": f"{len(days)}", "label": "days logged"},
        {"value": f"{round(avg.get('kcal') or 0)}", "label": "avg kcal"},
        {"value": f"{round(avg.get('protein_g') or 0)}g", "label": "avg protein"},
    ]
    bits = [f"{len(days)} day(s) logged so far."]
    if prot_t and avg.get("protein_g") is not None and avg["protein_g"] < prot_t:
        bits.append(f"Protein is averaging {round(avg['protein_g'])}g against a "
                    f"{round(prot_t)}g target, the main thing to push.")
    elif kcal_t and avg.get("kcal") is not None and avg["kcal"] < kcal_t:
        bits.append(f"Calories are averaging {round(avg['kcal'])} against {round(kcal_t)}, "
                    "a little under for marathon training load.")
    else:
        bits.append("Keep logging to build a trend.")
    return {"kpis": kpis, "narrative": " ".join(bits)}


# ------------------------------------------------------------------------------ plan

def _race_info() -> tuple[date, str, str]:
    """Race date, label and target, from the profile if available else the constants."""
    race_date, label, target = MARATHON_DATE, "San Sebastian marathon", "sub-3:25 (4:51/km)"
    try:
        prof = json.loads(PROFILE_JSON.read_text(encoding="utf-8"))
        if prof.get("race_date"):
            race_date = date.fromisoformat(prof["race_date"])
        label = prof.get("race_label", label)
        target = prof.get("race_target", target)
    except Exception:
        pass
    return race_date, label, target


def _block_note(name: str, weeks: int, long_km: float, qmin: int) -> str:
    """Clean, em-dash-free description of the current periodisation block."""
    if name == "build":
        return (f"Build phase, {weeks} weeks to race. Long run building toward "
                f"{long_km:.0f} km with about {qmin} min of marathon-pace work a week.")
    if name == "taper":
        return (f"Taper, {weeks} weeks to race. Easy running only as the taper rules "
                "take over and you freshen up for race day.")
    return (f"Base phase, {weeks} weeks to race. Long run holding around "
            f"{long_km:.0f} km while you build aerobic volume before the specific work.")


def build_plan() -> dict:
    """The repeating training cycle from plan_template.json, plus the live periodisation
    block (base / build / taper) computed from progression.py for today."""
    try:
        tmpl = json.loads(PLAN_TEMPLATE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"generated_at": date.today().isoformat(), "empty": True}

    race_date, race_label, race_target = _race_info()
    today = datetime.now(TZ).date()
    weeks = progression.weeks_to_race(today, race_date)
    block_name = progression.block_for(today, race_date)
    long_km = progression.long_run_km(today, race_date)
    qmin = progression.quality_minutes(today, race_date)
    block = {
        "name": block_name,
        "label": _block_note(block_name, max(0, weeks), long_km, qmin),
        "long_run_km": long_km,
        "quality_minutes": qmin,
    }

    days = []
    for d in tmpl.get("cycle_days", []):
        rd = d.get("run_details") or {}
        days.append({
            "day_label": d.get("day_label"),
            "session_type": d.get("session_type"),
            "session_kind": d.get("session_kind"),
            "duration_min": d.get("duration_min"),
            "run": {
                "pace": rd.get("pace"),
                "hr_target": rd.get("hr_target"),
                "distance": rd.get("distance"),
                "effort": rd.get("effort"),
            } if rd else None,
            "exercises": [
                {"name": e.get("name"), "sets_reps": e.get("sets_reps"),
                 "weight": e.get("weight")}
                for e in (d.get("exercises") or [])
            ],
            "details": d.get("details") or "",
            "purpose": d.get("purpose") or "",
        })

    return {
        "generated_at": date.today().isoformat(),
        "empty": False,
        "race": {
            "date": race_date.isoformat(),
            "label": race_label,
            "target": race_target,
            "weeks_to_race": max(0, weeks),
        },
        "block": block,
        "cycle_start_date": tmpl.get("cycle_start_date"),
        "days": days,
        "hard_rules": tmpl.get("hard_rules", []),
    }


# Calendar window: how far back / forward the dated calendar reaches.
CAL_WEEKS_BACK = 4
CAL_WEEKS_FWD = 8


def _profile_id() -> str:
    """Profile key for store lookups; falls back to 'luke' if the file is missing."""
    try:
        return json.loads(PROFILE_JSON.read_text(encoding="utf-8")).get("id", "luke")
    except (FileNotFoundError, ValueError):
        return "luke"


def _session_view(session: dict) -> dict:
    """Project a raw cycle/override session into the compact shape the UI reads,
    matching build_plan's day shape so the calendar and Plan tab agree."""
    rd = session.get("run_details") or {}
    return {
        "day_label": session.get("day_label"),
        "session_type": session.get("session_type"),
        "session_kind": session.get("session_kind"),
        "duration_min": session.get("duration_min"),
        "run": {
            "pace": rd.get("pace"),
            "hr_target": rd.get("hr_target"),
            "distance": rd.get("distance"),
            "effort": rd.get("effort"),
        } if rd else None,
        "exercises": [
            {"name": e.get("name"), "sets_reps": e.get("sets_reps"),
             "weight": e.get("weight")}
            for e in (session.get("exercises") or [])
        ],
        "details": session.get("details") or "",
        "purpose": session.get("purpose") or "",
        # Carried so a web edit can round-trip the full session (these feed the
        # daily email but are not shown on the calendar card itself).
        "warm_up": session.get("warm_up") or "",
        "extras": session.get("extras") or "",
        "short_version": session.get("short_version") or "",
    }


def build_calendar() -> dict:
    """A dated calendar around today: the repeating cycle projected onto real dates,
    with per-date overrides applied and past dates flagged where activity was logged.

    This is also the only place overrides reach the dashboard (build_plan shows the
    bare template), so an edited day shows up here once the data is rebuilt."""
    try:
        tmpl = json.loads(PLAN_TEMPLATE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"generated_at": date.today().isoformat(), "empty": True}

    today = datetime.now(TZ).date()
    overrides = store.get_overrides(_profile_id())

    # Dates with logged activity, for the "completed" flag on past days.
    run_dates = {a.date for a in strava_csv.read_activities(STRAVA_CSV)
                 if a.kind == "run" and a.distance_km > 0}
    try:
        lift_dates = {s.date for s in strong_csv.read_lifts(STRONG_CSV)}
    except FileNotFoundError:
        lift_dates = set()

    start = today - timedelta(weeks=CAL_WEEKS_BACK)
    # Snap the window start back to Monday so the UI grid lines up by week.
    start -= timedelta(days=start.weekday())
    end = today + timedelta(weeks=CAL_WEEKS_FWD)

    days = []
    d = start
    while d <= end:
        iso = d.isoformat()
        _, template_session, _ = plan_cycle.cycle_day(d, tmpl)
        record = overrides.get(iso)
        if record and record.get("session"):
            view = _session_view(record["session"])
            source = "override"
        else:
            view = _session_view(template_session)
            source = "template"

        ran, lifted = d in run_dates, d in lift_dates
        if d == today:
            status = "today"
        elif d > today:
            status = "upcoming"
        elif ran or lifted:
            status = "completed"
        else:
            status = "past"

        days.append({
            "date": iso,
            **view,
            "source": source,
            "status": status,
            "completed": {"run": ran, "lift": lifted},
        })
        d += timedelta(days=1)

    return {
        "generated_at": today.isoformat(),
        "empty": False,
        "profile_id": _profile_id(),
        "today": today.isoformat(),
        "window": {"start": start.isoformat(), "end": end.isoformat()},
        "days": days,
    }


def _plan_headline(plan: dict) -> dict:
    if plan.get("empty"):
        return {}
    race = plan["race"]
    block = plan["block"]
    return {
        "block_name": block["name"],
        "weeks_to_race": race["weeks_to_race"],
        "race_label": race["label"],
    }


# ------------------------------------------------------------------------------ home

def build_home(running: dict, lifting: dict, nutrition: dict, plan: dict) -> dict:
    return {
        "generated_at": date.today().isoformat(),
        "plan": _plan_headline(plan),
        "sections": {
            "running": running.get("headline", {}),
            "lifting": lifting.get("headline", {}),
            "nutrition": nutrition.get("headline", {}),
        },
    }


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    running = build_running()
    lifting = build_lifting()
    nutrition = build_nutrition()
    plan = build_plan()
    calendar = build_calendar()
    home = build_home(running, lifting, nutrition, plan)

    for name, data in [("running", running), ("lifting", lifting),
                       ("nutrition", nutrition), ("plan", plan),
                       ("calendar", calendar), ("home", home)]:
        path = OUT_DIR / f"{name}.json"
        path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        print(f"Wrote {path.relative_to(FITNESS_DIR)}")

    rs = running["summary"]
    print(f"  running: {rs['total_runs']} runs, {rs['total_km']} km, "
          f"{len(running['efficiency'])} EF months")
    if not lifting.get("empty"):
        ls = lifting["summary"]
        print(f"  lifting: {ls['total_sessions']} sessions, "
              f"{len(lifting['e1rm'])} e1RM months, current {lifting['current_e1rm']}")
    print(f"  nutrition: {nutrition['summary']['days_logged']} days logged")
    if not plan.get("empty"):
        b = plan["block"]
        print(f"  plan: {len(plan['days'])} day cycle, {b['name']} block, "
              f"{plan['race']['weeks_to_race']} weeks to race")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
