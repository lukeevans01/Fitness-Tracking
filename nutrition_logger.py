"""Nutrition logging — parses freeform food replies, estimates macros, persists daily logs.

Storage goes through store.py (SQLite, profile-keyed); the markdown nutrition_log/
files are no longer the source of truth.

Public API:
    log_food(reply_text, target_date, profile=None) -> LogResult
    read_day(target_date, profile_id=None)           -> DayLog | None
    daily_totals(day_log)                            -> dict
    weekly_summary(days=7, end_date=None, targets=None, profile_id=None) -> dict
    render_day_markdown(day_log, targets=None)       -> str   (display-only view)
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import coach_orchestrator
import store
from profile import Profile, default_profile
from specialists import nutrition_lookup

ROOT = Path(__file__).parent
TZ_AMSTERDAM = ZoneInfo("Europe/Amsterdam")
SCHEMA_VERSION = 1

# Deprecated module-level fallback. Callers should pass targets from profile.daily_targets.
# Retained equal to the "luke" profile so legacy call sites keep working unchanged.
DAILY_TARGETS = {
    "protein_g": 130,
    "carbs_g": 432,
    "fat_g": 72,
    "kcal": 2800,
}

# Parses the per-100g string format produced by nutrition_lookup._fetch_off:
# "Name: 89 kcal, 23.0g carbs, 1.1g protein, 0.3g fat (per 100g)"
_OFF_RE = re.compile(
    r":\s*(\d+)\s*kcal,\s*([\d.]+)\s*g\s*carbs,\s*([\d.]+)\s*g\s*protein,\s*([\d.]+)\s*g\s*fat",
    re.IGNORECASE,
)


@dataclass
class FoodItem:
    name: str
    quantity: str
    kcal: float
    protein_g: float
    carbs_g: float
    fat_g: float
    confidence: str  # high | medium | low
    source: str      # off | gemini
    meal: str        # breakfast | lunch | dinner | snack | unspecified


@dataclass
class LogResult:
    items: list[FoodItem]
    running_totals: dict
    delta_vs_target: dict
    coach_note: str = ""


@dataclass
class DayLog:
    log_date: date
    schema_version: int
    items: list[FoodItem]


# ──────────────────────────────────────────────────────────────────────────
# Public API
# ──────────────────────────────────────────────────────────────────────────

def log_food(reply_text: str, target_date: date, profile: Profile | None = None) -> LogResult:
    """Parse reply_text via Gemini + OFF, append to target_date's nutrition log, return totals."""
    profile = profile or default_profile()
    targets = profile.daily_targets
    existing = read_day(target_date, profile.id)
    today_so_far = daily_totals(existing)

    parsed = coach_orchestrator.generate_food_log_response(
        reply_text=reply_text,
        today_so_far=today_so_far,
        targets=targets,
        profile=profile,
    )

    new_items = [_resolve_item(raw) for raw in parsed["items"]]
    store.append_nutrition(
        profile.id, target_date.isoformat(), [_item_to_dict(i) for i in new_items]
    )

    refreshed = read_day(target_date, profile.id)
    totals = daily_totals(refreshed)
    delta = {k: totals[k] - targets[k] for k in ("protein_g", "carbs_g", "fat_g", "kcal")}

    return LogResult(
        items=new_items,
        running_totals=totals,
        delta_vs_target=delta,
        coach_note=parsed.get("coach_note", ""),
    )


def read_day(target_date: date, profile_id: str | None = None) -> DayLog | None:
    """Return the day's nutrition log from the store. None if nothing logged."""
    profile_id = profile_id or default_profile().id
    raw = store.read_day(profile_id, target_date.isoformat())
    if not raw or not raw.get("items"):
        return None
    items = [_item_from_dict(d) for d in raw["items"]]
    return DayLog(log_date=target_date, schema_version=SCHEMA_VERSION, items=items)


def _item_to_dict(item: FoodItem) -> dict:
    return asdict(item)


def _item_from_dict(d: dict) -> FoodItem:
    return FoodItem(
        name=d.get("name", ""),
        quantity=d.get("quantity", ""),
        kcal=float(d.get("kcal", 0) or 0),
        protein_g=float(d.get("protein_g", 0) or 0),
        carbs_g=float(d.get("carbs_g", 0) or 0),
        fat_g=float(d.get("fat_g", 0) or 0),
        confidence=d.get("confidence", "low"),
        source=d.get("source", "gemini"),
        meal=d.get("meal", "unspecified"),
    )


