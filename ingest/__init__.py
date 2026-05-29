"""Source-agnostic ingestion package — the Phase 4 data-capture boundary.

`models.py` defines the normalised `Activity`/`LiftSet` records. Each source is one
adapter implementing the read contract (`read_activities`, `read_lifts`). The registry
below lets the active source be selected by name; today only the CSV adapters are
registered. A future API source (`strava_api`, `apple_health`) registers here under the
same interface with no change downstream.
"""

from . import strava_csv, strong_csv
from .models import Activity, LiftSet

# name -> {"activities": read_activities, "lifts": read_lifts}
_SOURCES = {
    "csv": {
        "activities": strava_csv.read_activities,
        "lifts": strong_csv.read_lifts,
    },
}

DEFAULT_SOURCE = "csv"


def get_reader(record_kind: str, source: str = DEFAULT_SOURCE):
    """Return the reader callable for a record kind ("activities" or "lifts").

    `source` selects the registered adapter set (default "csv"). Raises KeyError for an
    unknown source or record kind so misconfiguration fails loudly.
    """
    return _SOURCES[source][record_kind]


__all__ = ["Activity", "LiftSet", "get_reader", "DEFAULT_SOURCE", "strava_csv", "strong_csv"]
