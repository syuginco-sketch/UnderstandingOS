from __future__ import annotations

import queue
import re
import threading
import time
from collections import defaultdict
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

from app.storage_manager import StorageManager
from runtime.exceptions import EventBusError
from runtime.protocols import ShutdownPriority


EVENT_ID_PATTERN = re.compile(r"^EVT-\d{4}-\d{6}$")
MAX_EVENT_SEQUENCE = 999_999
EVENT_ID_BLOCK_SIZE = 100


class EventBus:
    shutdown_priority = ShutdownPriority.EVENT_TRANSPORT

    def __init__(
        self,
        logger: object | None = None,
        storage_manager: StorageManager | None = None,
    ) -> None:
        self.logger = logger
        self.storage_manager = storage_manager
        self.runtime_queue: queue.Queue[dict[str, Any]] = queue.Queue()
        self.audit_queue: queue.Queue[dict[str, Any]] = queue.Queue()
        self._subscribers: dict[str, list[queue.Queue[dict[str, Any]]]] = defaultdict(list)
        self._lock = threading.RLock()
        self._sequence_lock = threading.Lock()
        self._last_sequence = 0
        self._reserved_through = 0
        self._sequence_year = datetime.now(timezone.utc).year
        self._accepting_runtime = True
        self._accepting_audit = True
        self.state = "INITIALIZED"

    def initialize(self) -> None:
        year = datetime.now(timezone.utc).year
        self._sequence_year = year
        self._last_sequence = 0
        self._reserved_through = 0
        if self.storage_manager is not None:
            previous = self.storage_manager.restore_json("runtime", "event_sequence.json")
            if previous is not None and previous.get("year") == year:
                reserved_through = previous.get("reserved_through")
                if isinstance(reserved_through, int) and 0 <= reserved_through <= MAX_EVENT_SEQUENCE:
                    self._last_sequence = reserved_through
                    self._reserved_through = reserved_through

    def start(self) -> None:
        self._accepting_runtime = True
        self._accepting_audit = True
        self.state = "RUNNING"
        self._log("EventBusStarted", "RUNNING", "In-process event bus started")

    def stop(self) -> None:
        self._log("shutdown_started", "STOPPING", "EventBus shutdown started")
        self._accepting_runtime = False
        self._accepting_audit = False
        self._discard(self.runtime_queue)
        self.state = "STOPPED"
        self._log("shutdown_completed", "STOPPED", "EventBus shutdown completed")

    def publish(self, event: Mapping[str, Any]) -> bool:
        return self._publish(event, audit=False)

    def publish_audit(self, event: Mapping[str, Any]) -> bool:
        return self._publish(event, audit=True)

    def subscribe(self, event_type: str) -> queue.Queue[dict[str, Any]]:
        if not isinstance(event_type, str) or not event_type:
            raise EventBusError("Event type must be a non-empty string")
        subscriber: queue.Queue[dict[str, Any]] = queue.Queue()
        with self._lock:
            self._subscribers[event_type].append(subscriber)
        return subscriber

    def unsubscribe(self, event_type: str) -> bool:
        with self._lock:
            return self._subscribers.pop(event_type, None) is not None

    def create_event(
        self,
        event_type: str,
        source: str,
        payload: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not event_type or not source:
            raise EventBusError("Event type and source are required")
        return {
            "event_id": self._next_event_id(),
            "event_type": event_type,
            "source": source,
            "timestamp": _timestamp(),
            "payload": dict(payload or {}),
        }

    def flush_audit(self, timeout: float = 30.0) -> bool:
        if timeout < 0:
            raise ValueError("timeout cannot be negative")
        deadline = time.monotonic() + timeout
        while not self.audit_queue.empty():
            if time.monotonic() > deadline:
                return False
            try:
                event = self.audit_queue.get_nowait()
            except queue.Empty:
                continue
            if not self._deliver(event):
                self.audit_queue.task_done()
                try:
                    self.audit_queue.put_nowait(event)
                except queue.Full:
                    self._log("AuditPublishFailure", "WARNING", "Audit queue rejected an event")
                return False
            self.audit_queue.task_done()
        return True

    def stop_accepting_runtime(self) -> None:
        self._accepting_runtime = False

    def publish_shutdown_event(self, event: Mapping[str, Any]) -> bool:
        self._validate_event(event)
        if self.state != "RUNNING":
            raise EventBusError("Event bus is not running")
        try:
            self.runtime_queue.put_nowait(dict(event))
        except queue.Full as error:
            raise EventBusError("Runtime queue rejected a shutdown event") from error
        self._deliver_pending(self.runtime_queue)
        return True

    def discard_runtime_queue(self) -> None:
        self._discard(self.runtime_queue)

    def health(self) -> dict[str, str]:
        return {"service": "EventBus", "state": self.state}

    def _publish(self, event: Mapping[str, Any], *, audit: bool) -> bool:
        self._validate_event(event)
        if self.state != "RUNNING":
            raise EventBusError("Event bus is not running")
        if audit and not self._accepting_audit:
            raise EventBusError("Audit publishing is stopping")
        if not audit and not self._accepting_runtime:
            raise EventBusError("Runtime event publishing is stopping")
        target = self.audit_queue if audit else self.runtime_queue
        immutable_event = dict(event)
        try:
            target.put_nowait(immutable_event)
        except queue.Full as error:
            self._log("EventRejected", "WARNING", "Local event queue rejected an event")
            raise EventBusError("Local event queue rejected an event") from error

        if audit:
            self._deliver_pending(self.audit_queue)
        else:
            self._deliver_pending(self.runtime_queue)
        return True

    def _deliver_pending(self, pending: queue.Queue[dict[str, Any]]) -> bool:
        success = True
        while True:
            try:
                event = pending.get_nowait()
            except queue.Empty:
                return success
            delivered = self._deliver(event)
            pending.task_done()
            if not delivered:
                success = False
                try:
                    pending.put_nowait(event)
                except queue.Full:
                    self._log("EventRejected", "WARNING", "Local event queue rejected an event")
                return success

    def _deliver(self, event: dict[str, Any]) -> bool:
        with self._lock:
            subscribers = tuple(self._subscribers.get(event["event_type"], ()))
        try:
            for subscriber in subscribers:
                subscriber.put_nowait(dict(event))
        except queue.Full:
            self._log("EventRejected", "WARNING", f"No queue capacity for {event['event_type']}")
            return False
        self._log("EventPublished", "RUNNING", f"Published {event['event_type']}")
        return True

    def _validate_event(self, event: Mapping[str, Any]) -> None:
        required = {"event_id", "event_type", "source", "timestamp", "payload"}
        missing = required - set(event)
        if missing:
            raise EventBusError(f"Event is missing fields: {sorted(missing)}")
        if EVENT_ID_PATTERN.fullmatch(str(event["event_id"])) is None:
            raise EventBusError("event_id must match EVT-YYYY-NNNNNN")
        if not isinstance(event["event_type"], str) or not event["event_type"]:
            raise EventBusError("event_type must be a non-empty string")
        if not isinstance(event["source"], str) or not event["source"]:
            raise EventBusError("source must be a non-empty string")
        if not isinstance(event["payload"], dict):
            raise EventBusError("payload must be a JSON object")
        if not _is_utc_timestamp(event["timestamp"]):
            raise EventBusError("timestamp must be an ISO-8601 UTC value")

    def _next_event_id(self) -> str:
        with self._sequence_lock:
            year = datetime.now(timezone.utc).year
            if year != self._sequence_year:
                self._sequence_year = year
                self._last_sequence = 0
                self._reserved_through = 0
            if self._last_sequence >= self._reserved_through:
                self._reserve_event_ids(year)
            if self._last_sequence >= MAX_EVENT_SEQUENCE:
                raise EventBusError(f"Event ID sequence exhausted for {year}")
            self._last_sequence += 1
            return f"EVT-{year:04d}-{self._last_sequence:06d}"

    def _reserve_event_ids(self, year: int) -> None:
        end = min(self._last_sequence + EVENT_ID_BLOCK_SIZE, MAX_EVENT_SEQUENCE)
        self._reserved_through = end
        if self.storage_manager is not None:
            self.storage_manager.write_metadata(
                "runtime",
                "event_sequence.json",
                {"year": year, "reserved_through": end},
            )

    def _discard(self, pending: queue.Queue[dict[str, Any]]) -> None:
        while True:
            try:
                pending.get_nowait()
            except queue.Empty:
                return
            pending.task_done()

    def _log(self, action: str, state: str, message: str) -> None:
        if self.logger is not None:
            self.logger.log(action, state, message, component="EventBus")


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _is_utc_timestamp(value: object) -> bool:
    if not isinstance(value, str) or not value.endswith("Z"):
        return False
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        return False
    return parsed.utcoffset() is not None and parsed.utcoffset().total_seconds() == 0
