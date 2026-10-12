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


"""Directory application, caller transactions and HTTP/SDK/MCP contracts."""

from __future__ import annotations

import asyncio
from contextlib import nullcontext
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import insert, select

from powercontext.artifacts.search import ArtifactSearchExecutionContext
from powercontext.builtin.artifacts.atomic_memory.models import AtomicMemory
from powercontext.builtin.persistence.artifact_governance import ArtifactGovernanceRepository, ArtifactLifecycleState
from powercontext.builtin.persistence.artifacts import ArtifactRepository
from powercontext.builtin.persistence.atomic_memory_directory import (
    AtomicMemoryDirectoryAuthorizationUnavailableError,
    AtomicMemoryDirectoryRepository,
    AtomicMemoryDirectoryUnavailableError,
)
from powercontext.builtin.persistence.cursor_codec import SignedCursorCodec
from powercontext.builtin.persistence.sqlite import SQLiteConfig, SQLiteProfile
from powercontext.builtin.persistence.sqlite.atomic_memory_index import SQLiteAtomicMemoryIndex
from powercontext.builtin.records import InvalidCursorError
from powercontext.builtin.runtime.atomic_memory import AtomicMemoryApplication
from tests.builtin.persistence.test_atomic_memory_directory import directory_profile, publish, query


def test_file_sqlite_bound_transaction_sees_publication_and_rolls_back_directory_state(tmp_path):
    async def scenario():
        from powercontext.server.authz import AccessDeniedError

        reader = AtomicMemoryDirectoryRepository(SignedCursorCodec(secret=b"fixture"))
        async with directory_profile(tmp_path / "directory.sqlite") as (profile, scopes, _):
            artifacts = ArtifactRepository((AtomicMemory,), atomic_memory_directory=reader)
            scoped = AtomicMemoryApplication(profile.database, artifacts, SQLiteAtomicMemoryIndex(None)).for_scope(
                "scope-a"
            )
            local = ArtifactSearchExecutionContext(trusted_local=True)
            for context in (None, ArtifactSearchExecutionContext()):
                with pytest.raises(AccessDeniedError):
                    await scoped.query_directory(context=context)
            uncomposed = AtomicMemoryApplication(
                profile.database, ArtifactRepository((AtomicMemory,)), SQLiteAtomicMemoryIndex(None)
            ).for_scope("scope-a")
            with pytest.raises(AtomicMemoryDirectoryUnavailableError):
                await uncomposed.query_directory(context=local)
            async with profile.database.engine.connect() as connection:
                transaction = await connection.begin()
                await publish(connection, reader)
                page = await scoped.query_directory(context=local, connection=connection)
                assert page.items[0].artifact.artifact_id == "a"
                assert connection.in_transaction() and not connection.closed
                await transaction.rollback()
            assert (await scoped.query_directory(context=local)).items == ()
            async with profile.database.transaction(consistent_snapshot=True) as connection:
                assert (await query(connection, reader)).items == ()
                assert await connection.scalar(select(scopes.c.atomic_memory_directory_generation)) == 0

    asyncio.run(scenario())


