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

from __future__ import annotations

import asyncio
import hashlib
import json
import urllib.request
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, cast

import pytest
import rfc8785
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from referencing.exceptions import Unresolvable

from powercontext.builtin.runtime.relational import _json_schema_validator, _validate_source_observation
from powercontext.builtin.sources import CONTENT_SOURCE_DEFINITION, ContentCapture, ContentSource
from powercontext.http import SourceDefinitionManifest as HttpSourceDefinitionManifest
from powercontext.http import SourceObservation as HttpSourceObservation
from powercontext.http import SubmitSourceObservationRequest
from powercontext.limits import MAX_VERSION_LENGTH
from powercontext.server.mapping import runtime_source_definition_manifest, submit_source_observation_request
from powercontext.sources import (
    TEXT_EVIDENCE_PROJECTION_KEY,
    MemoryEvidenceAuthority,
    MemoryEvidenceDeclaration,
    MemoryEvidenceVerification,
    Source,
    SourceCatalog,
    SourceDefinition,
    SourceDefinitionManifest,
    SourceDefinitionRegistry,
    SourceMaterialization,
    SourceObservation,
    TextEvidence,
    definition_memory_evidence,
    manifest_for_definition,
    project_source_for_transport,
)
from powercontext.sources.definitions import AdapterSourceDefinition
from tests.source_contract_models import source_contract_case


class EmptySourceBackend:
    async def list(self) -> tuple[Source, ...]:
        return ()

    async def get(self, source: Source, /) -> Source:
        raise AssertionError(source)


@pytest.mark.parametrize(
    "case",
    ["nested", "title_collision", "reference_collision", "custom_serializer", "aliased_declaration", "aliased_default"],
)
def test_source_contract_preserves_pre_declaration_schema_and_payload(case: str) -> None:
    # Frozen worker output produced without Source declaration metadata.
    baseline = json.loads(
        (Path(__file__).parent / "fixtures/source_declaration_upgrade.json").read_text(encoding="utf-8")
    )
    definition, source = source_contract_case(case)
    registry = SourceDefinitionRegistry((definition,))
    manifest = manifest_for_definition(definition)
    observation = project_source_for_transport(registry, source)

    assert manifest.model_dump(mode="json", by_alias=True) == baseline[case]["manifest"]
    assert observation.payload == baseline[case]["observation"]["payload"]
    _json_schema_validator(manifest.name, manifest.source_schema).validate(observation.payload)


def _legacy_manifest(current: SourceDefinitionManifest) -> SourceDefinitionManifest:
    """Return the declaration shape emitted before the evidence field existed."""

    payload = {
        "name": current.name,
        "version": current.version,
        "source_schema": current.source_schema,
        "projections": [projection.model_dump(mode="json", by_alias=True) for projection in current.projections],
    }
    return SourceDefinitionManifest(
        name=current.name,
        version=current.version,
        source_schema=current.source_schema,
        projections=current.projections,
        fingerprint=f"sha256:{hashlib.sha256(rfc8785.dumps(payload)).hexdigest()}",
    )


def test_neutral_declaration_keeps_the_pre_change_definition_identity() -> None:
    """A Definition registered before the declaration existed must still re-register.

    The declaration is inherited by every Source class, so adding it would otherwise
    change each class's schema, and the fingerprint covers that schema. The
    fingerprint below was computed from the same Definition on the revision before
    the declaration was introduced, so it pins the identity an upgraded worker
    re-registers against a manifest an older worker already registered.
    """

    manifest = manifest_for_definition(CONTENT_SOURCE_DEFINITION)

    declared_properties = cast("dict[str, object]", manifest.source_schema["properties"])
    assert "memory_evidence" not in declared_properties
    # Frozen from the same Definition on master caf2a35a, without declaration metadata.
    assert manifest.fingerprint == ("sha256:4baf43bbfbfe384d33ecb2c8bfb79fca303a065bf9a51545af1de3fb34a87c99")


