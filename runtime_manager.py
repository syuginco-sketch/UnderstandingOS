from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterable, Mapping
from enum import Enum
from typing import Any

from app.storage_manager import StorageManager
from runtime.exceptions import LifecycleError
from runtime.protocols import (
    AuditPublisherProtocol,
    LifecycleService,
    LoggerProtocol,
    RuntimeEventTransportProtocol,
    ShutdownPriority,
)
from runtime.recovery_coordinator import RecoveryCoordinator


RUNTIME_STATES = (
    "INITIALIZED",
    "STARTING",
    "RUNNING",
    "DEGRADED",
    "STOPPING",
    "STOPPED",
    "FAILED",
    "RECOVERING",
)


class ShutdownOutcome(str, Enum):
    NOT_STARTED = "NOT_STARTED"
    IN_PROGRESS = "IN_PROGRESS"
    STOPPED = "STOPPED"
    STOPPED_WITH_WARNINGS = "STOPPED_WITH_WARNINGS"
    FAILED = "FAILED"


ALLOWED_TRANSITIONS = {
    "INITIALIZED": {"STARTING", "STOPPING", "FAILED"},
    "STARTING": {"RUNNING", "DEGRADED", "STOPPING", "FAILED"},
    "RUNNING": {"DEGRADED", "RECOVERING", "STOPPING", "FAILED"},
    "DEGRADED": {"RECOVERING", "STOPPING", "FAILED"},
    "STOPPING": {"STOPPED", "FAILED"},
    "STOPPED": {"STARTING", "RECOVERING", "FAILED"},
    "FAILED": {"RECOVERING", "STOPPING"},
    "RECOVERING": {"STARTING", "RUNNING", "DEGRADED", "STOPPING", "FAILED"},
}


