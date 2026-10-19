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

"""Public Source ingestion and capture preserve Definition-owned metadata."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path
from typing import cast

import httpx
import pytest
from pydantic import model_serializer

from powercontext.builtin.records import ArtifactRevisionActor, ArtifactWrite
from powercontext.builtin.runtime import BuiltinConfig, CaptureSource, open_builtin_contexts, open_builtin_runtime
from powercontext.builtin.scope import ScopeDraft
from powercontext.builtin.sources import CONTENT_SOURCE_DEFINITION, ContentCapture
from powercontext.errors import InvalidSourceResultError
from powercontext.server.app import ServerApplication, create_app
from powercontext.sources import (
    MemoryEvidenceAuthority,
    MemoryEvidenceDeclaration,
    MemoryEvidenceVerification,
    Source,
    SourceDefinitionRegistry,
    SourceMaterialization,
    manifest_for_definition,
    project_source_for_transport,
)
from powercontext.sources.definitions import AdapterSourceDefinition
from tests.e2e.atomic_memory_migration_backend import MigrationBackend
from tests.source_contract_models import _CapturedAdapter, source_contract_case


@pytest.mark.parametrize(
    "case",
    ["nested", "title_collision", "reference_collision", "custom_serializer", "aliased_declaration", "aliased_default"],
)
def test_neutral_worker_upgrade_preserves_http_registration_and_replay(
    migration_backend: MigrationBackend,
    case: str,
) -> None:
    baseline = json.loads(
        (Path(__file__).parents[1] / "fixtures/source_declaration_upgrade.json").read_text(encoding="utf-8")
    )[case]
    definition, source = source_contract_case(case)
    registry = SourceDefinitionRegistry((definition,))
    config = BuiltinConfig(database=migration_backend.config)

    async def scenario() -> None:
        async with open_builtin_runtime(config) as runtime:
            assert runtime.scopes is not None
            scope = await runtime.scopes.create(
                ScopeDraft(title="Sources", summary="Source contract", idempotency_key="source-contract")
            )
            app = create_app(application=cast(ServerApplication, runtime))
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://testserver"
            ) as client:
                registered = await client.post(
                    "/v1/source-definitions/register", json={"manifest": baseline["manifest"]}
                )
                assert registered.status_code == 200, registered.text
                accepted = await client.post(
                    "/v1/source-observations", json={"scope_id": scope.scope_id, "observation": baseline["observation"]}
                )
                assert accepted.status_code == 202, accepted.text
                receipt = accepted.json()
        async with open_builtin_runtime(config) as runtime:
            app = create_app(application=cast(ServerApplication, runtime))
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://testserver"
            ) as client:
                registered = await client.post(
                    "/v1/source-definitions/register",
                    json={"manifest": manifest_for_definition(definition).model_dump(mode="json", by_alias=True)},
                )
                assert registered.status_code == 200, registered.text
                replayed = await client.post(
                    "/v1/source-observations",
                    json={
                        "scope_id": scope.scope_id,
                        "observation": project_source_for_transport(registry, source).model_dump(mode="json"),
                    },
                )
                assert replayed.status_code == 202, replayed.text
                assert replayed.json() == receipt

    asyncio.run(scenario())


@pytest.mark.parametrize("attested", [False, True])
def test_lossy_source_encoding_is_rejected_before_persistence(
    migration_backend: MigrationBackend,
    attested: bool,
) -> None:
    class LegacyEncodedSource(Source):
        content: str

        @model_serializer(mode="wrap")
        def serialize_legacy(self, handler):
            value = handler(self)
            value.pop("memory_evidence", None)
            return value

    declaration = (
        MemoryEvidenceDeclaration(
            authority=MemoryEvidenceAuthority.SYSTEM_ATTESTED, verification=MemoryEvidenceVerification.VERIFIED
        )
        if attested
        else MemoryEvidenceDeclaration()
    )
    definition = AdapterSourceDefinition(
        _CapturedAdapter("legacy-encoded", LegacyEncodedSource, LegacyEncodedSource), memory_evidence=declaration
    )
    registry = SourceDefinitionRegistry((definition,))
    config = BuiltinConfig(database=migration_backend.config)
    source = LegacyEncodedSource(
        name="same-input", materialization=SourceMaterialization.CAPTURED, content="Identical captured content."
    )

    async def scenario() -> None:
        expected: tuple[Source, ...] = ()
        async with open_builtin_runtime(config) as runtime:
            assert runtime.scopes is not None
            scope = await runtime.scopes.create(
                ScopeDraft(title="Capture", summary="Source encoding", idempotency_key="source-encoding")
            )
        async with open_builtin_contexts(config, source_registry=registry) as contexts:
            context = await contexts.get(scope.scope_id)
            if attested:
                with pytest.raises(InvalidSourceResultError):
                    await context.sources.add(source)
                assert await context.sources.list() == ()
            else:
                stored = await context.sources.add(source)
                expected = (stored,)
                assert stored.memory_evidence == declaration
                assert await context.sources.get(stored) == stored
                assert await context.sources.add(source) == stored
        async with open_builtin_contexts(config, source_registry=registry) as contexts:
            context = await contexts.get(scope.scope_id)
            assert await context.sources.list() == expected

    asyncio.run(scenario())


@pytest.mark.parametrize("native_first", [True, False])
def test_record_and_native_capture_stamp_the_same_declaration(
    migration_backend: MigrationBackend,
    native_first: bool,
) -> None:
    declaration = MemoryEvidenceDeclaration(
        authority=MemoryEvidenceAuthority.SYSTEM_ATTESTED, verification=MemoryEvidenceVerification.VERIFIED
    )
    registry = SourceDefinitionRegistry((replace(CONTENT_SOURCE_DEFINITION, memory_evidence=declaration),))
    config = BuiltinConfig(database=migration_backend.config)

    async def scenario() -> None:
        async with open_builtin_runtime(config, source_registry=registry) as runtime:
            assert runtime.scopes is not None
            scope = await runtime.scopes.create(
                ScopeDraft(title="Capture", summary="Capture contract", idempotency_key="capture-contract")
            )
        value = ContentCapture(source_id="same-input", content="Identical captured content.")

        async def native() -> None:
            async with open_builtin_contexts(config, source_registry=registry) as contexts:
                context = await contexts.get(scope.scope_id)
                stored, sequence = await context.sources.capture(value)
                assert stored.memory_evidence == declaration
                assert sequence == 1

        async def recorded() -> None:
            async with open_builtin_runtime(config, source_registry=registry) as runtime:
                receipt = await runtime.sources.for_scope(scope.scope_id).capture(
                    CaptureSource(source_id=value.source_id, content=value.content, metadata={})
                )
                assert receipt.sequence == 1

        for capture in (native, recorded) if native_first else (recorded, native):
            await capture()
        await native()

    asyncio.run(scenario())


def test_artifact_rollback_preserves_source_declaration_and_revision_metadata(
    migration_backend: MigrationBackend,
) -> None:
    declaration = MemoryEvidenceDeclaration(
        authority=MemoryEvidenceAuthority.SYSTEM_ATTESTED, verification=MemoryEvidenceVerification.VERIFIED
    )
    registry = SourceDefinitionRegistry((replace(CONTENT_SOURCE_DEFINITION, memory_evidence=declaration),))
    config = BuiltinConfig(database=migration_backend.config)

    async def scenario() -> None:
        async with open_builtin_runtime(config) as runtime:
            assert runtime.scopes is not None
            scope = await runtime.scopes.create(
                ScopeDraft(title="Rollback", summary="Source declaration on rollback", idempotency_key="rollback")
            )
        async with open_builtin_contexts(config, source_registry=registry) as contexts:
            context = await contexts.get(scope.scope_id)
            records = contexts.records
            await records.create_artifact(
                scope.scope_id, "profile", ArtifactWrite(content={"content": "# Approved profile"})
            )
            first = await records.get_artifact(scope.scope_id, "profile", "profile")
            await records.replace_artifact(
                scope.scope_id,
                "profile",
                "profile",
                '"revision:1"',
                ArtifactWrite(content={"content": "# Later profile"}),
            )
            restored = await records.replace_artifact(
                scope.scope_id,
                "profile",
                "profile",
                '"revision:2"',
                ArtifactWrite(
                    content={"content": first.content["content"]},
                    restored_from_revision=1,
                    reason="Restore approved profile",
                ),
                actor_type="user",
                actor_id="operator",
            )
            assert restored.revision == 3
            assert restored.restored_from_revision == 1
            assert restored.reason == "Restore approved profile"
            assert restored.created_by == ArtifactRevisionActor(type="user", id="operator")
            assert restored.content["content"] == first.content["content"]
            generation = restored.content["generation"]
            assert isinstance(generation, dict)
            assert generation["mode"] == "rollback"
            sources = {source.name: source for source in await context.sources.list()}
            for record in (first, restored):
                assert record.sources
                for ref in record.sources:
                    assert sources[ref.source_id].memory_evidence == declaration

        async with open_builtin_contexts(config) as contexts:
            context = await contexts.get(scope.scope_id)
            assert await contexts.records.get_artifact(scope.scope_id, "profile", "profile") == restored
            sources = {source.name: source for source in await context.sources.list()}
            assert all(sources[ref.source_id].memory_evidence == declaration for ref in restored.sources)

    asyncio.run(scenario())