def test_version_parts_are_rejected_before_they_cannot_be_transported() -> None:
    """A version length must be validated where it is accepted, not on the way out.

    A declaration version travels through the transport model and a bounded
    identity column, so a longer value would register locally and then fail when
    it is converted for transport.
    """

    accepted = MemoryEvidenceDeclaration(declaration_version="v" * MAX_VERSION_LENGTH)
    assert len(accepted.declaration_version) == MAX_VERSION_LENGTH
    with pytest.raises(ValueError, match="declaration_version"):
        MemoryEvidenceDeclaration(declaration_version="v" * (MAX_VERSION_LENGTH + 1))


def _strict_content_definition() -> AdapterSourceDefinition[ContentCapture, ContentSource, str]:
    """Return a Definition whose Source class rejects properties it does not declare."""

    class StrictContentSource(ContentSource):
        model_config = ConfigDict(extra="forbid")

    class StrictContentAdapter:
        name = "strict-content"
        input_class = ContentCapture
        source_class: type[ContentSource] = StrictContentSource

        async def resolve(self, value: ContentCapture, /) -> ContentSource:
            return StrictContentSource(
                name=value.source_id,
                materialization=SourceMaterialization.CAPTURED,
                content=value.content,
            )

        async def read(self, source: ContentSource, /) -> str:
            return source.content

    return AdapterSourceDefinition(StrictContentAdapter())


def test_generated_payload_satisfies_its_contract_and_excludes_declaration_metadata() -> None:
    """A generated payload must satisfy the schema its manifest advertises.

    The declaration is Definition-owned metadata, so a Source class that forbids
    extra properties would reject its own generated observation, and an unchanged
    Source would carry a different payload after the field was introduced, which
    breaks replaying an observation captured before the upgrade.
    """

    async def scenario() -> None:
        definition = _strict_content_definition()
        registry = SourceDefinitionRegistry((definition,))
        manifest = manifest_for_definition(definition)
        source = await registry.resolve(ContentCapture(source_id="turn-1", content="Captured body."))
        observation = project_source_for_transport(registry, source)

        assert "memory_evidence" not in observation.payload
        properties = cast("dict[str, object]", manifest.source_schema["properties"])
        assert "memory_evidence" not in properties
        # The payload the worker generates must pass the contract it advertises.
        _json_schema_validator(manifest.name, manifest.source_schema).validate(observation.payload)

    asyncio.run(scenario())


def test_captured_models_keep_a_field_that_shares_the_declaration_name() -> None:
    """Only the declaration a Source inherits is metadata; a captured field is content.

    A captured model may legitimately declare a field of its own called
    ``memory_evidence``. Dropping it would change that model's contract, so the
    removal identifies the inherited declaration by what it is, not by its name.
    """

    class CapturedReport(BaseModel):
        model_config = ConfigDict(extra="forbid")

        memory_evidence: str
        summary: str

    class ReportingSource(ContentSource):
        report: CapturedReport

    class ReportingAdapter:
        name = "reporting-content"
        input_class = ContentCapture
        source_class: type[ContentSource] = ReportingSource

        async def resolve(self, value: ContentCapture, /) -> ContentSource:
            return ReportingSource(
                name=value.source_id,
                materialization=SourceMaterialization.CAPTURED,
                content=value.content,
                report=CapturedReport(memory_evidence="captured detail", summary="summary"),
            )

        async def read(self, source: ContentSource, /) -> str:
            return source.content

    definition = AdapterSourceDefinition(ReportingAdapter())
    registry = SourceDefinitionRegistry((definition,))
    manifest = manifest_for_definition(definition)

    definitions = cast("dict[str, dict[str, object]]", manifest.source_schema["$defs"])
    captured = cast("dict[str, object]", definitions["CapturedReport"]["properties"])
    assert "memory_evidence" in captured
    root = cast("dict[str, object]", manifest.source_schema["properties"])
    assert "memory_evidence" not in root

    source = asyncio.run(registry.resolve(ContentCapture(source_id="turn-1", content="Captured body.")))
    observation = project_source_for_transport(registry, source)
    report = cast("dict[str, object]", observation.payload["report"])
    assert report["memory_evidence"] == "captured detail"
    _json_schema_validator(manifest.name, manifest.source_schema).validate(observation.payload)


