from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from app.storage_manager import StorageManager
from contracts.definition import ContractDefinition, ReferenceRule
from runtime.exceptions import ClockError, ContractValidationError, VersionCompatibilityError
from runtime.protocols import ShutdownPriority


IDENTIFIER_PATTERNS = {
    "Decision": r"^DEC-\d{4}-\d{6}$",
    "Evidence": r"^EVD-\d{4}-\d{6}$",
    "Knowledge": r"^KNW-\d{4}-\d{6}$",
    "Incident": r"^INC-\d{4}-\d{6}$",
    "Audit": r"^AUD-\d{4}-\d{6}$",
}
REFERENCE_TARGETS = {
    "incident_id": ("Incident", False),
    "incident_refs": ("Incident", True),
    "related_incident": ("Incident", False),
    "decision_id": ("Decision", False),
    "decision_refs": ("Decision", True),
    "previous_decision_id": ("Decision", False),
    "related_decision": ("Decision", False),
    "evidence_id": ("Evidence", False),
    "evidence_refs": ("Evidence", True),
    "related_evidence": ("Evidence", False),
    "knowledge_refs": ("Knowledge", True),
    "related_knowledge": ("Knowledge", False),
    "audit_id": ("Audit", False),
}
SCHEMA_FILES = (
    "151_DECISION_SCHEMA.md",
    "152_STATISTICS_SCHEMA.md",
    "153_KNOWLEDGE_SCHEMA.md",
    "154_EVIDENCE_SCHEMA.md",
    "155_INCIDENT_SCHEMA.md",
    "156_AUDIT_SCHEMA.md",
)
VERSION_PATTERN = re.compile(r"^(\d+)\.(\d+)$")


