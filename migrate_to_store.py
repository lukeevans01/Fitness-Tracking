#!/usr/bin/env python3
"""One-shot migration: load the legacy git-as-database files into store.py (SQLite).

Reads the existing per-file artefacts for a single profile and writes them into
data/app.db via the store API. Run this ONCE: state, overrides, pending choice and
adaptation are upserts (safe to repeat), but feedback and nutrition are append-only
inserts, so re-running would duplicate those rows. The legacy files are NOT deleted
— they remain in place as a backup until the store path is proven in production.

Usage:
    python3 migrate_to_store.py            # migrates the "luke" profile
    PROFILE_ID=luke python3 migrate_to_store.py
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

import store
from profile import default_profile

ROOT = Path(__file__).parent


def _load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    with open(path) as f:
        return json.load(f)


def _strip_comment(d: dict) -> dict:
    return {k: v for k, v in d.items() if k != "_comment"}


# ──────────────────────────────────────────────────────────────────────────
# adaptation_state.md parsing
# ──────────────────────────────────────────────────────────────────────────

def _parse_adaptation_md(path: Path) -> dict:
    """Pull the key: value lines out of adaptation_state.md's fenced blocks."""
    if not path.exists():
        return {}
    content = path.read_text()
    fields: dict = {}

    def _grab(key: str):
        m = re.search(rf"(?m)^{re.escape(key)}:\s*(\S+)", content)
        return m.group(1) if m else None

    mode = _grab("mode")
    if mode:
        fields["mode"] = mode
    last_updated = _grab("last_updated")
    if last_updated:
        fields["last_updated"] = last_updated

    week_start = _grab("week_start")
    if week_start:
        fields["week_start"] = week_start
    for key, cast in (("strength_sessions", int), ("run_sessions", int), ("run_km_total", float)):
        raw = _grab(key)
        if raw is not None:
            try:
                fields[key] = cast(raw)
            except ValueError:
                pass

    taper_active = _grab("taper_active")
    if taper_active is not None:
        fields["taper_active"] = taper_active.lower() == "true"
    taper_start = _grab("taper_start_date")
    if taper_start is not None:
        fields["taper_start_date"] = None if taper_start == "null" else taper_start

    return fields


# ──────────────────────────────────────────────────────────────────────────
# nutrition_log/*.md parsing (defensive — dir may be empty)
# ──────────────────────────────────────────────────────────────────────────

def _parse_nutrition_md(path: Path) -> tuple[str | None, list[dict]]:
    """Return (first_logged_at, [item dict]) from a legacy nutrition markdown file."""
    content = path.read_text()
    first_logged = None
    fm_match = re.search(r"(?m)^first_logged_at:\s*(\S+)", content)
    if fm_match:
        first_logged = fm_match.group(1)

    items: list[dict] = []
    in_table = False
    for line in content.splitlines():
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
            continue
        try:
            items.append({
                "meal": cells[0],
                "name": cells[1],
                "quantity": cells[2],
                "kcal": float(cells[3]),
                "protein_g": float(cells[4]),
                "carbs_g": float(cells[5]),
                "fat_g": float(cells[6]),
                "confidence": cells[7],
                "source": cells[8],
            })
        except ValueError:
            continue
    return first_logged, items


# ──────────────────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────────────────

def main() -> None:
    profile = default_profile()
    pid = profile.id
    print(f"[migrate] Target profile: {pid}")
    print(f"[migrate] Database: {store._db_path()}")

    counts: dict[str, int] = {}

    # 1. State
    state = _strip_comment(_load_json(ROOT / "state.json"))
    if state:
        store.set_state(pid, state)
        counts["state"] = 1
    else:
        counts["state"] = 0

    # 2. Overrides
    overrides = _load_json(ROOT / "overrides.json").get("overrides", {})
    for iso_date, record in overrides.items():
        store.set_override(pid, iso_date, record)
    counts["overrides"] = len(overrides)

    # 3. Pending choice
    pending = _load_json(ROOT / "plans" / "pending-choice.json")
    if pending:
        store.set_pending_choice(pid, pending)
        counts["pending_choice"] = 1
    else:
        counts["pending_choice"] = 0

    # 4. Feedback log
    feedback_path = ROOT / "feedback_log.jsonl"
    fb_count = 0
    if feedback_path.exists():
        with open(feedback_path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    print(f"[warn] skipping malformed feedback line: {line[:80]!r}")
                    continue
                store.append_feedback(pid, entry)
                fb_count += 1
    counts["feedback"] = fb_count

    # 5. Adaptation state
    adaptation = _parse_adaptation_md(ROOT / "adaptation_state.md")
    if adaptation:
        store.set_adaptation(pid, adaptation)
    counts["adaptation_fields"] = len(adaptation)

    # 6. Nutrition logs
    nutrition_dir = ROOT / "nutrition_log"
    day_count = 0
    item_count = 0
    if nutrition_dir.exists():
        for md in sorted(nutrition_dir.glob("*.md")):
            day_iso = md.stem
            try:
                datetime.fromisoformat(day_iso)
            except ValueError:
                print(f"[warn] skipping non-date nutrition file: {md.name}")
                continue
            _first_logged, items = _parse_nutrition_md(md)
            if items:
                store.append_nutrition(pid, day_iso, items)
                day_count += 1
                item_count += len(items)
    counts["nutrition_days"] = day_count
    counts["nutrition_items"] = item_count

    print("\n[migrate] Done. Rows written:")
    for key in (
        "state", "overrides", "pending_choice", "feedback",
        "adaptation_fields", "nutrition_days", "nutrition_items",
    ):
        print(f"  {key:18} {counts.get(key, 0)}")


if __name__ == "__main__":
    main()
