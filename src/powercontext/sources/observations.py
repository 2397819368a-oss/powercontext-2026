# Copyright (c) 2026 OceanBase.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Worker-owned Source Definition manifests and projected observations."""

from __future__ import annotations

import hashlib
from dataclasses import is_dataclass
from typing import Any, Literal

import rfc8785
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    SerializerFunctionWrapHandler,
    TypeAdapter,
    field_validator,
    model_serializer,
    model_validator,
)
from pydantic.json_schema import GenerateJsonSchema, JsonSchemaValue
from pydantic_core import core_schema
from typing_extensions import override

from powercontext.errors import (
    InvalidSourceDefinitionError,
    InvalidSourceObservationError,
    InvalidSourceProjectionError,
)
from powercontext.limits import MAX_SOURCE_TYPE_LENGTH
from powercontext.sources.definitions import (
    SourceDefinition,
    SourceDefinitionRegistry,
    definition_memory_evidence,
)
from powercontext.sources.models import (
    DECLARATION_OWNED_FIELDS,
    MemoryEvidenceDeclaration,
    Source,
    SourceMaterialization,
    SourceProjectionKey,
)

_JSON_VALUE = TypeAdapter(JsonValue)


class SourceProjectionManifest(BaseModel):
    """Declarative schema for one worker-computed named projection."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    key: SourceProjectionKey
    schema_: dict[str, JsonValue] = Field(alias="schema")

    @field_validator("schema_")
    @classmethod
    def validate_schema_references(cls, value: dict[str, JsonValue]) -> dict[str, JsonValue]:
        _reject_remote_schema_references(value)
        return value


class SourceDefinitionManifest(BaseModel):
    """Immutable declarative identity registered by a remote worker."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    version: str
    fingerprint: str
    source_schema: dict[str, JsonValue]
    projections: tuple[SourceProjectionManifest, ...] = ()
    memory_evidence: MemoryEvidenceDeclaration = MemoryEvidenceDeclaration()

    @field_validator("name", "version")
    @classmethod
    def validate_identity(cls, value: str) -> str:
        if not value or value.strip() != value or len(value) > MAX_SOURCE_TYPE_LENGTH:
            raise ValueError("manifest identity must be a bounded non-empty trimmed string")  # noqa: TRY003
        return value

    @field_validator("projections")
    @classmethod
    def validate_projection_limit(
        cls,
        value: tuple[SourceProjectionManifest, ...],
    ) -> tuple[SourceProjectionManifest, ...]:
        if len(value) > 16:
            raise ValueError("manifest must not declare more than 16 projections")  # noqa: TRY003
        return value

    @field_validator("source_schema")
    @classmethod
    def validate_source_schema_references(cls, value: dict[str, JsonValue]) -> dict[str, JsonValue]:
        _reject_remote_schema_references(value)
        return value

    @field_validator("fingerprint")
    @classmethod
    def validate_fingerprint_shape(cls, value: str) -> str:
        if not value.startswith("sha256:") or len(value) != 71:
            raise ValueError("manifest fingerprint must use sha256:<hex>")  # noqa: TRY003
        try:
            int(value.removeprefix("sha256:"), 16)
        except ValueError as error:
            raise ValueError("manifest fingerprint must contain lowercase hexadecimal") from error  # noqa: TRY003
        if value != value.lower():
            raise ValueError("manifest fingerprint must contain lowercase hexadecimal")  # noqa: TRY003
        return value

    @model_validator(mode="after")
    def validate_manifest(self) -> SourceDefinitionManifest:
        keys = tuple(projection.key for projection in self.projections)
        if len(set(keys)) != len(keys):
            raise ValueError("manifest projection keys must be unique")  # noqa: TRY003
        expected = _source_definition_fingerprint(
            name=self.name,
            version=self.version,
            source_schema=self.source_schema,
            projections=self.projections,
            memory_evidence=self.memory_evidence,
        )
        if self.fingerprint != expected:
            raise ValueError("manifest fingerprint does not match its declaration")  # noqa: TRY003
        return self

    @model_serializer(mode="wrap")
    def serialize_manifest(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        """Serialize the manifest in the canonical form its fingerprint covers.

        A manifest whose declaration is absent or neutral carries no ranking
        preference, so it must serialize without that field. Writing the neutral
        default would make the stored payload validate against a fingerprint that
        excludes it, which makes registration unreadable.
        """

        dumped = handler(self)
        if _declared_evidence(self.memory_evidence) is None:
            dumped.pop("memory_evidence", None)
        return dumped


class SourceProjectionValue(BaseModel):
    """One named projection computed by the worker that owns the Definition."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    key: SourceProjectionKey
    value: JsonValue


class SourceObservation(Source):
    """Canonical captured observation stored without loading worker plugin code."""

    materialization: Literal[SourceMaterialization.CAPTURED] = SourceMaterialization.CAPTURED
    source_type: str
    definition_fingerprint: str
    payload: dict[str, JsonValue]
    projections: tuple[SourceProjectionValue, ...] = ()

    @field_validator("source_type")
    @classmethod
    def validate_source_type(cls, value: str) -> str:
        if not value or value.strip() != value or len(value) > MAX_SOURCE_TYPE_LENGTH:
            raise ValueError("source_type must be a bounded non-empty trimmed string")  # noqa: TRY003
        return value

    @model_validator(mode="after")
    def validate_envelope_identity(self) -> SourceObservation:
        expected = {
            "name": self.name,
            "definition_version": self.definition_version,
            "materialization": self.materialization.value,
            "description": self.description,
        }
        for field, value in expected.items():
            if self.payload.get(field) != value:
                raise ValueError(f"projected Source payload {field} does not match its envelope")  # noqa: TRY003
        keys = tuple(projection.key for projection in self.projections)
        if len(set(keys)) != len(keys):
            raise ValueError("projected Source projection keys must be unique")  # noqa: TRY003
        return self

    def projection(self, key: SourceProjectionKey, /) -> JsonValue:
        for projection in self.projections:
            if projection.key == key:
                return projection.value
        raise InvalidSourceProjectionError(key.name, "key", "was not supplied by the worker")


def _input_contract_schema(source_class: type[Source]) -> dict[str, JsonValue]:
    """Return a Source class's input contract, without Definition-owned metadata.

    The declaration is stamped from the Definition rather than carried by the
    captured value, so it is not part of what a worker's payload has to satisfy.
    Leaving it in would change the schema of every Source class that inherits it,
    and the manifest fingerprint covers that schema, so a Definition registered
    before the field existed would no longer match the one a worker emits. The
    orphaned definitions the removed property referenced are dropped with it,
    because an unused ``$defs`` entry still changes the hashed document.
    """

    return _json_object(source_class.model_json_schema(schema_generator=_SourceInputSchema))


class _SourceInputSchema(GenerateJsonSchema):
    """Omit Source metadata before Pydantic assigns captured schema reference names."""

    @override
    def model_schema(self, schema: core_schema.ModelSchema) -> JsonSchemaValue:
        if issubclass(schema["cls"], Source):
            schema = schema.copy()
            schema["schema"] = _source_input_fields(schema["schema"])
        return super().model_schema(schema)

    @override
    def encode_default(self, dft: Any) -> Any:
        if isinstance(dft, (BaseModel, list, tuple, set, frozenset, dict)) or is_dataclass(dft):
            dft = _normalize_default(dft, by_alias=self.by_alias)
        return super().encode_default(dft)


def _source_input_fields(schema: Any) -> Any:
    """Copy only the wrappers containing this Source's field definitions."""

    if schema["type"] == "model-fields":
        return {
            **schema,
            "fields": {name: field for name, field in schema["fields"].items() if name not in DECLARATION_OWNED_FIELDS},
        }
    if schema["type"] in {"function-before", "function-after", "function-wrap"}:
        return {**schema, "schema": _source_input_fields(schema["schema"])}
    return schema


def _normalize_default(value: Any, *, by_alias: bool) -> Any:
    """Serialize defaults while removing Source-owned declaration fields."""
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json", by_alias=by_alias, context={"source_transport_payload": True})
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_normalize_default(item, by_alias=by_alias) for item in value]
    if isinstance(value, dict):
        return {key: _normalize_default(item, by_alias=by_alias) for key, item in value.items()}
    if is_dataclass(value) and not isinstance(value, type):
        return TypeAdapter(type(value)).dump_python(
            value, mode="json", by_alias=by_alias, context={"source_transport_payload": True}
        )
    return value