class ContractRegistry:
    shutdown_priority = ShutdownPriority.DEFAULT

    def __init__(
        self,
        schema_directory: str | Path | None = None,
        storage_manager: StorageManager | None = None,
        audit_client: object | None = None,
    ) -> None:
        repository_root = Path(__file__).resolve().parents[1]
        self.schema_directory = (
            Path(schema_directory)
            if schema_directory is not None
            else repository_root / "specs" / "CLOCKv3.2" / "150_DATA_CONTRACTS"
        )
        self.storage_manager = storage_manager or StorageManager()
        self.audit_client = audit_client
        self._definitions: dict[str, ContractDefinition] = {}
        self._previous_metadata: dict[str, dict[str, Any]] = {}
        self.pending_audit_events: list[tuple[str, str]] = []
        self.state = "INITIALIZED"

    def load(self) -> tuple[ContractDefinition, ...]:
        self.state = "STARTING"
        self._definitions.clear()
        try:
            persisted = self.storage_manager.restore_json(
                "contracts", "compatibility_matrix.json"
            )
            previous = persisted.get("contracts", {}) if persisted else {}
            if not isinstance(previous, dict):
                raise ContractValidationError("Persisted compatibility metadata is invalid")
            self._previous_metadata = previous
            for filename in SCHEMA_FILES:
                path = self.schema_directory / filename
                if not path.is_file():
                    raise ContractValidationError(f"Required schema document is missing: {path}")
                definition = parse_schema_document(path)
                self.register(definition)
            self._persist_registry_metadata()
        except (ClockError, OSError):
            self.state = "FAILED"
            raise
        self.state = "INITIALIZED"
        return self.list_schemas()

    def register(self, schema: ContractDefinition | dict[str, Any]) -> ContractDefinition:
        definition = (
            ContractDefinition.from_dict(schema)
            if isinstance(schema, dict)
            else schema
        )
        validate_definition(definition)
        name = definition.schema_name
        if name in self._definitions:
            raise ContractValidationError(f"Contract is already registered: {name}")
        self._check_compatibility(definition)
        self._definitions[name] = definition
        self._emit("ContractRegistered", f"Registered {name} schema {definition.schema_version}")
        return definition

    def resolve(self, schema_name: str) -> ContractDefinition:
        try:
            return self._definitions[schema_name]
        except KeyError as error:
            raise ContractValidationError(f"Contract is not registered: {schema_name}") from error

    def exists(self, schema_name: str) -> bool:
        return schema_name in self._definitions

    def version(self, schema_name: str) -> str:
        return self.resolve(schema_name).schema_version

    def list_schemas(self) -> tuple[ContractDefinition, ...]:
        return tuple(self._definitions[name] for name in sorted(self._definitions))

    def validate(self, schema: ContractDefinition | dict[str, Any]) -> None:
        definition = ContractDefinition.from_dict(schema) if isinstance(schema, dict) else schema
        validate_definition(definition)

    def validate_record(self, schema_name: str, record: dict[str, Any]) -> None:
        definition = self.resolve(schema_name)
        if not isinstance(record, dict):
            raise ContractValidationError("Contract records must be JSON objects")

        supported_fields = set(definition.required_fields) | set(definition.optional_fields)
        missing = set(definition.required_fields) - set(record)
        if missing:
            raise ContractValidationError(f"Missing required fields: {sorted(missing)}")
        unknown = set(record) - supported_fields
        if unknown:
            raise ContractValidationError(f"Fields are not declared by the schema: {sorted(unknown)}")

        for field_name, value in record.items():
            expected_type = definition.field_types.get(field_name)
            if expected_type is None:
                raise ContractValidationError(
                    f"No example-derived type is defined for field {field_name}"
                )
            if not _matches_type(value, expected_type):
                raise ContractValidationError(
                    f"Field {field_name} must be {expected_type}, got {type(value).__name__}"
                )

        version = record.get("schema_version")
        if not isinstance(version, str) or not _version_is_compatible(
            version, definition.schema_version
        ):
            raise ContractValidationError(
                f"Record version {version!r} is incompatible with {definition.schema_version}"
            )

        identifier = record.get(definition.identifier_field)
        if definition.identifier_pattern and (
            not isinstance(identifier, str)
            or re.fullmatch(definition.identifier_pattern, identifier) is None
        ):
            raise ContractValidationError(
                f"{definition.identifier_field} does not match the required identifier format"
            )

        for timestamp_field in definition.timestamp_fields:
            if timestamp_field in record and not _is_utc_timestamp(record[timestamp_field]):
                raise ContractValidationError(
                    f"Field {timestamp_field} must be an ISO-8601 UTC timestamp"
                )

        for rule in definition.reference_rules:
            if rule.field_name not in record:
                continue
            value = record[rule.field_name]
            values = value if rule.is_list else [value]
            for reference in values:
                if not isinstance(reference, str):
                    raise ContractValidationError(
                        f"Reference {rule.field_name} must contain string identifiers"
                    )
                if rule.identifier_pattern and re.fullmatch(rule.identifier_pattern, reference) is None:
                    raise ContractValidationError(
                        f"Reference {reference!r} is not a valid {rule.target_schema} identifier"
                    )

    def initialize(self) -> None:
        self.load()

    def start(self) -> None:
        if not self._definitions:
            self.load()
        self.state = "RUNNING"

    def stop(self) -> None:
        self.state = "STOPPED"

    def health(self) -> dict[str, str]:
        return {"service": "ContractRegistry", "state": self.state}

    def _check_compatibility(self, definition: ContractDefinition) -> None:
        previous = self._previous_metadata.get(definition.schema_name)
        if not previous:
            return
        old_version = previous.get("schema_version")
        if not isinstance(old_version, str):
            raise VersionCompatibilityError(
                f"Persisted version metadata is invalid for {definition.schema_name}"
            )
        old_parts = VERSION_PATTERN.fullmatch(old_version)
        new_parts = VERSION_PATTERN.fullmatch(definition.schema_version)
        if old_parts is None or new_parts is None:
            raise VersionCompatibilityError("Contract versions must use MAJOR.MINOR format")
        old_major, old_minor = map(int, old_parts.groups())
        new_major, new_minor = map(int, new_parts.groups())
        if old_major != new_major:
            raise VersionCompatibilityError(
                f"{definition.schema_name} major version change requires migration"
            )
        if new_minor < old_minor:
            raise VersionCompatibilityError(
                f"{definition.schema_name} cannot register an older minor version"
            )
        if new_minor == old_minor and previous.get("fingerprint") != _fingerprint(definition):
            if not _is_type_enrichment(previous, definition):
                raise VersionCompatibilityError(
                    f"{definition.schema_name} changed without a version increment"
                )
        if new_minor > old_minor:
            old_required = set(previous.get("required_fields", []))
            new_required = set(definition.required_fields)
            if old_required != new_required:
                raise VersionCompatibilityError(
                    f"{definition.schema_name} minor versions may not change required fields"
                )
            old_optional = set(previous.get("optional_fields", []))
            new_optional = set(definition.optional_fields)
            if not old_optional <= new_optional:
                raise VersionCompatibilityError(
                    f"{definition.schema_name} minor versions may not remove optional fields"
                )
            old_types = previous.get("field_types", {})
            for field_name, old_type in old_types.items():
                if definition.field_types.get(field_name) != old_type:
                    raise VersionCompatibilityError(
                        f"{definition.schema_name} minor versions may not change field types"
                    )

    def _persist_registry_metadata(self) -> None:
        contracts = {
            name: {
                "schema_version": definition.schema_version,
                "required_fields": list(definition.required_fields),
                "optional_fields": list(definition.optional_fields),
                "field_types": dict(definition.field_types),
                "fingerprint": _fingerprint(definition),
            }
            for name, definition in sorted(self._definitions.items())
        }
        registry_summary = {
            "registered_contracts": sorted(self._definitions),
            "contract_versions": {
                name: definition.schema_version
                for name, definition in sorted(self._definitions.items())
            },
        }
        self.storage_manager.write_metadata(
            "contracts", "registered_contracts.json", registry_summary
        )
        self.storage_manager.write_metadata(
            "contracts",
            "contract_versions.json",
            {"versions": registry_summary["contract_versions"]},
        )
        self.storage_manager.write_metadata(
            "contracts", "compatibility_matrix.json", {"contracts": contracts}
        )

    def _emit(self, event_type: str, reason: str) -> None:
        if self.audit_client is None:
            self.pending_audit_events.append((event_type, reason))
            return
        self.audit_client.publish(event_type, reason=reason)


