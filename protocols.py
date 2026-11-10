from __future__ import annotations

from collections.abc import Mapping
from enum import IntEnum
from typing import Any, Protocol


class ShutdownPriority(IntEnum):
    HEALTH_MONITOR = 10
    DEFAULT = 50
    AUDIT_PUBLISHER = 90
    EVENT_TRANSPORT = 100


class LifecycleService(Protocol):
    state: str
    shutdown_priority: ShutdownPriority

    def initialize(self) -> None: ...

    def start(self) -> None: ...

    def stop(self) -> None: ...

    def health(self) -> Mapping[str, Any]: ...


class LoggerProtocol(Protocol):
    def log(
        self,
        action: str,
        state: str,
        message: str,
        *,
        component: str = "DIVERGUARD",
        level: int = 20,
    ) -> None: ...

    def error(self, action: str, state: str, message: str, *, component: str) -> None: ...


class AuditPublisherProtocol(LifecycleService, Protocol):
    def publish(self, event_type: str, *, reason: str, payload: Mapping[str, Any] | None = None) -> bool: ...

    def flush(self, timeout: float = 30.0) -> bool: ...

    def stop(self, *, flush: bool = True) -> None: ...


class RuntimeEventTransportProtocol(LifecycleService, Protocol):
    def stop_accepting_runtime(self) -> None: ...

    def discard_runtime_queue(self) -> None: ...

    def create_event(
        self,
        event_type: str,
        source: str,
        payload: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]: ...

    def publish_shutdown_event(self, event: Mapping[str, Any]) -> bool: ...

    def stop(self) -> None: ...

    def health(self) -> Mapping[str, Any]: ...


class RecoveryStatePort(Protocol):
    @property
    def state(self) -> str: ...

    def recovery_started(self, service_name: str, reason: str) -> None: ...

    def recovery_succeeded(self, service_name: str) -> None: ...

    def recovery_failed(self, service_name: str, reason: str) -> None: ...
