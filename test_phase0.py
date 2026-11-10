from __future__ import annotations

import io
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from threading import Barrier, Thread
from typing import Any, Mapping

from app.storage_manager import StorageManager
from runtime.audit_client import AuditClient
from runtime.bootstrap import Bootstrap
from runtime.config_loader import ConfigurationLoader
from runtime.contract_registry import ContractRegistry, SCHEMA_FILES, parse_schema_document
from runtime.dependency_container import DependencyContainer
from runtime.event_bus import EventBus
from runtime.exceptions import (
    ConfigurationError,
    ContractValidationError,
    DependencyResolutionError,
    LifecycleError,
    VersionCompatibilityError,
)
from runtime.health_monitor import HealthMonitor
from runtime.logger import StructuredLogger
from runtime.runtime_manager import RUNTIME_STATES, RuntimeManager, ShutdownOutcome
from runtime.exceptions import ClockError
from runtime.protocols import ShutdownPriority
from tools.check_phase0 import validate_structure


ROOT = Path(__file__).resolve().parents[1]
SCHEMA_ROOT = ROOT / "specs" / "CLOCKv3.2" / "150_DATA_CONTRACTS"


class FakeProcess:
    def __init__(self, cpu: float = 0, memory: float = 0) -> None:
        self.cpu = cpu
        self.memory = memory

    def cpu_percent(self, interval: float | None = None) -> float:
        return self.cpu

    def memory_percent(self) -> float:
        return self.memory


