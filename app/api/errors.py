"""engine_services errors -> HTTP, in one place (Phase 1c).

ONE ENVELOPE, EVERYWHERE UNDER /v1
----------------------------------
Every /v1 failure is the same JSON shape, whatever produced it::

    {"error": "<stable machine code>",
     "message": "<a human sentence>",
     "detail":  { ... optional, machine-readable ... }}

`SeatsUnavailable` is the one documented variation: it carries `conflicts`
instead of `detail`, because a checkout UI must repaint exactly the seats it
lost and needs one entry per offending seat. That payload is passed through
VERBATIM from `engine_services.errors.SeatsUnavailable.as_dict()` - the handler
adds nothing and renames nothing, so the service layer's contract and the wire
format are literally the same object.

`error` is the contract. It is never localised, never reworded and never
reused for a different condition; `message` is for humans and may change.

WHY THE HANDLERS REFUSE TO TOUCH LEGACY PATHS
---------------------------------------------
The frozen marketplace is served by the same FastAPI app, and its clients parse
FastAPI's default `{"detail": ...}` body. An exception handler is registered
app-wide, not per-router, so installing one that reshapes `HTTPException` would
silently change every legacy 401/403/404 response. Each handler below therefore
checks the request path first and re-raises into the default handler for
anything that is not /v1. That is why `install_error_handlers` takes the app and
captures the originals rather than simply decorating.

NOTHING LEAKS
-------------
No handler renders a traceback, a SQL statement or a driver message into a
response. The database errors that ARE mapped (the frozen-layout trigger, the
integer-money type guard) are recognised by matching their own text, and the
text that goes back to the client is written here, not copied from the driver.
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exception_handlers import (
    http_exception_handler as default_http_exception_handler,
)
from fastapi.exception_handlers import (
    request_validation_exception_handler as default_validation_handler,
)
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import DBAPIError, StatementError
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.engine_services.errors import EngineServiceError

logger = logging.getLogger("kursi.api")

API_PREFIX = "/v1"

#: Substrings the Phase 1a freeze guard puts in its own error text. Postgres
#: raises it from a PL/pgSQL trigger, SQLite from `RAISE(ABORT, ...)`, so the
#: exception CLASS differs by dialect and the message is the only portable
#: signal. Both strings are ours - they are written in the migration.
_FROZEN_LAYOUT_MARKERS = (
    "immutable once frozen",
    "cannot leave the frozen state",
)
#: Written by `engine_models.MinorAmount`, which refuses a float or Decimal at
#: bind time rather than letting the driver round it.
_MINOR_UNIT_MARKERS = ("minor units",)

INTEGER_MONEY_MESSAGE = (
    "monetary values must be sent as an integer number of minor units, never as "
    "a decimal or float: KWD 5.500 is 5500, USD 12.99 is 1299. See the "
    "amount_minor convention in the API description."
)


class ApiError(Exception):
    """A failure raised by the /v1 layer itself, in the same shape as the
    service layer's errors so one handler can serve both."""

    code = "api_error"
    http_status = 400

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        http_status: int | None = None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code
        if http_status is not None:
            self.http_status = http_status
        self.detail = detail or {}

    def as_dict(self) -> dict[str, Any]:
        body: dict[str, Any] = {"error": self.code, "message": self.message}
        if self.detail:
            body["detail"] = self.detail
        return body


class Unauthenticated(ApiError):
    """No credential, or one we cannot verify."""

    code = "unauthenticated"
    http_status = 401


class Forbidden(ApiError):
    """A verified caller who may not do this."""

    code = "forbidden"
    http_status = 403


class Conflict(ApiError):
    code = "conflict"
    http_status = 409


def _is_api(request: Request) -> bool:
    return request.url.path.startswith(API_PREFIX)


def _envelope(status_code: int, body: dict[str, Any]) -> JSONResponse:
    return JSONResponse(status_code=status_code, content=body)


def _looks_like(exc: BaseException, markers: tuple[str, ...]) -> bool:
    text = str(exc).lower()
    return any(marker in text for marker in markers)


