"""AI Layout Generator endpoints.

Backs the Kursi.io frontend's AI panel. Proxies Anthropic Claude with a
structured prompt, validates the model's output against the v2 layout schema,
and returns the validated JSON to the client.

Endpoints:
  POST /api/ai/generate-layout   — synthesize a layout from a textual brief (+ optional sketch)
  POST /api/ai/refine-layout     — apply a natural-language tweak to an existing layout

Both require a valid Auth0 JWT. Rate-limited to 10 requests per user per hour.

Error map:
  400  invalid input
  401  missing/invalid JWT (raised by the auth dependency)
  422  model output did not parse OR failed schema validation (excerpt included)
  429  rate limit exceeded (Retry-After header)
  502  Anthropic API itself failed (network, 5xx, etc.)
  503  ANTHROPIC_API_KEY not configured on the server
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from collections import defaultdict
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.ai_validator import validate_layout_v2, SCHEMA_VERSION, VENUE_TYPES
from app.auth import get_current_user
from app.models import User

logger = logging.getLogger("kursi.ai")

router = APIRouter(prefix="/api/ai", tags=["ai"])

# ── Configuration ───────────────────────────────────────────
ANTHROPIC_MODEL = "claude-sonnet-4-20250514"
MAX_OUTPUT_TOKENS = 16000
RATE_LIMIT = 10
RATE_WINDOW_SEC = 60 * 60
MAX_SKETCH_BYTES = 5 * 1024 * 1024  # 5 MB binary
# Base64 grows the byte size by 4/3, so a 5 MB binary ≈ 6.67M base64 characters.
MAX_SKETCH_B64_CHARS = int(MAX_SKETCH_BYTES * 4 / 3) + 64  # tiny slack for padding / data-URL prefix

# In-memory sliding-window rate limiter. Will move to Redis once we run >1 worker.
_rate_buckets: dict[str, list[float]] = defaultdict(list)


def _reset_rate_limits_for_testing() -> None:
    """Test helper — never called in production. Clears the bucket between tests."""
    _rate_buckets.clear()


def _check_rate_limit(user_id: str) -> tuple[bool, int]:
    """Returns (allowed, retry_after_seconds). Records the request when allowed."""
    now = time.time()
    bucket = _rate_buckets[user_id]
    cutoff = now - RATE_WINDOW_SEC
    if bucket and bucket[0] < cutoff:
        bucket[:] = [t for t in bucket if t >= cutoff]
    if len(bucket) >= RATE_LIMIT:
        oldest = bucket[0]
        retry_after = int(RATE_WINDOW_SEC - (now - oldest)) + 1
        return False, max(1, retry_after)
    bucket.append(now)
    return True, 0


def _require_api_key() -> str:
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise HTTPException(status_code=503, detail="AI features temporarily unavailable")
    return key


def _parse_sketch(blob: str) -> tuple[str, str]:
    """Accepts a raw base64 string or a data URL. Returns (base64_payload, media_type)."""
    if blob.startswith("data:"):
        m = re.match(r"^data:([^;]+);base64,(.+)$", blob, re.DOTALL)
        if not m:
            raise HTTPException(status_code=400, detail="sketch_base64 has invalid data URL format")
        return m.group(2), m.group(1)
    return blob, "image/png"


def _extract_json(text: str) -> dict | None:
    """Best-effort extraction of a single JSON object from the model's reply.

    Strips ```json fences and trailing prose. Returns None if no parseable
    object is found.
    """
    text = text.strip()
    if text.startswith("```"):
        nl = text.find("\n")
        if nl > 0:
            text = text[nl + 1:]
        if text.endswith("```"):
            text = text[:-3]
        text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(text)):
        c = text[i]
        if esc:
            esc = False
            continue
        if c == "\\":
            esc = True
            continue
        if c == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start:i + 1])
                except json.JSONDecodeError:
                    return None
    return None


