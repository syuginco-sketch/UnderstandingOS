from __future__ import annotations

import threading
import time
from collections.abc import Callable, Mapping

from runtime.exceptions import LifecycleError
from runtime.protocols import (
    AuditPublisherProtocol,
    LifecycleService,
    LoggerProtocol,
    RecoveryStatePort,
)


RECOVERY_BACKOFF_SECONDS = (30, 60, 120)


class RecoveryCoordinator:
    def __init__(
        self,
        logger: LoggerProtocol,
        audit_publisher: AuditPublisherProtocol,
        runtime_state: RecoveryStatePort,
        record_recovery: Callable[[str], None],
        *,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self.logger = logger
        self.audit_publisher = audit_publisher
        self.runtime_state = runtime_state
        self.record_recovery = record_recovery
        self.sleeper = sleeper
        self._locks_guard = threading.Lock()
        self._service_locks: dict[str, threading.Lock] = {}

    def recover(self, service: LifecycleService, reason: str) -> None:
        service_name = _service_name(service)
        lock = self._service_lock(service_name)
        if not lock.acquire(blocking=False):
            raise LifecycleError(f"Recovery is already in progress for {service_name}")
        try:
            self.runtime_state.recovery_started(service_name, reason)
            self._publish_audit("ServiceFailed", f"{service_name}: {reason}")
            self.logger.error("service_failure", "DEGRADED", reason, component=service_name)
            for retry_number, delay in enumerate(RECOVERY_BACKOFF_SECONDS, start=1):
                self.sleeper(delay)
                try:
                    self._restart_service(service)
                    self._verify_running(service)
                except Exception as error:
                    self.logger.error(
                        "service_recovery",
                        "RECOVERING",
                        f"Retry {retry_number} failed for {service_name}: {error}",
                        component="RecoveryCoordinator",
                    )
                    continue
                self.record_recovery(service_name)
                self.runtime_state.recovery_succeeded(service_name)
                self._publish_audit("ServiceRecovered", f"{service_name} recovered")
                return

            reason = f"{service_name} recovery retries exhausted"
            self.runtime_state.recovery_failed(service_name, reason)
            self._publish_audit("ServiceFailed", reason)
            raise LifecycleError(f"Recovery retries exhausted for {service_name}")
        except Exception as error:
            if self.runtime_state.state == "RECOVERING":
                self.runtime_state.recovery_failed(service_name, str(error))
            raise
        finally:
            lock.release()

    def _service_lock(self, service_name: str) -> threading.Lock:
        with self._locks_guard:
            return self._service_locks.setdefault(service_name, threading.Lock())

    @staticmethod
    def _restart_service(service: LifecycleService) -> None:
        restart = getattr(service, "restart", None)
        if callable(restart):
            restart()
            return
        service.stop()
        service.initialize()
        service.start()

    @staticmethod
    def _verify_running(service: LifecycleService) -> None:
        health = service.health()
        if not isinstance(health, Mapping) or health.get("state") != "RUNNING":
            raise LifecycleError(
                f"{_service_name(service)} did not report RUNNING after recovery"
            )

    def _publish_audit(self, event_type: str, reason: str) -> None:
        if self.audit_publisher.state == "RUNNING":
            self.audit_publisher.publish(event_type, reason=reason)


def _service_name(service: LifecycleService) -> str:
    explicit = getattr(service, "service_name", None)
    if isinstance(explicit, str):
        return explicit
    return type(service).__name__