#: HTTP status -> the stable `error` code used when the raiser did not pick one.
#: Only for `HTTPException`s that reach /v1 from shared code (chiefly the Auth0
#: dependency's 401s); everything /v1 raises itself carries its own code.
_STATUS_CODES: dict[int, str] = {
    400: "invalid_request",
    401: "unauthenticated",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    409: "conflict",
    422: "invalid_request",
    429: "rate_limited",
    500: "internal_error",
    503: "unavailable",
}


def install_error_handlers(app: FastAPI) -> None:
    """Register the /v1 exception handlers on `app`.

    Every handler falls through to FastAPI's default for non-/v1 paths, so the
    legacy routers' response bodies are bit-for-bit what they were.
    """

    @app.exception_handler(EngineServiceError)
    async def _engine_error(request: Request, exc: EngineServiceError):
        # `http_status` is declared by the service layer itself (409 for the
        # seat conflicts, 404 for NotFound, 422 for validation), and `as_dict`
        # is its own payload - including `SeatsUnavailable.conflicts`, which
        # goes out untouched.
        return _envelope(exc.http_status, exc.as_dict())

    @app.exception_handler(ApiError)
    async def _api_error(request: Request, exc: ApiError):
        return _envelope(exc.http_status, exc.as_dict())

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException):
        if not _is_api(request):
            return await default_http_exception_handler(request, exc)
        detail = exc.detail
        body: dict[str, Any] = {
            "error": _STATUS_CODES.get(exc.status_code, "error"),
            "message": detail if isinstance(detail, str) else "request failed",
        }
        if not isinstance(detail, str) and detail is not None:
            body["detail"] = detail
        return JSONResponse(
            status_code=exc.status_code, content=body, headers=getattr(exc, "headers", None)
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError):
        if not _is_api(request):
            return await default_validation_handler(request, exc)
        errors = [
            {
                "location": list(err.get("loc", ())),
                "message": err.get("msg"),
                "type": err.get("type"),
            }
            for err in exc.errors()
        ]
        # A float where an integer minor amount was expected is common enough,
        # and confusing enough, to earn its own sentence instead of pydantic's
        # "Input should be a valid integer".
        money = any(
            err["type"] in ("int_type", "int_from_float", "int_parsing")
            and any("amount_minor" in str(p) or "minor" in str(p) for p in err["location"])
            for err in errors
        )
        return _envelope(
            422,
            {
                "error": "invalid_request",
                "message": INTEGER_MONEY_MESSAGE if money else "the request body or parameters are not valid",
                "detail": {"errors": errors},
            },
        )

    @app.exception_handler(StatementError)
    async def _statement_error(request: Request, exc: StatementError):
        if not _is_api(request):
            raise exc
        if _looks_like(exc, _MINOR_UNIT_MARKERS):
            # `MinorAmount` rejected a float/Decimal at bind time. The client
            # sent money in the wrong unit; that is a 422, not a 500.
            return _envelope(
                422,
                {"error": "invalid_request", "message": INTEGER_MONEY_MESSAGE},
            )
        return await _database_error(request, exc)

    @app.exception_handler(DBAPIError)
    async def _database_error(request: Request, exc: DBAPIError):
        if not _is_api(request):
            raise exc
        if _looks_like(exc, _FROZEN_LAYOUT_MARKERS):
            # The Phase 1a freeze guard fired. This is the DB enforcing spec 2,
            # and it means the caller tried to edit sealed inventory.
            return _envelope(
                409,
                {
                    "error": "layout_frozen",
                    "message": (
                        "this layout version is frozen and can no longer be "
                        "edited. A version is frozen the first time a "
                        "performance materialises inventory from it, and is "
                        "never unfrozen; create the next draft version instead."
                    ),
                },
            )
        # Anything else is ours to fix, not the caller's. Log it with the
        # traceback; return nothing that describes our schema.
        logger.exception("unhandled database error on %s", request.url.path)
        return _envelope(
            500,
            {"error": "internal_error", "message": "the request could not be completed"},
        )
