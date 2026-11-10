from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from typing import Any

import psutil

from app.storage_manager import StorageManager
from runtime.exceptions import ClockError
from runtime.protocols import ShutdownPriority


HEALTH_LEVELS = ("HEALTHY", "WARNING", "CRITICAL")


class HealthMonitor:
    shutdown_priority = ShutdownPriority.HEALTH_MONITOR

    def __init__(
        self,
        event_bus: object,
        logger: object,
        storage_manager: StorageManager,
        *,
        interval_seconds: int = 30,
        process: Any | None = None,
        monotonic: Any = time.monotonic,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError("interval_seconds must be greater than 0")
        self.event_bus = event_bus
        self.logger = logger
        self.storage_manager = storage_manager
        self.interval_seconds = interval_seconds
        self.process = process or psutil.Process()
        self._monotonic = monotonic
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_heartbeat: float | None = None
        self._last_heartbeat_timestamp: str | None = None
        self._last_status: str | None = None
        self._last_result: dict[str, Any] = {
            "service": "HealthMonitor",
            "state": "INITIALIZED",
            "status": "WARNING",
            "reasons": ["HeartbeatUnavailable"],
        }
        self._metadata = self.storage_manager.restore_json("runtime", "health_metadata.json") or {}
        self.state = "INITIALIZED"

    def initialize(self) -> None:
        self.state = "INITIALIZED"

    def start(self) -> None:
        self._stop_event.clear()
        self.state = "RUNNING"
        self._record_heartbeat()
        self._thread = threading.Thread(
            target=self._heartbeat_loop,
            name="DIVERGUARD-HealthMonitor",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self.logger.log(
            "heartbeat_shutdown_started",
            "STOPPING",
            "Signaling heartbeat thread to stop",
            component="HealthMonitor",
        )
        self._stop_event.set()
        if self._thread is not None:
            self.logger.log(
                "heartbeat_join_started",
                "STOPPING",
                f"Waiting for heartbeat thread {self._thread.name}",
                component="HealthMonitor",
            )
            self._thread.join(timeout=self.interval_seconds + 1)
            if self._thread.is_alive():
                self.state = "FAILED"
                self.logger.error(
                    "heartbeat_shutdown",
                    "FAILED",
                    f"Heartbeat thread did not stop: {self._thread.name}",
                    component="HealthMonitor",
                )
                raise RuntimeError("Health monitor heartbeat thread did not stop")
            self.logger.log(
                "heartbeat_join_completed",
                "COMPLETED",
                f"Heartbeat thread stopped: {self._thread.name}",
                component="HealthMonitor",
            )
        self._thread = None
        self.state = "STOPPED"
        self.logger.log(
            "heartbeat_shutdown_completed",
            "STOPPED",
            "HealthMonitor heartbeat shutdown completed",
            component="HealthMonitor",
        )

    def check_health(self, *, service_failure: bool = False) -> dict[str, Any]:
        reasons: list[str] = []
        levels: list[str] = []
        try:
            cpu_percent = float(self.process.cpu_percent(interval=None))
            memory_percent = float(self.process.memory_percent())
        except psutil.Error as error:
            cpu_percent = None
            memory_percent = None
            service_failure = True
            reasons.append(f"ResourceMetricFailure:{error}")

        if cpu_percent is not None:
            if cpu_percent >= 90:
                levels.append("CRITICAL")
                reasons.append("ProcessCpuAtLeast90Percent")
            elif cpu_percent >= 70:
                levels.append("WARNING")
                reasons.append("ProcessCpuAtLeast70Percent")
        if memory_percent is not None:
            if memory_percent >= 90:
                levels.append("CRITICAL")
                reasons.append("ProcessMemoryAtLeast90Percent")
            elif memory_percent >= 70:
                levels.append("WARNING")
                reasons.append("ProcessMemoryAtLeast70Percent")

        delay = None if self._last_heartbeat is None else self._monotonic() - self._last_heartbeat
        if delay is None:
            levels.append("WARNING")
            reasons.append("HeartbeatUnavailable")
        elif delay > 120:
            levels.append("CRITICAL")
            reasons.append("HeartbeatDelayOver120Seconds")
        elif delay > 60:
            levels.append("WARNING")
            reasons.append("HeartbeatDelayOver60Seconds")

        if service_failure:
            levels.append("CRITICAL")
            reasons.append("ServiceFailure")

        status = max(levels, key=HEALTH_LEVELS.index) if levels else "HEALTHY"
        result = {
            "service": "HealthMonitor",
            "state": self.state,
            "status": status,
            "cpu_percent": cpu_percent,
            "memory_percent": memory_percent,
            "heartbeat_delay_seconds": delay,
            "heartbeat_timestamp": self._last_heartbeat_timestamp,
            "reasons": reasons,
        }
        self._record_status_change(status, reasons)
        self._last_result = result
        return dict(result)

    def health(self) -> dict[str, Any]:
        return self.check_health()

    def record_service_failure(self, reason: str) -> dict[str, Any]:
        self.logger.error("service_failure", "CRITICAL", reason, component="HealthMonitor")
        return self.check_health(service_failure=True)

    def _heartbeat_loop(self) -> None:
        while not self._stop_event.wait(self.interval_seconds):
            self._record_heartbeat()

    def _record_heartbeat(self) -> None:
        now = self._monotonic()
        timestamp = _timestamp()
        self._last_heartbeat = now
        self._last_heartbeat_timestamp = timestamp
        try:
            event = self.event_bus.create_event(
                "HeartbeatGenerated",
                source="HealthMonitor",
                payload={"service": "HealthMonitor", "timestamp": timestamp, "state": self.state},
            )
            if not self.event_bus.publish(event):
                self.logger.error(
                    "heartbeat_publish",
                    "WARNING",
                    "Heartbeat was rejected by the event bus",
                    component="HealthMonitor",
                )
        except ClockError as error:
            self.logger.error(
                "heartbeat_publish",
                "WARNING",
                str(error),
                component="HealthMonitor",
            )

    def _record_status_change(self, status: str, reasons: list[str]) -> None:
        if status == self._last_status:
            return
        old_status = self._last_status
        self._last_status = status
        now = _timestamp()
        if status == "HEALTHY":
            self._metadata["last_healthy_timestamp"] = now
            if old_status in {"WARNING", "CRITICAL"}:
                self._metadata["recovery_count"] = int(self._metadata.get("recovery_count", 0)) + 1
                self._metadata["last_recovery_timestamp"] = now
        elif status == "WARNING":
            self._metadata["last_warning_timestamp"] = now
        self.storage_manager.write_metadata(
            "runtime", "health_metadata.json", self._metadata
        )
        self.logger.log(
            "health_status_changed",
            status,
            f"Health changed from {old_status} to {status}: {', '.join(reasons)}",
            component="HealthMonitor",
        )
        try:
            event = self.event_bus.create_event(
                "HealthStatusChanged",
                source="HealthMonitor",
                payload={"previous_status": old_status, "status": status, "reasons": reasons},
            )
            self.event_bus.publish(event)
        except ClockError as error:
            self.logger.error(
                "health_status_event",
                status,
                str(error),
                component="HealthMonitor",
            )


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
