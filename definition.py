from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ReferenceRule:
    field_name: str
    target_schema: str
    is_list: bool
    identifier_pattern: str | None = None


@dataclass(frozen=True)
class ContractDefinition:
    schema_name: str
    schema_version: str
    required_fields: tuple[str, ...]
    optional_fields: tuple[str, ...]
    field_types: dict[str, str]
    identifier_field: str
    identifier_pattern: str | None
    reference_rules: tuple[ReferenceRule, ...] = ()
    lifecycle_states: tuple[str, ...] = ()
    timestamp_fields: tuple[str, ...] = ()
    source_path: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_name": self.schema_name,
            "schema_version": self.schema_version,
            "required_fields": list(self.required_fields),
            "optional_fields": list(self.optional_fields),
            "field_types": dict(self.field_types),
            "identifier_field": self.identifier_field,
            "identifier_pattern": self.identifier_pattern,
            "reference_rules": [
                {
                    "field_name": rule.field_name,
                    "target_schema": rule.target_schema,
                    "is_list": rule.is_list,
                    "identifier_pattern": rule.identifier_pattern,
                }
                for rule in self.reference_rules
            ],
            "lifecycle_states": list(self.lifecycle_states),
            "timestamp_fields": list(self.timestamp_fields),
            "source_path": self.source_path,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> ContractDefinition:
        return cls(
            schema_name=value["schema_name"],
            schema_version=value["schema_version"],
            required_fields=tuple(value["required_fields"]),
            optional_fields=tuple(value["optional_fields"]),
            field_types=dict(value["field_types"]),
            identifier_field=value["identifier_field"],
            identifier_pattern=value.get("identifier_pattern"),
            reference_rules=tuple(
                ReferenceRule(
                    field_name=rule["field_name"],
                    target_schema=rule["target_schema"],
                    is_list=rule["is_list"],
                    identifier_pattern=rule.get("identifier_pattern"),
                )
                for rule in value.get("reference_rules", [])
            ),
            lifecycle_states=tuple(value.get("lifecycle_states", [])),
            timestamp_fields=tuple(value.get("timestamp_fields", [])),
            source_path=value.get("source_path", ""),
        )