def parse_schema_document(path: str | Path) -> ContractDefinition:
    source_path = Path(path)
    text = source_path.read_text(encoding="utf-8")
    schema_name = _schema_name(source_path)
    examples = _json_examples(text)
    canonical = _runtime_schema_definition(examples, schema_name)
    if canonical is None:
        required = _field_headings(text, "Required Fields")
        optional = _field_headings(text, "Optional Fields")
        versioned_example = next(
            (example for example in examples if isinstance(example.get("schema_version"), str)),
            None,
        )
        if versioned_example is None:
            raise ContractValidationError(
                f"No versioned JSON schema example found in {source_path}"
            )
        schema_version = versioned_example["schema_version"]
        fields = set(required) | set(optional)
        field_types, timestamp_fields = _infer_example_types(examples, fields, source_path)
    else:
        schema_version = canonical["schema_version"]
        required_types = canonical["required_fields"]
        optional_types = canonical["optional_fields"]
        required = list(required_types)
        optional = list(optional_types)
        fields = set(required) | set(optional)
        field_types = {**required_types, **optional_types}
        timestamp_fields = {
            field_name for field_name, field_type in field_types.items()
            if field_type == "datetime"
        }
        _validate_canonical_examples(examples, field_types, source_path)

    identifier_field = _identifier_field(schema_name)
    reference_rules = tuple(
        ReferenceRule(
            field_name=name,
            target_schema=target,
            is_list=is_list,
            identifier_pattern=IDENTIFIER_PATTERNS.get(target),
        )
        for name, (target, is_list) in REFERENCE_TARGETS.items()
        if name in fields
    )
    lifecycle_states = _lifecycle_states(text)
    definition = ContractDefinition(
        schema_name=schema_name,
        schema_version=schema_version,
        required_fields=tuple(required),
        optional_fields=tuple(optional),
        field_types=field_types,
        identifier_field=identifier_field,
        identifier_pattern=IDENTIFIER_PATTERNS.get(schema_name),
        reference_rules=reference_rules,
        lifecycle_states=tuple(lifecycle_states),
        timestamp_fields=tuple(sorted(timestamp_fields)),
        source_path=str(source_path),
    )
    validate_definition(definition)
    return definition