def manifest_for_definition(definition: SourceDefinition[Any, Any, Any], /) -> SourceDefinitionManifest:
    """Build the immutable declaration transported by a remote worker."""

    source_schema = _input_contract_schema(definition.source_class)
    declared = definition_memory_evidence(definition)
    projections = tuple(
        SourceProjectionManifest(
            key=SourceProjectionKey(name=projection.name, version=projection.version),
            schema=projection.output_class.model_json_schema(),
        )
        for projection in definition.projections
    )
    # A neutral declaration is carried by omission, never as an explicit field. The
    # fingerprint excludes it, so setting it would make this manifest serialize
    # differently from the same manifest decoded from its stored form, and the
    # round trip a remote worker checks would no longer agree.
    return SourceDefinitionManifest(
        name=definition.name,
        version=definition.version,
        fingerprint=_source_definition_fingerprint(
            name=definition.name,
            version=definition.version,
            source_schema=source_schema,
            projections=projections,
            memory_evidence=declared,
        ),
        source_schema=source_schema,
        projections=projections,
        **({} if _declared_evidence(declared) is None else {"memory_evidence": declared}),
    )


def project_source_for_transport(
    registry: SourceDefinitionRegistry,
    source: Source,
    /,
) -> SourceObservation:
    """Execute one worker-owned Definition and serialize its durable result."""

    definition = registry.definition_for_source(source)
    if source.materialization is not SourceMaterialization.CAPTURED:
        raise InvalidSourceObservationError(
            "materialization",
            "remote Source observations must retain their canonical value",
        )
    manifest = manifest_for_definition(definition)
    # The captured payload has to satisfy the contract the manifest advertises, and
    # Definition-owned metadata is not part of it. Carrying it here would make a
    # generated observation fail its own schema, and would change the payload of an
    # unchanged Source, so replaying a pre-upgrade observation would conflict.
    payload = _json_object(source.model_dump(mode="json", context={"source_transport_payload": True}))
    projections = tuple(
        SourceProjectionValue(key=key, value=registry.project(source, key)) for key in registry.projection_keys(source)
    )
    return SourceObservation(
        name=source.name,
        definition_version=source.definition_version,
        materialization=SourceMaterialization.CAPTURED,
        description=source.description,
        source_type=definition.name,
        definition_fingerprint=manifest.fingerprint,
        memory_evidence=manifest.memory_evidence,
        payload=payload,
        projections=projections,
    )


