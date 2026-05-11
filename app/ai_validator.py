"""Inline validator for the v2 layout schema.

Mirrors the JS validator in the frontend's index.html so the contract is
enforced identically on both sides. Returns a list of human-readable error
strings — empty list means the document is valid.

This intentionally does not pull in jsonschema or pydantic-based validation:
the layout shape is loose enough (free-form numbers everywhere) that a flat
recursive walk is clearer and avoids adding a runtime dependency.
"""
from __future__ import annotations

import re
from typing import Any

SCHEMA_VERSION = "2.0.0"

VENUE_TYPES = {"theater", "cinema", "stadium", "opera", "circle", "conference", "talk_show"}
SEAT_TYPES = {"standard", "recliner", "box", "premium"}
SEAT_STATUSES = {"available", "reserved", "blocked", "sold"}
OBJ_TYPES = {"stage", "screen", "pitch", "dj_booth", "walkway", "label"}

_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$", re.I)
_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
# Loose ISO-8601 check — matches "YYYY-MM-DDTHH:MM:SS" prefix with optional ms/tz.
_ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")


def _is_uuid(s: Any) -> bool:
    return isinstance(s, str) and bool(_UUID_RE.match(s))


def _is_num(n: Any) -> bool:
    # bool is a subclass of int in Python — exclude it explicitly.
    return isinstance(n, (int, float)) and not isinstance(n, bool)


def _is_int(n: Any) -> bool:
    return isinstance(n, int) and not isinstance(n, bool)


def _is_iso(s: Any) -> bool:
    return isinstance(s, str) and bool(_ISO_RE.match(s))