@pytest.mark.parametrize("provider_name", ("builtin", "casbin"))
@pytest.mark.parametrize("audit_location", ("business", "independent"))
def test_shared_directory_access_preserves_policy_audit_and_global_owner_failure(
    provider_name, audit_location, tmp_path, monkeypatch
):
    async def scenario():
        from powercontext.server.authz import (
            AccessAuditContext,
            AccessBinding,
            AccessBindingState,
            AccessControlService,
            AccessDeniedError,
            AccessProviderCapabilities,
            AccessRole,
            AccessUnavailableError,
            BuiltinAuthorizationProvider,
            CasbinAuthorizationProvider,
            GroupRef,
            PrincipalRef,
            ResourceRef,
        )
        from powercontext.server.authz.models import ArtifactOwnerRelation
        from powercontext.server.authz.repository import (
            ACCESS_BINDINGS_TABLE,
            ACCESS_TABLES,
            RelationalAccessRepository,
            _binding_row,
        )

        now = [datetime.now(UTC)]
        principal = PrincipalRef(type="user", id="reader")
        audit_context = AccessAuditContext(transport="test", operation="directory-query")
        reader = AtomicMemoryDirectoryRepository(SignedCursorCodec(secret=b"fixture"))
        async with directory_profile(tmp_path / "policy.sqlite", extra_tables=ACCESS_TABLES) as (profile, _, _):
            relationships = RelationalAccessRepository(profile.database)
            provider_type = BuiltinAuthorizationProvider if provider_name == "builtin" else CasbinAuthorizationProvider
            scope_binding = await relationships.create_binding(
                AccessBinding(
                    binding_id="scope-reader",
                    subject=principal,
                    resource=ResourceRef.scope("scope-a"),
                    role=AccessRole.SCOPE_VIEWER,
                    granted_by=principal,
                    reason="fixture",
                    created_at=datetime.now(UTC),
                    expires_at=now[0] + timedelta(minutes=1),
                    state=AccessBindingState.ACTIVE,
                    version=1,
                    policy_revision="pending",
                    idempotency_key="fixture",
                )
            )
            async with profile.database.transaction() as connection:
                # Unrelated direct grants in this same Scope must not be loaded
                # to decide Scope read. Seed via the shared encoding authority.
                await connection.execute(
                    insert(ACCESS_BINDINGS_TABLE),
                    [
                        _binding_row(
                            replace(
                                scope_binding,
                                binding_id=f"unrelated-{index}",
                                idempotency_key=f"unrelated-{index}",
                                role=AccessRole.ARTIFACT_VIEWER,
                                resource=ResourceRef.artifact(
                                    "scope-a", family="atomic-memory", artifact_id=f"unrelated-{index}"
                                ),
                            )
                        )
                        for index in range(5000)
                    ],
                )
            scoped = AtomicMemoryApplication(
                profile.database,
                ArtifactRepository((AtomicMemory,), atomic_memory_directory=reader),
                SQLiteAtomicMemoryIndex(None),
            ).for_scope("scope-a")
            # Preserve both default same-file audit and an independent audit store.
            async with (
                SQLiteProfile.open(SQLiteConfig(), tables=ACCESS_TABLES)
                if audit_location == "independent"
                else nullcontext(profile) as audit_profile
            ):
                audit = RelationalAccessRepository(audit_profile.database)
                provider = provider_type(relationships, clock=lambda: now[0])
                access = AccessControlService(provider, relationships=relationships, audit=audit)
                context = ArtifactSearchExecutionContext(principal=principal, access=access, audit=audit_context)
                async with profile.database.transaction() as connection:
                    for identity in ("a", "b", "z"):
                        await publish(connection, reader, identity)
                    governance = ArtifactGovernanceRepository(atomic_memory_directory=reader)
                    current = await governance.get(connection, "scope-a", "atomic-memory", "z")
                    await governance.transition_merge(
                        connection, "scope-a", "atomic-memory", "z", current, ArtifactLifecycleState.RETIRED
                    )
                for identity in ("a", "b"):
                    await relationships.establish_artifact_owner(
                        ArtifactOwnerRelation(
                            resource=ResourceRef.artifact("scope-a", family="atomic-memory", artifact_id=identity),
                            owner=principal,
                            established_at=datetime.now(UTC),
                            policy_revision="0",
                            idempotency_key=identity,
                        )
                    )
                # Even the active first page fails for an unowned retired row elsewhere.
                with pytest.raises(AccessUnavailableError, match="owner"):
                    await scoped.query_directory(limit=1, context=context)
                with pytest.raises(AccessDeniedError):
                    await scoped.query_directory(
                        context=replace(context, principal=PrincipalRef(type="user", id="stranger"))
                    )
                await relationships.establish_artifact_owner(
                    ArtifactOwnerRelation(
                        resource=ResourceRef.artifact("scope-a", family="atomic-memory", artifact_id="z"),
                        owner=principal,
                        established_at=datetime.now(UTC),
                        policy_revision="0",
                        idempotency_key="z",
                    )
                )
                # Measure actual application row materialization, including
                # authorization, instead of inferring cost from selector LIMIT.
                async with profile.database.engine.connect() as observed:
                    execute = type(observed).execute
                returned_rows = []

                async def measured(current, statement, *args, **kwargs):
                    result = await execute(current, statement, *args, **kwargs)
                    if result.returns_rows:
                        frozen = result.freeze()
                        returned_rows.append(len(frozen.data))
                        return frozen()
                    return result

                with monkeypatch.context() as patch:
                    patch.setattr(type(observed), "execute", measured)
                    first = await scoped.query_directory(limit=1, context=context)
                assert returned_rows and max(returned_rows) <= 2
                assert [item.artifact.artifact_id for item in first.items] == ["a"] and first.next_cursor
                async with (
                    access.defer_decision_audit(),
                    profile.database.transaction(consistent_snapshot=True) as connection,
                ):
                    tail = await scoped.query_directory(
                        limit=1, cursor=first.next_cursor, context=context, connection=connection
                    )
                    assert connection.in_transaction() and not connection.closed
                assert [item.artifact.artifact_id for item in tail.items] == ["b"] and tail.next_cursor is None
                # Host-resolved groups and configured provider are cursor context,
                # even when a direct Scope grant still permits both pages.
                with pytest.raises(InvalidCursorError):
                    await scoped.query_directory(
                        limit=1,
                        cursor=first.next_cursor,
                        context=replace(
                            context, audit=replace(audit_context, subject_groups=(GroupRef("group", "g"),))
                        ),
                    )
                alternate_type = (
                    CasbinAuthorizationProvider if provider_name == "builtin" else BuiltinAuthorizationProvider
                )
                alternate = AccessControlService(
                    alternate_type(relationships), relationships=relationships, audit=audit
                )
                with pytest.raises(InvalidCursorError):
                    await scoped.query_directory(
                        limit=1, cursor=first.next_cursor, context=replace(context, access=alternate)
                    )
                disabled = AccessControlService(
                    provider,
                    relationships=relationships,
                    audit=audit,
                    provider_capabilities=AccessProviderCapabilities(False, True, True),
                )
                with pytest.raises(AtomicMemoryDirectoryAuthorizationUnavailableError):
                    await scoped.query_directory(context=replace(context, access=disabled))
                assert access.audit is audit and access.provider is provider and provider._repository is relationships
                assert await audit.list_audit(resource=ResourceRef.scope("scope-a"), limit=10)
                now[0] += timedelta(minutes=2)
                with pytest.raises(AccessDeniedError):
                    await scoped.query_directory(limit=1, cursor=first.next_cursor, context=context)
                now[0] -= timedelta(minutes=2)
                await relationships.create_binding(
                    AccessBinding(
                        binding_id="another-reader",
                        subject=PrincipalRef(type="user", id="other"),
                        resource=ResourceRef.scope("scope-a"),
                        role=AccessRole.SCOPE_VIEWER,
                        granted_by=principal,
                        reason="fixture policy change",
                        created_at=now[0],
                        expires_at=None,
                        state=AccessBindingState.ACTIVE,
                        version=1,
                        policy_revision="pending",
                        idempotency_key="another-reader",
                    )
                )
                with pytest.raises(InvalidCursorError):
                    await scoped.query_directory(limit=1, cursor=first.next_cursor, context=context)
                first = await scoped.query_directory(limit=1, context=context)
                await relationships.revoke_binding(
                    "scope-reader",
                    revoked_by=principal,
                    expected_version=1,
                    idempotency_key="revoke",
                    revoked_at=datetime.now(UTC),
                )
                # Revocation is checked before trusting a still-signed continuation.
                with pytest.raises(AccessDeniedError):
                    await scoped.query_directory(limit=1, cursor=first.next_cursor, context=context)

    asyncio.run(scenario())