def _declared_evidence(
    memory_evidence: MemoryEvidenceDeclaration | None,
) -> dict[str, JsonValue] | None:
    """Return the evidence fields a fingerprint covers, or ``None`` when neutral.

    The neutral declaration is Definition-owned metadata that carries no ranking
    preference, so it must not contribute to a Definition's content-addressed
    identity. Folding it in would change the fingerprint of every Definition that
    already registered one, and the immutable ``(name, version)`` registration
    would then reject the re-registration every remote worker performs on each
    run. A declaration that actually asserts an authority or verification level
    still changes the fingerprint, because it does change what the Definition
    attests.
    """

    if memory_evidence is None or memory_evidence == MemoryEvidenceDeclaration():
        return None
    return {"memory_evidence": memory_evidence.model_dump(mode="json")}


def _source_definition_fingerprint(
    *,
    name: str,
    version: str,
    source_schema: dict[str, JsonValue],
    projections: tuple[SourceProjectionManifest, ...],
    memory_evidence: MemoryEvidenceDeclaration | None,
) -> str:
    declaration = {
        "name": name,
        "version": version,
        "source_schema": source_schema,
        "projections": [projection.model_dump(mode="json", by_alias=True) for projection in projections],
        **(_declared_evidence(memory_evidence) or {}),
    }
    encoded = rfc8785.dumps(_JSON_VALUE.validate_python(declaration))
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _json_object(value: object) -> dict[str, JsonValue]:
    validated = _JSON_VALUE.validate_python(value)
    if not isinstance(validated, dict):
        raise InvalidSourceDefinitionError(type(value), "schema", "must be a JSON object")
    return validated


def _reject_remote_schema_references(schema: JsonValue) -> None:
    pending = [schema]
    while pending:
        value = pending.pop()
        if isinstance(value, dict):
            for key, nested in value.items():
                if key in {"$ref", "$dynamicRef"} and isinstance(nested, str) and not nested.startswith("#"):
                    raise ValueError("remote schema references are not allowed")  # noqa: TRY003
                pending.append(nested)
        elif isinstance(value, list):
            pending.extend(value)


__all__ = [
    "SourceDefinitionManifest",
    "SourceObservation",
    "SourceProjectionManifest",
    "SourceProjectionValue",
    "manifest_for_definition",
    "project_source_for_transport",
]
