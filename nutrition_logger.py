"""Nutrition logging — parses freeform food replies, estimates macros, writes daily logs.

Public API:
    log_food(reply_text, target_date) -> LogResult
    read_day(target_date)             -> DayLog | None
    daily_totals(day_log)             -> dict
    weekly_summary(days=7, end_date=None) -> dict
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import coach_orchestrator
from specialists import nutrition_lookup

ROOT = Path(__file__).parent
LOG_DIR = ROOT / "nutrition_log"
TZ_AMSTERDAM = ZoneInfo("Europe/Amsterdam")
SCHEMA_VERSION = 1

# TODO(refactor): per-user targets. Hardcoded to Luke for Phase 2.
# Pulled from specialists/nutrition.py — 72kg body weight, 1.8g/kg protein, 6g/kg carbs baseline.
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

def log_food(reply_text: str, target_date: date) -> LogResult:
    """Parse reply_text via Gemini + OFF, append to target_date's nutrition log, return totals."""
    LOG_DIR.mkdir(exist_ok=True)
    existing = read_day(target_date)
    today_so_far = daily_totals(existing)

    parsed = coach_orchestrator.generate_food_log_response(
        reply_text=reply_text,
        today_so_far=today_so_far,
        targets=DAILY_TARGETS,
    )

    new_items = [_resolve_item(raw) for raw in parsed["items"]]
    _append_day_file(target_date, new_items)

    refreshed = read_day(target_date)
    totals = daily_totals(refreshed)
    delta = {k: totals[k] - DAILY_TARGETS[k] for k in ("protein_g", "carbs_g", "fat_g", "kcal")}

    return LogResult(
        items=new_items,
        running_totals=totals,
        delta_vs_target=delta,
        coach_note=parsed.get("coach_note", ""),
    )


def read_day(target_date: date) -> DayLog | None:
    """Parse the nutrition log file for target_date. Returns None if missing or schema invalid."""
    path = _path_for(target_date)
    if not path.exists():
        return None
    try:
        content = path.read_text()
    except OSError:
        return None

    frontmatter, body = _split_frontmatter(content)
    fm = _parse_frontmatter(frontmatter)

    raw_version = fm.get("schema_version")
    try:
        schema_v = int(raw_version) if raw_version is not None else 0
    except (TypeError, ValueError):
        print(f"[warn] {path.name}: invalid schema_version {raw_version!r}, skipping file")
        return None
    if schema_v == 0:
        print(f"[warn] {path.name}: missing schema_version, skipping file")
        return None
    if schema_v > SCHEMA_VERSION:
        print(f"[warn] {path.name}: schema_version {schema_v} > {SCHEMA_VERSION}, best-effort parse")

    items = _parse_items_table(body)
    return DayLog(log_date=target_date, schema_version=schema_v, items=items)


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


def weekly_summary(days: int = 7, end_date: date | None = None) -> dict:
    """Aggregate the last `days` days of nutrition logs into trend metrics + pattern flags."""
    end = end_date if end_date is not None else datetime.now(TZ_AMSTERDAM).date()
    daily = []
    for offset in range(days):
        d = end - timedelta(days=days - 1 - offset)
        day = read_day(d)
        if day and day.items:
            daily.append({"date": d.isoformat(), "totals": daily_totals(day), "logged": True})
        else:
            daily.append({"date": d.isoformat(), "totals": None, "logged": False})

    logged = [d for d in daily if d["logged"]]
    patterns = _detect_patterns(daily)

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
            1 for d in logged if d["totals"]["protein_g"] >= DAILY_TARGETS["protein_g"]
        ),
        "lowest_protein_day": {"date": lowest["date"], "g": lowest["totals"]["protein_g"]},
        "patterns": patterns,
    }


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
# Internals — file I/O
# ──────────────────────────────────────────────────────────────────────────

def _path_for(target_date: date) -> Path:
    return LOG_DIR / f"{target_date.isoformat()}.md"


def _split_frontmatter(content: str) -> tuple[str, str]:
    if not content.startswith("---"):
        return "", content
    parts = content.split("---", 2)
    if len(parts) < 3:
        return "", content
    return parts[1].strip(), parts[2].lstrip()


