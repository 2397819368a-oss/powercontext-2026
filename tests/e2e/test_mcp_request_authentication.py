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
import json
import socket
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, Self

import httpx
import pytest
import uvicorn
from fastapi import FastAPI
from sse_starlette.sse import AppStatus
from starlette.middleware import Middleware

from powercontext.builtin.persistence.sqlite import SQLiteConfig, SQLiteProfile
from powercontext.builtin.runtime.atomic_memory import AtomicMemoryAccess, AtomicMemoryPage
from powercontext.server.app import create_app
from powercontext.server.authentication import (
    AuthenticationRejectedError,
    AuthenticationRequest,
    AuthenticationResult,
    ProviderReadiness,
)
from powercontext.server.authz import (
    AccessBinding,
    AccessBindingState,
    AccessControlService,
    AccessRole,
    BuiltinAuthorizationProvider,
    GroupRef,
    PrincipalRef,
    ResourceRef,
)
from powercontext.server.authz.repository import ACCESS_TABLES, RelationalAccessRepository
from powercontext.server.mcp import mount_mcp
from powercontext.server.middleware import AuthenticationMiddleware

ALICE = PrincipalRef(type="user", id="alice")
BOB = PrincipalRef(type="user", id="bob")
TEAM = GroupRef(type="group", id="alice-team")
FIRST_ACTOR = PrincipalRef(type="service", id="first-actor")
CURRENT_ACTOR = PrincipalRef(type="service", id="current-actor")


class _AuthenticationProvider:
    def __init__(self) -> None:
        self.identities = {
            "Bearer alice-token": AuthenticationResult(subject=ALICE, actor=FIRST_ACTOR, subject_groups=(TEAM,)),
            "Bearer bob-token": AuthenticationResult(subject=BOB),
        }

    async def authenticate(self, request: AuthenticationRequest, /) -> AuthenticationResult:
        result = self.identities.get(request.headers.get("authorization", ""))
        if result is None:
            raise AuthenticationRejectedError
        return result

    async def readiness(self) -> ProviderReadiness:
        return ProviderReadiness(ready=True)


class _MemoryApplication:
    def __init__(self, database, scope_id: str = "") -> None:
        self.database = database
        self.scope_id = scope_id

    def for_scope(self, scope_id: str) -> Self:
        return type(self)(self.database, scope_id)

    async def list(self, *, context, **kwargs):
        assert context is not None
        async with self.database.transaction() as connection:
            await AtomicMemoryAccess().filters(connection, self.scope_id, context)
        return AtomicMemoryPage(items=())


