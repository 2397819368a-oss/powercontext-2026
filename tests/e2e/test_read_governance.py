# Copyright (c) 2026 OceanBase.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Keep exact evidence, current governance and authority independently observable."""

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import delete, update

from powercontext.builtin.runtime.atomic_memory_rebuild import rebuild_atomic_memory_projection
from powercontext.builtin.runtime.config import RuntimeConfig
from powercontext.server.authz.casbin import CasbinAuthorizationProvider
from powercontext.server.authz.repository import ACCESS_OWNERS_TABLE
from powercontext.server.dashboard.routes import LABELS
from powercontext.server.factory import create_server_app
from powercontext.server.settings import (
    AccessControlConfig,
    BearerAuthConfig,
    DashboardConfig,
    McpConfig,
    MetricsConfig,
    ServerSettings,
)
from tests.e2e.atomic_memory_migration_backend import MigrationBackend
from tests.test_dashboard import create_scope


@pytest.fixture
def dashboard(migration_backend: MigrationBackend):
    app = create_server_app(
        settings=ServerSettings(
            database=migration_backend.config,
            runtime=RuntimeConfig(artifact_processing_families=()),
            dashboard=DashboardConfig(enabled=True),
            access=AccessControlConfig(mode="enforced"),
            auth=BearerAuthConfig(token=SecretStr("governance-acceptance")),
            mcp=McpConfig(enabled=False),
            metrics=MetricsConfig(enabled=False),
        ),
        scheduler_path=migration_backend.directory / "scheduler.db",
    )
    with TestClient(app, headers={"Authorization": "Bearer governance-acceptance"}) as client:
        yield client


def _revised_memory(client, scope):
    created = client.post(
        f"/v1/scopes/{scope}/artifacts",
        json={"family": "atomic-memory", "content": {"kind": "fact", "text": "Original exact evidence."}},
    )
    assert created.status_code == 201, created.text
    artifact = created.json()["artifact_id"]
    path = f"/v1/scopes/{scope}/artifacts/atomic-memory/{artifact}"
    current = client.get(path)
    revised = client.put(
        path,
        headers={"If-Match": current.headers["etag"]},
        json={"content": {"kind": "fact", "text": "Current revised evidence."}},
    )
    assert revised.status_code == 200, revised.text
    assert revised.json()["revision"] == 2
    return artifact, path


@pytest.mark.parametrize("damage", ["missing", "stale"])
def test_exact_revision_and_rebuild_do_not_depend_on_owner_or_current_projection(dashboard, damage):
    scope = create_scope(dashboard, "Projection-independent evidence")["scope_id"]
    artifact, path = _revised_memory(dashboard, scope)
    runtime = dashboard.app.state.application
    index = runtime.atomic_memory._application.index
    query = {"scope": scope, "artifact": artifact, "revision": 1}
    before = dashboard.get(f"{path}/revisions/1")
    assert before.status_code == 200, before.text
    current = dashboard.get(f"{path}/state")
    assert current.status_code == 200, current.text

    async def damage_derived_state():
        async with runtime.primary_database.transaction() as connection:
            await connection.execute(delete(ACCESS_OWNERS_TABLE).where(ACCESS_OWNERS_TABLE.c.scope_id == scope))
            identity = (index.table.c.scope_id == scope, index.table.c.artifact_id == artifact)
            statement = (
                delete(index.table).where(*identity)
                if damage == "missing"
                else update(index.table).where(*identity).values(state_version=99)
            )
            await connection.execute(statement)

    dashboard.portal.call(damage_derived_state)
    listed = dashboard.post("/v1/atomic-memory/list", json={"scope_id": scope})
    assert listed.status_code == 200, listed.text
    # A browse implementation may skip damaged projections or use authority.
    # Every exposed result must still name the exact authoritative version.
    for item in listed.json()["items"]:
        assert item["artifact"] == {"family": "atomic-memory", "artifact_id": artifact, "revision": 2}
        assert item["text"] == "Current revised evidence."
        assert item["state"] == "active"
        assert item["state_version"] == current.json()["state_version"]
    page = dashboard.get("/dashboard/notes", params={"scope": scope})
    assert page.status_code == 200, page.text
    assert "Original exact evidence." not in page.text
    exact = dashboard.get("/dashboard/notes", params=query)
    assert exact.status_code == 200, exact.text
    assert "Original exact evidence." in exact.text
    assert LABELS["historical_revision"] in exact.text
    assert f"{LABELS['current_revision']} @2" in exact.text

    async def rebuild():
        return await rebuild_atomic_memory_projection(runtime.primary_database, index, maintenance_confirmed=True)

    report = dashboard.portal.call(rebuild)
    assert report.ready, report.errors
    assert dashboard.get(f"{path}/revisions/1").json() == before.json()
    recovered = dashboard.get("/dashboard/notes", params={"scope": scope})
    assert recovered.status_code == 200, recovered.text
    assert "Current revised evidence." in recovered.text
    assert LABELS["historical_revision"] not in recovered.text