def validate_layout_v2(d: Any) -> list[str]:
    errs: list[str] = []
    if not isinstance(d, dict):
        return ["layout must be an object"]

    if d.get("schema_version") != SCHEMA_VERSION:
        errs.append(f'schema_version must be "{SCHEMA_VERSION}", got "{d.get("schema_version")}"')

    v = d.get("venue")
    if not isinstance(v, dict):
        errs.append("venue missing")
    else:
        if not _is_uuid(v.get("id")):
            errs.append("venue.id must be a UUID v4")
        name = v.get("name")
        if not isinstance(name, str) or not name:
            errs.append("venue.name must be a non-empty string")
        if v.get("type") not in VENUE_TYPES:
            errs.append(f"venue.type must be one of {sorted(VENUE_TYPES)}")
        dims = v.get("dimensions")
        if not isinstance(dims, dict) or not _is_num(dims.get("width_m")) or not _is_num(dims.get("depth_m")):
            errs.append("venue.dimensions.width_m/depth_m must be numbers")
        if not _is_uuid(v.get("owner_id")):
            errs.append("venue.owner_id must be a UUID v4")
        if not _is_iso(v.get("created_at")):
            errs.append("venue.created_at must be ISO-8601")
        if not _is_iso(v.get("updated_at")):
            errs.append("venue.updated_at must be ISO-8601")

    sections = d.get("sections")
    if not isinstance(sections, list) or len(sections) < 1:
        errs.append("sections must be a non-empty array")
    else:
        for i, s in enumerate(sections):
            if not isinstance(s, dict):
                errs.append(f"sections[{i}] must be an object")
                continue
            if not _is_uuid(s.get("id")):
                errs.append(f"sections[{i}].id must be UUID")
            if not isinstance(s.get("name"), str):
                errs.append(f"sections[{i}].name must be string")
            if not isinstance(s.get("label"), str):
                errs.append(f"sections[{i}].label must be string")
            origin = s.get("origin")
            if not isinstance(origin, dict) or not _is_num(origin.get("x")) or not _is_num(origin.get("y")):
                errs.append(f"sections[{i}].origin.x/y must be numbers")
            bounds = s.get("bounds")
            if not isinstance(bounds, dict) or not _is_num(bounds.get("width")) or not _is_num(bounds.get("height")):
                errs.append(f"sections[{i}].bounds.width/height must be numbers")
            if not _is_num(s.get("rotation_deg")):
                errs.append(f"sections[{i}].rotation_deg must be number")

    cats = d.get("categories")
    if not isinstance(cats, list):
        errs.append("categories must be array")
    else:
        for i, c in enumerate(cats):
            if not isinstance(c, dict):
                errs.append(f"categories[{i}] must be an object")
                continue
            if not _is_uuid(c.get("id")):
                errs.append(f"categories[{i}].id must be UUID")
            if not isinstance(c.get("name"), str):
                errs.append(f"categories[{i}].name must be string")
            color = c.get("color")
            if not isinstance(color, str) or not _COLOR_RE.match(color):
                errs.append(f"categories[{i}].color must be #RRGGBB")
            if not _is_num(c.get("default_price")):
                errs.append(f"categories[{i}].default_price must be number")

    seats = d.get("seats")
    if not isinstance(seats, list):
        errs.append("seats must be array")
    else:
        for i, s in enumerate(seats):
            if not isinstance(s, dict):
                errs.append(f"seats[{i}] must be an object")
                continue
            if not _is_uuid(s.get("id")):
                errs.append(f"seats[{i}].id must be UUID")
            if not _is_uuid(s.get("section_id")):
                errs.append(f"seats[{i}].section_id must be UUID")
            if not _is_num(s.get("x")) or not _is_num(s.get("y")):
                errs.append(f"seats[{i}].x/y must be numbers")
            if not isinstance(s.get("row"), str) or not isinstance(s.get("number"), str):
                errs.append(f"seats[{i}].row/number must be strings")
            if not _is_uuid(s.get("category_id")):
                errs.append(f"seats[{i}].category_id must be UUID")
            po = s.get("price_override")
            if po is not None and not _is_num(po):
                errs.append(f"seats[{i}].price_override must be number or null")
            acc = s.get("accessibility")
            if (
                not isinstance(acc, dict)
                or not isinstance(acc.get("wheelchair"), bool)
                or not isinstance(acc.get("companion"), bool)
            ):
                errs.append(f"seats[{i}].accessibility.wheelchair/companion must be boolean")
            if s.get("seat_type") not in SEAT_TYPES:
                errs.append(f"seats[{i}].seat_type invalid")
            if s.get("status") not in SEAT_STATUSES:
                errs.append(f"seats[{i}].status invalid")
            if not isinstance(s.get("notes"), str):
                errs.append(f"seats[{i}].notes must be string")

    objs = d.get("objects")
    if not isinstance(objs, list):
        errs.append("objects must be array")
    else:
        for i, o in enumerate(objs):
            if not isinstance(o, dict):
                errs.append(f"objects[{i}] must be an object")
                continue
            if not _is_uuid(o.get("id")):
                errs.append(f"objects[{i}].id must be UUID")
            if o.get("type") not in OBJ_TYPES:
                errs.append(f"objects[{i}].type invalid")
            sec_id = o.get("section_id")
            if sec_id is not None and not _is_uuid(sec_id):
                errs.append(f"objects[{i}].section_id must be UUID or null")
            if (
                not _is_num(o.get("x"))
                or not _is_num(o.get("y"))
                or not _is_num(o.get("width"))
                or not _is_num(o.get("height"))
                or not _is_num(o.get("rotation_deg"))
            ):
                errs.append(f"objects[{i}] geometry must be numeric")
            if not isinstance(o.get("label"), str):
                errs.append(f"objects[{i}].label must be string")
            if not _is_int(o.get("z_index")):
                errs.append(f"objects[{i}].z_index must be integer")
            if not _is_uuid(o.get("layer_id")):
                errs.append(f"objects[{i}].layer_id must be UUID")

    layers = d.get("layers")
    if not isinstance(layers, list):
        errs.append("layers must be array")
    else:
        for i, l in enumerate(layers):
            if not isinstance(l, dict):
                errs.append(f"layers[{i}] must be an object")
                continue
            if not _is_uuid(l.get("id")):
                errs.append(f"layers[{i}].id must be UUID")
            if not isinstance(l.get("name"), str):
                errs.append(f"layers[{i}].name must be string")
            if not isinstance(l.get("visible"), bool) or not isinstance(l.get("locked"), bool):
                errs.append(f"layers[{i}].visible/locked must be boolean")
            if not _is_int(l.get("z_order")):
                errs.append(f"layers[{i}].z_order must be integer")

    return errs