def _infer_example_types(
    examples: list[dict[str, Any]],
    fields: set[str],
    source_path: Path,
) -> tuple[dict[str, str], set[str]]:
    observed_types: dict[str, set[str]] = {}
    timestamp_fields: set[str] = set()
    for example in examples:
        for name, value in example.items():
            if name not in fields:
                continue
            data_type = _json_type(value)
            observed_types.setdefault(name, set()).add(data_type)
            if isinstance(value, str) and _is_utc_timestamp(value):
                timestamp_fields.add(name)
    normalized_types = {
        name: _normalize_example_types(values)
        for name, values in observed_types.items()
    }
    conflicting = {
        name: values for name, values in normalized_types.items() if len(values) > 1
    }
    if conflicting:
        raise ContractValidationError(
            f"Conflicting example-derived field types in {source_path}: {conflicting}"
        )
    field_types = {name: next(iter(types)) for name, types in normalized_types.items()}
    return field_types, timestamp_fields


def _runtime_schema_definition(
    examples: list[dict[str, Any]], schema_name: str
) -> dict[str, Any] | None:
    for example in examples:
        if (
            example.get("schema_name") != schema_name
            or not isinstance(example.get("schema_version"), str)
        ):
            continue
        required = example.get("required_fields")
        optional = example.get("optional_fields")
        if not isinstance(required, dict) or not isinstance(optional, dict):
            continue
        if any(not isinstance(value, str) for value in (*required.values(), *optional.values())):
            raise ContractValidationError(
                f"Runtime schema field types must be strings for {schema_name}"
            )
        return {
            "schema_version": example["schema_version"],
            "required_fields": required,
            "optional_fields": optional,
        }
    return None


def _validate_canonical_examples(
    examples: list[dict[str, Any]],
    field_types: dict[str, str],
    source_path: Path,
) -> None:
    for example in examples:
        for field_name, value in example.items():
            if field_name not in field_types:
                continue
            example_type = _json_type(value)
            expected_type = field_types[field_name]
            if not _example_type_matches(example_type, expected_type, value):
                raise ContractValidationError(
                    f"Example type for {field_name} conflicts with its declared "
                    f"type in {source_path}: {example_type} != {expected_type}"
                )


def _example_type_matches(example_type: str, expected_type: str, value: Any) -> bool:
    if expected_type == "datetime":
        return isinstance(value, str) and _is_utc_timestamp(value)
    if expected_type == "array" and example_type.startswith("array["):
        return True
    if expected_type.startswith("array[") and example_type == "array" and not value:
        return True
    return example_type == expected_type


def validate_definition(definition: ContractDefinition) -> None:
    if not definition.schema_name or not VERSION_PATTERN.fullmatch(definition.schema_version):
        raise ContractValidationError("Schema name and MAJOR.MINOR version are required")
    if not definition.required_fields:
        raise ContractValidationError(f"{definition.schema_name} has no required fields")
    required = set(definition.required_fields)
    optional = set(definition.optional_fields)
    if required & optional:
        raise ContractValidationError(
            f"Fields cannot be both required and optional: {sorted(required & optional)}"
        )
    missing_types = (required | optional) - set(definition.field_types)
    if missing_types:
        raise ContractValidationError(
            f"Schema does not define types for fields: {sorted(missing_types)}"
        )
    unsupported_types = {
        field_name: field_type
        for field_name, field_type in definition.field_types.items()
        if not _is_supported_type(field_type)
    }
    if unsupported_types:
        raise ContractValidationError(f"Schema contains unsupported field types: {unsupported_types}")
    if definition.identifier_field not in required:
        raise ContractValidationError(
            f"Identifier field {definition.identifier_field} must be required"
        )
    if definition.identifier_pattern and re.compile(definition.identifier_pattern) is None:
        raise ContractValidationError("Identifier pattern is invalid")


def _schema_name(path: Path) -> str:
    match = re.fullmatch(r"\d+_([A-Z]+)_SCHEMA\.md", path.name)
    if match is None:
        raise ContractValidationError(f"Unsupported schema filename: {path.name}")
    return match.group(1).capitalize()


def _field_headings(text: str, section: str) -> list[str]:
    match = re.search(
        rf"^# {re.escape(section)}\s*\r?\n([\s\S]*?)(?=^# (?!#)|\Z)",
        text,
        re.MULTILINE,
    )
    if match is None:
        raise ContractValidationError(f"Schema is missing the {section} section")
    return re.findall(r"^## ([A-Za-z][A-Za-z0-9_]*)\s*$", match.group(1), re.MULTILINE)


def _json_examples(text: str) -> list[dict[str, Any]]:
    examples = []
    for block in re.findall(r"```json\s*(.*?)```", text, re.DOTALL | re.IGNORECASE):
        try:
            value = json.loads(block)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            examples.append(value)
    return examples


