"""Shared fixture data for the Phase 1b service tests.

Kept out of `conftest.py` so test modules can import it by name. pytest loads
conftest files under private module names, so importing constants *from* a
conftest would load a second copy of it - this module is imported exactly once.
"""
from __future__ import annotations

from datetime import datetime, timezone


#: A fixed instant every test starts from, so expiry arithmetic is readable.
T0 = datetime(2026, 1, 15, 12, 0, 0, tzinfo=timezone.utc)

#: The 144-seat house: 12 rows (A..L) of 12.
GRID_ROWS = 12
GRID_COLS = 12
TOTAL_SEATS = GRID_ROWS * GRID_COLS

#: Two seats the layout marks unsellable, and two marked accessible.
BLOCKED_UIDS = ("F-6", "F-7")
ACCESSIBLE_UIDS = ("A-1", "A-12")

VIP_ROWS = ("A", "B", "C")
PRICES = {"vip": 25_000, "standard": 12_000}  # KWD 25.000 / 12.000


def make_layout_data(
    rows: int = GRID_ROWS,
    cols: int = GRID_COLS,
    *,
    blocked: tuple[str, ...] = BLOCKED_UIDS,
    accessible: tuple[str, ...] = ACCESSIBLE_UIDS,
) -> dict:
    """A layout document in the authoring shape: seats[], objects[], categories[].

    Coordinates follow the unchanged 50 px/metre canvas contract - seats are
    0.8 m apart, which is 40 canvas units.
    """
    seats = []
    for r in range(rows):
        row_label = chr(ord("A") + r)
        for c in range(cols):
            uid = f"{row_label}-{c + 1}"
            seats.append(
                {
                    "uid": uid,
                    "x": 100 + c * 40,
                    "y": 100 + r * 40,
                    "section": "Stalls",
                    "row": row_label,
                    "seat_number": str(c + 1),
                    "label": uid,
                    "category_key": "vip" if row_label in VIP_ROWS else "standard",
                    "blocked": uid in blocked,
                    "accessibility": uid in accessible,
                }
            )
    return {
        "seats": seats,
        "objects": [{"type": "stage", "x": 0, "y": 0, "w": 640, "h": 60}],
        "categories": [
            {"key": "vip", "name": "VIP", "name_ar": "في آي بي", "color": "#c9a227"},
            {"key": "standard", "name": "Standard", "color": "#3b82f6"},
        ],
    }
