from __future__ import annotations

import logging

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError

log = logging.getLogger(__name__)


class NotFoundError(Exception):
    def __init__(self, resource: str, identifier: object):
        self.resource = resource
        self.identifier = identifier
        super().__init__(f"{resource} {identifier!r} not found")


class ConflictError(Exception):
    def __init__(self, message: str):
        self.message = message
        super().__init__(message)


class UnauthorizedError(Exception):
    def __init__(self, message: str = "Unauthorized"):
        self.message = message
        super().__init__(message)


def _body(detail: str, request_id: str | None = None, **extra) -> dict:
    """Uniform error body. `request_id` lets operators correlate a client-facing failure with logs."""
    body: dict = {"detail": detail}
    if request_id:
        body["request_id"] = request_id
    body.update(extra)
    return body


def _rid(request: Request) -> str | None:
    return getattr(request.state, "request_id", None)


def install_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(NotFoundError)
    async def _not_found(request: Request, exc: NotFoundError):
        return JSONResponse(
            status_code=status.HTTP_404_NOT_FOUND,
            content=_body(str(exc), _rid(request), resource=exc.resource),
        )

    @app.exception_handler(ConflictError)
    async def _conflict(request: Request, exc: ConflictError):
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT, content=_body(exc.message, _rid(request))
        )

    @app.exception_handler(UnauthorizedError)
    async def _unauthorized(request: Request, exc: UnauthorizedError):
        return JSONResponse(
            status_code=status.HTTP_401_UNAUTHORIZED,
            content=_body(exc.message, _rid(request)),
            headers={"WWW-Authenticate": "Bearer"},
        )

    @app.exception_handler(IntegrityError)
    async def _integrity(request: Request, exc: IntegrityError):
        msg = str(getattr(exc.orig, "args", [""])[0] or exc.orig or exc)
        log.warning("integrity error: %s", msg, extra={"request_id": _rid(request)})
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content=_body(
                "Constraint violation", _rid(request), db_error=msg.splitlines()[0][:300]
            ),
        )

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError):
        # Default FastAPI handler returns a "detail" with a list of pydantic errors; mirror that
        # shape but also attach our request_id for log correlation.
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content=_body("Validation error", _rid(request), errors=jsonable_encoder(exc.errors())),
        )

    @app.exception_handler(HTTPException)
    async def _http(request: Request, exc: HTTPException):
        return JSONResponse(
            status_code=exc.status_code,
            content=_body(exc.detail, _rid(request)),
            headers=getattr(exc, "headers", None),
        )

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception):
        # Anything not caught above is treated as 500. We do NOT leak the exception message —
        # the request_id lets ops correlate this response with the structured-log stack trace.
        rid = _rid(request)
        log.exception("unhandled exception", extra={"request_id": rid})
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content=_body("Internal server error", rid),
        )