def daily_totals(day_log: DayLog | None) -> dict:
    """Sum macros across all items in the day. Empty totals if None or no items."""
    if not day_log or not day_log.items:
        return {"protein_g": 0.0, "carbs_g": 0.0, "fat_g": 0.0, "kcal": 0.0}
    return {
        "protein_g": sum(i.protein_g for i in day_log.items),
        "carbs_g": sum(i.carbs_g for i in day_log.items),
        "fat_g": sum(i.fat_g for i in day_log.items),
        "kcal": sum(i.kcal for i in day_log.items),
    }


def weekly_summary(
    days: int = 7,
    end_date: date | None = None,
    targets: dict | None = None,
    profile_id: str | None = None,
) -> dict:
    """Aggregate the last `days` days of nutrition logs into trend metrics + pattern flags."""
    targets = targets if targets is not None else DAILY_TARGETS
    profile_id = profile_id or default_profile().id
    end = end_date if end_date is not None else datetime.now(TZ_AMSTERDAM).date()
    daily = []
    for offset in range(days):
        d = end - timedelta(days=days - 1 - offset)
        day = read_day(d, profile_id)
        if day and day.items:
            daily.append({"date": d.isoformat(), "totals": daily_totals(day), "logged": True})
        else:
            daily.append({"date": d.isoformat(), "totals": None, "logged": False})

    logged = [d for d in daily if d["logged"]]
    patterns = _detect_patterns(daily, targets)

    if not logged:
        return {
            "days_logged": 0,
            "avg_protein_g": 0.0,
            "avg_carbs_g": 0.0,
            "avg_fat_g": 0.0,
            "avg_kcal": 0.0,
            "protein_target_hits": 0,
            "lowest_protein_day": None,
            "patterns": patterns,
        }

    n = len(logged)
    lowest = min(logged, key=lambda d: d["totals"]["protein_g"])
    return {
        "days_logged": n,
        "avg_protein_g": sum(d["totals"]["protein_g"] for d in logged) / n,
        "avg_carbs_g": sum(d["totals"]["carbs_g"] for d in logged) / n,
        "avg_fat_g": sum(d["totals"]["fat_g"] for d in logged) / n,
        "avg_kcal": sum(d["totals"]["kcal"] for d in logged) / n,
        "protein_target_hits": sum(
            1 for d in logged if d["totals"]["protein_g"] >= targets["protein_g"]
        ),
        "lowest_protein_day": {"date": lowest["date"], "g": lowest["totals"]["protein_g"]},
        "patterns": patterns,
    }


# ──────────────────────────────────────────────────────────────────────────
# Rendering — markdown is a display-only view, NEVER parsed back
# ──────────────────────────────────────────────────────────────────────────

def render_day_markdown(day_log: DayLog | None, targets: dict | None = None) -> str:
    """Render a day's log as the human markdown layout.

    This is a one-way view for human reading and Phase 2 exports. The store
    (full-precision structured rows) is the source of truth; this output is
    never parsed back into items. Rounding here is presentation only.
    """
    targets = targets if targets is not None else DAILY_TARGETS
    log_date = day_log.log_date if day_log else datetime.now(TZ_AMSTERDAM).date()
    items = day_log.items if day_log else []

    fm = (
        "---\n"
        f"schema_version: {SCHEMA_VERSION}\n"
        f"log_date: {log_date.isoformat()}\n"
        "---\n\n"
    )
    title = f"# Nutrition log — {log_date.strftime('%a %d %b %Y')}\n\n"
    table = (
        "## Items\n\n"
        "| Meal | Item | Quantity | kcal | P (g) | C (g) | F (g) | Confidence | Source |\n"
        "|---|---|---|---|---|---|---|---|---|\n"
    )
    for i in items:
        table += (
            f"| {i.meal} | {i.name} | {i.quantity} | "
            f"{i.kcal:.0f} | {i.protein_g:.1f} | {i.carbs_g:.1f} | {i.fat_g:.1f} | "
            f"{i.confidence} | {i.source} |\n"
        )
    table += "\n"

    totals = daily_totals(day_log)
    dk = totals["kcal"] - targets["kcal"]
    dp = totals["protein_g"] - targets["protein_g"]
    dc = totals["carbs_g"] - targets["carbs_g"]
    df = totals["fat_g"] - targets["fat_g"]
    s = lambda x, d=0: (f"+{x:.{d}f}" if x >= 0 else f"{x:.{d}f}")

    totals_block = (
        "## Daily totals\n\n"
        f"- **Calories:** {totals['kcal']:.0f} / {targets['kcal']} ({s(dk)})\n"
        f"- **Protein:** {totals['protein_g']:.1f}g / {targets['protein_g']}g ({s(dp, 1)}g)\n"
        f"- **Carbs:** {totals['carbs_g']:.1f}g / {targets['carbs_g']}g ({s(dc, 1)}g)\n"
        f"- **Fat:** {totals['fat_g']:.1f}g / {targets['fat_g']}g ({s(df, 1)}g)\n"
    )
    return fm + title + table + totals_block


