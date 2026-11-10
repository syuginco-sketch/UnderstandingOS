from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from runtime.exceptions import ClockError
from runtime.protocols import ShutdownPriority


class AuditClient:
    shutdown_priority = ShutdownPriority.AUDIT_PUBLISHER

    def __init__(self, event_bus: object, logger: object) -> None:
        self.event_bus = event_bus
        self.logger = logger
        self.state = "INITIALIZED"

    def initialize(self) -> None:
        self.state = "INITIALIZED"

    def start(self) -> None:
        self.state = "RUNNING"

    def publish(
        self,
        event_type: str,
        *,
        reason: str,
        payload: Mapping[str, Any] | None = None,
    ) -> bool:
        event_payload = dict(payload or {})
        event_payload["reason"] = reason
        try:
            event = self.event_bus.create_event(
                event_type,
                source="DiverGuard",
                payload=event_payload,
            )
            accepted = self.event_bus.publish_audit(event)
        except ClockError as error:
            self.logger.error("audit_publish", "WARNING", str(error), component="AuditClient")
            self._publish_failure_events(str(error))
            return False
        if not accepted:
            self.logger.error(
                "audit_publish",
                "WARNING",
                f"Audit event was not accepted: {event_type}",
                component="AuditClient",
            )
            self._publish_failure_events(f"Audit event was not accepted: {event_type}")
            return False
        return True

    def flush(self, timeout: float = 30.0) -> bool:
        try:
            return self.event_bus.flush_audit(timeout=timeout)
        except ClockError as error:
            self.logger.error("audit_flush", "WARNING", str(error), component="AuditClient")
            self._publish_failure_events(str(error))
            return False

    def stop(self, *, flush: bool = True) -> None:
        if flush:
            self.flush()
        self.state = "STOPPED"

    def health(self) -> dict[str, str]:
        return {"service": "AuditClient", "state": self.state}

    def _publish_failure_events(self, reason: str) -> None:
        for event_type in ("RuntimeWarning", "AuditPublishFailure"):
            try:
                event = self.event_bus.create_event(
                    event_type,
                    source="DiverGuard",
                    payload={"reason": reason},
                )
                self.event_bus.publish(event)
            except ClockError as error:
                self.logger.error(
                    "audit_failure_reporting",
                    "FAILED",
                    str(error),
                    component="AuditClient",
                )
