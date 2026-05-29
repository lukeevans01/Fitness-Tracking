"""Routine template library — Luke's documented preferred strength sessions.

The CSVs in `routines/` are the editable source of truth for the strength session
menu. Each file is one template named "Week <n> Routine <slot>.csv" with the header
`Exercise,Type,Sets,Reps,Notes`. This module parses them into structured records,
renders a compact prompt block for the coach, and can write a template back to disk
so the app can edit a routine in response to Luke's feedback.

Standard library only (csv); no pandas. Run the tests with:
    python -m unittest tests.test_routine_library
"""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass
from pathlib import Path

ROUTINES_DIR = Path(__file__).parent / "routines"

# "Week 1 Routine A" -> week=1, slot="A"
_NAME_RE = re.compile(r"week\s*(\d+)\s*routine\s*([a-z])", re.IGNORECASE)

_FIELDNAMES = ["Exercise", "Type", "Sets", "Reps", "Notes"]


@dataclass(frozen=True)
class RoutineExercise:
    name: str
    kind: str          # Compound | Isolation | Core | Power (free text from the CSV)
    sets: int
    reps: int
    notes: str = ""    # e.g. "/leg", "/side", "m" — verbatim from the CSV

    def describe(self) -> str:
        """One-line human description, e.g. 'Back squat (compound) 4x8 /leg'."""
        base = f"{self.name} ({self.kind.lower()}) {self.sets}x{self.reps}"
        return f"{base} {self.notes}".rstrip()


@dataclass(frozen=True)
class RoutineTemplate:
    week: int
    slot: str                       # "A" | "B" | "C"
    exercises: tuple[RoutineExercise, ...]
    source_path: Path | None = None

    @property
    def name(self) -> str:
        return f"Week {self.week} Routine {self.slot}"

    @property
    def primary(self) -> RoutineExercise | None:
        """First compound movement, the de-facto focus lift of the session."""
        for ex in self.exercises:
            if ex.kind.lower() == "compound":
                return ex
        return self.exercises[0] if self.exercises else None


def _parse_int(value: str, default: int = 0) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _template_meta(path: Path) -> tuple[int, str]:
    """Derive (week, slot) from the filename. Falls back to (0, stem) if unmatched."""
    match = _NAME_RE.search(path.stem)
    if match:
        return int(match.group(1)), match.group(2).upper()
    return 0, path.stem


def read_template(path: Path) -> RoutineTemplate:
    """Parse a single routine CSV into a RoutineTemplate.

    Rows with a blank exercise name are skipped. Raises FileNotFoundError if missing.
    """
    if not path.exists():
        raise FileNotFoundError(f"Routine template not found: {path}")

    week, slot = _template_meta(path)
    exercises: list[RoutineExercise] = []
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            name = (row.get("Exercise") or "").strip()
            if not name:
                continue
            exercises.append(
                RoutineExercise(
                    name=name,
                    kind=(row.get("Type") or "").strip() or "Compound",
                    sets=_parse_int(row.get("Sets"), 0),
                    reps=_parse_int(row.get("Reps"), 0),
                    notes=(row.get("Notes") or "").strip(),
                )
            )
    return RoutineTemplate(week=week, slot=slot, exercises=tuple(exercises), source_path=path)


def load_templates(directory: Path | None = None) -> list[RoutineTemplate]:
    """Load every routine CSV in `directory`, sorted by (week, slot).

    Returns an empty list if the directory does not exist (so callers degrade
    gracefully rather than crash when no templates are deployed).
    """
    directory = directory or ROUTINES_DIR
    if not directory.exists():
        return []
    templates = [read_template(p) for p in sorted(directory.glob("*.csv"))]
    return sorted(templates, key=lambda t: (t.week, t.slot))


def save_template(template: RoutineTemplate, directory: Path | None = None) -> Path:
    """Write a template back to its CSV so the app can edit a routine on feedback.

    Uses template.source_path when set, else derives the path from name in `directory`.
    Returns the path written.
    """
    directory = directory or ROUTINES_DIR
    path = template.source_path or (directory / f"{template.name}.csv")
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(_FIELDNAMES)
        for ex in template.exercises:
            writer.writerow([ex.name, ex.kind, ex.sets, ex.reps, ex.notes])
    return path


def to_prompt_block(templates: list[RoutineTemplate] | None = None) -> str:
    """Render the template library as a compact text block for a Gemini prompt.

    Returns an empty string when there are no templates so callers can skip the
    section cleanly.
    """
    templates = load_templates() if templates is None else templates
    if not templates:
        return ""
    lines = [
        "ROUTINE TEMPLATE LIBRARY (Luke's documented preferred strength sessions).",
        "Use these as the basis for strength advice and session design. Prefer movements",
        "and rep schemes drawn from this library before inventing new ones.",
        "",
    ]
    for t in templates:
        primary = t.primary
        focus = f" (focus: {primary.name})" if primary else ""
        lines.append(f"{t.name}{focus}:")
        for ex in t.exercises:
            lines.append(f"  - {ex.describe()}")
        lines.append("")
    return "\n".join(lines).rstrip()