def _parse_frontmatter(fm: str) -> dict:
    out: dict[str, str] = {}
    for line in fm.splitlines():
        if ":" in line:
            k, _, v = line.partition(":")
            out[k.strip()] = v.strip()
    return out


def _parse_items_table(body: str) -> list[FoodItem]:
    """Parse the | Meal | Item | … | rows from the body. Tolerant of malformed rows."""
    items: list[FoodItem] = []
    in_table = False
    for line in body.splitlines():
        s = line.strip()
        if not s.startswith("|"):
            in_table = False
            continue
        if "Meal" in s and "Item" in s:
            in_table = True
            continue
        if re.match(r"^\|[\s|:\-]+\|$", s):
            continue
        if not in_table:
            continue
        cells = [c.strip() for c in s.strip("|").split("|")]
        if len(cells) < 9:
            print(f"[warn] malformed nutrition row, skipping: {s!r}")
            continue
        try:
            items.append(FoodItem(
                meal=cells[0],
                name=cells[1],
                quantity=cells[2],
                kcal=float(cells[3]),
                protein_g=float(cells[4]),
                carbs_g=float(cells[5]),
                fat_g=float(cells[6]),
                confidence=cells[7],
                source=cells[8],
            ))
        except (ValueError, IndexError) as exc:
            print(f"[warn] could not parse nutrition row {s!r}: {exc}")
    return items


def _append_day_file(target_date: date, new_items: list[FoodItem]) -> None:
    """Append new_items to target_date's log. Creates the file if it doesn't exist."""
    path = _path_for(target_date)
    now_iso = datetime.now(TZ_AMSTERDAM).isoformat(timespec="seconds")

    if path.exists():
        existing = read_day(target_date)
        items = (existing.items if existing else []) + new_items
        frontmatter, _ = _split_frontmatter(path.read_text())
        first_logged = _parse_frontmatter(frontmatter).get("first_logged_at", now_iso)
    else:
        items = list(new_items)
        first_logged = now_iso

    path.write_text(_render_day_file(target_date, items, first_logged, now_iso))


def _render_day_file(
    target_date: date,
    items: list[FoodItem],
    first_logged: str,
    last_updated: str,
) -> str:
    fm = (
        "---\n"
        f"schema_version: {SCHEMA_VERSION}\n"
        f"log_date: {target_date.isoformat()}\n"
        f"first_logged_at: {first_logged}\n"
        f"last_updated_at: {last_updated}\n"
        "---\n\n"
    )
    title = f"# Nutrition log — {target_date.strftime('%a %d %b %Y')}\n\n"
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

    totals = daily_totals(DayLog(log_date=target_date, schema_version=SCHEMA_VERSION, items=items))
    dk = totals["kcal"] - DAILY_TARGETS["kcal"]
    dp = totals["protein_g"] - DAILY_TARGETS["protein_g"]
    dc = totals["carbs_g"] - DAILY_TARGETS["carbs_g"]
    df = totals["fat_g"] - DAILY_TARGETS["fat_g"]
    s = lambda x, d=0: (f"+{x:.{d}f}" if x >= 0 else f"{x:.{d}f}")

    totals_block = (
        "## Daily totals\n\n"
        f"- **Calories:** {totals['kcal']:.0f} / {DAILY_TARGETS['kcal']} ({s(dk)})\n"
        f"- **Protein:** {totals['protein_g']:.1f}g / {DAILY_TARGETS['protein_g']}g ({s(dp, 1)}g)\n"
        f"- **Carbs:** {totals['carbs_g']:.1f}g / {DAILY_TARGETS['carbs_g']}g ({s(dc, 1)}g)\n"
        f"- **Fat:** {totals['fat_g']:.1f}g / {DAILY_TARGETS['fat_g']}g ({s(df, 1)}g)\n"
    )
    return fm + title + table + totals_block


# ──────────────────────────────────────────────────────────────────────────
# Internals — pattern detection (deterministic, no LLM)
# ──────────────────────────────────────────────────────────────────────────

def _detect_patterns(daily: list[dict]) -> list[str]:
    """Surface factual flags about the week's nutrition logs."""
    patterns: list[str] = []

    # 1. Protein <80% of target for 3+ consecutive days (logged days only; a gap resets the streak)
    threshold = 0.8 * DAILY_TARGETS["protein_g"]
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