def test_http_sdk_and_mcp_share_compact_pages_authentication_and_restart_semantics(monkeypatch):
    async def scenario():
        from types import SimpleNamespace

        import httpx
        from fastmcp import Client
        from fastmcp.client.transports import StreamableHttpTransport
        from starlette.middleware import Middleware

        from powercontext.builtin.persistence import atomic_memory_directory as directory
        from powercontext.builtin.persistence.tags import RelationalTagService
        from powercontext.builtin.tags import ArtifactTagTarget
        from powercontext.client import PowerContextClient
        from powercontext.client.errors import ServerResponseError
        from powercontext.http import QueryAtomicMemoryDirectoryRequest
        from powercontext.server.app import create_app
        from powercontext.server.authentication import StaticBearerAuthenticationProvider
        from powercontext.server.authz import (
            AccessBinding,
            AccessBindingState,
            AccessControlService,
            AccessRole,
            BuiltinAuthorizationProvider,
            PrincipalRef,
            ResourceRef,
        )
        from powercontext.server.authz.models import ArtifactOwnerRelation
        from powercontext.server.authz.repository import ACCESS_TABLES, RelationalAccessRepository
        from powercontext.server.mcp import mount_mcp
        from powercontext.server.middleware import AuthenticationMiddleware

        principal = PrincipalRef(type="user", id="reader")
        reader = AtomicMemoryDirectoryRepository(SignedCursorCodec(secret=b"fixture"))
        async with directory_profile(extra_tables=ACCESS_TABLES) as (profile, _, _):
            artifacts = ArtifactRepository((AtomicMemory,), atomic_memory_directory=reader)
            atomic = AtomicMemoryApplication(profile.database, artifacts, SQLiteAtomicMemoryIndex(None))
            relationships = RelationalAccessRepository(profile.database)
            await relationships.create_binding(
                AccessBinding(
                    binding_id="viewer",
                    subject=principal,
                    resource=ResourceRef.scope("scope-a"),
                    role=AccessRole.SCOPE_VIEWER,
                    granted_by=principal,
                    reason="fixture",
                    created_at=datetime.now(UTC),
                    expires_at=None,
                    state=AccessBindingState.ACTIVE,
                    version=1,
                    policy_revision="pending",
                    idempotency_key="viewer",
                )
            )
            tags = tuple(chr(0x4E00 + index) * 64 for index in range(16))
            tag_service = RelationalTagService(profile.database, artifacts=artifacts)
            async with profile.database.transaction() as connection:
                for identity in ("a", "b"):
                    await publish(connection, reader, identity, text="Private body must not enter the directory.")
            for identity in ("a", "b"):
                await relationships.establish_artifact_owner(
                    ArtifactOwnerRelation(
                        resource=ResourceRef.artifact("scope-a", family="atomic-memory", artifact_id=identity),
                        owner=principal,
                        established_at=datetime.now(UTC),
                        policy_revision="0",
                        idempotency_key=identity,
                    )
                )
                target = ArtifactTagTarget(family="atomic-memory", artifact_id=identity)
                current = await tag_service.get("scope-a", target)
                await tag_service.replace("scope-a", target, tags, expected_etag=current.etag)
            access = AccessControlService(
                BuiltinAuthorizationProvider(relationships), relationships=relationships, audit=relationships
            )
            authentication = StaticBearerAuthenticationProvider("fixture-token", principal)
            app = create_app(
                application=SimpleNamespace(atomic_memory=atomic),
                access_control=access,
                authentication_provider=authentication,
                middleware=(Middleware(AuthenticationMiddleware, provider=authentication),),
            )
            mount_mcp(app)
            url = "http://127.0.0.1"
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url=url) as http:
                credential = "fixture-token"
                sdk = PowerContextClient(url, token=credential, http_client=http)
                request = QueryAtomicMemoryDirectoryRequest.model_validate({
                    "scope_id": "scope-a",
                    "limit": 1,
                    "tags": list(tags),
                })
                first = await sdk.query_atomic_memory_directory(request)
                assert first.next_cursor and len(first.next_cursor) <= 4096
                first_json = first.model_dump(mode="json")
                assert set(first_json) == {"scope_id", "items", "next_cursor"}
                assert set(first_json["items"][0]) == {"artifact", "kind", "state", "state_version", "merged_into_id"}
                assert first_json["items"][0]["artifact"] == {
                    "family": "atomic-memory",
                    "artifact_id": "a",
                    "revision": 1,
                }
                assert "Private body" not in first.model_dump_json()

                def create_http_client(headers=None, timeout=None, auth=None, **_):
                    return httpx.AsyncClient(
                        transport=transport,
                        base_url=url,
                        headers={"Authorization": "Bearer fixture-token", **(headers or {})},
                        timeout=timeout,
                        auth=auth,
                        follow_redirects=True,
                    )

                mcp_transport = StreamableHttpTransport(f"{url}/mcp/", httpx_client_factory=create_http_client)
                async with app.router.lifespan_context(app), Client(mcp_transport) as mcp:
                    tool = next(tool for tool in await mcp.list_tools() if tool.name == "query_atomic_memory_directory")
                    assert tool.annotations and tool.annotations.readOnlyHint
                    continuation = request.model_copy(update={"cursor": first.next_cursor}).model_dump(mode="json")
                    result = await mcp.call_tool("query_atomic_memory_directory", continuation)
                    assert not result.is_error
                    assert result.data["items"][0]["artifact"]["artifact_id"] == "b"
                    assert result.data["next_cursor"] is None
                    # An actual tag write invalidates both transports' old cursor.
                    target = ArtifactTagTarget(family="atomic-memory", artifact_id="a")
                    current = await tag_service.get("scope-a", target)
                    await tag_service.replace("scope-a", target, (), expected_etag=current.etag)
                    invalid = await mcp.call_tool("query_atomic_memory_directory", continuation, raise_on_error=False)
                    assert invalid.is_error and "invalid_cursor" in invalid.content[0].text
                with pytest.raises(ServerResponseError) as invalid:
                    await sdk.query_atomic_memory_directory(request.model_copy(update={"cursor": first.next_cursor}))
                assert invalid.value.status_code == 400 and invalid.value.code == "invalid_cursor"
                # Single-item failure is mapped without returning a partial page.
                with monkeypatch.context() as patch:
                    patch.setattr(directory, "DIRECTORY_ITEM_BYTES", 1)
                    with pytest.raises(ServerResponseError) as oversized:
                        await sdk.query_atomic_memory_directory(QueryAtomicMemoryDirectoryRequest(scope_id="scope-a"))
                assert oversized.value.status_code == 413
                assert oversized.value.code == "atomic_memory_directory_item_too_large"
                # Wire constraints are enforced as well as domain validation.
                for changes in ({"limit": True}, {"states": []}, {"states": ["active", "active"]}, {"kind": " "}):
                    response = await http.post(
                        "/v1/atomic-memory/directory/query",
                        json={"scope_id": "scope-a", **changes},
                        headers={"Authorization": "Bearer fixture-token"},
                    )
                    assert response.status_code == 422 and "items" not in response.json()
                unauthenticated = await http.post("/v1/atomic-memory/directory/query", json={"scope_id": "scope-a"})
                assert unauthenticated.status_code == 401
                from powercontext.server.authz import AccessProviderCapabilities

                app.state.access_control = AccessControlService(
                    access.provider,
                    relationships=relationships,
                    audit=relationships,
                    provider_capabilities=AccessProviderCapabilities(False, True, True),
                )
                try:
                    with pytest.raises(ServerResponseError) as unsupported:
                        await sdk.query_atomic_memory_directory(QueryAtomicMemoryDirectoryRequest(scope_id="scope-a"))
                    assert unsupported.value.status_code == 503
                    assert unsupported.value.code == "atomic_memory_directory_authorization_unavailable"
                finally:
                    app.state.access_control = access
                await relationships.revoke_binding(
                    "viewer",
                    revoked_by=principal,
                    expected_version=1,
                    idempotency_key="revoke",
                    revoked_at=datetime.now(UTC),
                )
                with pytest.raises(ServerResponseError) as revoked:
                    await sdk.query_atomic_memory_directory(QueryAtomicMemoryDirectoryRequest(scope_id="scope-a"))
                assert revoked.value.status_code == 403
            audit = await relationships.list_audit(resource=ResourceRef.scope("scope-a"), limit=100)
            decisions = [event for event in audit if event.operation == "query_atomic_memory_directory"]
            assert {event.transport for event in decisions} >= {"http", "mcp"}
            assert all(event.principal == principal for event in decisions)

    asyncio.run(scenario())


