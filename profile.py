#!/usr/bin/env python3
"""Profile abstraction — per-user identity, race goal, macro targets, and coach prompt text.

A Profile bundles everything the coaching pipeline needs to know about *who* it is
coaching: email identity, race goal, daily macro targets, and the free-text profile
block fed into Gemini prompts. The live cron runs a single profile ("luke"); this
abstraction lets the same code serve multiple users without hardcoding identity.

Usage:
    from profile import default_profile, load_profile

    profile = default_profile()           # reads PROFILE_ID env, falls back to "luke"
    profile = load_profile("luke")        # explicit

Profiles are stored as JSON in profiles/<id>.json. This module does NOT split data
files (nutrition logs, overrides, etc.) by profile id — that is a later concern.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import date
from pathlib import Path

PROFILES_DIR = Path(__file__).parent / "profiles"
DEFAULT_PROFILE_ID = "luke"


@dataclass(frozen=True)
class Profile:
    id: str
    email: str
    display_name: str
    race_date: date
    race_label: str
    race_target: str
    daily_targets: dict
    profile_text: str
    specialist_overrides: dict | None = None
    # Explicit marathon pace, e.g. "4:51/km". None means there is no time goal, so
    # quality work is prescribed by effort and heart rate instead of a pace.
    marathon_pace: str | None = None
    marathon_pace_hr: str | None = None


def load_profile(profile_id: str) -> Profile:
    """Load profiles/<profile_id>.json into a Profile. Raises if missing or malformed."""
    path = PROFILES_DIR / f"{profile_id}.json"
    if not path.exists():
        raise FileNotFoundError(f"No profile config at {path}")
    data = json.loads(path.read_text())

    required = {
        "id", "email", "display_name", "race_date", "race_label",
        "race_target", "daily_targets", "profile_text",
    }
    missing = required - set(data.keys())
    if missing:
        raise ValueError(f"Profile {profile_id!r} missing keys: {missing}")

    return Profile(
        id=data["id"],
        email=data["email"],
        display_name=data["display_name"],
        race_date=date.fromisoformat(data["race_date"]),
        race_label=data["race_label"],
        race_target=data["race_target"],
        daily_targets=data["daily_targets"],
        profile_text=data["profile_text"],
        specialist_overrides=data.get("specialist_overrides"),
        marathon_pace=data.get("marathon_pace"),
        marathon_pace_hr=data.get("marathon_pace_hr"),
    )


def default_profile() -> Profile:
    """Load the profile named by the PROFILE_ID env var, falling back to "luke"."""
    profile_id = os.environ.get("PROFILE_ID") or DEFAULT_PROFILE_ID
    return load_profile(profile_id)