class FakeClock:
    def __init__(self, value: float = 100.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


class FakeService:
    shutdown_priority = ShutdownPriority.DEFAULT

    def __init__(self, fail_starts: int = 0) -> None:
        self.state = "INITIALIZED"
        self.fail_starts = fail_starts
        self.starts = 0
        self.stops = 0
        self.fail_stop = False

    def initialize(self) -> None:
        self.state = "INITIALIZED"

    def start(self) -> None:
        self.starts += 1
        if self.fail_starts:
            self.fail_starts -= 1
            self.state = "FAILED"
            raise RuntimeError("controlled startup failure")
        self.state = "RUNNING"

    def stop(self) -> None:
        self.stops += 1
        if self.fail_stop:
            raise RuntimeError("controlled shutdown failure")
        self.state = "STOPPED"

    def health(self) -> dict[str, str]:
        return {"service": "FakeService", "state": self.state}


def build_event_bus(root: Path | None = None) -> tuple[EventBus, StructuredLogger]:
    stream = io.StringIO()
    logger = StructuredLogger(name=f"test-{id(stream)}", stream=stream)
    bus = EventBus(logger)
    bus.initialize()
    bus.start()
    return bus, logger


class RepositoryStructureTests(unittest.TestCase):
    def test_test_001_required_structure_exists(self) -> None:
        self.assertEqual(validate_structure(ROOT), [])
        self.assertTrue((ROOT / "runtime" / "bootstrap.py").is_file())
        self.assertTrue((ROOT / "contracts" / "definition.py").is_file())


class ConfigurationTests(unittest.TestCase):
    def test_test_002_defaults_and_environment_precedence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.json"
            config_path.write_text(
                json.dumps({"runtime": {"log_level": "WARNING", "health_interval_seconds": 45}}),
                encoding="utf-8",
            )
            loader = ConfigurationLoader(
                config_path,
                {"DG_LOG_LEVEL": "debug", "DG_HEALTH_INTERVAL_SECONDS": "15"},
            )
            self.assertEqual(
                loader.load(),
                {"runtime": {"log_level": "DEBUG", "health_interval_seconds": 15}},
            )

        defaults = ConfigurationLoader(environment={}).load()
        self.assertEqual(defaults, {"runtime": {"log_level": "INFO", "health_interval_seconds": 30}})

    def test_test_002_invalid_configuration_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.json"
            config_path.write_text(
                json.dumps({"runtime": {"health_interval_seconds": 0}}),
                encoding="utf-8",
            )
            with self.assertRaises(ConfigurationError):
                ConfigurationLoader(config_path, {}).load()

            config_path.write_text("{", encoding="utf-8")
            with self.assertRaises(ConfigurationError):
                ConfigurationLoader(config_path, {}).load()


class ContractRegistryTests(unittest.TestCase):
    def test_test_003_all_documented_contracts_register(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            storage = StorageManager(Path(directory) / "app")
            registry = ContractRegistry(SCHEMA_ROOT, storage)
            definitions = registry.load()
            self.assertEqual(len(definitions), len(SCHEMA_FILES))
            self.assertEqual(
                {definition.schema_name for definition in definitions},
                {"Decision", "Statistics", "Knowledge", "Evidence", "Incident", "Audit"},
            )
            self.assertEqual(registry.version("Decision"), "1.0")
            expected_optional_types = {
                "Decision": {
                    "incident_id": "string",
                    "recommendation_id": "string",
                    "evidence_refs": "array[string]",
                    "knowledge_refs": "array[string]",
                    "policy_refs": "array[string]",
                    "previous_decision_id": "string",
                    "metadata": "object",
                },
                "Statistics": {
                    "impact_score": "integer",
                    "trend": "string",
                    "metadata": "object",
                },
                "Knowledge": {
                    "description": "string",
                    "owner": "string",
                    "taxonomy_ref": "string",
                    "tags": "array[string]",
                    "related_knowledge": "array[string]",
                    "metadata": "object",
                },
                "Evidence": {
                    "node_id": "string",
                    "trigger_source": "string",
                    "trigger_reason": "string",
                    "trust_zone": "string",
                    "tags": "array[string]",
                    "references": "array[string]",
                    "metadata": "object",
                },
                "Incident": {
                    "classification": "string",
                    "description": "string",
                    "tags": "array[string]",
                    "external_references": "array[string]",
                    "metadata": "object",
                },
                "Audit": {
                    "related_decision": "string",
                    "related_evidence": "string",
                    "related_incident": "string",
                    "related_knowledge": "string",
                    "metadata": "object",
                },
            }
            for definition in definitions:
                self.assertEqual(
                    set(definition.required_fields) | set(definition.optional_fields),
                    set(definition.field_types),
                )
                for field_name, field_type in expected_optional_types[
                    definition.schema_name
                ].items():
                    self.assertEqual(definition.field_types[field_name], field_type)
            self.assertTrue((Path(directory) / "app" / "contracts" / "registered_contracts.json").is_file())

    def test_test_003_record_validation_checks_fields_versions_and_identifiers(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            registry = ContractRegistry(SCHEMA_ROOT, StorageManager(Path(directory) / "app"))
            registry.load()
            decision = {
                "decision_id": "DEC-2026-000001",
                "schema_version": "1.0",
                "decision_type": "APPROVAL",
                "decision": "APPROVED",
                "authority_level": "OWNER",
                "actor": "Human",
                "timestamp": "2026-01-01T12:00:00Z",
                "reason": "Evidence reviewed",
            }
            registry.validate_record("Decision", decision)
            decision["decision_id"] = "DEC-1"
            with self.assertRaises(ContractValidationError):
                registry.validate_record("Decision", decision)

    def test_test_003_reference_existence_is_deferred(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            registry = ContractRegistry(SCHEMA_ROOT, StorageManager(Path(directory) / "app"))
            registry.load()
            knowledge = {
                "knowledge_id": "KNW-2026-000001",
                "schema_version": "1.0",
                "title": "Example",
                "type": "INCIDENT_PATTERN",
                "state": "APPROVED",
                "confidence": 92,
                "evidence_refs": ["EVD-2026-999999"],
                "incident_refs": ["INC-2026-999999"],
                "created_at": "2026-01-01T00:00:00Z",
                "updated_at": "2026-01-15T00:00:00Z",
            }
            registry.validate_record("Knowledge", knowledge)
            knowledge["evidence_refs"] = [42]
            with self.assertRaises(ContractValidationError):
                registry.validate_record("Knowledge", knowledge)

    def test_test_003_values_conflicting_with_authoritative_types_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            registry = ContractRegistry(SCHEMA_ROOT, StorageManager(Path(directory) / "app"))
            registry.load()
            decision = {
                "decision_id": "DEC-2026-000001",
                "schema_version": "1.0",
                "decision_type": "APPROVAL",
                "decision": "APPROVED",
                "authority_level": "OWNER",
                "actor": "Human",
                "timestamp": "2026-01-01T12:00:00Z",
                "reason": "Reviewed",
                "metadata": True,
            }
            with self.assertRaises(ContractValidationError):
                registry.validate_record("Decision", decision)

    def test_test_003_json_examples_map_all_supported_primitive_types(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            schema_path = Path(directory) / "157_SAMPLE_SCHEMA.md"
            schema_path.write_text(
                """# Sample Schema

## Example

```json
{
  "sample_id": "SAMPLE-1",
  "schema_version": "1.0",
  "text_value": "",
  "integer_value": 0,
  "float_value": 0.0,
  "boolean_value": true,
  "array_value": [],
  "string_array": ["item"],
  "object_value": {}
}
```

# Required Fields
## sample_id
## schema_version

# Optional Fields
## text_value
## integer_value
## float_value
## boolean_value
## array_value
## string_array
## object_value
""",
                encoding="utf-8",
            )
            definition = parse_schema_document(schema_path)
            self.assertEqual(
                definition.field_types,
                {
                    "sample_id": "string",
                    "schema_version": "string",
                    "text_value": "string",
                    "integer_value": "integer",
                    "float_value": "float",
                    "boolean_value": "boolean",
                    "array_value": "array",
                    "string_array": "array[string]",
                    "object_value": "object",
                },
            )
            registry = ContractRegistry(
                storage_manager=StorageManager(Path(directory) / "app")
            )
            registry.register(definition)
            valid_record = {
                "sample_id": "SAMPLE-1",
                "schema_version": "1.0",
                "text_value": "",
                "integer_value": 0,
                "float_value": 0.0,
                "boolean_value": True,
                "array_value": [],
                "string_array": ["item"],
                "object_value": {},
            }
            registry.validate_record("Sample", valid_record)
            valid_record["float_value"] = 0
            with self.assertRaises(ContractValidationError):
                registry.validate_record("Sample", valid_record)

    def test_test_003_audit_metadata_contents_are_not_validated(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            registry = ContractRegistry(SCHEMA_ROOT, StorageManager(Path(directory) / "app"))
            registry.load()
            audit = {
                "audit_id": "AUD-2026-000001",
                "schema_version": "1.0",
                "timestamp": "2026-01-01T12:00:00Z",
                "actor": "Administrator",
                "action": "APPROVE",
                "target": "DEC-2026-000001",
                "source": "Spectrum",
                "related_decision": "DEC-2026-000001",
                "metadata": {"extension": [1, True, {"nested": "value"}]},
            }
            registry.validate_record("Audit", audit)
            audit["metadata"] = []
            with self.assertRaises(ContractValidationError):
                registry.validate_record("Audit", audit)

    def test_test_003_registry_accepts_optional_type_enrichment(self) -> None:
        registry = ContractRegistry(SCHEMA_ROOT)
        decision = parse_schema_document(SCHEMA_ROOT / "151_DECISION_SCHEMA.md")
        previous_types = dict(decision.field_types)
        previous_types.pop("metadata")
        previous_types["evidence_refs"] = "array"
        previous_types["timestamp"] = "string"
        registry._previous_metadata["Decision"] = {
            "schema_version": "1.0",
            "required_fields": list(decision.required_fields),
            "optional_fields": list(decision.optional_fields),
            "field_types": previous_types,
            "fingerprint": "previous-fingerprint",
        }
        registry._check_compatibility(decision)
        registry._previous_metadata["Decision"]["field_types"]["decision"] = "integer"
        with self.assertRaises(VersionCompatibilityError):
            registry._check_compatibility(decision)


class DependencyContainerTests(unittest.TestCase):
    def test_test_004_dependencies_resolve_and_inject(self) -> None:
        container = DependencyContainer()
        container.register("base", lambda: 5)
        container.register("total", lambda base: base + 3, ("base",))
        container.validate()
        self.assertEqual(container.resolve("total"), 8)

        target = type("Target", (), {})()
        container.inject(target, number="total")
        self.assertEqual(target.number, 8)

    def test_test_004_missing_and_circular_dependencies_fail_fast(self) -> None:
        missing = DependencyContainer()
        missing.register("service", lambda dependency: dependency, ("absent",))
        with self.assertRaises(DependencyResolutionError):
            missing.validate()

        circular = DependencyContainer()
        circular.register("first", lambda second: second, ("second",))
        circular.register("second", lambda first: first, ("first",))
        with self.assertRaises(DependencyResolutionError):
            circular.validate()


class EventBusAndAuditTests(unittest.TestCase):
    def test_event_bus_preserves_fifo_and_supports_unsubscribe(self) -> None:
        bus, logger = build_event_bus()
        subscriber = bus.subscribe("TestEvent")
        first = bus.create_event("TestEvent", "Test", {"order": 1})
        second = bus.create_event("TestEvent", "Test", {"order": 2})
        self.assertTrue(bus.publish(first))
        self.assertTrue(bus.publish(second))
        self.assertEqual(subscriber.get_nowait()["payload"]["order"], 1)
        self.assertEqual(subscriber.get_nowait()["payload"]["order"], 2)
        self.assertTrue(bus.unsubscribe("TestEvent"))
        self.assertFalse(bus.unsubscribe("TestEvent"))
        bus.stop()
        logger.close()

    def test_event_ids_are_not_reused_after_restart(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            storage = StorageManager(Path(directory) / "app")
            storage.initialize()
            stream = io.StringIO()
            logger = StructuredLogger(name=f"sequence-test-{id(stream)}", stream=stream)
            first_bus = EventBus(logger, storage)
            first_bus.initialize()
            first_bus.start()
            first_id = first_bus.create_event("TestEvent", "Test")["event_id"]
            first_bus.stop()

            second_bus = EventBus(logger, storage)
            second_bus.initialize()
            second_bus.start()
            second_id = second_bus.create_event("TestEvent", "Test")["event_id"]
            self.assertGreater(int(second_id.rsplit("-", 1)[1]), int(first_id.rsplit("-", 1)[1]))
            second_bus.stop()
            logger.close()

    def test_test_009_audit_client_publishes_events_without_owning_storage(self) -> None:
        bus, logger = build_event_bus()
        subscriber = bus.subscribe("RuntimeStarted")
        client = AuditClient(bus, logger)
        client.start()
        self.assertTrue(client.publish("RuntimeStarted", reason="Startup completed"))
        event = subscriber.get_nowait()
        self.assertEqual(event["source"], "DiverGuard")
        self.assertEqual(event["payload"]["reason"], "Startup completed")
        self.assertTrue(client.flush(timeout=0.1))
        self.assertFalse(hasattr(client, "search"))
        bus.stop()
        logger.close()

    def test_test_009_invalid_event_is_rejected(self) -> None:
        bus, logger = build_event_bus()
        with self.assertRaises(ClockError):
            bus.publish({"event_type": "Bad"})
        bus.stop()
        logger.close()


class LoggingAndHealthTests(unittest.TestCase):
    def test_test_007_logging_emits_structured_required_fields(self) -> None:
        stream = io.StringIO()
        logger = StructuredLogger(name=f"log-test-{id(stream)}", stream=stream)
        logger.start()
        logger.error("failure", "FAILED", "visible failure", component="Test")
        records = [json.loads(line) for line in stream.getvalue().splitlines()]
        self.assertTrue(records)
        self.assertTrue(
            {"timestamp", "component", "action", "state", "message"} <= set(records[-1])
        )
        self.assertEqual(records[-1]["component"], "Test")
        self.assertEqual(records[-1]["state"], "FAILED")
        logger.close()

    def test_test_008_health_uses_independent_highest_threshold(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bus, logger = build_event_bus()
            clock = FakeClock()
            monitor = HealthMonitor(
                bus,
                logger,
                StorageManager(Path(directory) / "app"),
                interval_seconds=3600,
                process=FakeProcess(cpu=75, memory=10),
                monotonic=clock,
            )
            monitor.start()
            self.assertEqual(monitor.health()["status"], "WARNING")
            monitor.process.cpu = 90
            self.assertEqual(monitor.health()["status"], "CRITICAL")
            clock.value = 221
            monitor.process.cpu = 5
            self.assertEqual(monitor.health()["status"], "CRITICAL")
            monitor.stop()
            bus.stop()
            logger.close()


class RuntimeLifecycleTests(unittest.TestCase):
    @classmethod
    def tearDownClass(cls) -> None:
        threads = sorted(threading.enumerate(), key=lambda thread: thread.name)
        thread_report = ", ".join(
            f"{thread.name}(daemon={thread.daemon}, alive={thread.is_alive()})"
            for thread in threads
        )
        sys.stderr.write(
            f"[RuntimeLifecycleTests] Running thread count: {len(threads)}; "
            f"threads: {thread_report or 'none'}\n"
        )
        sys.stderr.flush()

    def test_test_005_and_006_bootstrap_starts_and_stops_cleanly(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            stream = io.StringIO()
            app = Bootstrap(
                config_path=Path(directory) / "config.json",
                schema_directory=SCHEMA_ROOT,
                app_root=Path(directory) / "app",
                environment={"DG_HEALTH_INTERVAL_SECONDS": "3600"},
                sleeper=lambda _: None,
            )
            app.logger._handler.setStream(stream)
            app.run()
            self.assertEqual(app.health()["state"], "RUNNING")
            self.assertEqual(app.runtime_manager.health()["state"], "RUNNING")
            self.assertEqual(len(app.contract_registry.list_schemas()), 6)
            app.stop()
            self.assertEqual(app.health()["state"], "STOPPED")
            self.assertEqual(app.runtime_manager.health()["state"], "STOPPED")
            self.assertTrue((Path(directory) / "app" / "runtime" / "runtime_metadata.json").is_file())
            output = stream.getvalue()
            self.assertIn("RUNNING -> STOPPING", output)
            self.assertIn("STOPPING -> STOPPED", output)
            self.assertIn('"message":"RuntimeStopped was published; shutdown continues"', output)
            self.assertIn('"component":"EventBus","action":"shutdown_started"', output)
            self.assertIn('"component":"HealthMonitor","action":"heartbeat_shutdown_completed"', output)
            self.assertIn('"component":"HealthMonitor","action":"shutdown_started"', output)
            self.assertIn('"component":"Executor","action":"shutdown_started"', output)
            self.assertIn('"component":"EventBus","action":"shutdown_completed"', output)
            self.assertIn('"component":"StructuredLogger","action":"shutdown_started"', output)
            self.assertIn('"component":"StructuredLogger","action":"shutdown_completed"', output)
            self.assertIn('"component":"Executor","action":"shutdown_completed"', output)
            final_logs = []
            for line in output.splitlines():
                record = json.loads(line)
                action = record["action"]
                message = record["message"]
                if (
                    action.startswith("shutdown_")
                    or action.startswith("heartbeat_")
                    or (
                        action == "runtime_state"
                        and ("STOPPING" in message or "STOPPED" in message)
                    )
                ):
                    final_logs.append(line)
            sys.stderr.write("[RuntimeLifecycleTests] Final shutdown logs:\n")
            for line in final_logs:
                sys.stderr.write(f"{line}\n")
            sys.stderr.flush()

    def test_test_005_and_006_recovery_retries_are_serialized(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bus, logger = build_event_bus()
            client = AuditClient(bus, logger)
            client.start()
            storage = StorageManager(Path(directory) / "app")
            storage.initialize()
            service = FakeService(fail_starts=1)
            manager = RuntimeManager(
                logger,
                client,
                bus,
                storage,
                sleeper=lambda _: None,
            )
            manager.initialize()
            manager.start([service])
            self.assertEqual(service.health()["state"], "RUNNING")
            self.assertEqual(manager.state, "RUNNING")
            manager.stop()
            self.assertEqual(manager.state, "STOPPED")
            logger.close()

    def test_recovery_coordinator_handles_distinct_services_concurrently(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bus, logger = build_event_bus()
            client = AuditClient(bus, logger)
            client.start()
            storage = StorageManager(Path(directory) / "app")
            storage.initialize()
            barrier = Barrier(2)

            class ServiceA(FakeService):
                pass

            class ServiceB(FakeService):
                pass

            manager = RuntimeManager(
                logger,
                client,
                bus,
                storage,
                sleeper=lambda _: barrier.wait(timeout=2),
            )
            manager.initialize()
            manager.start([])
            services = (ServiceA(), ServiceB())
            errors: list[Exception] = []

            def recover(service: FakeService) -> None:
                try:
                    manager.recovery_coordinator.recover(service, "controlled failure")
                except Exception as error:
                    errors.append(error)

            threads = [Thread(target=recover, args=(service,)) for service in services]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=3)

            self.assertFalse(any(thread.is_alive() for thread in threads))
            self.assertEqual(errors, [])
            self.assertTrue(all(service.health()["state"] == "RUNNING" for service in services))
            self.assertEqual(manager.state, "RUNNING")
            manager.stop()
            logger.close()

    def test_recovery_exhaustion_transitions_runtime_to_failed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bus, logger = build_event_bus()
            client = AuditClient(bus, logger)
            client.start()
            storage = StorageManager(Path(directory) / "app")
            storage.initialize()
            manager = RuntimeManager(
                logger,
                client,
                bus,
                storage,
                sleeper=lambda _: None,
            )
            manager.initialize()
            service = FakeService(fail_starts=4)
            with self.assertRaises(LifecycleError):
                manager.start([service])
            self.assertEqual(manager.state, "FAILED")
            self.assertNotIn("recover", RuntimeManager.__dict__)
            manager.stop()
            logger.close()

    def test_event_bus_recovery_uses_runtime_recovery_coordinator(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bus, logger = build_event_bus()
            client = AuditClient(bus, logger)
            client.start()
            storage = StorageManager(Path(directory) / "app")
            storage.initialize()
            manager = RuntimeManager(logger, client, bus, storage, sleeper=lambda _: None)
            manager.initialize()
            manager.start([])

            manager.recovery_coordinator.recover(bus, "controlled EventBus failure")

            self.assertEqual(bus.health()["state"], "RUNNING")
            self.assertEqual(manager.state, "RUNNING")
            manager.stop()
            logger.close()

    def test_shutdown_priority_and_outcome_separate_warnings_from_failures(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bus, logger = build_event_bus()
            client = AuditClient(bus, logger)
            client.start()
            storage = StorageManager(Path(directory) / "app")
            storage.initialize()
            stop_order: list[str] = []

            class PriorityService(FakeService):
                def __init__(self, name: str, priority: ShutdownPriority) -> None:
                    super().__init__()
                    self.service_name = name
                    self.shutdown_priority = priority

                def stop(self) -> None:
                    stop_order.append(self.service_name)
                    super().stop()

            manager = RuntimeManager(logger, client, bus, storage, sleeper=lambda _: None)
            manager.initialize()
            later = PriorityService("Later", ShutdownPriority.DEFAULT)
            earlier = PriorityService("Earlier", ShutdownPriority.HEALTH_MONITOR)
            manager.start([later, earlier])
            manager.stop()
            self.assertEqual(stop_order, ["Earlier", "Later"])
            self.assertEqual(manager.state, "STOPPED")
            self.assertEqual(manager.shutdown_outcome, ShutdownOutcome.STOPPED)
            runtime_metadata = storage.read_json("runtime", "runtime_metadata.json")
            self.assertEqual(runtime_metadata["shutdown_outcome"], "STOPPED")
            logger.close()

        with tempfile.TemporaryDirectory() as directory:
            bus, logger = build_event_bus()
            client = AuditClient(bus, logger)
            client.start()

            class FailingMetadataStorage(StorageManager):
                fail_runtime_metadata = False

                def write_metadata(
                    self,
                    category: str,
                    filename: str,
                    value: Mapping[str, Any],
                    **kwargs: Any,
                ):
                    if self.fail_runtime_metadata and category == "runtime" and filename == "runtime_metadata.json":
                        raise OSError("controlled metadata failure")
                    return super().write_metadata(category, filename, value, **kwargs)

            storage = FailingMetadataStorage(Path(directory) / "app")
            storage.initialize()
            manager = RuntimeManager(logger, client, bus, storage, sleeper=lambda _: None)
            manager.initialize()
            manager.start([])
            storage.fail_runtime_metadata = True
            manager.stop()
            self.assertEqual(manager.state, "STOPPED")
            self.assertEqual(
                manager.shutdown_outcome,
                ShutdownOutcome.STOPPED_WITH_WARNINGS,
            )
            logger.close()

        with tempfile.TemporaryDirectory() as directory:
            bus, logger = build_event_bus()
            client = AuditClient(bus, logger)
            client.start()
            storage = StorageManager(Path(directory) / "app")
            storage.initialize()
            service = FakeService()
            service.fail_stop = True
            manager = RuntimeManager(logger, client, bus, storage, sleeper=lambda _: None)
            manager.initialize()
            manager.start([service])
            with self.assertRaises(LifecycleError):
                manager.stop()
            self.assertEqual(manager.state, "FAILED")
            self.assertEqual(manager.shutdown_outcome, ShutdownOutcome.FAILED)
            logger.close()

    def test_test_006_failed_audit_flush_does_not_block_shutdown(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bus, logger = build_event_bus()

            class FailingAuditClient(AuditClient):
                def flush(self, timeout: float = 30.0) -> bool:
                    return False

            client = FailingAuditClient(bus, logger)
            client.start()
            storage = StorageManager(Path(directory) / "app")
            storage.initialize()
            manager = RuntimeManager(logger, client, bus, storage, sleeper=lambda _: None)
            manager.initialize()
            manager.start([bus, client])
            manager.stop()
            self.assertEqual(manager.state, "STOPPED")
            logger.close()


class ExceptionFrameworkTests(unittest.TestCase):
    def test_test_010_typed_configuration_failures_are_explicit(self) -> None:
        self.assertTrue(issubclass(ConfigurationError, ClockError))
        with self.assertRaises(ConfigurationError):
            ConfigurationLoader(environment={"DG_HEALTH_INTERVAL_SECONDS": "invalid"}).load()


if __name__ == "__main__":
    unittest.main()