def test_definition_without_a_declaration_attribute_still_registers() -> None:
    """An adapter written before the declaration existed must keep registering.

    The attribute is part of the Definition contract now, but an out-of-tree
    adapter built directly on the protocol does not carry it. Resolving that to the
    neutral declaration keeps such an adapter usable instead of failing its
    registration.
    """

    class LegacyDefinition:
        name = "legacy"
        version = "1"
        projections: tuple[object, ...] = ()
        input_class = ContentCapture
        source_class = ContentSource

        async def resolve(self, value: ContentCapture, /) -> ContentSource:
            return ContentSource(
                name=value.source_id,
                materialization=SourceMaterialization.CAPTURED,
                content=value.content,
            )

        async def read(self, source: ContentSource, /) -> str:
            return source.content

    definition = LegacyDefinition()
    # Deliberately outside the current protocol: the attribute does not exist yet,
    # which is what the runtime has to tolerate.
    manifest = manifest_for_definition(cast(SourceDefinition[Any, Any, Any], definition))

    assert definition_memory_evidence(definition) == MemoryEvidenceDeclaration()
    assert manifest.memory_evidence == MemoryEvidenceDeclaration()
    # A neutral declaration keeps the manifest at its pre-declaration identity.
    assert manifest.fingerprint == _legacy_manifest(manifest).fingerprint


def test_definition_manifest_has_a_stable_content_addressed_identity() -> None:
    first = manifest_for_definition(CONTENT_SOURCE_DEFINITION)
    second = manifest_for_definition(CONTENT_SOURCE_DEFINITION)

    assert first == second
    assert first.fingerprint.startswith("sha256:")
    with pytest.raises(ValidationError, match="fingerprint does not match"):
        SourceDefinitionManifest.model_validate(first.model_dump(mode="json", by_alias=True) | {"version": "2"})


def test_definition_manifest_carries_versioned_memory_evidence_to_remote_observations() -> None:
    async def scenario() -> None:
        definition = replace(
            CONTENT_SOURCE_DEFINITION,
            memory_evidence=MemoryEvidenceDeclaration(
                authority=MemoryEvidenceAuthority.SYSTEM_ATTESTED,
                verification=MemoryEvidenceVerification.VERIFIED,
                declaration_version="attestation-v2",
            ),
        )
        registry = SourceDefinitionRegistry((definition,))
        manifest = manifest_for_definition(definition)
        source = await registry.resolve(ContentCapture(source_id="turn-1", content="Keep this declaration."))
        observation = project_source_for_transport(registry, source)

        assert manifest.memory_evidence == definition.memory_evidence
        assert source.memory_evidence == definition.memory_evidence
        assert observation.memory_evidence == definition.memory_evidence
        assert registry.memory_evidence(observation) == definition.memory_evidence
        assert manifest.fingerprint != manifest_for_definition(CONTENT_SOURCE_DEFINITION).fingerprint

        runtime = submit_source_observation_request(
            SubmitSourceObservationRequest(
                scope_id="scope",
                observation=HttpSourceObservation.model_validate(observation.model_dump(mode="json")),
            )
        )
        assert runtime.observation.memory_evidence == definition.memory_evidence
        _validate_source_observation(runtime.observation, manifest)

    asyncio.run(scenario())