# ── System prompts ──────────────────────────────────────────
_SCHEMA_BLURB = """SCHEMA (v2.0.0):
{
  "schema_version": "2.0.0",
  "venue":      {"id": uuid, "name": str, "type": "theater|cinema|stadium|opera|circle|conference|talk_show",
                 "dimensions": {"width_m": num, "depth_m": num},
                 "owner_id": uuid, "created_at": iso-8601, "updated_at": iso-8601},
  "sections":   [{"id": uuid, "name": slug, "label": str,
                  "origin": {"x": num, "y": num},
                  "bounds": {"width": num, "height": num},
                  "rotation_deg": num}],
  "seats":      [{"id": uuid, "section_id": uuid, "x": num, "y": num,
                  "row": str, "number": str, "category_id": uuid,
                  "price_override": null|num,
                  "accessibility": {"wheelchair": bool, "companion": bool},
                  "seat_type": "standard|recliner|box|premium",
                  "status": "available|reserved|blocked|sold",
                  "notes": str}],
  "categories": [{"id": uuid, "name": str, "color": "#RRGGBB", "default_price": num}],
  "objects":    [{"id": uuid, "type": "stage|screen|pitch|dj_booth|walkway|label",
                  "section_id": uuid|null, "x": num, "y": num,
                  "width": num, "height": num, "rotation_deg": num,
                  "label": str, "z_index": int, "layer_id": uuid}],
  "layers":     [{"id": uuid, "name": str, "visible": bool, "locked": bool, "z_order": int}]
}"""

_GEOMETRY_BLURB = """COORDINATES:
- All seat/section/object coordinates are in canvas units. 1 metre = 50 canvas units.
- A seat is a 14×14 cu square (~28 cm). Use ~22 cu between seat centres for comfortable spacing.
- Place the stage (or screen / pitch) near y=0 (front of house) unless the type calls for centre placement.

GEOMETRIC HEURISTICS BY VENUE TYPE:
- theater: raked rows facing a stage at front; optional centre aisle splits left/right; front rows = highest tier.
- cinema:  straight rows facing a screen; rows curve very slightly toward the screen for sightlines; two side aisles.
- stadium: curved tiers around a central pitch; typically four sections (N/E/S/W), each spanning ~90 degrees of arc.
- opera:   orchestra floor + at least one balcony tier + small side boxes; multi-section.
- circle:  full ring (in-the-round); 3–6 concentric rings of seats around a central stage circle.
- conference: classroom-style straight rows facing a stage; no curve, even spacing, single category is fine.
- talk_show: audience wraps ~270 degrees around a central host area; 3–5 rows.

CATEGORIES:
- Always include at least one category. Tiered venues (theater, opera, circle, talk_show) usually need 2–4
  (e.g. Bronze, Silver, Gold, VIP) with distinct hex colours and increasing prices.
- Single-tier venues (cinema, conference, stadium) can use one general category.

LAYERS:
- Always emit four layers in this order with these z_order values:
    stage (0), seating (10), aisles (20), labels (30)
- All visible:true, locked:false.

OBJECTS:
- Emit at least one stage/screen/pitch object near the appropriate edge (or centre, for circle/talk_show).
- Use the stage layer_id for stage/screen/pitch; use the aisles layer_id for walkway objects.

IDENTIFIERS:
- Every "id", "section_id", "category_id", "layer_id", "owner_id", "venue.id" must be a UUID v4 string.
- Timestamps (created_at, updated_at) are ISO-8601 (e.g. "2026-01-01T00:00:00Z").
- All section_id / category_id / layer_id references on seats and objects must point to a real id in the document.

SEAT NUMBERING:
- Cluster seats into rows by y-coordinate. Within each row, assign sequential numbers starting at 1.
- Row labels follow A, B, C, ... (then AA, AB, ...) from front to back unless the user specifies otherwise.
- Seat number and row are strings, NOT integers (e.g. "1", "12B", "AA").

SIZE TARGETS:
- Match the user's requested seat_count as closely as you reasonably can given the dimensions.
- Within ±5% of the target seat count is fine — exact matching is not required."""


SYSTEM_PROMPT_GENERATE = f"""You are a professional venue layout designer for Kursi.io, an event ticketing platform.

Your job: produce a complete JSON layout document matching the v2.0.0 schema, given a brief description of the venue.

CRITICAL OUTPUT RULES:
- Output ONLY a single JSON object. No prose, no explanation, no apologies, no markdown fences.
- Every field in the schema is required. Wrong types or missing fields will reject the output.
- Use real UUID v4 strings everywhere an id is needed.

{_SCHEMA_BLURB}

{_GEOMETRY_BLURB}
"""

