"""Append-only audit writes (spec 6).

Every state change the Engine makes on someone's behalf leaves a row here. The
helper exists so callers cannot forget to make `data` JSON-safe: datetimes are
serialised to ISO-8601 and nothing else is allowed to reach a JSONB column.

Audit rows are written INSIDE the caller's transaction, so an audited action and
its audit row commit or vanish together. There is no "log even if it failed"
path: a rolled-back action did not happen.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Any

from sqlalchemy.orm import Session

from app.engine_models import AuditLog

# ── Action vocabulary (spec 6: "Phase 1 writes"). Stable strings. ───────────
ACTION_ORG_PROVISIONED = "organization.provisioned"
ACTION_LAYOUT_FROZEN = "layout_version.frozen"
ACTION_PERFORMANCE_PUBLISHED = "performance.published"
ACTION_ORDER_CREATED = "order.created"
ACTION_ORDER_EXTENDED = "order.extended"
ACTION_ORDER_CANCELLED = "order.cancelled"
ACTION_ORDER_COMPLETED = "order.completed"
ACTION_ORDER_EXPIRED = "order.expired"
ACTION_TICKET_ISSUED = "ticket.issued"
ACTION_TICKET_CHECKED_IN = "ticket.checked_in"
ACTION_TICKET_CREDENTIAL_ROTATED = "ticket.credential_rotated"
ACTION_TICKET_CANCELLED = "ticket.cancelled"
ACTION_TICKET_REFUNDED = "ticket.refunded"


def json_safe(value: Any) -> Any:
    """Coerce a value into something a JSONB column will accept."""
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [json_safe(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def record_audit(
    session: Session,
    *,
    organization_id: int,
    action: str,
    entity_type: str | None = None,
    entity_id: int | None = None,
    data: dict[str, Any] | None = None,
    actor_user_id: int | None = None,
    actor_api_key_id: int | None = None,
    occurred_at: datetime | None = None,
) -> AuditLog:
    row = AuditLog(
        organization_id=organization_id,
        actor_user_id=actor_user_id,
        actor_api_key_id=actor_api_key_id,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        data=json_safe(data or {}),
    )
    if occurred_at is not None:
        row.occurred_at = occurred_at
    session.add(row)
    return row