def test_legacy_remote_definition_manifest_remains_neutral_and_accepted() -> None:
    current = manifest_for_definition(CONTENT_SOURCE_DEFINITION)
    legacy = _legacy_manifest(current)
    transported = HttpSourceDefinitionManifest.model_validate(
        legacy.model_dump(mode="json", by_alias=True, exclude={"memory_evidence"})
    )

    assert runtime_source_definition_manifest(transported) == legacy
    assert legacy.memory_evidence.authority == "untrusted"
    assert legacy.memory_evidence.verification == "unknown"

    async def legacy_source() -> SourceObservation:
        registry = SourceDefinitionRegistry((CONTENT_SOURCE_DEFINITION,))
        resolved = await registry.resolve(ContentCapture(source_id="turn-1", content="Legacy transport."))
        return project_source_for_transport(registry, resolved)

    source = asyncio.run(legacy_source()).model_copy(update={"definition_fingerprint": legacy.fingerprint})
    legacy_observation = HttpSourceObservation.model_validate(
        source.model_dump(mode="json", exclude={"memory_evidence"})
    )
    runtime_observation = submit_source_observation_request(
        SubmitSourceObservationRequest(scope_id="scope", observation=legacy_observation)
    ).observation
    assert "memory_evidence" not in runtime_observation.__pydantic_fields_set__
    _validate_source_observation(runtime_observation, legacy)


def test_legacy_definition_manifest_round_trips_without_gaining_the_evidence_field() -> None:
    current = manifest_for_definition(CONTENT_SOURCE_DEFINITION)
    legacy = _legacy_manifest(current)

    stored = SourceDefinitionManifest.model_validate_json(legacy.model_dump_json(by_alias=True))
    transported = HttpSourceDefinitionManifest.model_validate(legacy.model_dump(mode="json", by_alias=True))

    assert stored == legacy
    assert transported.memory_evidence is None
    assert "memory_evidence" not in json.loads(legacy.model_dump_json(by_alias=True))
    assert SourceDefinitionManifest.model_validate_json(current.model_dump_json(by_alias=True)) == current

    # A neutral declaration carries no ranking preference, so it does not change
    # the manifest a Definition emits and can be omitted from the transport.
    assert current == legacy
    assert "memory_evidence" not in current.model_dump(mode="json", by_alias=True)

    declared = manifest_for_definition(
        replace(
            CONTENT_SOURCE_DEFINITION,
            memory_evidence=MemoryEvidenceDeclaration(authority=MemoryEvidenceAuthority.SYSTEM_ATTESTED),
        )
    )
    assert declared != current
    assert "memory_evidence" in declared.model_dump(mode="json", by_alias=True)


def test_source_observation_request_mapping_preserves_null_projection_values() -> None:
    async def scenario() -> None:
        registry = SourceDefinitionRegistry((CONTENT_SOURCE_DEFINITION,))
        resolved = await registry.resolve(ContentCapture(source_id="turn-1", content="Null is a value."))
        projected = project_source_for_transport(registry, resolved)
        (projection,) = projected.projections
        transport = HttpSourceObservation.model_validate(
            projected.model_dump(mode="json", exclude={"memory_evidence"})
            | {
                "payload": projected.payload | {"nullable_field": None},
                "projections": [{"key": projection.key.model_dump(mode="json"), "value": None}],
            }
        )

        observation = submit_source_observation_request(
            SubmitSourceObservationRequest(scope_id="scope", observation=transport)
        ).observation

        assert observation.projections[0].value is None
        assert observation.payload == transport.payload
        assert observation.payload["nullable_field"] is None
        assert "memory_evidence" not in observation.__pydantic_fields_set__

    asyncio.run(scenario())


def test_source_observation_remains_usable_without_worker_definition_code() -> None:
    async def scenario() -> None:
        registry = SourceDefinitionRegistry((CONTENT_SOURCE_DEFINITION,))
        source = await registry.resolve(
            ContentCapture(source_id="turn-1", content="Keep the remote contract declarative.")
        )
        projected = project_source_for_transport(registry, source)
        catalog = SourceCatalog(backend=EmptySourceBackend())
        payload = await catalog.read(projected)
        projection = catalog.project(projected, TEXT_EVIDENCE_PROJECTION_KEY)

        assert isinstance(projected, SourceObservation)
        assert catalog.as_ref(projected).model_dump() == {"source_type": "content", "source_id": "turn-1"}
        assert payload == projected.payload
        assert catalog.projection_keys(projected) == (TEXT_EVIDENCE_PROJECTION_KEY,)
        assert registry.project(projected, TEXT_EVIDENCE_PROJECTION_KEY) == projection
        assert TextEvidence.model_validate(projection).content == "Keep the remote contract declarative."

    asyncio.run(scenario())