SYSTEM_PROMPT_REFINE = f"""You are a professional venue layout designer for Kursi.io.

The user will give you (1) a current layout in JSON and (2) a natural-language refinement instruction.
Apply the refinement and return the FULL modified layout in the same v2.0.0 schema. Do not return a diff.

CRITICAL OUTPUT RULES:
- Output ONLY the modified JSON object. No prose, no diff, no markdown fences.
- Preserve every id that the user did not ask you to remove. New seats/sections you add need fresh UUIDs.
- Keep schema_version as "2.0.0". Update venue.updated_at to the current UTC time if you change anything.

{_SCHEMA_BLURB}

{_GEOMETRY_BLURB}
"""


# ── Request bodies ──────────────────────────────────────────
class _Dimensions(BaseModel):
    width: float = Field(gt=0, le=10000)
    depth: float = Field(gt=0, le=10000)


class _Constraints(BaseModel):
    aisle_count: int | None = None
    aisle_width: float | None = None
    accessible_seats: int | None = None
    balcony_tiers: int | None = None
    section_count: int | None = None
    custom_instructions: str | None = None


class GenerateLayoutRequest(BaseModel):
    venue_type: str
    dimensions: _Dimensions
    seat_count: int = Field(gt=0, le=100000)
    constraints: _Constraints | None = None
    sketch_base64: str | None = None


class RefineLayoutRequest(BaseModel):
    current_layout: dict[str, Any]
    refinement_prompt: str = Field(min_length=1, max_length=4000)


# ── Anthropic call ──────────────────────────────────────────
def _call_anthropic(system: str, user_content: Any) -> str:
    """Synchronous call to Claude. Returns the concatenated text response.

    Raises HTTPException(503) if the SDK is missing,
           HTTPException(502) for any other transport / API failure.
    """
    api_key = _require_api_key()
    try:
        from anthropic import Anthropic
    except ImportError:
        logger.error("anthropic SDK not installed")
        raise HTTPException(status_code=503, detail="AI features temporarily unavailable (SDK missing)")

    try:
        client = Anthropic(api_key=api_key)
        msg = client.messages.create(
            model=ANTHROPIC_MODEL,
            max_tokens=MAX_OUTPUT_TOKENS,
            system=system,
            messages=[{"role": "user", "content": user_content}],
        )
    except HTTPException:
        raise
    except Exception as e:
        # Don't leak the API key class or stack into the response.
        logger.error("anthropic call failed: %s: %s", type(e).__name__, e)
        raise HTTPException(status_code=502, detail=f"AI provider error: {type(e).__name__}")

    parts: list[str] = []
    for block in getattr(msg, "content", []) or []:
        btype = getattr(block, "type", None) or (block.get("type") if isinstance(block, dict) else None)
        if btype == "text":
            text = getattr(block, "text", None) or (block.get("text") if isinstance(block, dict) else "")
            if text:
                parts.append(text)
    return "".join(parts)


def _build_generate_prompt(p: GenerateLayoutRequest) -> str:
    lines = [
        f"Generate a layout for a {p.venue_type} venue.",
        f"Dimensions: {p.dimensions.width:.1f} m wide × {p.dimensions.depth:.1f} m deep.",
        f"Target seat count: approximately {p.seat_count} seats.",
    ]
    if p.constraints:
        c = p.constraints
        details: list[str] = []
        if c.aisle_count is not None:
            details.append(f"- {c.aisle_count} aisle(s)")
        if c.aisle_width is not None:
            details.append(f"- aisle width {c.aisle_width:.1f} m")
        if c.accessible_seats is not None:
            details.append(f"- include at least {c.accessible_seats} wheelchair-accessible seat(s) and matching companion seats")
        if c.balcony_tiers is not None:
            details.append(f"- {c.balcony_tiers} balcony tier(s)")
        if c.section_count is not None:
            details.append(f"- prefer {c.section_count} section(s)")
        if c.custom_instructions:
            details.append(f"- additional instructions: {c.custom_instructions}")
        if details:
            lines.append("")
            lines.append("Constraints:")
            lines.extend(details)
    return "\n".join(lines)


