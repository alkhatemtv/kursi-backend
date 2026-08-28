"""limit/offset paging, with caps, shared by every /v1 list endpoint.

A list endpoint that can return an entire organisation's inventory in one
response is a denial-of-service primitive pointed at ourselves, so `limit` is
capped rather than merely defaulted. `total` is returned alongside the page
because an SDK cannot render "page 3 of 12" without it, and the count is over an
indexed tenant column in every case here.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Generic, TypeVar

from fastapi import Depends, Query
from pydantic import BaseModel
from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session

DEFAULT_LIMIT = 50
MAX_LIMIT = 200

T = TypeVar("T")


@dataclass(frozen=True)
class Page:
    limit: int
    offset: int


def page_params(
    limit: int = Query(
        DEFAULT_LIMIT,
        ge=1,
        le=MAX_LIMIT,
        description=f"Maximum rows to return (1-{MAX_LIMIT}).",
    ),
    offset: int = Query(0, ge=0, description="Rows to skip."),
) -> Page:
    return Page(limit=limit, offset=offset)


PageDep = Depends(page_params)


class Paginated(BaseModel, Generic[T]):
    """The envelope every /v1 list endpoint returns."""

    items: list[T]
    total: int
    limit: int
    offset: int


def paginate(
    session: Session, statement: Select, page: Page, *, count_over: Any
) -> tuple[list[Any], int]:
    """Run `statement` for one page and count the whole set.

    `count_over` is the column to count - passed explicitly rather than derived,
    because counting `SELECT *` of a statement carrying ORDER BY and joins is
    both slower and easy to get subtly wrong.
    """
    total = session.execute(
        select(func.count()).select_from(
            statement.with_only_columns(count_over).order_by(None).subquery()
        )
    ).scalar_one()
    rows = list(
        session.execute(statement.limit(page.limit).offset(page.offset)).scalars()
    )
    return rows, int(total)