def _json_type(value: Any) -> str:
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "float"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        if value:
            item_types = {_json_type(item) for item in value}
            if len(item_types) == 1:
                return f"array[{next(iter(item_types))}]"
        return "array"
    if isinstance(value, dict):
        return "object"
    raise ContractValidationError(f"Unsupported JSON example value type: {type(value).__name__}")


def _normalize_example_types(types: set[str]) -> set[str]:
    parameterized_arrays = {value for value in types if value.startswith("array[")}
    other_types = types - {"array"} - parameterized_arrays
    if "array" in types and not other_types and len(parameterized_arrays) <= 1:
        return parameterized_arrays or {"array"}
    return types


def _identifier_field(schema_name: str) -> str:
    if schema_name == "Statistics":
        return "statistics_id"
    return f"{schema_name.lower()}_id"


def _lifecycle_states(text: str) -> list[str]:
    match = re.search(
        r"^# [^\r\n]*Lifecycle[^\r\n]*\s*\r?\n([\s\S]*?)(?=^# (?!#)|\Z)",
        text,
        re.MULTILINE | re.IGNORECASE,
    )
    if match is None:
        return []
    return re.findall(r"^## ([A-Z][A-Z_]+)\s*$", match.group(1), re.MULTILINE)


def _matches_type(value: Any, expected_type: str) -> bool:
    if expected_type.startswith("array[") and expected_type.endswith("]"):
        item_type = expected_type[6:-1]
        return isinstance(value, list) and all(
            _matches_type(item, item_type) for item in value
        )
    return {
        "string": lambda item: isinstance(item, str),
        "integer": lambda item: isinstance(item, int) and not isinstance(item, bool),
        "float": lambda item: isinstance(item, float),
        "boolean": lambda item: isinstance(item, bool),
        "datetime": lambda item: isinstance(item, str) and _is_utc_timestamp(item),
        "array": lambda item: isinstance(item, list),
        "object": lambda item: isinstance(item, dict),
    }.get(expected_type, lambda _: False)(value)


def _is_supported_type(field_type: str) -> bool:
    if field_type in {"string", "integer", "float", "boolean", "datetime", "array", "object"}:
        return True
    if field_type.startswith("array[") and field_type.endswith("]"):
        return _is_supported_type(field_type[6:-1])
    return False


def _is_type_enrichment(
    previous: dict[str, Any], definition: ContractDefinition
) -> bool:
    if set(previous.get("required_fields", [])) != set(definition.required_fields):
        return False
    if set(previous.get("optional_fields", [])) != set(definition.optional_fields):
        return False
    old_types = previous.get("field_types", {})
    if not isinstance(old_types, dict):
        return False
    optional_fields = set(definition.optional_fields)
    all_fields = set(definition.required_fields) | optional_fields
    if set(old_types) - all_fields:
        return False
    for field_name in all_fields:
        old_type = old_types.get(field_name)
        new_type = definition.field_types.get(field_name)
        if old_type is None:
            if field_name not in optional_fields:
                return False
            continue
        if old_type == new_type:
            continue
        if old_type == "array" and isinstance(new_type, str) and new_type.startswith("array["):
            continue
        if (
            old_type == "string"
            and new_type == "datetime"
            and field_name in definition.timestamp_fields
        ):
            continue
        return False
    return True


def _version_is_compatible(record_version: str, registered_version: str) -> bool:
    record = VERSION_PATTERN.fullmatch(record_version)
    registered = VERSION_PATTERN.fullmatch(registered_version)
    if record is None or registered is None:
        return False
    record_major, record_minor = map(int, record.groups())
    current_major, current_minor = map(int, registered.groups())
    return record_major == current_major and record_minor <= current_minor


def _is_utc_timestamp(value: object) -> bool:
    if not isinstance(value, str) or not value.endswith("Z"):
        return False
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        return False
    return parsed.utcoffset() is not None and parsed.utcoffset().total_seconds() == 0


def _fingerprint(definition: ContractDefinition) -> str:
    import hashlib

    canonical = json.dumps(
        {
            "required_fields": definition.required_fields,
            "optional_fields": definition.optional_fields,
            "field_types": definition.field_types,
            "identifier_pattern": definition.identifier_pattern,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