# ── Endpoints ───────────────────────────────────────────────
@router.post("/generate-layout")
def generate_layout(
    payload: GenerateLayoutRequest,
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    t0 = time.time()
    _require_api_key()

    # Extra semantic validation beyond pydantic.
    if payload.venue_type not in VENUE_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"venue_type must be one of: {', '.join(sorted(VENUE_TYPES))}",
        )

    sketch_present = bool(payload.sketch_base64)
    if sketch_present and len(payload.sketch_base64) > MAX_SKETCH_B64_CHARS:
        raise HTTPException(status_code=400, detail="sketch_base64 exceeds the 5 MB limit")

    allowed, retry_after = _check_rate_limit(user.auth0_sub)
    if not allowed:
        raise HTTPException(
            status_code=429,
            detail="Rate limit exceeded — try again later",
            headers={"Retry-After": str(retry_after)},
        )

    text_prompt = _build_generate_prompt(payload)
    if sketch_present:
        b64, media_type = _parse_sketch(payload.sketch_base64)
        user_content: Any = [
            {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": b64}},
            {"type": "text", "text": text_prompt + "\n\nA rough sketch is attached — use it as a shape guide."},
        ]
    else:
        user_content = text_prompt

    raw_text = _call_anthropic(SYSTEM_PROMPT_GENERATE, user_content)

    parsed = _extract_json(raw_text)
    if parsed is None:
        dt = time.time() - t0
        logger.warning(
            "generate user=%s type=%s seats=%d sketch=%s outcome=parse_fail dt=%.2fs",
            user.auth0_sub, payload.venue_type, payload.seat_count, sketch_present, dt,
        )
        raise HTTPException(
            status_code=422,
            detail={
                "message": "Model output could not be parsed as JSON",
                "raw_excerpt": raw_text[:500],
            },
        )

    errors = validate_layout_v2(parsed)
    if errors:
        dt = time.time() - t0
        logger.warning(
            "generate user=%s type=%s seats=%d sketch=%s outcome=validation_fail errors=%d dt=%.2fs",
            user.auth0_sub, payload.venue_type, payload.seat_count, sketch_present, len(errors), dt,
        )
        raise HTTPException(
            status_code=422,
            detail={
                "message": "Model output failed schema validation",
                "errors": errors[:10],
                "raw_excerpt": raw_text[:500],
            },
        )

    dt = time.time() - t0
    logger.info(
        "generate user=%s type=%s seats=%d sketch=%s outcome=ok seats_out=%d dt=%.2fs",
        user.auth0_sub, payload.venue_type, payload.seat_count, sketch_present,
        len(parsed.get("seats", [])), dt,
    )
    return parsed


@router.post("/refine-layout")
def refine_layout(
    payload: RefineLayoutRequest,
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    t0 = time.time()
    _require_api_key()

    if not isinstance(payload.current_layout, dict) or not payload.current_layout:
        raise HTTPException(status_code=400, detail="current_layout must be a non-empty JSON object")

    allowed, retry_after = _check_rate_limit(user.auth0_sub)
    if not allowed:
        raise HTTPException(
            status_code=429,
            detail="Rate limit exceeded — try again later",
            headers={"Retry-After": str(retry_after)},
        )

    # The current layout is sent as a JSON string inside the user message so the
    # model sees it as data, not as a system instruction.
    user_prompt = (
        f"Refinement instruction: {payload.refinement_prompt.strip()}\n\n"
        f"Current layout (JSON):\n{json.dumps(payload.current_layout)}"
    )

    raw_text = _call_anthropic(SYSTEM_PROMPT_REFINE, user_prompt)

    parsed = _extract_json(raw_text)
    if parsed is None:
        dt = time.time() - t0
        logger.warning("refine user=%s outcome=parse_fail dt=%.2fs", user.auth0_sub, dt)
        raise HTTPException(
            status_code=422,
            detail={
                "message": "Model output could not be parsed as JSON",
                "raw_excerpt": raw_text[:500],
            },
        )

    errors = validate_layout_v2(parsed)
    if errors:
        dt = time.time() - t0
        logger.warning(
            "refine user=%s outcome=validation_fail errors=%d dt=%.2fs",
            user.auth0_sub, len(errors), dt,
        )
        raise HTTPException(
            status_code=422,
            detail={
                "message": "Model output failed schema validation",
                "errors": errors[:10],
                "raw_excerpt": raw_text[:500],
            },
        )

    dt = time.time() - t0
    logger.info(
        "refine user=%s outcome=ok seats_out=%d dt=%.2fs",
        user.auth0_sub, len(parsed.get("seats", [])), dt,
    )
    return parsed