def test_uncomposed_http_directory_fails_closed_and_full_record_tagged_list_still_works():
    async def scenario():
        from types import SimpleNamespace

        import httpx

        from powercontext.builtin.persistence.tags import RelationalTagService
        from powercontext.builtin.tags import ArtifactTagTarget
        from powercontext.server.app import create_app

        reader = AtomicMemoryDirectoryRepository(SignedCursorCodec(secret=b"fixture"))
        async with directory_profile() as (profile, _, _):
            async with profile.database.transaction() as connection:
                await publish(connection, reader, text="Full record text remains available.")
            artifacts = ArtifactRepository((AtomicMemory,))
            tag_service = RelationalTagService(profile.database, artifacts=artifacts)
            target = ArtifactTagTarget(family="atomic-memory", artifact_id="a")
            current = await tag_service.get("scope-a", target)
            await tag_service.replace("scope-a", target, ("PRODUCTION",), expected_etag=current.etag)
            atomic = AtomicMemoryApplication(profile.database, artifacts, SQLiteAtomicMemoryIndex(None))
            app = create_app(application=SimpleNamespace(atomic_memory=atomic), access_mode="disabled")
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1") as http:
                response = await http.post("/v1/atomic-memory/directory/query", json={"scope_id": "scope-a"})
                assert response.status_code == 503
                assert response.json()["error"]["code"] == "atomic_memory_directory_unavailable"
                response = await http.post(
                    "/v1/atomic-memory/list", json={"scope_id": "scope-a", "tags": ["production"]}
                )
                assert response.status_code == 200
                assert response.json()["items"][0]["text"] == "Full record text remains available."

    asyncio.run(scenario())
