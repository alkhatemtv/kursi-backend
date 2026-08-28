"""Venues, seating layouts and their immutable versions (spec 2)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Path, Response, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.auth import (
    ANY_MEMBER,
    USER_ONLY,
    VENUE_WRITE,
    Access,
    Principal,
    org_from_path,
)
from app.api.errors import Conflict
from app.api.keys import SCOPE_READ, SCOPE_WRITE
from app.api.pagination import Page, Paginated, page_params, paginate
from app.api.v1.lookups import get_layout, get_layout_version, get_venue
from app.api.v1.schemas import (
    LayoutCreate,
    LayoutDataUpdate,
    LayoutOut,
    LayoutVersionCreate,
    LayoutVersionDetail,
    LayoutVersionOut,
    VenueCreate,
    VenueOut,
    VenuePatch,
)
from app.database import get_db
from app.engine_models import LayoutVersion, Venue, VenueLayout
from app.engine_services.audit import record_audit
from app.engine_services.errors import ValidationError
from app.engine_services.layout import parse_layout
from app.engine_services.uow import unit_of_work

router = APIRouter(prefix="/orgs/{org_id}", tags=["venues"])

_READ = Access(org_from_path, roles=ANY_MEMBER, scope=SCOPE_READ)
_WRITE = Access(org_from_path, roles=VENUE_WRITE, scope=SCOPE_WRITE)
#: `layout_versions.created_by_user_id` is NOT NULL and points at `users`: the
#: schema insists a human authored a seating document. An API key is not one, so
#: this endpoint says so up front rather than accepting the call and failing it
#: halfway through.
_AUTHOR = Access(org_from_path, roles=VENUE_WRITE, accept=USER_ONLY)


# ── Venues ──────────────────────────────────────────────────────────────────
@router.get(
    "/venues",
    response_model=Paginated[VenueOut],
    summary="List venues",
    description="**Auth:** " + _READ.describe(),
)
def list_venues(
    org_id: int = Path(...),
    principal: Principal = Depends(_READ),
    page: Page = Depends(page_params),
    db: Session = Depends(get_db),
) -> Paginated[VenueOut]:
    statement = (
        select(Venue)
        .where(Venue.organization_id == principal.organization_id)
        .order_by(Venue.id)
    )
    rows, total = paginate(db, statement, page, count_over=Venue.id)
    return Paginated[VenueOut](
        items=[VenueOut.model_validate(r) for r in rows],
        total=total,
        limit=page.limit,
        offset=page.offset,
    )


@router.post(
    "/venues",
    response_model=VenueOut,
    status_code=status.HTTP_201_CREATED,
    summary="Create a venue",
    description=(
        "A venue is the physical place. Its seating lives in layouts underneath "
        "it, so a venue with several halls is one venue and several layouts.\n\n"
        "**Auth:** " + _WRITE.describe()
    ),
)
def create_venue(
    body: VenueCreate,
    org_id: int = Path(...),
    principal: Principal = Depends(_WRITE),
    db: Session = Depends(get_db),
) -> VenueOut:
    with unit_of_work(db):
        venue = Venue(organization_id=principal.organization_id, **body.model_dump())
        db.add(venue)
        db.flush()
        record_audit(
            db,
            organization_id=principal.organization_id,
            action="venue.created",
            entity_type="venue",
            entity_id=venue.id,
            actor_user_id=principal.actor_user_id,
            actor_api_key_id=principal.actor_api_key_id,
            data={"name": venue.name},
        )
    db.refresh(venue)
    return VenueOut.model_validate(venue)


@router.get(
    "/venues/{venue_id}",
    response_model=VenueOut,
    summary="Read a venue",
    description="**Auth:** " + _READ.describe(),
)
def read_venue(
    org_id: int = Path(...),
    venue_id: int = Path(...),
    principal: Principal = Depends(_READ),
    db: Session = Depends(get_db),
) -> VenueOut:
    return VenueOut.model_validate(
        get_venue(db, principal.organization_id, venue_id)
    )


@router.patch(
    "/venues/{venue_id}",
    response_model=VenueOut,
    summary="Update a venue",
    description="**Auth:** " + _WRITE.describe(),
)
def update_venue(
    body: VenuePatch,
    org_id: int = Path(...),
    venue_id: int = Path(...),
    principal: Principal = Depends(_WRITE),
    db: Session = Depends(get_db),
) -> VenueOut:
    venue = get_venue(db, principal.organization_id, venue_id)
    changes = body.model_dump(exclude_unset=True)
    with unit_of_work(db):
        for field, value in changes.items():
            setattr(venue, field, value)
    db.refresh(venue)
    return VenueOut.model_validate(venue)


@router.delete(
    "/venues/{venue_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a venue",
    description=(
        "Refused with 409 while any layout still hangs off the venue. Deleting "
        "a venue out from under live inventory would orphan the seating a sold "
        "ticket refers to, so the foreign keys are RESTRICT and this endpoint "
        "says so in words rather than letting a constraint surface as a 500.\n\n"
        "**Auth:** " + _WRITE.describe()
    ),
)
def delete_venue(
    org_id: int = Path(...),
    venue_id: int = Path(...),
    principal: Principal = Depends(_WRITE),
    db: Session = Depends(get_db),
) -> Response:
    venue = get_venue(db, principal.organization_id, venue_id)
    layouts = db.execute(
        select(func.count())
        .select_from(VenueLayout)
        .where(VenueLayout.venue_id == venue.id)
    ).scalar_one()
    if layouts:
        raise Conflict(
            f"venue {venue_id} still has {layouts} layout(s) and cannot be deleted",
            code="venue_in_use",
            detail={"venue_id": venue_id, "layouts": int(layouts)},
        )
    with unit_of_work(db):
        db.delete(venue)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ── Layouts ─────────────────────────────────────────────────────────────────
@router.get(
    "/venues/{venue_id}/layouts",
    response_model=Paginated[LayoutOut],
    tags=["layouts"],
    summary="List a venue's layouts",
    description="**Auth:** " + _READ.describe(),
)
def list_layouts(
    org_id: int = Path(...),
    venue_id: int = Path(...),
    principal: Principal = Depends(_READ),
    page: Page = Depends(page_params),
    db: Session = Depends(get_db),
) -> Paginated[LayoutOut]:
    venue = get_venue(db, principal.organization_id, venue_id)
    statement = (
        select(VenueLayout)
        .where(VenueLayout.venue_id == venue.id)
        .order_by(VenueLayout.id)
    )
    rows, total = paginate(db, statement, page, count_over=VenueLayout.id)
    return Paginated[LayoutOut](
        items=[LayoutOut.model_validate(r) for r in rows],
        total=total,
        limit=page.limit,
        offset=page.offset,
    )


@router.post(
    "/venues/{venue_id}/layouts",
    response_model=LayoutOut,
    status_code=status.HTTP_201_CREATED,
    tags=["layouts"],
    summary="Create a layout",
    description=(
        "A layout is a named seating arrangement of a venue - \"Main Hall — "
        "Full\", \"Main Hall — Half House\". It holds no seats itself: seats "
        "live in its versions, which is what makes an arrangement editable "
        "without disturbing performances already selling against an earlier "
        "one.\n\n"
        "**Auth:** " + _WRITE.describe()
    ),
)
def create_layout(
    body: LayoutCreate,
    org_id: int = Path(...),
    venue_id: int = Path(...),
    principal: Principal = Depends(_WRITE),
    db: Session = Depends(get_db),
) -> LayoutOut:
    venue = get_venue(db, principal.organization_id, venue_id)
    with unit_of_work(db):
        layout = VenueLayout(venue_id=venue.id, **body.model_dump())
        db.add(layout)
        db.flush()
    db.refresh(layout)
    return LayoutOut.model_validate(layout)


# ── Layout versions ─────────────────────────────────────────────────────────
@router.get(
    "/layouts/{layout_id}/versions",
    response_model=Paginated[LayoutVersionOut],
    tags=["layouts"],
    summary="List a layout's versions",
    description=(
        "Newest first. `layout_data` is omitted from the list - it is the "
        "largest field in the schema and a 5,000-seat document has no business "
        "in a page of twenty. Fetch one version to get it.\n\n"
        "**Auth:** " + _READ.describe()
    ),
)
def list_layout_versions(
    org_id: int = Path(...),
    layout_id: int = Path(...),
    principal: Principal = Depends(_READ),
    page: Page = Depends(page_params),
    db: Session = Depends(get_db),
) -> Paginated[LayoutVersionOut]:
    layout = get_layout(db, principal.organization_id, layout_id)
    statement = (
        select(LayoutVersion)
        .where(LayoutVersion.venue_layout_id == layout.id)
        .order_by(LayoutVersion.version_number.desc())
    )
    rows, total = paginate(db, statement, page, count_over=LayoutVersion.id)
    return Paginated[LayoutVersionOut](
        items=[LayoutVersionOut.model_validate(r) for r in rows],
        total=total,
        limit=page.limit,
        offset=page.offset,
    )


@router.post(
    "/layouts/{layout_id}/versions",
    response_model=LayoutVersionDetail,
    status_code=status.HTTP_201_CREATED,
    tags=["layouts"],
    summary="Create a draft layout version",
    description=(
        "Versions are numbered from 1 and are always created as drafts. A "
        "version is frozen automatically the first time a performance "
        "materialises inventory from it, and is never unfrozen - so **editing a "
        "frozen layout means creating the next version**, which is what this "
        "endpoint is for. Omit `layout_data` to start from a copy of the "
        "layout's most recent version.\n\n"
        "The document is validated here, while the version is still a draft "
        "that can be fixed: unknown category references, duplicate seat ids and "
        "a missing `seats` array are all 422 now rather than a failed publish "
        "later.\n\n"
        "**Auth:** " + _WRITE.describe()
    ),
)
def create_layout_version(
    body: LayoutVersionCreate,
    org_id: int = Path(...),
    layout_id: int = Path(...),
    principal: Principal = Depends(_AUTHOR),
    db: Session = Depends(get_db),
) -> LayoutVersionDetail:
    layout = get_layout(db, principal.organization_id, layout_id)

    latest = db.execute(
        select(LayoutVersion)
        .where(LayoutVersion.venue_layout_id == layout.id)
        .order_by(LayoutVersion.version_number.desc())
        .limit(1)
    ).scalar_one_or_none()

    layout_data = body.layout_data
    if layout_data is None:
        if latest is None:
            raise ValidationError(
                "this layout has no versions yet, so there is nothing to copy; "
                "send layout_data",
                layout_id=layout_id,
            )
        layout_data = latest.layout_data

    # Validate before writing. `parse_layout` raises LayoutInvalid (422) listing
    # every problem at once, so an authoring tool can show them all.
    parse_layout(layout_data)

    next_number = (latest.version_number + 1) if latest else 1
    try:
        with unit_of_work(db):
            version = LayoutVersion(
                venue_layout_id=layout.id,
                version_number=next_number,
                status="draft",
                created_by_user_id=principal.actor_user_id,
                layout_data=layout_data,
            )
            db.add(version)
            db.flush()
    except IntegrityError:
        # UNIQUE(venue_layout_id, version_number): someone else created version
        # n while we were deciding it was ours. Retryable, and the client gets
        # the next number by simply asking again.
        db.rollback()
        raise Conflict(
            f"version {next_number} of this layout was created concurrently; retry",
            code="layout_version_conflict",
            detail={"layout_id": layout_id, "version_number": next_number},
        ) from None

    db.refresh(version)
    return LayoutVersionDetail.model_validate(version)


@router.get(
    "/layout-versions/{version_id}",
    response_model=LayoutVersionDetail,
    tags=["layouts"],
    summary="Read a layout version, with its document",
    description="**Auth:** " + _READ.describe(),
)
def read_layout_version(
    org_id: int = Path(...),
    version_id: int = Path(...),
    principal: Principal = Depends(_READ),
    db: Session = Depends(get_db),
) -> LayoutVersionDetail:
    return LayoutVersionDetail.model_validate(
        get_layout_version(db, principal.organization_id, version_id)
    )


@router.put(
    "/layout-versions/{version_id}/layout-data",
    response_model=LayoutVersionDetail,
    tags=["layouts"],
    summary="Replace a draft version's document",
    description=(
        "Wholesale replacement, drafts only.\n\n"
        "**A frozen version returns 409 `layout_frozen`.** That refusal is not "
        "a check in this endpoint - it is a database trigger, so it holds for "
        "every writer including a migration or a psql session, not only for "
        "callers who came through the API. Freezing happens the first time a "
        "performance materialises inventory from the version, and is one-way. "
        "Create the next version instead.\n\n"
        "**Auth:** " + _WRITE.describe()
    ),
)
def replace_layout_data(
    body: LayoutDataUpdate,
    org_id: int = Path(...),
    version_id: int = Path(...),
    principal: Principal = Depends(_WRITE),
    db: Session = Depends(get_db),
) -> LayoutVersionDetail:
    version = get_layout_version(db, principal.organization_id, version_id)
    parse_layout(body.layout_data)
    was_frozen = version.status == "frozen"

    with unit_of_work(db):
        # Deliberately NOT guarded by an `if version.status == "frozen"` here:
        # the database is the arbiter of this invariant, and letting the UPDATE
        # reach it is what proves the guard is actually installed on whichever
        # database this process is talking to. The handler in `api.errors` turns
        # the trigger's rejection into 409 `layout_frozen`.
        version.layout_data = body.layout_data
        db.flush()

    if was_frozen:
        # The write survived, so the payload was byte-identical to what is
        # already there and the trigger's "IS DISTINCT FROM" let it pass as a
        # no-op. The caller still asked to edit a frozen version; answer the
        # same way we answer everyone else who asks.
        raise Conflict(
            "this layout version is frozen and can no longer be edited; create "
            "the next draft version instead",
            code="layout_frozen",
            detail={"layout_version_id": version_id},
        )

    db.refresh(version)
    return LayoutVersionDetail.model_validate(version)
