# DIVERGUARD Phase 0 Foundation

This implementation provides runtime foundation services only. It does not
implement observation, classification, baseline generation, detection,
recognition, evidence, knowledge, preservation, collection, or authority.

## Runtime layout

- `runtime/config_loader.py` loads `config.json`, applies `DG_` environment
  overrides, and validates the Phase 0 configuration contract.
- `runtime/contract_registry.py` builds ephemeral contract definitions from
  the six Markdown schemas and persists registration metadata only.
- `runtime/dependency_container.py` resolves explicitly registered services
  and rejects missing or circular dependencies.
- `runtime/event_bus.py` provides process-local FIFO messaging and transient
  runtime/audit queues.
- `runtime/health_monitor.py` emits heartbeats and evaluates process CPU,
  process RSS percentage, heartbeat delay, and service failures independently.
- `runtime/audit_client.py` publishes events; it does not store or search audit
  history.
- `runtime/runtime_manager.py` manages the specified lifecycle states,
  lifecycle transitions, ordered shutdown, and runtime metadata.
- `runtime/recovery_coordinator.py` owns bounded service-recovery retries and
  per-service coordination, including EventBus recovery, separate from runtime
  lifecycle control.
- `runtime/protocols.py` defines lifecycle, logging, audit, event transport,
  recovery, and shutdown-priority interfaces.
- `app/storage_manager.py` atomically persists JSON metadata under `app/`.
- `runtime/bootstrap.py` wires the Phase 0 services and exposes `main()`.

## Configuration

Defaults are `runtime.log_level = INFO` and
`runtime.health_interval_seconds = 30`. Precedence is environment, `config.json`,
then defaults. Supported environment variables are `DG_LOG_LEVEL` and
`DG_HEALTH_INTERVAL_SECONDS`.

## Contract generation

The registry consumes the Decision schema's canonical runtime definition and
derives the remaining field types from explicit JSON examples in the
authoritative schema Markdown files. It does not infer types from prose.
Examples distinguish strings, integers, floats, booleans, objects, and arrays;
non-empty arrays retain and validate their item type (for example,
`array[string]`). All required and optional fields now have declared types.
Metadata validation checks the value is an object but does not inspect its
contents. Phase 0 validates structure, identifiers, and versions; it does not
resolve whether referenced cross-domain objects exist.

Registry metadata is stored under `app/contracts/`. Generated runtime
definitions remain in memory and can be rebuilt from the Markdown sources.

## Persistence and audit

APP JSON metadata is stored under `app/metadata/`, `app/contracts/`, and
`app/runtime/`. Runtime memory, thread/session state, queues, observations, and
audit history are not persisted. DIVERGUARD publishes audit events through the
local Event Bus; Spectrum remains the audit owner.

Event delivery is process-local, FIFO, and best effort. Event IDs use
`EVT-YYYY-NNNNNN`; reserved sequence ranges are persisted to prevent ID reuse
across process restarts within the same year. The audit queue is flushed within
the configured 30-second shutdown budget; failures are logged and reported as
runtime events while graceful shutdown continues.

### Event Bus Contract

The Phase 0 EventBus contract guarantees:

- Event IDs match `EVT-YYYY-NNNNNN`.
- Event timestamps are ISO-8601 UTC values ending in `Z`.
- Event payloads are objects.
- Runtime and audit events use separate in-process queues; neither queue is
  persisted.
- Runtime and audit publishing have separate shutdown admission gates.
  Normal publishing is rejected after its gate closes. The shutdown-event path
  remains available while the EventBus is `RUNNING`; stopping the bus closes
  both gates and discards pending runtime events.
- Delivery is synchronous: `publish()` queues and delivers pending events on
  the caller's thread. Events are processed FIFO within each queue. Runtime
  and audit queues have no shared ordering guarantee.

The contract does not guarantee delivery persistence, cross-process transport,
distributed ordering, guaranteed delivery, or replay capability. Subscriber
queues are also transient and unbounded in Phase 0.

Shutdown emits start/completion diagnostics for EventBus, HealthMonitor and its
heartbeat thread, registered services, and logger. Executor shutdown is logged
as a no-op because Phase 0 registers no executor. Bootstrap also prints the
live thread list after shutdown for operational verification.

## Runtime state and recovery policy

The eight contract-defined Runtime states remain closed; shutdown severity is
reported separately as `ShutdownOutcome` (`STOPPED`,
`STOPPED_WITH_WARNINGS`, or `FAILED`). Metadata-write and audit-flush failures
are recorded as warnings, while a required service stop failure leaves Runtime
in `FAILED`. EventBus and AuditClient are consumed through protocols and declare
their shutdown priorities; services are stopped in ascending priority order.

`RECOVERING` means the coordinator is retrying a failed service. `DEGRADED` is
the transition marker after a failure is detected and before recovery begins.
`FAILED` means startup/recovery was exhausted or a required service could not
stop. Phase 0 automatically retries service start/restart failures only, using
three delays (30, 60, and 120 seconds); exhausting them requires operator
review. Configuration and contract validation failures are not retried.
Recovery metadata marks interrupted prior runs but does not automatically
restart arbitrary services during bootstrap.

Recovery remains synchronous for its caller in Phase 0. `RecoveryCoordinator`
uses separate per-service locks and RuntimeManager aggregates simultaneous
recovery state, so independent recovery calls can proceed concurrently; no
executor, background scheduler, or automatic concurrency launcher is added.
Services do not own separate retry loops; recovery attempts use the shared
coordinator policy.
Future work may add that scheduler and refine failure classification and retry
limits without expanding the Runtime state set. Automatic recovery remains
limited to retryable lifecycle failures; manual recovery follows exhaustion or
non-retryable configuration/contract failures.

## Validation

Run the Phase 0 tests from the repository root:

```text
python -m unittest discover -s tests -v
```

Run the directory check:

```text
python tools/check_phase0.py
```

The latest validation completed `compileall`, all 26 unit tests, and the
repository structure check. `RuntimeLifecycleTests` also passes in isolation
with verbose shutdown-transition logs and reports the remaining thread count.