class RuntimeManager:
    def __init__(
        self,
        logger: LoggerProtocol,
        audit_client: AuditPublisherProtocol,
        event_bus: RuntimeEventTransportProtocol,
        storage_manager: StorageManager,
        *,
        sleeper: Callable[[float], None] = time.sleep,
        recovery_coordinator: RecoveryCoordinator | None = None,
    ) -> None:
        self.logger = logger
        self.audit_client = audit_client
        self.event_bus = event_bus
        self.storage_manager = storage_manager
        self.state = "INITIALIZED"
        self._services: list[LifecycleService] = []
        self.registered_service_names: list[str] = []
        self._state_lock = threading.RLock()
        self._recovery_state_lock = threading.RLock()
        self._active_recoveries: set[str] = set()
        self._recovery_failures: set[str] = set()
        self.shutdown_outcome = ShutdownOutcome.NOT_STARTED
        self.shutdown_warnings: list[str] = []
        self.recovery_coordinator = recovery_coordinator or RecoveryCoordinator(
            logger,
            audit_client,
            self,
            self._record_recovery,
            sleeper=sleeper,
        )

    def initialize(self) -> None:
        previous = self.storage_manager.restore_json("runtime", "runtime_metadata.json")
        if previous is not None:
            self.storage_manager.write_metadata(
                "runtime",
                "recovery_metadata.json",
                {
                    "previous_runtime_state": previous.get("state", "UNKNOWN"),
                    "recovery_required": previous.get("state") not in {"STOPPED", "FAILED"},
                    "recovery_count": int(previous.get("recovery_count", 0)),
                },
            )
        self.state = "INITIALIZED"

    def start(self, services: Iterable[LifecycleService]) -> None:
        if self.state not in {"INITIALIZED", "STOPPED", "RECOVERING"}:
            raise LifecycleError(f"Runtime cannot start from state {self.state}")
        self._services = list(services)
        self.registered_service_names = list(
            dict.fromkeys(
                [*self.registered_service_names, *(_service_name(service) for service in self._services)]
            )
        )
        self._transition("STARTING", "Runtime startup began")
        attempted: list[LifecycleService] = []
        try:
            for service in self._services:
                attempted.append(service)
                self._start_service(service)
            self._transition("RUNNING", "All required runtime services are running")
            self._persist_state()
            self.audit_client.publish("RuntimeStarted", reason="All required services reached RUNNING")
        except Exception as error:
            self._transition("FAILED", f"Runtime startup failed: {error}")
            self._publish_audit("ServiceFailed", f"Runtime startup failed: {error}")
            self.logger.error("runtime_start", "FAILED", str(error), component="RuntimeManager")
            cleanup_failures = self._stop_services(reversed(attempted))
            message = f"Runtime startup failed: {error}"
            if cleanup_failures:
                message += "; " + "; ".join(cleanup_failures)
            raise LifecycleError(message) from error

    def stop(self) -> None:
        with self._recovery_state_lock:
            if self.state == "STOPPED":
                return
            if self._active_recoveries:
                raise LifecycleError(
                    "Runtime shutdown cannot begin while service recovery is active: "
                    f"{sorted(self._active_recoveries)}"
                )
            self.shutdown_outcome = ShutdownOutcome.IN_PROGRESS
            self.shutdown_warnings = []
            self._transition("STOPPING", "Runtime shutdown began")
        self._log_shutdown_step(
            "EventBus",
            "shutdown_gate_started",
            "STARTING",
            "Stopping new runtime event submissions",
        )
        self.event_bus.stop_accepting_runtime()
        self._log_shutdown_step(
            "EventBus",
            "shutdown_gate_completed",
            "COMPLETED",
            "Runtime event submissions are stopped",
        )

        self._log_shutdown_step(
            "Executor",
            "shutdown_started",
            "STARTING",
            "Checking for registered executor services",
        )
        self._log_shutdown_step(
            "Executor",
            "shutdown_completed",
            "COMPLETED",
            "No executor service is registered; no shutdown action required",
        )

        self._log_shutdown_step(
            "EventBus",
            "shutdown_event_started",
            "STARTING",
            "Publishing RuntimeStopped before draining runtime events",
        )
        self.audit_client.publish("RuntimeStopped", reason="Graceful runtime shutdown")
        self._log_shutdown_step(
            "EventBus",
            "shutdown_event_completed",
            "COMPLETED",
            "RuntimeStopped was published; shutdown continues",
        )

        shutdown_deadline = time.monotonic() + 30.0
        self._log_shutdown_step(
            "AuditClient",
            "shutdown_flush_started",
            "STARTING",
            "Flushing pending audit events",
        )
        audit_flushed = self.audit_client.flush(timeout=30.0)
        self._log_shutdown_step(
            "AuditClient",
            "shutdown_flush_completed",
            "COMPLETED" if audit_flushed else "WARNING",
            f"Audit flush {'completed' if audit_flushed else 'did not complete'}",
        )
        if not audit_flushed:
            self._record_shutdown_warning("Audit queue did not flush during shutdown")
            self.logger.error(
                "audit_flush",
                "WARNING",
                "Audit queue did not flush within 30 seconds; continuing graceful shutdown",
                component="RuntimeManager",
            )
            self._publish_runtime_event(
                "RuntimeWarning", "Audit queue flush failed during shutdown"
            )
            self._publish_runtime_event(
                "AuditPublishFailure", "Audit queue flush failed during shutdown"
            )

        self._log_shutdown_step(
            "EventBus",
            "shutdown_queue_started",
            "STARTING",
            "Discarding remaining transient runtime events",
        )
        self.event_bus.discard_runtime_queue()
        self._log_shutdown_step(
            "EventBus",
            "shutdown_queue_completed",
            "COMPLETED",
            "Transient runtime event queue drained",
        )
        failure_messages: list[str] = []
        try:
            self._persist_state()
        except Exception as error:
            self._record_shutdown_warning(f"Unable to persist shutdown metadata: {error}")
            self.logger.error("metadata_persist", "WARNING", str(error), component="RuntimeManager")

        services = self._shutdown_services()
        pre_audit_services = [
            service
            for service in services
            if _shutdown_priority(service) < ShutdownPriority.AUDIT_PUBLISHER
        ]
        failure_messages.extend(self._stop_services(pre_audit_services))
        if failure_messages:
            self._transition("FAILED", "One or more required services failed to stop")
        else:
            self._transition("STOPPED", "Runtime stopped cleanly")
        self.shutdown_outcome = (
            ShutdownOutcome.FAILED if failure_messages
            else ShutdownOutcome.STOPPED_WITH_WARNINGS if self.shutdown_warnings
            else ShutdownOutcome.STOPPED
        )
        try:
            self._persist_state()
        except Exception as error:
            self._record_shutdown_warning(f"Unable to persist final runtime metadata: {error}")
            if not failure_messages:
                self.shutdown_outcome = ShutdownOutcome.STOPPED_WITH_WARNINGS
            self.logger.error("metadata_persist", "WARNING", str(error), component="RuntimeManager")

        if self.audit_client.state == "RUNNING":
            remaining = max(0.0, shutdown_deadline - time.monotonic())
            flushed = self.audit_client.flush(timeout=remaining)
            if not flushed:
                self._record_shutdown_warning("Audit queue did not flush during final shutdown")
                self.logger.error(
                    "audit_flush",
                    "WARNING",
                    "Audit queue did not flush within 30 seconds; continuing graceful shutdown",
                    component="RuntimeManager",
                )
                self._publish_runtime_event(
                    "RuntimeWarning", "Audit queue flush failed during final shutdown"
                )
                self._publish_runtime_event(
                    "AuditPublishFailure", "Audit queue flush failed during final shutdown"
                )
                try:
                    self._persist_state()
                except Exception as error:
                    self._record_shutdown_warning(
                        f"Unable to persist final shutdown warnings: {error}"
                    )
                    self.logger.error(
                        "metadata_persist",
                        "WARNING",
                        str(error),
                        component="RuntimeManager",
                    )
        post_audit_services = [
            service
            for service in services
            if _shutdown_priority(service) >= ShutdownPriority.AUDIT_PUBLISHER
        ]
        failure_messages.extend(self._stop_services(post_audit_services))
        if failure_messages:
            self._transition("FAILED", "One or more shutdown-priority services failed to stop")
            self.shutdown_outcome = ShutdownOutcome.FAILED
            try:
                self._persist_state()
            except Exception as error:
                self._record_shutdown_warning(
                    f"Unable to persist failed shutdown metadata: {error}"
                )
                self.logger.error(
                    "metadata_persist", "FAILED", str(error), component="RuntimeManager"
                )
        if failure_messages:
            raise LifecycleError("; ".join(failure_messages))

    def _shutdown_services(self) -> list[LifecycleService]:
        services: list[LifecycleService] = []
        seen: set[int] = set()
        for service in (*self._services, self.audit_client, self.event_bus):
            if id(service) not in seen:
                services.append(service)
                seen.add(id(service))
        return sorted(services, key=_shutdown_priority)

    def _record_shutdown_warning(self, message: str) -> None:
        self.shutdown_warnings.append(message)
        if self.shutdown_outcome not in {
            ShutdownOutcome.IN_PROGRESS,
            ShutdownOutcome.FAILED,
        }:
            self.shutdown_outcome = ShutdownOutcome.STOPPED_WITH_WARNINGS

    def _log_shutdown_step(
        self,
        component: str,
        action: str,
        state: str,
        message: str,
    ) -> None:
        self.logger.log(action, state, message, component=component)

    def restart(self) -> None:
        self.stop()
        self._transition("RECOVERING", "Runtime restart requested")
        self.start(self._services)

    def recovery_started(self, service_name: str, reason: str) -> None:
        with self._recovery_state_lock:
            if self.state in {"STOPPING", "STOPPED"}:
                raise LifecycleError(f"Cannot recover {service_name} while runtime is {self.state}")
            self._active_recoveries.add(service_name)
            if self.state in {"RUNNING", "STARTING"}:
                self._transition("DEGRADED", f"{service_name} failed: {reason}")
            if self.state != "RECOVERING":
                self._transition("RECOVERING", f"Recovering {service_name}")

    def recovery_succeeded(self, service_name: str) -> None:
        with self._recovery_state_lock:
            self._active_recoveries.discard(service_name)
            self._recovery_failures.discard(service_name)
            if not self._active_recoveries and self.state == "RECOVERING":
                if self._recovery_failures:
                    self._transition(
                        "FAILED",
                        f"Recovery failed for {sorted(self._recovery_failures)}",
                    )
                else:
                    self._transition("RUNNING", "All active service recoveries succeeded")

    def recovery_failed(self, service_name: str, reason: str) -> None:
        with self._recovery_state_lock:
            self._active_recoveries.discard(service_name)
            self._recovery_failures.add(service_name)
            if not self._active_recoveries and self.state != "FAILED":
                self._transition("FAILED", reason)

    def health(self) -> dict[str, str]:
        return {"service": "RuntimeManager", "state": self.state}

    def _start_service(self, service: LifecycleService) -> None:
        try:
            service_state = _reported_state(service)
            if service_state == "RUNNING":
                self._verify_running(service)
                return
            if service_state == "INITIALIZED":
                initialize = getattr(service, "initialize", None)
                if callable(initialize):
                    initialize()
            service.start()
            self._verify_running(service)
        except Exception as error:
            self.recovery_coordinator.recover(service, str(error))

    @staticmethod
    def _verify_running(service: LifecycleService) -> None:
        health = service.health()
        if not isinstance(health, Mapping) or health.get("state") != "RUNNING":
            raise LifecycleError(
                f"{_service_name(service)} did not report RUNNING after startup"
            )

    def _stop_services(self, services: Iterable[LifecycleService]) -> list[str]:
        failures = []
        for service in services:
            stop = getattr(service, "stop", None)
            if not callable(stop):
                continue
            name = _service_name(service)
            self.logger.log(
                "shutdown_started",
                "STARTING",
                f"Stopping {name}",
                component=name,
            )
            try:
                stop()
                self.logger.log(
                    "shutdown_completed",
                    "COMPLETED",
                    f"{name} stopped",
                    component=name,
                )
            except Exception as error:
                failures.append(f"Unable to stop {name}: {error}")
                self.logger.error("service_stop", "FAILED", str(error), component=name)
        return failures

    def _persist_state(self) -> None:
        health = self._find_service("HealthMonitor")
        health_result = health.health() if health is not None else {}
        self.storage_manager.write_metadata(
            "runtime",
            "runtime_metadata.json",
            {
                "service": "RuntimeManager",
                "state": self.state,
                "version": "1.0",
                "registered_services": self.registered_service_names,
                "shutdown_outcome": self.shutdown_outcome.value,
                "shutdown_warnings": list(self.shutdown_warnings),
            },
        )
        self.storage_manager.write_metadata(
            "metadata",
            "configuration.json",
            {"configuration": self._find_service_config()},
        )
        self.storage_manager.write_metadata(
            "runtime",
            "recovery_metadata.json",
            {
                "recovery_required": self.state not in {"STOPPED", "FAILED"},
                "recovery_count": self._recovery_count(),
                "last_runtime_state": self.state,
            },
        )
        if health_result:
            self.storage_manager.write_metadata(
                "runtime", "health_status.json", health_result
            )

    def _find_service(self, name: str) -> LifecycleService | None:
        return next((service for service in self._services if _service_name(service) == name), None)

    def _find_service_config(self) -> dict[str, Any]:
        service = self._find_service("ConfigurationLoader")
        configuration = getattr(service, "configuration", {})
        return configuration if isinstance(configuration, dict) else {}

    def _recovery_count(self) -> int:
        previous = self.storage_manager.read_json("runtime", "recovery_metadata.json") or {}
        return int(previous.get("recovery_count", 0))

    def _record_recovery(self, service_name: str) -> None:
        previous = self.storage_manager.read_json("runtime", "recovery_metadata.json") or {}
        self.storage_manager.write_metadata(
            "runtime",
            "recovery_metadata.json",
            {
                "recovery_required": False,
                "recovery_count": int(previous.get("recovery_count", 0)) + 1,
                "last_recovered_service": service_name,
            },
        )

    def _publish_audit(self, event_type: str, reason: str) -> None:
        if self.audit_client.state == "RUNNING":
            self.audit_client.publish(event_type, reason=reason)

    def _transition(self, state: str, reason: str) -> None:
        with self._state_lock:
            if state not in RUNTIME_STATES:
                raise LifecycleError(f"Unsupported runtime state: {state}")
            previous = self.state
            if state != previous and state not in ALLOWED_TRANSITIONS[previous]:
                raise LifecycleError(f"Invalid runtime transition: {previous} -> {state}")
            self.state = state
            self.logger.log(
                "runtime_state",
                state,
                f"{previous} -> {state}: {reason}",
                component="RuntimeManager",
            )

    def _publish_runtime_event(self, event_type: str, reason: str) -> None:
        try:
            event = self.event_bus.create_event(
                event_type,
                source="RuntimeManager",
                payload={"reason": reason},
            )
            self.event_bus.publish_shutdown_event(event)
        except Exception as error:
            self.logger.error(
                "runtime_event",
                "WARNING",
                str(error),
                component="RuntimeManager",
            )


def _service_name(service: LifecycleService) -> str:
    explicit = getattr(service, "service_name", None)
    if isinstance(explicit, str):
        return explicit
    return type(service).__name__


def _reported_state(service: LifecycleService) -> str:
    health = service.health()
    if not isinstance(health, Mapping):
        raise LifecycleError(f"{_service_name(service)} returned an invalid health result")
    return str(health.get("state", "UNKNOWN"))


def _shutdown_priority(service: LifecycleService) -> ShutdownPriority:
    return ShutdownPriority(service.shutdown_priority)
