"""Managed collect instances preserve Scope progress and independent resource lifetimes."""

from __future__ import annotations

import asyncio
import json
import shutil

import yaml

from powercontext.limits import MAX_SOURCE_OBSERVATION_BYTES
from tests.e2e.source_collect_helpers import (
    bind_server,
    cli,
    grant_scope,
    instance_state,
    prepare_environment,
    set_enabled,
    stop_services,
)
from tests.source_helpers import ROOT, create_scope, running_server

EXAMPLE = ROOT / "examples/source_plugins/ticket_chat"


async def checkpoint(client, scope, identifier, *, after=None):
    async with asyncio.timeout(40):
        while True:
            response = await client.post(
                "/v1/connector-checkpoints/get",
                json={
                    "binding": {
                        "scope_id": scope,
                        "binding_id": identifier,
                        "connector_name": "example-ticket-chat",
                        "connector_version": "1",
                    }
                },
            )
            assert response.status_code == 200, response.text
            current = response.json()["checkpoint"]
            if current is not None and current != after:
                return current
            await asyncio.sleep(0.1)


async def received(client, scope, target, count):
    async with asyncio.timeout(40):
        while True:
            response = await client.post("/v1/source-management/status", json={"scope_id": scope, "target_id": target})
            assert response.status_code == 200, response.text
            if len(response.json()["source_refs"]) == count:
                return response.json()
            await asyncio.sleep(0.1)


def test_managed_instances_isolate_scopes_and_release_resources(tmp_path):
    asyncio.run(_managed_instances(tmp_path))


async def _managed_instances(directory):
    envfile, state = prepare_environment(directory)
    environment = {
        "POWERCONTEXT_SERVER_SOURCE_PLUGINS_DIR": str(directory / "source-plugins"),
        "POWERCONTEXT_SERVER_SOURCE_MANAGEMENT": json.dumps({"state_dir": str(state)}),
    }
    async with running_server(directory, environment=environment) as (admin, url, _process):
        bind_server(envfile, url)
        scopes = [await create_scope(admin, name) for name in ("first", "second")]
        for scope in scopes:
            await grant_scope(admin, scope)
        first, second = directory / "first", directory / "second"
        shutil.copytree(EXAMPLE, first, ignore=shutil.ignore_patterns("__pycache__"))
        second.mkdir()
        shutil.copy(first / "events.json", second / "events.json")
        entry = first / "__init__.py"
        entry.write_text(
            entry.read_text()
            + """

_collect = apply

async def apply(ctx, config):
    def closed():
        (ctx.plugin_dir / "closed").touch()
    ctx.on_dispose(closed)
    await _collect(ctx, config)
"""
        )
        targets = {}
        try:
            for instance, selected in ((first, scopes), (second, scopes[1:])):
                manifest = {
                    "id": instance.name,
                    "plugin": str(first),
                    "scope_ids": selected,
                    "config": {"namespace": instance.name, "interval_seconds": 0.1},
                    "targets": [
                        {"scope_id": scope, "capability": "ticket-chat.dataset", "capability_version": "1", "input": {}}
                        for scope in selected
                    ],
                }
                path = instance / "powercontext.yml"
                path.write_text(yaml.safe_dump(manifest))
                await cli(directory, "source", "load", "--config", str(path), "--env-file", str(envfile))
                await instance_state(admin, instance.name, "running")
                for scope in selected:
                    added = await admin.post(
                        "/v1/source-management/add",
                        json={
                            "scope_id": scope,
                            "instance_id": instance.name,
                            "capability": "ticket-chat.dataset",
                            "capability_version": "1",
                            "input": {},
                        },
                    )
                    assert added.status_code == 200, added.text
                    target = added.json()["target_id"]
                    targets[instance.name, scope] = target
                    await received(admin, scope, target, 2)
            previous = await checkpoint(admin, scopes[1], "first")
            stopped_target = await admin.post(
                "/v1/source-management/target-set-enabled",
                json={
                    "scope_id": scopes[1],
                    "target_id": targets["first", scopes[1]],
                    "enabled": False,
                },
            )
            assert stopped_target.status_code == 200, stopped_target.text
            async with asyncio.timeout(40):
                while True:
                    status = await admin.post(
                        "/v1/source-management/status",
                        json={
                            "scope_id": scopes[1],
                            "target_id": targets["first", scopes[1]],
                        },
                    )
                    if status.json()["tracking"] == "stopped":
                        break
                    await asyncio.sleep(0.1)
            fixture = json.loads((first / "events.json").read_text())
            fixture["tickets"][0]["body"] = "Changed in the first Scope only"
            replacement = first / "events.next.json"
            replacement.write_text(json.dumps(fixture))
            replacement.replace(first / "events.json")
            await received(admin, scopes[0], targets["first", scopes[0]], 3)
            assert await checkpoint(admin, scopes[1], "first") == previous
            await set_enabled(admin, "first", False)
            assert (first / "closed").exists()
            assert not (second / "closed").exists()
            assert (await instance_state(admin, "second"))["actual"] == "running"
            previous = await checkpoint(admin, scopes[1], "second")
            fixture["tickets"][0]["body"] = "Second instance continues after the first stops"
            replacement = second / "events.next.json"
            replacement.write_text(json.dumps(fixture))
            replacement.replace(second / "events.json")
            await received(admin, scopes[1], targets["second", scopes[1]], 3)
            # Submission reports can precede checkpoint persistence. Wait for both.
            previous = await checkpoint(admin, scopes[1], "second", after=previous)
            # A rejected observation must not advance the Scope's checkpoint.
            fixture["tickets"][0]["body"] = "x" * MAX_SOURCE_OBSERVATION_BYTES
            replacement.write_text(json.dumps(fixture))
            replacement.replace(second / "events.json")
            await instance_state(admin, "second", "failed")
            assert (second / "closed").exists()
            assert await checkpoint(admin, scopes[1], "second") == previous
            assert len((await received(admin, scopes[1], targets["second", scopes[1]], 3))["source_refs"]) == 3
        finally:
            await stop_services(directory, state)