# ──────────────────────────────────────────────────────────────────────────
# Internals — OFF resolution
# ──────────────────────────────────────────────────────────────────────────

def _resolve_item(raw: dict) -> FoodItem:
    """If source=needs_lookup, try OFF and scale by quantity_g. Else keep Gemini's macros."""
    source = raw.get("source", "gemini")
    if source == "needs_lookup":
        off_data = nutrition_lookup.lookup_food(raw.get("name", "")) if raw.get("name") else ""
        scaled = _scale_off(off_data, raw.get("quantity_g"))
        if scaled is not None:
            return FoodItem(
                name=raw.get("name", ""),
                quantity=raw.get("quantity", ""),
                kcal=scaled["kcal"],
                protein_g=scaled["protein_g"],
                carbs_g=scaled["carbs_g"],
                fat_g=scaled["fat_g"],
                confidence="high",
                source="off",
                meal=raw.get("meal", "unspecified"),
            )
        # OFF miss — fall through to Gemini's estimate, normalised to source=gemini.
        source = "gemini"

    return FoodItem(
        name=raw.get("name", ""),
        quantity=raw.get("quantity", ""),
        kcal=float(raw.get("kcal", 0) or 0),
        protein_g=float(raw.get("protein_g", 0) or 0),
        carbs_g=float(raw.get("carbs_g", 0) or 0),
        fat_g=float(raw.get("fat_g", 0) or 0),
        confidence=raw.get("confidence", "low"),
        source=source if source in ("off", "gemini") else "gemini",
        meal=raw.get("meal", "unspecified"),
    )


def _scale_off(off_str: str, quantity_g) -> dict | None:
    """Parse the OFF per-100g string and scale by quantity_g (grams). None if unparseable."""
    if not off_str:
        return None
    match = _OFF_RE.search(off_str)
    if not match:
        return None
    try:
        qg = float(quantity_g) if quantity_g is not None else 100.0
    except (TypeError, ValueError):
        qg = 100.0
    factor = qg / 100.0
    kcal_100, carbs_100, protein_100, fat_100 = match.groups()
    return {
        "kcal": float(kcal_100) * factor,
        "carbs_g": float(carbs_100) * factor,
        "protein_g": float(protein_100) * factor,
        "fat_g": float(fat_100) * factor,
    }


# ──────────────────────────────────────────────────────────────────────────
# Internals — pattern detection (deterministic, no LLM)
# ──────────────────────────────────────────────────────────────────────────

def _detect_patterns(daily: list[dict], targets: dict | None = None) -> list[str]:
    """Surface factual flags about the week's nutrition logs."""
    targets = targets if targets is not None else DAILY_TARGETS
    patterns: list[str] = []

    # 1. Protein <80% of target for 3+ consecutive days (logged days only; a gap resets the streak)
    threshold = 0.8 * targets["protein_g"]
    streak = 0
    longest = 0
    for d in daily:
        if d["logged"] and d["totals"]["protein_g"] < threshold:
            streak += 1
            longest = max(longest, streak)
        else:
            streak = 0
    if longest >= 3:
        patterns.append(
            f"protein <{int(threshold)}g for {longest} consecutive days"
        )

    # 2. Gaps — days with no log
    gaps = [d["date"] for d in daily if not d["logged"]]
    if gaps:
        patterns.append(f"no log for {', '.join(gaps)}")

    # 3. Average kcal <2,200 across logged days
    logged = [d for d in daily if d["logged"]]
    if logged:
        avg_kcal = sum(d["totals"]["kcal"] for d in logged) / len(logged)
        if avg_kcal < 2200:
            patterns.append(
                f"average kcal {avg_kcal:.0f} (below 2,200 threshold — possible under-eating)"
            )

    return patterns
