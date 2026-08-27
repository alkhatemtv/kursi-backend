"""Reading `layout_versions.layout_data` into inventory records.

WHAT SHAPE IS layout_data?
--------------------------
The spec fixes the *contract* - "seats[], objects[], categories[] definitions
with colors; the authoring document", coordinates in canvas units at 50 px/metre
- but not the field names, and two shapes are already in the repository:

  legacy marketplace  {"id": "s0-1", "x": .., "y": .., "catId": "cat-vip",
                       "row": 1, "col": 2, "label": "A2", "blocked": false}
  Phase 1a fixtures   {"uid": "A-12"}

Phase 1c has to migrate the first of these (spec 7 step 3) and Phase 1a's own
tests wrote the second, so this reader accepts both and normalises. The aliases
are listed explicitly below rather than guessed at parse time, so adding a
producer means editing one table, not hunting through the materialiser.

VALIDATION IS UP FRONT AND TOTAL
--------------------------------
Materialisation happens in the same transaction as the layout freeze, and a
freeze is irreversible. So the document is validated completely - every seat,
every category, all errors collected - BEFORE anything is written. A layout that
would produce broken inventory must be rejected while its version is still a
draft that can be fixed.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.engine_models import SEAT_STATUSES
from app.engine_services.errors import LayoutInvalid

#: Accepted spellings, in priority order, for each normalised seat field.
_SEAT_UID_KEYS = ("seat_uid", "uid", "id")
_SEAT_CATEGORY_KEYS = ("category_key", "categoryKey", "catId", "catid", "category")
_SEAT_LABEL_KEYS = ("label",)
_SEAT_SECTION_KEYS = ("section",)
_SEAT_ROW_KEYS = ("row_label", "rowLabel", "row")
_SEAT_NUMBER_KEYS = ("seat_number", "seatNumber", "number", "col")
_SEAT_ACCESSIBLE_KEYS = ("accessibility", "accessible", "isAccessible")
_SEAT_BLOCKED_KEYS = ("blocked", "isBlocked")

_CATEGORY_KEY_KEYS = ("category_key", "key", "id")
_CATEGORY_NAME_KEYS = ("name", "title", "label")


def _first(source: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        if key in source and source[key] not in (None, ""):
            return source[key]
    return None


def _as_text(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


def _as_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "y")
    return False


@dataclass(frozen=True)
class LayoutSeat:
    """One seat of the authoring document, normalised for `performance_seats`."""

    seat_uid: str
    section: str | None = None
    row_label: str | None = None
    seat_number: str | None = None
    label: str | None = None
    x: float | None = None
    y: float | None = None
    category_key: str | None = None
    status: str = "available"
    accessibility: bool = False


@dataclass(frozen=True)
class LayoutCategory:
    """One category definition; the layout owns identity + colour, the
    performance owns the price (spec 3)."""

    category_key: str
    name: str
    name_ar: str | None = None
    color: str | None = None


@dataclass(frozen=True)
class ParsedLayout:
    seats: list[LayoutSeat] = field(default_factory=list)
    categories: list[LayoutCategory] = field(default_factory=list)

    @property
    def category_keys(self) -> set[str]:
        return {c.category_key for c in self.categories}

    @property
    def referenced_category_keys(self) -> set[str]:
        return {s.category_key for s in self.seats if s.category_key}


def parse_layout(layout_data: Any) -> ParsedLayout:
    """Normalise a layout document, or raise LayoutInvalid listing every problem."""
    problems: list[str] = []

    if not isinstance(layout_data, dict):
        raise LayoutInvalid(
            "layout_data must be a JSON object with a 'seats' array",
            problems=[f"layout_data is {type(layout_data).__name__}, not an object"],
        )

    raw_categories = layout_data.get("categories") or []
    if not isinstance(raw_categories, list):
        problems.append("'categories' must be an array")
        raw_categories = []

    categories: list[LayoutCategory] = []
    seen_category_keys: set[str] = set()
    for index, item in enumerate(raw_categories):
        if not isinstance(item, dict):
            problems.append(f"categories[{index}] is not an object")
            continue
        key = _as_text(_first(item, _CATEGORY_KEY_KEYS))
        if not key:
            problems.append(f"categories[{index}] has no key/id")
            continue
        if key in seen_category_keys:
            problems.append(f"categories[{index}] duplicates category key {key!r}")
            continue
        seen_category_keys.add(key)
        categories.append(
            LayoutCategory(
                category_key=key,
                name=_as_text(_first(item, _CATEGORY_NAME_KEYS)) or key,
                name_ar=_as_text(item.get("name_ar") or item.get("nameAr")),
                color=_as_text(item.get("color")),
            )
        )

    raw_seats = layout_data.get("seats")
    if not isinstance(raw_seats, list):
        raise LayoutInvalid(
            "layout_data has no 'seats' array",
            problems=problems + ["'seats' must be an array"],
        )
    if not raw_seats:
        problems.append("'seats' is empty - a performance needs inventory to sell")

    seats: list[LayoutSeat] = []
    seen_uids: set[str] = set()
    for index, item in enumerate(raw_seats):
        if not isinstance(item, dict):
            problems.append(f"seats[{index}] is not an object")
            continue
        seat_uid = _as_text(_first(item, _SEAT_UID_KEYS))
        if not seat_uid:
            problems.append(f"seats[{index}] has no seat_uid/uid/id")
            continue
        if seat_uid in seen_uids:
            # UNIQUE(performance_id, seat_uid) would catch this later; catching
            # it here means the layout is rejected before anything is frozen.
            problems.append(f"seats[{index}] duplicates seat_uid {seat_uid!r}")
            continue
        seen_uids.add(seat_uid)

        category_key = _as_text(_first(item, _SEAT_CATEGORY_KEYS))
        if category_key and seen_category_keys and category_key not in seen_category_keys:
            problems.append(
                f"seats[{index}] ({seat_uid}) references unknown category "
                f"{category_key!r}"
            )

        # An explicit status wins; otherwise `blocked` maps onto 'blocked'.
        status = _as_text(item.get("status"))
        if status is not None and status not in SEAT_STATUSES:
            problems.append(
                f"seats[{index}] ({seat_uid}) has unknown status {status!r}; "
                f"expected one of {', '.join(SEAT_STATUSES)}"
            )
            status = None
        if status is None:
            status = "blocked" if _as_bool(_first(item, _SEAT_BLOCKED_KEYS)) else "available"

        row_label = _as_text(_first(item, _SEAT_ROW_KEYS))
        seat_number = _as_text(_first(item, _SEAT_NUMBER_KEYS))
        label = _as_text(_first(item, _SEAT_LABEL_KEYS))
        if label is None and row_label and seat_number:
            label = f"{row_label}-{seat_number}"

        seats.append(
            LayoutSeat(
                seat_uid=seat_uid,
                section=_as_text(_first(item, _SEAT_SECTION_KEYS)),
                row_label=row_label,
                seat_number=seat_number,
                label=label or seat_uid,
                x=_as_float(item.get("x")),
                y=_as_float(item.get("y")),
                category_key=category_key,
                status=status,
                accessibility=_as_bool(_first(item, _SEAT_ACCESSIBLE_KEYS)),
            )
        )

    if problems:
        raise LayoutInvalid(
            f"layout_data is not usable as inventory ({len(problems)} problem(s))",
            problems=problems,
        )

    return ParsedLayout(seats=seats, categories=categories)