def test_remote_source_observation_requires_captured_materialization() -> None:
    async def scenario() -> None:
        registry = SourceDefinitionRegistry((CONTENT_SOURCE_DEFINITION,))
        source = await registry.resolve(ContentCapture(source_id="turn-1", content="Retain this value."))
        observation = project_source_for_transport(registry, source)

        with pytest.raises(ValidationError):
            SourceObservation.model_validate(observation.model_dump(mode="json") | {"materialization": "referenced"})

    asyncio.run(scenario())


def test_remote_schema_references_are_rejected_without_network_access(monkeypatch: pytest.MonkeyPatch) -> None:
    attempted = False

    def reject_request(*_args: object, **_kwargs: object) -> None:
        nonlocal attempted
        attempted = True
        raise OSError("network access is forbidden")  # noqa: TRY003

    monkeypatch.setattr(urllib.request, "urlopen", reject_request)
    validator = _json_schema_validator("remote", {"$ref": "http://127.0.0.1:9/schema"})

    with pytest.raises(Unresolvable):
        validator.validate({})
    assert not attempted

    with pytest.raises(ValidationError, match="remote schema references are not allowed"):
        SourceDefinitionManifest(
            name="remote",
            version="1",
            fingerprint=f"sha256:{'0' * 64}",
            source_schema={"$dynamicRef": "https://example.invalid/schema"},
        )


@pytest.mark.parametrize(
    "shape",
    [
        "dataclass",
        "pydantic_dataclass",
        "aliased_dataclass",
        "integer_keys",
        "boolean_keys",
        "enum_keys",
        "alias",
        "set",
        "frozenset",
    ],
)
def test_serialized_nested_sources_satisfy_manifest(shape: str) -> None:
    from pydantic.dataclasses import dataclass as pydantic_dataclass

    class Child(Source):
        model_config = ConfigDict(extra="forbid", frozen=True)
        content: str

    @dataclass
    class Wrapper:
        child: Child

    @pydantic_dataclass
    class PydanticWrapper:
        child: Child

    class AliasedWrapper(BaseModel):
        model_config = ConfigDict(serialize_by_alias=True)
        child: Child = Field(alias="captured")

    from enum import Enum

    class KeyEnum(Enum):
        PRIMARY = "primary"

    @pydantic_dataclass(config=ConfigDict(serialize_by_alias=True))
    class AliasedDataclass:
        child: Child = Field(alias="item")

    from pydantic.dataclasses import rebuild_dataclass

    rebuild_dataclass(AliasedDataclass, _types_namespace={"Child": Child})

    annotation, value = {
        "dataclass": (Wrapper, Wrapper),
        "pydantic_dataclass": (PydanticWrapper, PydanticWrapper),
        "integer_keys": (dict[int, Child], lambda child: {1: child}),
        "boolean_keys": (dict[bool, Child], lambda child: {True: child}),
        "enum_keys": (dict[KeyEnum, Child], lambda child: {KeyEnum.PRIMARY: child}),
        "aliased_dataclass": (AliasedDataclass, lambda child: AliasedDataclass(item=child)),
        "alias": (AliasedWrapper, lambda child: AliasedWrapper(captured=child)),
        "set": (set[Child], lambda child: {child}),
        "frozenset": (frozenset[Child], lambda child: frozenset({child})),
    }[shape]
    from pydantic import create_model

    parent = create_model("ContainerSource", __base__=Source, captured=(annotation, ...))
    child = Child(name="child", materialization=SourceMaterialization.CAPTURED, content="evidence")
    source = parent.model_validate({
        "name": "parent",
        "materialization": SourceMaterialization.CAPTURED,
        "captured": value(child),
    })
    from tests.source_contract_models import _CapturedAdapter

    definition = AdapterSourceDefinition(_CapturedAdapter("nested-shape", parent, parent))
    registry = SourceDefinitionRegistry((definition,))
    stamped = registry.materialize(source)
    observation = project_source_for_transport(registry, stamped)
    manifest = manifest_for_definition(definition)
    _json_schema_validator(manifest.name, manifest.source_schema).validate(observation.payload)
    assert "memory_evidence" not in json.dumps(observation.payload)