@pytest.mark.parametrize("provider", ["builtin", "casbin"])
def test_exact_share_does_not_restore_scope_access_or_infer_ownership(dashboard, monkeypatch, provider):
    access = dashboard.app.state.access_control
    if provider == "casbin":
        monkeypatch.setattr(
            access,
            "provider",
            CasbinAuthorizationProvider(access.relationships, deployment_id=access.deployment_id),
        )
    scope = create_scope(dashboard, "Private scope title")["scope_id"]
    artifact, path = _revised_memory(dashboard, scope)
    neighbor = dashboard.post(
        f"/v1/scopes/{scope}/artifacts",
        json={"family": "atomic-memory", "content": {"kind": "fact", "text": "Private neighbor evidence."}},
    )
    assert neighbor.status_code == 201, neighbor.text
    principal = dashboard.get("/v1/access/me").json()["principal"]
    shared = dashboard.post(
        "/v1/access/bindings/create",
        json={
            "subject": principal,
            "resource": {
                "type": "artifact",
                "scope_id": scope,
                "identity": {"family": "atomic-memory", "artifact_id": artifact},
            },
            "role": "artifact.viewer",
            "idempotency_key": "exact-share",
        },
    )
    assert shared.status_code == 201, shared.text
    bindings = dashboard.post(
        "/v1/access/bindings/list",
        json={"management_resource": {"type": "scope", "scope_id": scope}},
    )
    assert bindings.status_code == 200, bindings.text
    for binding in bindings.json()["items"]:
        if binding["resource"]["type"] != "scope":
            continue
        revoked = dashboard.post(
            "/v1/access/bindings/revoke",
            json={
                "binding_id": binding["binding_id"],
                "expected_version": binding["version"],
                "idempotency_key": f"revoke-{binding['binding_id']}",
            },
        )
        assert revoked.status_code == 200, revoked.text

    runtime = dashboard.app.state.application

    async def lose_owner():
        async with runtime.primary_database.transaction() as connection:
            await connection.execute(delete(ACCESS_OWNERS_TABLE).where(ACCESS_OWNERS_TABLE.c.scope_id == scope))

    dashboard.portal.call(lose_owner)
    query = {"scope": scope, "artifact": artifact, "revision": 1}
    exact = dashboard.get("/dashboard/notes", params=query)
    assert exact.status_code == 200, exact.text
    assert "Original exact evidence." in exact.text
    assert LABELS["historical_revision"] in exact.text
    assert "Private scope title" not in exact.text
    assert "Private neighbor evidence." not in exact.text
    assert "Current revised evidence." not in exact.text
    for page in ("home", "notes", "usage"):
        denied = dashboard.get(f"/dashboard/{page}", params={"scope": scope})
        assert denied.status_code == 403, denied.text
        assert "Private scope title" not in denied.text
    assert dashboard.get(f"{path}/revisions/1").status_code == 200
    revoked = dashboard.post(
        "/v1/access/bindings/revoke",
        json={
            "binding_id": shared.json()["binding_id"],
            "expected_version": shared.json()["version"],
            "idempotency_key": "revoke-exact-share",
        },
    )
    assert revoked.status_code == 200, revoked.text
    denied = dashboard.get("/dashboard/notes", params=query)
    assert denied.status_code == 403, denied.text
    assert "Original exact evidence." not in denied.text