def _rpc_body(response: httpx.Response) -> dict[str, Any]:
    response.raise_for_status()
    if response.headers["content-type"].startswith("application/json"):
        return response.json()
    return next(json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: "))


@asynccontextmanager
async def _http_client(app: FastAPI, *, over_tcp: bool) -> AsyncIterator[httpx.AsyncClient]:
    if not over_tcp:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
            yield client
        return

    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        server = uvicorn.Server(uvicorn.Config(app, lifespan="off", access_log=False, log_level="warning"))
        task = asyncio.create_task(server.serve(sockets=[listener]))
        try:
            async with asyncio.timeout(10):
                while not server.started:
                    if task.done():
                        await task
                        message = "MCP test server stopped before startup"
                        raise RuntimeError(message)
                    await asyncio.sleep(0.01)
            async with httpx.AsyncClient(
                base_url=f"http://127.0.0.1:{listener.getsockname()[1]}", trust_env=False
            ) as client:
                yield client
        finally:
            server.should_exit = True
            await asyncio.wait_for(task, timeout=10)


@pytest.mark.parametrize("over_tcp", [False, True], ids=["asgi", "tcp"])
def test_mcp_authorization_and_audit_use_each_http_requests_identity(over_tcp: bool, monkeypatch) -> None:
    # An in-process Uvicorn shutdown must not stop SSE responses in later tests.
    monkeypatch.setattr(AppStatus, "should_exit", False)

    async def scenario() -> None:
        async with SQLiteProfile.open(SQLiteConfig(), tables=ACCESS_TABLES) as profile:
            repository = RelationalAccessRepository(profile.database)
            for subject, scope_id in ((TEAM, "alice-scope"), (BOB, "bob-scope")):
                await repository.create_binding(
                    AccessBinding(
                        binding_id=scope_id,
                        subject=subject,
                        resource=ResourceRef.scope(scope_id),
                        role=AccessRole.SCOPE_VIEWER,
                        granted_by=FIRST_ACTOR,
                        reason="test bootstrap",
                        created_at=datetime.now(UTC),
                        expires_at=None,
                        state=AccessBindingState.ACTIVE,
                        version=1,
                        policy_revision="pending",
                        idempotency_key=scope_id,
                    )
                )
            access = AccessControlService(
                BuiltinAuthorizationProvider(repository), relationships=repository, audit=repository
            )
            authentication = _AuthenticationProvider()
            memory = _MemoryApplication(profile.database)
            app = create_app(
                application=SimpleNamespace(atomic_memory=memory, records=memory),
                access_control=access,
                authentication_provider=authentication,
                middleware=(Middleware(AuthenticationMiddleware, provider=authentication),),
            )
            mount_mcp(app)
            async with app.router.lifespan_context(app), _http_client(app, over_tcp=over_tcp) as client:
                headers = {"Accept": "application/json, text/event-stream", "Authorization": "Bearer alice-token"}
                initialized = await client.post(
                    "/mcp/",
                    headers=headers,
                    json={
                        "jsonrpc": "2.0",
                        "id": "initialize",
                        "method": "initialize",
                        "params": {
                            "protocolVersion": "2025-11-25",
                            "capabilities": {},
                            "clientInfo": {"name": "identity-regression", "version": "1"},
                        },
                    },
                )
                headers["MCP-Protocol-Version"] = _rpc_body(initialized)["result"]["protocolVersion"]
                # Reuse an issued session on affected servers, or send a stale ID to a stateless server.
                headers["mcp-session-id"] = initialized.headers.get("mcp-session-id", "stale-alice-session")
                notified = await client.post(
                    "/mcp/",
                    headers=headers,
                    json={
                        "jsonrpc": "2.0",
                        "method": "notifications/initialized",
                    },
                )
                assert notified.status_code == 202

                async def call(token: str, scope_id: str, request_id: str) -> dict[str, Any]:
                    response = await client.post(
                        "/mcp/",
                        headers={
                            **headers,
                            "Authorization": f"Bearer {token}-token",
                        },
                        json={
                            "jsonrpc": "2.0",
                            "id": request_id,
                            "method": "tools/call",
                            "params": {"name": "list_memory_entries", "arguments": {"scope_id": scope_id}},
                        },
                    )
                    return _rpc_body(response)["result"]

                alice = await call("alice", "alice-scope", "alice-read")
                assert not alice.get("isError", False)
                bob_denied = await call("bob", "alice-scope", "bob-denied")
                assert bob_denied["isError"] is True
                assert "403" in bob_denied["content"][0]["text"]
                bob = await call("bob", "bob-scope", "bob-read")
                assert not bob.get("isError", False)

                concurrent = await asyncio.gather(
                    call("alice", "alice-scope", "alice-concurrent"),
                    call("bob", "bob-scope", "bob-concurrent"),
                )
                assert all(not result.get("isError", False) for result in concurrent)

                # The same credential now resolves to a different actor with no group grant.
                authentication.identities["Bearer alice-token"] = AuthenticationResult(
                    subject=ALICE, actor=CURRENT_ACTOR
                )
                reduced = await call("alice", "alice-scope", "alice-reduced")
                assert reduced["isError"] is True
                assert "403" in reduced["content"][0]["text"]

                rejected = await client.post(
                    "/mcp/",
                    headers={**headers, "Authorization": "Bearer invalid"},
                    json={
                        "jsonrpc": "2.0",
                        "id": "invalid",
                        "method": "tools/list",
                    },
                )
                assert rejected.status_code == 401

            decisions = list(
                {
                    event.request_id: event
                    for event in await repository.list_audit(resource=ResourceRef.server())
                    if event.operation == "list_memory_entries"
                }.values()
            )
            assert len(decisions) == 6
            assert all(event.transport == "mcp" for event in decisions)
            assert all(event.request_id is not None for event in decisions)
            assert decisions[0].principal == ALICE
            assert decisions[0].actor == FIRST_ACTOR
            assert decisions[0].matched_subject == TEAM
            assert decisions[1].principal == BOB and decisions[1].allowed is False
            assert decisions[2].principal == BOB and decisions[2].allowed is True
            assert {event.principal for event in decisions[3:5]} == {ALICE, BOB}
            assert decisions[-1].principal == ALICE and decisions[-1].allowed is False
            assert decisions[-1].actor == CURRENT_ACTOR
            assert decisions[-1].matched_subject is None

    asyncio.run(scenario())
