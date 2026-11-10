from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from typing import TextIO


class _StructuredFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        fields = getattr(record, "clock_fields", {})
        return json.dumps(
            {
                "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                "component": fields.get("component", record.name),
                "action": fields.get("action", record.levelname.lower()),
                "state": fields.get("state", "UNKNOWN"),
                "message": record.getMessage(),
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )


class StructuredLogger:
    def __init__(
        self,
        name: str = "DIVERGUARD",
        level: str = "INFO",
        stream: TextIO | None = None,
    ) -> None:
        self._logger = logging.getLogger(name)
        self._logger.handlers.clear()
        self._logger.propagate = False
        self._handler = logging.StreamHandler(stream or sys.stderr)
        self._handler.setFormatter(_StructuredFormatter())
        self._logger.addHandler(self._handler)
        self.set_level(level)
        self.state = "INITIALIZED"

    def set_level(self, level: str) -> None:
        self._logger.setLevel(getattr(logging, level.upper()))

    def log(
        self,
        action: str,
        state: str,
        message: str,
        *,
        component: str = "DIVERGUARD",
        level: int = logging.INFO,
    ) -> None:
        self._logger.log(
            level,
            message,
            extra={"clock_fields": {"component": component, "action": action, "state": state}},
        )

    def error(self, action: str, state: str, message: str, *, component: str) -> None:
        self.log(action, state, message, component=component, level=logging.ERROR)

    def initialize(self) -> None:
        self.state = "INITIALIZED"

    def start(self) -> None:
        self.state = "RUNNING"
        self.log("startup", "RUNNING", "Structured logging started", component="StructuredLogger")

    def stop(self) -> None:
        self.log("shutdown", "STOPPING", "Structured logging stopped", component="StructuredLogger")
        self.state = "STOPPED"

    def health(self) -> dict[str, str]:
        return {"service": "StructuredLogger", "state": self.state}

    def close(self) -> None:
        self._logger.removeHandler(self._handler)
        self._handler.close()
