from __future__ import annotations

import json
import logging
import time
import uuid
from collections.abc import Iterable
from contextvars import ContextVar

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.types import ASGIApp, Message, Receive, Scope, Send

# Exposed to the JSON formatter so every log line in a request gets the correlation id,
# even when the call site doesn't have the Request object handy.
request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)

log = logging.getLogger("http")


class RequestIDMiddleware(BaseHTTPMiddleware):
    """Attach a request id to every request; log one structured line per request.

    Accepts an inbound `X-Request-ID` so it's possible to trace through a gateway/sidecar.
    Generates one otherwise. Always echoes back in the response header.
    """

    async def dispatch(self, request: Request, call_next):
        rid = request.headers.get("x-request-id") or str(uuid.uuid4())
        request.state.request_id = rid
        token = request_id_var.set(rid)
        start = time.perf_counter()
        try:
            response = await call_next(request)
        finally:
            elapsed_ms = (time.perf_counter() - start) * 1000.0
            request_id_var.reset(token)

        response.headers["X-Request-ID"] = rid
        log.info(
            "request",
            extra={
                "request_id": rid,
                "method": request.method,
                "path": request.url.path,
                "status_code": response.status_code,
                "duration_ms": round(elapsed_ms, 2),
            },
        )
        return response


class MaxBodySizeMiddleware:
    """Pure-ASGI middleware that streams the request body and aborts with 413 once the
    cumulative byte count exceeds `max_bytes`. Scoped to a configured set of path prefixes
    so we don't pay the wrapping cost on every request.

    Why ASGI (not BaseHTTPMiddleware): BaseHTTPMiddleware materializes the body before
    handing it down. We need to intercept *during* `receive()` so we can reject before
    the full body is buffered. nginx's `client_max_body_size` is the right primary gate
    in production — this is defense-in-depth for environments without a reverse proxy
    (local dev, tests, direct uvicorn).
    """

    def __init__(self, app: ASGIApp, max_bytes: int, path_prefixes: Iterable[str]) -> None:
        self.app = app
        self.max_bytes = max_bytes
        self.path_prefixes = tuple(path_prefixes)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not scope["path"].startswith(self.path_prefixes):
            await self.app(scope, receive, send)
            return

        # Fast reject on declared Content-Length, before we touch the body at all.
        for name, value in scope.get("headers", []):
            if name == b"content-length":
                try:
                    if int(value) > self.max_bytes:
                        await _send_413(send, self.max_bytes)
                        return
                except ValueError:
                    pass
                break

        received = 0

        async def limited_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    raise _BodyTooLarge(self.max_bytes)
            return message

        try:
            await self.app(scope, limited_receive, send)
        except _BodyTooLarge as e:
            await _send_413(send, e.limit)


class _BodyTooLarge(Exception):
    def __init__(self, limit: int) -> None:
        self.limit = limit


async def _send_413(send: Send, limit: int) -> None:
    body = json.dumps({"detail": f"Upload exceeds {limit} bytes"}).encode()
    await send({
        "type": "http.response.start",
        "status": 413,
        "headers": [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode()),
        ],
    })
    await send({"type": "http.response.body", "body": body})
