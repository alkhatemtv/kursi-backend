"""Events and the performances underneath them (spec 3)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Path, Query, Response, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.auth import ANY_MEMBER, EVENT_WRITE, Access, Principal, org_from_path
from app.api.errors import Conflict
from app.api.keys import SCOPE_READ, SCOPE_WRITE
from app.api.pagination import Page, Paginated, page_params, paginate
from app.api.v1.lookups import get_event, get_layout_version, get_venue
from app.api.v1.schemas import (
    EventCreate,
    EventOut,
    EventPatch,
    PerformanceCreate,
    PerformanceOut,
)
from app.database import get_db
from app.engine_models import EngineEvent, Performance
from app.engine_services.audit import record_audit
from app.engine_services.uow import unit_of_work

router = APIRouter(prefix="/orgs/{org_id}", tags=["events"])

_READ = Access(org_from_path, roles=ANY_MEMBER, scope=SCOPE_READ)
_WRITE = Access(org_from_path, roles=EVENT_WRITE, scope=SCOPE_WRITE)


@router.get(
    "/events",
    response_model=Paginated[EventOut],
    summary="List events",
    description=(
        "Newest first. `status` filters to one lifecycle state.\n\n"
        "**Auth:** " + _READ.describe()
    ),
)
def list_events(
    org_id: int = Path(...),
    status_filter: str | None = Query(
        None, alias="status", description="Restrict to one event status."
    ),
    principal: Principal = Depends(_READ),
    page: Page = Depends(page_params),
    db: Session = Depends(get_db),
) -> Paginated[EventOut]:
    statement = select(EngineEvent).where(
        EngineEvent.organization_id == principal.organization_id
    )
    if status_filter:
        statement = statement.where(EngineEvent.status == status_filter)
    statement = statement.order_by(EngineEvent.id.desc())
    rows, total = paginate(db, statement, page, count_over=EngineEvent.id)
    return Paginated[EventOut](
        items=[EventOut.model_validate(r) for r in rows],
        total=total,
        limit=page.limit,
        offset=page.offset,
    )


@router.post(
    "/events",
    response_model=EventOut,
    status_code=status.HTTP_201_CREATED,
    summary="Create an event",
    description=(
        "An event is the show; its dated showings are performances. Seating is "
        "owned by the performance, not by the event, so an event can run in two "
        "venues without anything being duplicated.\n\n"
        "Created as `draft` unless you say otherwise.\n\n"
        "**Auth:** " + _WRITE.describe()
    ),
)
def create_event(
    body: EventCreate,
    org_id: int = Path(...),
    principal: Principal = Depends(_WRITE),
    db: Session = Depends(get_db),
) -> EventOut:
    payload = body.model_dump()
    if payload.get("venue_id") is not None:
        get_venue(db, principal.organization_id, payload["venue_id"])
    payload["cast"] = payload.get("cast") or {}
    payload["policies"] = payload.get("policies") or {}

    with unit_of_work(db):
        event = EngineEvent(organization_id=principal.organization_id, **payload)
        db.add(event)
        db.flush()
        record_audit(
            db,
            organization_id=principal.organization_id,
            action="event.created",
            entity_type="event",
            entity_id=event.id,
            actor_user_id=principal.actor_user_id,
            actor_api_key_id=principal.actor_api_key_id,
            data={"title": event.title, "status": event.status},
        )
    db.refresh(event)
    return EventOut.model_validate(event)


@router.get(
    "/events/{event_id}",
    response_model=EventOut,
    summary="Read an event",
    description="**Auth:** " + _READ.describe(),
)
def read_event(
    org_id: int = Path(...),
    event_id: int = Path(...),
    principal: Principal = Depends(_READ),
    db: Session = Depends(get_db),
) -> EventOut:
    return EventOut.model_validate(get_event(db, principal.organization_id, event_id))


@router.patch(
    "/events/{event_id}",
    response_model=EventOut,
    summary="Update an event",
    description="**Auth:** " + _WRITE.describe(),
)
def update_event(
    body: EventPatch,
    org_id: int = Path(...),
    event_id: int = Path(...),
    principal: Principal = Depends(_WRITE),
    db: Session = Depends(get_db),
) -> EventOut:
    event = get_event(db, principal.organization_id, event_id)
    changes = body.model_dump(exclude_unset=True)
    if changes.get("venue_id") is not None:
        get_venue(db, principal.organization_id, changes["venue_id"])
    with unit_of_work(db):
        for field, value in changes.items():
            setattr(event, field, value)
        if changes:
            record_audit(
                db,
                organization_id=principal.organization_id,
                action="event.updated",
                entity_type="event",
                entity_id=event.id,
                actor_user_id=principal.actor_user_id,
                actor_api_key_id=principal.actor_api_key_id,
                data={"fields": sorted(changes)},
            )
    db.refresh(event)
    return EventOut.model_validate(event)


@router.delete(
    "/events/{event_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete an event",
    description=(
        "Refused with 409 once the event has performances - those own "
        "inventory, and inventory may own tickets. Cancel or archive the event "
        "instead; both are `PATCH .../events/{id}` with a `status`.\n\n"
        "**Auth:** " + _WRITE.describe()
    ),
)
def delete_event(
    org_id: int = Path(...),
    event_id: int = Path(...),
    principal: Principal = Depends(_WRITE),
    db: Session = Depends(get_db),
) -> Response:
    event = get_event(db, principal.organization_id, event_id)
    performances = db.execute(
        select(func.count())
        .select_from(Performance)
        .where(Performance.event_id == event.id)
    ).scalar_one()
    if performances:
        raise Conflict(
            f"event {event_id} still has {performances} performance(s) and "
            f"cannot be deleted; cancel or archive it instead",
            code="event_in_use",
            detail={"event_id": event_id, "performances": int(performances)},
        )
    with unit_of_work(db):
        db.delete(event)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ── Performances of an event ────────────────────────────────────────────────
@router.get(
    "/events/{event_id}/performances",
    response_model=Paginated[PerformanceOut],
    tags=["performances"],
    summary="List an event's performances",
    description="Earliest first.\n\n**Auth:** " + _READ.describe(),
)
def list_performances(
    org_id: int = Path(...),
    event_id: int = Path(...),
    principal: Principal = Depends(_READ),
    page: Page = Depends(page_params),
    db: Session = Depends(get_db),
) -> Paginated[PerformanceOut]:
    event = get_event(db, principal.organization_id, event_id)
    statement = (
        select(Performance)
        .where(Performance.event_id == event.id)
        .order_by(Performance.starts_at)
    )
    rows, total = paginate(db, statement, page, count_over=Performance.id)
    return Paginated[PerformanceOut](
        items=[PerformanceOut.model_validate(r) for r in rows],
        total=total,
        limit=page.limit,
        offset=page.offset,
    )


@router.post(
    "/events/{event_id}/performances",
    response_model=PerformanceOut,
    status_code=status.HTTP_201_CREATED,
    tags=["performances"],
    summary="Create a performance",
    description=(
        "Binds a dated showing to the layout version it will sell. Created as "
        "`draft` and holding no inventory: seats are materialised by "
        "`POST /v1/performances/{id}/publish`, which is also what freezes the "
        "layout version.\n\n"
        "The layout version must belong to this organization; it may be a draft "
        "or already frozen, and sharing one frozen version across many "
        "performances is normal.\n\n"
        "**Auth:** " + _WRITE.describe()
    ),
)
def create_performance(
    body: PerformanceCreate,
    org_id: int = Path(...),
    event_id: int = Path(...),
    principal: Principal = Depends(_WRITE),
    db: Session = Depends(get_db),
) -> PerformanceOut:
    event = get_event(db, principal.organization_id, event_id)
    version = get_layout_version(db, principal.organization_id, body.layout_version_id)

    with unit_of_work(db):
        performance = Performance(
            event_id=event.id,
            layout_version_id=version.id,
            status="draft",
            **body.model_dump(exclude={"layout_version_id"}),
        )
        db.add(performance)
        db.flush()
        record_audit(
            db,
            organization_id=principal.organization_id,
            action="performance.created",
            entity_type="performance",
            entity_id=performance.id,
            actor_user_id=principal.actor_user_id,
            actor_api_key_id=principal.actor_api_key_id,
            data={
                "event_id": event.id,
                "layout_version_id": version.id,
                "starts_at": performance.starts_at,
            },
        )
    db.refresh(performance)
    return PerformanceOut.model_validate(performance)
