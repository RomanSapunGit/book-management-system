from __future__ import annotations

import logging

from pythonjsonlogger.json import JsonFormatter

from app.config import settings
from app.middleware import request_id_var


class RequestIdFilter(logging.Filter):
    """Inject the current request id (if any) into every log record."""

    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "request_id") or not record.request_id:
            rid = request_id_var.get()
            if rid:
                record.request_id = rid
        return True


def configure_logging() -> None:
    root = logging.getLogger()
    # Replace any handlers uvicorn/pytest installed so our format wins.
    for h in list(root.handlers):
        root.removeHandler(h)

    handler = logging.StreamHandler()
    handler.addFilter(RequestIdFilter())

    if settings.log_format.lower() == "json":
        fmt = JsonFormatter(
            "%(asctime)s %(levelname)s %(name)s %(message)s %(request_id)s",
            rename_fields={"asctime": "ts", "levelname": "level"},
        )
    else:
        fmt = logging.Formatter(
            "%(asctime)s %(levelname)s %(name)s %(message)s [rid=%(request_id)s]"
        )

    handler.setFormatter(fmt)
    root.addHandler(handler)
    root.setLevel(settings.log_level.upper())

    # Quiet uvicorn's access logger — our middleware already logs one line per request.
    logging.getLogger("uvicorn.access").disabled = True
