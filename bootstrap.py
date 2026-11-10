from __future__ import annotations

import time
import threading
from pathlib import Path
import sys
from typing import Any, Mapping

from app.storage_manager import StorageManager
from runtime.audit_client import AuditClient
from runtime.config_loader import ConfigurationLoader
from runtime.contract_registry import ContractRegistry
from runtime.dependency_container import DependencyContainer
from runtime.event_bus import EventBus
from runtime.exceptions import ClockError, LifecycleError
from runtime.health_monitor import HealthMonitor
from runtime.logger import StructuredLogger
from runtime.runtime_manager import RuntimeManager


class Bootstrap:
    def __init__(
        self,
        *,
        config_path: str | Path | None = None,
        schema_directory: str | Path | None = None,
        app_root: str | Path = "app",
        environment: Mapping[str, str] | None = None,
        process: Any | None = None,
        sleeper: Any = time.sleep,
    ) -> None:
        self.storage_manager = StorageManager(app_root)
        self.logger = StructuredLogger()
        self.config_loader = ConfigurationLoader(config_path, environment)
        self.event_bus = EventBus(self.logger, self.storage_manager)
        self.audit_client = AuditClient(self.event_bus, self.logger)
        self.contract_registry = ContractRegistry(
            schema_directory=schema_directory,
            storage_manager=self.storage_manager,
        )
        self.dependency_container = DependencyContainer()
        self.health_monitor: HealthMonitor | None = None
        self.runtime_manager = RuntimeManager(
            self.logger,
            self.audit_client,
            self.event_bus,
            self.storage_manager,
            sleeper=sleeper,
        )
        self.process = process
        self.state = "INITIALIZED"

    def run(self) -> Bootstrap:
        self.state = "STARTING"
        stage = "ConfigurationRejected"
        try:
            self.storage_manager.initialize()
            self.config_loader.initialize()
            self.config_loader.start()
            self.logger.set_level(self.config_loader.get("runtime.log_level"))
            self.logger.start()

            stage = "ContractValidationFailed"
            self.contract_registry.initialize()
            self.contract_registry.start()
            stage = "ServiceFailed"
            self.health_monitor = HealthMonitor(
                self.event_bus,
                self.logger,
                self.storage_manager,
                interval_seconds=self.config_loader.get("runtime.health_interval_seconds"),
                process=self.process,
            )
            self.health_monitor.initialize()

            self._register_services()
            self.dependency_container.initialize()
            self.dependency_container.start()
            self.runtime_manager.initialize()
            self.audit_client.initialize()
            self.runtime_manager.registered_service_names = [
                "ConfigurationLoader",
                "ContractRegistry",
                "DependencyContainer",
                "RuntimeManager",
                "HealthMonitor",
                "AuditClient",
                "StructuredLogger",
                "Bootstrap",
            ]

            self.runtime_manager.start(
                [
                    self.config_loader,
                    self.contract_registry,
                    self.dependency_container,
                    self.event_bus,
                    self.health_monitor,
                    self.audit_client,
                ]
            )
            self.storage_manager.start()
            self.state = "RUNNING"
            self.storage_manager.audit_client = self.audit_client
            self.contract_registry.audit_client = self.audit_client
            self.storage_manager.publish_pending_events()
            self._publish_pending_registry_events()
            self.audit_client.publish(
                "ConfigurationLoaded",
                reason="Configuration loaded and validated",
                payload={"source": str(self.config_loader.config_path)},
            )
            self._verify_required_services()
            return self
        except (ClockError, OSError, ValueError, RuntimeError) as error:
            self.state = "FAILED"
            self.logger.error("startup", "FAILED", str(error), component="Bootstrap")
            self._report_startup_failure(stage, str(error))
            if self.runtime_manager.state in {"RUNNING", "DEGRADED", "FAILED"}:
                try:
                    self.runtime_manager.stop()
                except LifecycleError as cleanup_error:
                    self.logger.error(
                        "startup_cleanup",
                        "FAILED",
                        str(cleanup_error),
                        component="Bootstrap",
                    )
            raise

    def stop(self) -> None:
        if self.state == "STOPPED":
            return
        try:
            self.runtime_manager.stop()
        finally:
            self.logger.log(
                "shutdown_started",
                "STARTING",
                "Stopping storage service",
                component="StorageManager",
            )
            self.storage_manager.stop()
            self.logger.log(
                "shutdown_completed",
                "COMPLETED",
                "Storage service stopped",
                component="StorageManager",
            )
            self.logger.log(
                "shutdown_started",
                "STARTING",
                "Stopping structured logger",
                component="StructuredLogger",
            )
            self.logger.stop()
            self.logger.log(
                "shutdown_completed",
                "COMPLETED",
                "Structured logger stopped",
                component="StructuredLogger",
            )
            self.logger.close()
            self.state = "STOPPED" if self.runtime_manager.state == "STOPPED" else "FAILED"
            remaining_threads = sorted(
                threading.enumerate(),
                key=lambda thread: thread.name,
            )
            report = ", ".join(
                f"{thread.name}(daemon={thread.daemon}, alive={thread.is_alive()})"
                for thread in remaining_threads
            )
            sys.stderr.write(
                f"[shutdown] Remaining threads ({len(remaining_threads)}): "
                f"{report or 'none'}\n"
            )
            sys.stderr.flush()

    def health(self) -> dict[str, str]:
        return {"service": "Bootstrap", "state": self.state}

    def _register_services(self) -> None:
        if self.health_monitor is None:
            raise LifecycleError("Health monitor must be initialized before dependency registration")
        services = {
            "ConfigurationLoader": self.config_loader,
            "ContractRegistry": self.contract_registry,
            "DependencyContainer": self.dependency_container,
            "RuntimeManager": self.runtime_manager,
            "HealthMonitor": self.health_monitor,
            "AuditClient": self.audit_client,
            "StructuredLogger": self.logger,
            "EventBus": self.event_bus,
            "StorageManager": self.storage_manager,
            "Bootstrap": self,
        }
        for name, service in services.items():
            self.dependency_container.register(name, service)

    def _publish_pending_registry_events(self) -> None:
        events = self.contract_registry.pending_audit_events
        self.contract_registry.pending_audit_events = []
        for event_type, reason in events:
            self.audit_client.publish(event_type, reason=reason)

    def _verify_required_services(self) -> None:
        if self.health_monitor is None:
            raise LifecycleError("Health monitor was not constructed")
        required = (
            self.config_loader,
            self.contract_registry,
            self.dependency_container,
            self.runtime_manager,
            self.health_monitor,
            self.audit_client,
            self.logger,
            self,
        )
        failed = [
            service.health().get("service", type(service).__name__)
            for service in required
            if service.health().get("state") != "RUNNING"
        ]
        if failed:
            raise LifecycleError(f"Required services are not RUNNING: {failed}")

    def _report_startup_failure(self, event_type: str, reason: str) -> None:
        try:
            if self.event_bus.state != "RUNNING":
                self.event_bus.start()
            if self.audit_client.state != "RUNNING":
                self.audit_client.start()
            self.audit_client.publish(event_type, reason=reason)
            self.audit_client.flush(timeout=1.0)
            if self.runtime_manager.state not in {"RUNNING", "DEGRADED", "FAILED"}:
                self.audit_client.stop(flush=False)
                self.event_bus.stop()
        except ClockError as error:
            self.logger.error(
                "startup_audit",
                "FAILED",
                str(error),
                component="Bootstrap",
            )


def main() -> int:
    application = Bootstrap()
    application.run()
    try:
        while application.state == "RUNNING":
            time.sleep(1)
    except KeyboardInterrupt:
        application.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
