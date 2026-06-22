#!/usr/bin/env python3
"""Apply a batch of plan edits made from the web dashboard.

Triggered by the apply-plan-edit GitHub workflow on a repository_dispatch. The
edit payload (validated again here, never trusted) arrives as JSON in the
PLAN_EDIT_PAYLOAD env var with the shape:

    {
      "profile_id": "luke",
      "edits": [
        {"iso_date": "2026-06-25", "session": { ...canonical session... }},
        {"iso_date": "2026-06-26", "clear": true}
      ]
    }

Each edit is written through store.set_override / delete_override so the web and
the email coach share one source of truth (data/app.db). The override record
mirrors the shape process_replies writes, so send_daily / _get_current_session
pick it up unchanged. The workflow commits app.db and redeploys afterwards.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import date, datetime
from zoneinfo import ZoneInfo

import store
from profile import default_profile

TZ = ZoneInfo("Europe/Amsterdam")

# Guard rails. The web editor covers a few upcoming weeks at most; reject anything
# that looks like a runaway or malformed batch.
MAX_EDITS = 60
VALID_KINDS = {"run", "strength", "rest"}


class EditError(ValueError):
    """A payload that fails validation; the workflow logs it and exits non-zero."""


def _validate_session(session: object, iso: str) -> dict:
    if not isinstance(session, dict):
        raise EditError(f"{iso}: session must be an object")
    kind = session.get("session_kind")
    if kind not in VALID_KINDS:
        raise EditError(f"{iso}: session_kind must be one of {sorted(VALID_KINDS)}")
    if not session.get("session_type"):
        raise EditError(f"{iso}: session_type is required")

    dur = session.get("duration_min")
    if dur is not None and (not isinstance(dur, int) or dur < 0 or dur > 600):
        raise EditError(f"{iso}: duration_min must be an integer between 0 and 600")

    exercises = session.get("exercises") or []
    if not isinstance(exercises, list):
        raise EditError(f"{iso}: exercises must be a list")
    clean_exercises = []
    for e in exercises:
        if not isinstance(e, dict) or not e.get("name"):
            raise EditError(f"{iso}: each exercise needs a name")
        clean_exercises.append({
            "name": str(e.get("name")),
            "sets_reps": str(e.get("sets_reps") or ""),
            "weight": str(e.get("weight") or ""),
            "rest": str(e.get("rest") or ""),
        })

    run_details = session.get("run_details") or {}
    if not isinstance(run_details, dict):
        raise EditError(f"{iso}: run_details must be an object")

    # Keep the canonical session keys send_daily / build_calendar read, dropping
    # anything unexpected the client may have sent.
    return {
        "day_label": session.get("day_label") or "",
        "session_type": str(session.get("session_type")),
        "session_kind": kind,
        "duration_min": dur,
        "warm_up": str(session.get("warm_up") or ""),
        "exercises": clean_exercises,
        "run_details": run_details,
        "details": str(session.get("details") or ""),
        "extras": str(session.get("extras") or ""),
        "short_version": str(session.get("short_version") or ""),
        "purpose": str(session.get("purpose") or ""),
    }


def apply_edits(payload: dict, today: date) -> dict:
    """Validate and persist the batch. Returns a summary dict. Raises EditError."""
    profile_id = payload.get("profile_id") or default_profile().id
    edits = payload.get("edits")
    if not isinstance(edits, list) or not edits:
        raise EditError("payload must contain a non-empty 'edits' list")
    if len(edits) > MAX_EDITS:
        raise EditError(f"too many edits ({len(edits)}); max is {MAX_EDITS}")

    written, cleared = [], []
    for edit in edits:
        if not isinstance(edit, dict):
            raise EditError("each edit must be an object")
        iso = edit.get("iso_date")
        try:
            target = date.fromisoformat(iso)
        except (TypeError, ValueError):
            raise EditError(f"invalid iso_date: {iso!r}")
        if target < today:
            raise EditError(f"{iso}: cannot edit a past date (today is {today.isoformat()})")

        if edit.get("clear"):
            store.delete_override(profile_id, iso)
            cleared.append(iso)
            continue

        session = _validate_session(edit.get("session"), iso)
        store.set_override(profile_id, iso, {
            "applied_at": datetime.now(TZ).isoformat(timespec="seconds"),
            "edit_source": "web",
            "session": session,
        })
        written.append(iso)

    return {"profile_id": profile_id, "written": written, "cleared": cleared}


def main() -> int:
    raw = os.environ.get("PLAN_EDIT_PAYLOAD") or ""
    if not raw.strip():
        print("[error] PLAN_EDIT_PAYLOAD is empty")
        return 1
    try:
        payload = json.loads(raw)
    except ValueError as exc:
        print(f"[error] PLAN_EDIT_PAYLOAD is not valid JSON: {exc}")
        return 1

    today = datetime.now(TZ).date()
    try:
        summary = apply_edits(payload, today)
    except EditError as exc:
        print(f"[error] {exc}")
        return 1

    print(f"[ok] profile={summary['profile_id']} "
          f"written={len(summary['written'])} cleared={len(summary['cleared'])}")
    for iso in summary["written"]:
        print(f"  set    {iso}")
    for iso in summary["cleared"]:
        print(f"  clear  {iso}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