def test_stamped_source_validation_preserves_aliases() -> None:
    class AliasedSource(Source):
        content: str = Field(alias="body")

    from tests.source_contract_models import _CapturedAdapter

    definition = AdapterSourceDefinition(_CapturedAdapter("aliased", AliasedSource, AliasedSource))
    registry = SourceDefinitionRegistry((definition,))
    source = AliasedSource(name="aliased", materialization=SourceMaterialization.CAPTURED, body="evidence")
    assert registry.materialize(source) == source


def test_stamped_source_rejects_incompatible_declaration_before_acceptance() -> None:
    from pydantic import model_validator

    from powercontext.errors import InvalidSourceResultError
    from tests.source_contract_models import _CapturedAdapter

    class VerifiedSource(Source):
        @model_validator(mode="after")
        def require_verified(self):
            if self.memory_evidence.verification is not MemoryEvidenceVerification.VERIFIED:
                raise ValueError("verified evidence required")  # noqa: TRY003
            return self

    declaration = MemoryEvidenceDeclaration(verification=MemoryEvidenceVerification.VERIFIED)
    source = VerifiedSource(
        name="verified", materialization=SourceMaterialization.CAPTURED, memory_evidence=declaration
    )
    adapter = _CapturedAdapter("verified", VerifiedSource, VerifiedSource)
    with pytest.raises(InvalidSourceResultError):
        SourceDefinitionRegistry((AdapterSourceDefinition(adapter),)).materialize(source)
    registry = SourceDefinitionRegistry((AdapterSourceDefinition(adapter, memory_evidence=declaration),))
    assert registry.materialize(source) == source


@pytest.mark.parametrize("shape", ["serialized_keys", "reordered_list"])
def test_custom_serializers_preserve_source_ownership(shape: str) -> None:
    from typing import Annotated

    from pydantic import PlainSerializer, field_serializer

    from tests.source_contract_models import _CapturedAdapter

    class Child(Source):
        model_config = ConfigDict(extra="forbid")
        content: str

    class Report(BaseModel):
        model_config = ConfigDict(extra="forbid")
        name: str
        memory_evidence: str

    class DictionarySource(Source):
        items: dict[
            Annotated[str, PlainSerializer(lambda value: value.lower(), return_type=str, when_used="json")], Child
        ]

    class ListSource(Source):
        items: list[Child | Report]

        @field_serializer("items")
        def sorted_items(self, values):
            return sorted(values, key=lambda item: item.name)

    child = Child(name="z-child", materialization=SourceMaterialization.CAPTURED, content="Evidence")
    report = Report(name="a-report", memory_evidence="Captured report evidence.")
    source = (
        DictionarySource(name="parent", materialization=SourceMaterialization.CAPTURED, items={"PRIMARY": child})
        if shape == "serialized_keys"
        else ListSource(name="parent", materialization=SourceMaterialization.CAPTURED, items=[child, report])
    )
    definition = AdapterSourceDefinition(_CapturedAdapter("custom-serializer", type(source), type(source)))
    registry = SourceDefinitionRegistry((definition,))
    observation = project_source_for_transport(registry, source)
    manifest = manifest_for_definition(definition)
    _json_schema_validator(manifest.name, manifest.source_schema).validate(observation.payload)
    items = cast(Any, observation.payload["items"])
    if shape == "serialized_keys":
        assert "memory_evidence" not in items["primary"]
    else:
        assert items[0]["memory_evidence"] == report.memory_evidence
        assert "memory_evidence" not in items[1]
    # Transport-only exclusion must never strip the persisted declaration.
    assert "memory_evidence" in source.model_dump(mode="json")
