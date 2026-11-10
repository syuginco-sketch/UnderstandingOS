from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from runtime.exceptions import PersistenceError


STORAGE_CATEGORIES = {"metadata", "contracts", "runtime"}


class StorageManager:
    def __init__(self, root: str | Path = "app", audit_client: object | None = None) -> None:
        self.root = Path(root)
        self.audit_client = audit_client
        self.pending_audit_events: list[tuple[str, str, dict[str, str]]] = []
        self.state = "INITIALIZED"

    def initialize(self) -> None:
        for category in STORAGE_CATEGORIES:
            (self.root / category).mkdir(parents=True, exist_ok=True)

    def start(self) -> None:
        self.initialize()
        self.state = "RUNNING"

    def stop(self) -> None:
        self.state = "STOPPED"

    def health(self) -> dict[str, str]:
        return {"service": "StorageManager", "state": self.state}

    def read_json(self, category: str, filename: str) -> dict[str, Any] | None:
        path = self._path(category, filename)
        if not path.exists():
            return None
        try:
            with path.open("r", encoding="utf-8") as file:
                value = json.load(file)
        except (OSError, json.JSONDecodeError) as error:
            raise PersistenceError(f"Unable to read metadata file {path}: {error}") from error
        if not isinstance(value, dict):
            raise PersistenceError(f"Metadata file must contain a JSON object: {path}")
        return value

    def write_json(self, category: str, filename: str, value: Mapping[str, Any]) -> Path:
        path = self._path(category, filename)
        if not isinstance(value, Mapping):
            raise PersistenceError("Only JSON object metadata can be stored")
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=path.parent,
                prefix=f".{path.name}.",
                suffix=".tmp",
                delete=False,
            ) as file:
                temporary_path = Path(file.name)
                json.dump(value, file, ensure_ascii=False, indent=2, sort_keys=True)
                file.write("\n")
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary_path, path)
        except (OSError, TypeError, ValueError) as error:
            if temporary_path is not None:
                try:
                    temporary_path.unlink(missing_ok=True)
                except OSError as cleanup_error:
                    raise PersistenceError(
                        f"Unable to persist metadata file {path}: {error}; "
                        f"temporary-file cleanup also failed: {cleanup_error}"
                    ) from error
            raise PersistenceError(f"Unable to persist metadata file {path}: {error}") from error
        return path

    def write_metadata(
        self,
        category: str,
        filename: str,
        value: Mapping[str, Any],
        *,
        source: str = "DiverGuard",
        schema_version: str = "1.0",
    ) -> Path:
        if category not in {"metadata", "runtime", "contracts"}:
            raise PersistenceError(f"Unsupported metadata category: {category}")
        prior = self.read_json(category, filename)
        now = _timestamp()
        record = dict(value)
        record.update(
            {
                "created_at": prior.get("created_at", now) if prior else now,
                "updated_at": now,
                "schema_version": schema_version,
                "source": source,
            }
        )
        path = self.write_json(category, filename, record)
        event_type = "ContractPersisted" if category == "contracts" else "MetadataSaved"
        self._emit(event_type, f"Persisted {category}/{filename}", {
            "category": category,
            "filename": filename,
        })
        return path

    def restore_json(self, category: str, filename: str) -> dict[str, Any] | None:
        value = self.read_json(category, filename)
        if value is not None:
            self._emit(
                "MetadataRestored",
                f"Restored {category}/{filename}",
                {"category": category, "filename": filename},
            )
        return value

    def publish_pending_events(self) -> None:
        if self.audit_client is None:
            return
        pending, self.pending_audit_events = self.pending_audit_events, []
        for event_type, reason, payload in pending:
            self.audit_client.publish(event_type, reason=reason, payload=payload)

    def _emit(self, event_type: str, reason: str, payload: dict[str, str]) -> None:
        if self.audit_client is None:
            self.pending_audit_events.append((event_type, reason, payload))
            return
        self.audit_client.publish(event_type, reason=reason, payload=payload)

    def _path(self, category: str, filename: str) -> Path:
        if category not in STORAGE_CATEGORIES:
            raise PersistenceError(f"Unsupported storage category: {category}")
        name = Path(filename)
        if name.name != filename or filename in {"", ".", ".."}:
            raise PersistenceError("Metadata filename must be a single path component")
        return self.root / category / filename


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
