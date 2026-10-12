# Copyright (c) 2026 OceanBase.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at http://www.apache.org/licenses/LICENSE-2.0
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Actual SQLite and HTTP adapter acceptance; no Vision or host mocks."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import os
import socket
import sqlite3
import threading
import time
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from io import BytesIO
from pathlib import Path
from typing import Any

import httpx
import pytest
import time_machine
import uvicorn
from PIL import Image
from sqlalchemy.exc import SQLAlchemyError
from typer.testing import CliRunner

from powercontext.builtin.images.config import ImageConfig
from powercontext.builtin.persistence.sqlite import SQLiteConfig
from powercontext.builtin.runtime import InferenceConfig, RuntimeConfig
from powercontext.cli.app import create_cli
from powercontext.client import PowerContextClient
from powercontext.http import (
    ExperimentalCaptureImageRequest,
    ExperimentalGetImageTaskRequest,
)
from powercontext.server.authentication import AuthenticationResult, ProviderReadiness
from powercontext.server.authz import GroupRef, PrincipalRef
from powercontext.server.authz.composition import open_builtin_access_control
from powercontext.server.factory import create_server_app
from powercontext.server.settings import (
    AccessControlConfig,
    McpConfig,
    MetricsConfig,
    ServerSettings,
)

CAPTURE = "/v1/experimental/images/capture"
ASSET_GET = "/v1/experimental/images/asset/get"
TASK_GET = "/v1/experimental/images/task/get"


def _picture(image_format: str = "PNG", color: str = "red", size: tuple[int, int] = (24, 16)) -> bytes:
    with BytesIO() as buffer:
        Image.new("RGB", size, color).save(buffer, format=image_format)
        return buffer.getvalue()


def _request(scope: str, key: str, data: bytes, media_type: str = "image/png") -> dict[str, object]:
    return {
        "scope_id": scope,
        "capture_key": key,
        "media_type": media_type,
        "bytes_base64": base64.b64encode(data).decode("ascii"),
        "origin": {"kind": "explicit_upload", "attachment_index": 0},
        "context": {"text": "截图中的中文错误"},
    }


@asynccontextmanager
async def _server(
    root: Path,
    *,
    enabled: bool = True,
    model: str | None = "offline-test-model",
    database: SQLiteConfig | None = None,
    **budgets,
):
    settings = ServerSettings(
        database=database or SQLiteConfig(url=f"sqlite+aiosqlite:///{root / 'images.db'}"),
        images=ImageConfig(enabled=enabled, asset_directory=root / "assets", **budgets),
        inference=InferenceConfig(
            vision_model=model, vision_base_url="https://vision.example.invalid/v1", vision_deployment_id="test-v1"
        ),
        runtime=RuntimeConfig(artifact_processing_families=()),
        access=AccessControlConfig(mode="disabled"),
        mcp=McpConfig(enabled=False),
        metrics=MetricsConfig(enabled=False),
    )
    app = create_server_app(settings=settings, scheduler_path=root / "scheduler.db")
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, raise_app_exceptions=False), base_url="http://localhost"
        ) as http,
    ):
        # Pause background parsing to exercise durable acceptance and pending
        # readback independently. Phase-three acceptance runs the actual worker.
        await app.state.application.images.aclose()
        yield app, http


async def _scope(http: httpx.AsyncClient, key: str = "project") -> str:
    response = await http.post("/v1/scopes", json={"title": key, "summary": "图片实验", "idempotency_key": key})
    assert response.status_code == 201, response.text
    return response.json()["scope_id"]


async def _capture(http: httpx.AsyncClient, payload: dict[str, object]) -> dict[str, Any]:
    response = await http.post(CAPTURE, json=payload)
    assert response.status_code == 202, response.text
    return response.json()


@pytest.mark.parametrize("image_format,media", [("PNG", "image/png"), ("JPEG", "image/jpeg")])
def test_original_and_source_survive_deleted_input_restart_and_disable(
    tmp_path: Path, image_format: str, media: str
) -> None:
    async def scenario():
        original = _picture(image_format)
        input_file = tmp_path / "original-image"
        input_file.write_bytes(original)
        async with _server(tmp_path) as (_, http):
            scope = await _scope(http)
            async with PowerContextClient("http://localhost", http_client=http) as client:
                accepted = await client.capture_image(
                    ExperimentalCaptureImageRequest.model_validate(
                        _request(scope, "turn-1", input_file.read_bytes(), media)
                    )
                )
                input_file.unlink()
                task = await client.get_image_task(
                    ExperimentalGetImageTaskRequest(scope_id=scope, task_id=accepted.task_id)
                )
                assert task.state.value == "pending" and task.attempt_count == 0
                assert task.original_payload is not None
                assert task.original_payload.context.text == "截图中的中文错误"
                assert task.original_payload.origin.kind == "explicit_upload"
                assert task.original_payload.name == accepted.original.source.source_id
        async with _server(tmp_path, enabled=False, model=None) as (_, http):
            asset = await http.post(ASSET_GET, json={"scope_id": scope, "asset_id": accepted.asset.asset_id})
            assert asset.status_code == 200, asset.text
            recovered = base64.b64decode(asset.json()["bytes_base64"], validate=True)
            assert recovered == original
            assert asset.json()["asset"]["digest"] == "sha256:" + hashlib.sha256(original).hexdigest()
            task = await http.post(TASK_GET, json={"scope_id": scope, "task_id": accepted.task_id})
            assert task.status_code == 200 and task.json()["state"] == "pending"
            assert task.json()["original_payload"]["name"] == accepted.original.source.source_id
            assert (await http.post(CAPTURE, json=_request(scope, "turn-2", original, media))).status_code == 503

    asyncio.run(scenario())


@pytest.mark.skipif(os.name != "nt", reason="Win32 asset filename length regression")
def test_long_windows_asset_path_preserves_capture_replay_and_readback(tmp_path: Path) -> None:
    async def scenario():
        root = tmp_path / ("nested-" + "x" * max(1, 170 - len(str(tmp_path))))
        root.mkdir()
        data = _picture()
        async with _server(root) as (_, http):
            scope = await _scope(http)
            request = _request(scope, "long-windows-path", data)
            first = await _capture(http, request)
            replay = await _capture(http, request)
            assert replay["replayed"] is True and replay["task_id"] == first["task_id"]
        async with _server(root, enabled=False, model=None) as (_, http):
            asset = await http.post(ASSET_GET, json={"scope_id": scope, "asset_id": first["asset"]["asset_id"]})
            assert asset.status_code == 200
            assert base64.b64decode(asset.json()["bytes_base64"]) == data

    asyncio.run(scenario())


def test_replay_keeps_original_profile_and_sequence_and_conflicts_on_changed_payload(tmp_path: Path) -> None:
    async def scenario():
        red, blue = _picture(), _picture(color="blue")
        async with _server(tmp_path) as (_, http):
            scope = await _scope(http)
            request = _request(scope, "same-text-turn", red)
            first = await _capture(http, request)
            duplicate = await _capture(http, request)
            assert duplicate == first | {"replayed": True}
            different = await http.post(CAPTURE, json=_request(scope, "same-text-turn", blue))
            assert different.status_code == 409
            later = await _capture(http, _request(scope, "next-turn", blue))
            assert later["original"] != first["original"]
            assert later["sequence"] == first["sequence"] + 1
        async with _server(tmp_path, model="changed-model") as (_, http):
            replay = await _capture(http, request)
            assert replay == first | {"replayed": True}

    asyncio.run(scenario())


def test_asset_dedup_preserves_each_scope_and_occurrence_and_queue_budget(tmp_path: Path) -> None:
    async def scenario():
        data = _picture()
        async with _server(tmp_path, max_pending_per_scope=2, max_asset_bytes_total=len(data)) as (_, http):
            scope = await _scope(http)
            other = await _scope(http, "other")
            first = await _capture(http, _request(scope, "one", data))
            second = await _capture(http, _request(scope, "two", data))
            assert first["asset"] == second["asset"] and first["original"] != second["original"]
            assert (await http.post(CAPTURE, json=_request(scope, "three", data))).json()["error"][
                "code"
            ] == "image_queue_full"
            foreign = await http.post(ASSET_GET, json={"scope_id": other, "asset_id": first["asset"]["asset_id"]})
            assert foreign.status_code == 404
            assert (await http.post(CAPTURE, json=_request(other, "one", data))).json()["error"][
                "code"
            ] == "image_storage_budget"
            assert (await http.post(TASK_GET, json={"scope_id": other, "task_id": first["task_id"]})).status_code == 404

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "data,media,status",
    [
        (b"not an image", "image/png", 422),
        (_picture("JPEG"), "image/png", 415),
        (_picture("GIF"), "image/png", 415),
        (_picture()[:-10], "image/png", 422),
        (_picture(size=(16_385, 1)), "image/png", 413),
    ],
)
def test_invalid_images_are_rejected_without_acceptance(tmp_path: Path, data: bytes, media: str, status: int) -> None:
    async def scenario():
        async with _server(tmp_path) as (_, http):
            scope = await _scope(http)
            failed = await http.post(CAPTURE, json=_request(scope, "image", data, media))
            assert failed.status_code == status, failed.text
            valid = await _capture(http, _request(scope, "image", _picture()))
            assert valid["replayed"] is False
            assert not list((tmp_path / "assets").rglob(".image-*"))

    asyncio.run(scenario())


def test_chunked_body_is_limited_before_json_and_default_off_creates_no_assets(tmp_path: Path) -> None:
    async def chunks():
        for _ in range(13):
            yield b"x" * 1024 * 1024

    async def scenario():
        async with _server(tmp_path, enabled=False, model=None) as (_, http):
            failed = await http.post(CAPTURE, content=chunks(), headers={"content-type": "application/json"})
            assert failed.status_code == 413, failed.text
            assert not (tmp_path / "assets").exists()
            assert (await http.get("/health/live")).status_code == 200
        with sqlite3.connect(tmp_path / "images.db") as database:
            assert (
                database.execute("select name from sqlite_master where name like 'pc_experimental_image_%'").fetchall()
                == []
            )

    asyncio.run(scenario())


@pytest.mark.parametrize("root_path", ["/pc", "/实验"])
def test_root_path_deployment_preserves_pre_json_image_body_budget(tmp_path: Path, root_path: str) -> None:
    async def scenario():
        async with (
            _server(tmp_path, enabled=False) as (app, _),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app, root_path=root_path), base_url="http://localhost" + root_path
            ) as http,
        ):

            async def chunks():
                for _ in range(12):
                    yield b"x" * 1024 * 1024
                yield b"x"

            assert (await http.get("/health/live")).status_code == 200
            response = await http.post(CAPTURE, content=chunks(), headers={"Content-Type": "application/json"})
            assert response.status_code == 413 and response.json()["error"]["code"] == "image_too_large"
            response = await http.post(CAPTURE, json={"bytes_base64": "A" * (4 * ((8 * 1024 * 1024 + 2) // 3) + 4)})
            assert response.status_code == 413 and response.json()["error"]["code"] == "image_too_large"

    asyncio.run(scenario())


def test_concurrent_replays_accept_one_occurrence(tmp_path: Path) -> None:
    async def scenario():
        async with _server(tmp_path) as (_, http):
            scope = await _scope(http)
            request = _request(scope, "concurrent", _picture())
            responses = await asyncio.gather(*[_capture(http, request) for _ in range(3)])
            assert sum(not response["replayed"] for response in responses) == 1
            assert len({response["task_id"] for response in responses}) == 1
            assert len({response["sequence"] for response in responses}) == 1

    asyncio.run(scenario())


def test_db_failure_does_not_accept_an_unreadable_or_partial_capture(tmp_path: Path, monkeypatch) -> None:
    async def reject(*args, **kwargs):
        raise OSError("injected storage failure")  # noqa: TRY003

    async def scenario():
        async with _server(tmp_path, max_asset_bytes_total=len(_picture())) as (app, http):
            scope = await _scope(http)
            sources = app.state.application.images.sources
            original = sources.add_with_status
            monkeypatch.setattr(sources, "add_with_status", reject)
            failed = await http.post(CAPTURE, json=_request(scope, "failure", _picture()))
            assert failed.status_code == 500
            monkeypatch.setattr(sources, "add_with_status", original)
            asset_id = "ia_" + hashlib.sha256(_picture()).hexdigest()
            assert (await http.post(ASSET_GET, json={"scope_id": scope, "asset_id": asset_id})).status_code == 404
            blocked = await http.post(CAPTURE, json=_request(scope, "new-color", _picture(color="blue")))
            assert blocked.status_code == 503 and blocked.json()["error"]["code"] == "image_storage_budget"
            accepted = await _capture(http, _request(scope, "failure", _picture()))
            assert accepted["replayed"] is False
            read = await http.post(ASSET_GET, json={"scope_id": scope, "asset_id": accepted["asset"]["asset_id"]})
            assert read.status_code == 200 and base64.b64decode(read.json()["bytes_base64"]) == _picture()

    asyncio.run(scenario())


def test_orphan_recovery_is_bounded_and_preserves_referenced_assets(tmp_path: Path, monkeypatch) -> None:
    async def reject(*args, **kwargs):
        raise OSError("injected")

    async def scenario():
        red, blue = _picture(), _picture(color="blue")
        async with _server(tmp_path, max_asset_bytes_total=len(red) + len(blue)) as (app, http):
            scope = await _scope(http)
            retained = await _capture(http, _request(scope, "retained", red))
            original = app.state.application.images.sources.add_with_status
            monkeypatch.setattr(app.state.application.images.sources, "add_with_status", reject)
            assert (await http.post(CAPTURE, json=_request(scope, "failed", blue))).status_code == 500
            monkeypatch.setattr(app.state.application.images.sources, "add_with_status", original)
            # Simulate the file state of an interrupted publication while its
            # capacity reservation remains durable in the actual database.
            interrupted = next((tmp_path / "assets").rglob("ia_" + hashlib.sha256(blue).hexdigest()))
            interrupted.rename(interrupted.with_name(".image-" + interrupted.name))
        with time_machine.travel(datetime.now(UTC) + timedelta(days=2), tick=False):
            async with _server(tmp_path, max_asset_bytes_total=len(red) + len(blue)) as (_, http):
                read = await http.post(ASSET_GET, json={"scope_id": scope, "asset_id": retained["asset"]["asset_id"]})
                assert read.status_code == 200 and base64.b64decode(read.json()["bytes_base64"]) == red
                assert len(list((tmp_path / "assets").rglob("ia_*"))) == 1
                assert (
                    await http.post(TASK_GET, json={"scope_id": scope, "task_id": retained["task_id"]})
                ).status_code == 200
                await _capture(http, _request(scope, "recovered", blue))
                disk_bytes = sum(path.stat().st_size for path in (tmp_path / "assets").rglob("*") if path.is_file())
                assert disk_bytes <= len(red) + len(blue)

    asyncio.run(scenario())


def test_asset_corruption_and_unknown_format_do_not_return_false_success(tmp_path: Path) -> None:
    async def scenario():
        async with _server(tmp_path) as (_, http):
            scope = await _scope(http)
            accepted = await _capture(http, _request(scope, "image", _picture()))
            stored = next((tmp_path / "assets").rglob(accepted["asset"]["asset_id"]))
            stored.write_bytes(b"changed")
            read = await http.post(ASSET_GET, json={"scope_id": scope, "asset_id": accepted["asset"]["asset_id"]})
            assert read.status_code == 500 and read.json()["error"]["code"] == "image_asset_corrupt"
        with sqlite3.connect(tmp_path / "images.db") as database:
            database.execute("update pc_experimental_image_state set format_version='unsupported'")
        async with _server(tmp_path) as (_, http):
            assert (
                await http.post(TASK_GET, json={"scope_id": scope, "task_id": accepted["task_id"]})
            ).status_code == 503
            assert (await http.get("/health/live")).status_code == 200
            text = await http.post(f"/v1/scopes/{scope}/sources", json={"content": "普通文字仍可保存"})
            assert text.status_code == 201

    asyncio.run(scenario())


@pytest.mark.parametrize("schema_change", ["missing_state_column", "missing_asset_table"])
def test_incomplete_image_schema_keeps_text_runtime_available(tmp_path: Path, schema_change: str) -> None:
    async def scenario():
        async with _server(tmp_path) as (_, http):
            scope = await _scope(http)
        with sqlite3.connect(tmp_path / "images.db") as database:
            if schema_change == "missing_state_column":
                database.execute("alter table pc_experimental_image_state drop column asset_bytes")
            else:
                database.execute("drop table pc_experimental_image_assets")
        async with _server(tmp_path) as (_, http):
            failed = await http.post(CAPTURE, json=_request(scope, "image", _picture()))
            assert failed.status_code == 503 and failed.json()["error"]["code"] == "image_format_unsupported"
            text = await http.post(f"/v1/scopes/{scope}/sources", json={"content": "普通文字仍可保存"})
            assert text.status_code == 201

    asyncio.run(scenario())


@pytest.mark.parametrize("in_memory", [True, False])
def test_non_durable_sqlite_rejects_image_acceptance(tmp_path: Path, in_memory: bool) -> None:
    async def scenario():
        database = (
            SQLiteConfig()
            if in_memory
            else SQLiteConfig(url=f"sqlite+aiosqlite:///{tmp_path / 'memory-journal.db'}", journal_mode="MEMORY")
        )
        async with _server(tmp_path, database=database) as (_, http):
            scope = await _scope(http)
            failed = await http.post(CAPTURE, json=_request(scope, "image", _picture()))
            assert failed.status_code == 503 and failed.json()["error"]["code"] == "image_database_not_durable"
            assert not (tmp_path / "assets").exists()
            text = await http.post(f"/v1/scopes/{scope}/sources", json={"content": "普通文字仍可保存"})
            assert text.status_code == 201

    asyncio.run(scenario())


def test_orphan_cleanup_io_failure_keeps_text_runtime_available(tmp_path: Path, monkeypatch) -> None:
    from powercontext.builtin.images.service import ImageService

    async def reject_cleanup(self):
        raise PermissionError("injected")

    async def scenario():
        monkeypatch.setattr(ImageService, "cleanup_orphan_assets", reject_cleanup)
        async with _server(tmp_path) as (_, http):
            scope = await _scope(http)
            failed = await http.post(CAPTURE, json=_request(scope, "image", _picture()))
            assert failed.status_code == 503 and failed.json()["error"]["code"] == "image_storage_unavailable"
            text = await http.post(f"/v1/scopes/{scope}/sources", json={"content": "普通文字仍可保存"})
            assert text.status_code == 201

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "failure", [OSError("locked orphan"), SQLAlchemyError("maintenance database failure")], ids=["io", "sql"]
)
def test_orphan_maintenance_failure_preserves_retained_image_readback(tmp_path: Path, monkeypatch, failure) -> None:
    from powercontext.builtin.images.service import ImageService

    async def scenario():
        async with _server(tmp_path) as (_, http):
            scope = await _scope(http)
            accepted = await _capture(http, _request(scope, "retained-before-maintenance", _picture()))
            task_request = {"scope_id": scope, "task_id": accepted["task_id"]}
            historical = (await http.post(TASK_GET, json=task_request)).json()

        async def fail_cleanup(service):
            raise failure

        monkeypatch.setattr(ImageService, "cleanup_orphan_assets", fail_cleanup)
        async with _server(tmp_path) as (_, http):
            task = await http.post(TASK_GET, json=task_request)
            assert task.status_code == 200 and task.json()["task_id"] == historical["task_id"]
            assert task.json()["original_payload"] == historical["original_payload"]
            asset = await http.post(ASSET_GET, json={"scope_id": scope, "asset_id": accepted["asset"]["asset_id"]})
            assert asset.status_code == 200 and base64.b64decode(asset.json()["bytes_base64"]) == _picture()
            assert (
                await http.post(CAPTURE, json=_request(scope, "maintenance-blocked", _picture()))
            ).status_code == 503
            assert (
                await http.post("/v1/experimental/images/task/retry", json={**task_request, "retry_key": "blocked"})
            ).status_code == 503
            assert (
                await http.post(
                    "/v1/experimental/images/reparse",
                    json={"scope_id": scope, "original": accepted["original"]["source"], "reparse_key": "blocked"},
                )
            ).status_code == 503
            assert (await http.get("/health/live")).status_code == 200
            assert await _scope(http, "text-during-maintenance-failure")

    asyncio.run(scenario())


@pytest.mark.parametrize("byte_length", [8 * 1024 * 1024 + 1, 8 * 1024 * 1024 + 2])
def test_decoded_and_encoded_byte_budgets_are_413(tmp_path: Path, byte_length: int) -> None:
    async def scenario():
        async with _server(tmp_path) as (_, http):
            scope = await _scope(http)
            result = await http.post(CAPTURE, json=_request(scope, "oversized", b"x" * byte_length))
            assert result.status_code == 413, result.text
            assert not (tmp_path / "assets").exists()

    asyncio.run(scenario())


class _Authentication:
    async def authenticate(self, request):
        name = request.headers.get("authorization", "Bearer admin").removeprefix("Bearer ")
        return AuthenticationResult(
            subject=PrincipalRef(type="service" if name == "admin" else "user", id=name),
            subject_groups=(GroupRef(type="group", id="writers"),) if name == "member" else (),
        )

    async def readiness(self):
        return ProviderReadiness(ready=True)


def test_scope_authorization_covers_original_task_capture_and_failed_provider(tmp_path: Path) -> None:
    async def scenario():
        database = SQLiteConfig(url=f"sqlite+aiosqlite:///{tmp_path / 'authorized.db'}")
        async with open_builtin_access_control(
            database, bootstrap_administrators=(PrincipalRef(type="service", id="admin"),), deployment_id="images"
        ) as access:
            access.provider_capabilities = replace(access.provider_capabilities, group_subjects=True)
            settings = ServerSettings(
                database=database,
                images=ImageConfig(enabled=True, asset_directory=tmp_path / "assets"),
                inference=InferenceConfig(
                    vision_model="offline",
                    vision_base_url="https://vision.example.invalid/v1",
                    vision_deployment_id="v1",
                ),
                runtime=RuntimeConfig(artifact_processing_families=()),
                mcp=McpConfig(enabled=False),
                metrics=MetricsConfig(enabled=False),
                access=AccessControlConfig(mode="enforced", deployment_id="images"),
            )
            app = create_server_app(
                settings=settings,
                authentication_provider=_Authentication(),
                access_control=access,
                scheduler_path=tmp_path / "scheduler.db",
            )
            async with (
                app.router.lifespan_context(app),
                httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost") as http,
            ):
                scope = await _scope(http)
                accepted = await _capture(http, _request(scope, "owner", _picture()))
                for name, role in (
                    ("reader", "scope.viewer"),
                    ("writer", "scope.contributor"),
                    ("manager", "scope.admin"),
                ):
                    grant = await http.post(
                        "/v1/access/bindings/create",
                        json={
                            "subject": {"type": "user", "id": name},
                            "resource": {"type": "scope", "scope_id": scope},
                            "role": role,
                            "idempotency_key": name,
                        },
                    )
                    assert grant.status_code == 201, grant.text
                asset = {"scope_id": scope, "asset_id": accepted["asset"]["asset_id"]}
                task = {"scope_id": scope, "task_id": accepted["task_id"]}
                for principal in ("outsider", "manager"):
                    headers = {"Authorization": f"Bearer {principal}"}
                    assert (await http.post(ASSET_GET, json=asset, headers=headers)).status_code == 403
                    assert (await http.post(TASK_GET, json=task, headers=headers)).status_code == 403
                reader = {"Authorization": "Bearer reader"}
                assert (await http.post(ASSET_GET, json=asset, headers=reader)).status_code == 200
                assert (await http.post(TASK_GET, json=task, headers=reader)).status_code == 200
                assert (
                    await http.post(CAPTURE, json=_request(scope, "denied", _picture()), headers=reader)
                ).status_code == 403
                for principal in ("outsider", "manager", "reader"):
                    headers = {"Authorization": f"Bearer {principal}"}
                    assert (
                        await http.post(
                            "/v1/experimental/images/task/retry",
                            json={**task, "retry_key": "denied-retry"},
                            headers=headers,
                        )
                    ).status_code == 403
                    assert (
                        await http.post(
                            "/v1/experimental/images/reparse",
                            json={
                                "scope_id": scope,
                                "original": accepted["original"]["source"],
                                "reparse_key": "denied",
                            },
                            headers=headers,
                        )
                    ).status_code == 403
                assert (
                    await http.post(
                        CAPTURE, json=_request(scope, "writer", _picture()), headers={"Authorization": "Bearer writer"}
                    )
                ).status_code == 202
                grant = await http.post(
                    "/v1/access/bindings/create",
                    json={
                        "subject": {"type": "group", "id": "writers"},
                        "resource": {"type": "scope", "scope_id": scope},
                        "role": "scope.contributor",
                        "idempotency_key": "writers",
                    },
                )
                assert grant.status_code == 201, grant.text
                text = await http.post(f"/v1/scopes/{scope}/sources", json={"content": "截图上下文"})
                assert text.status_code == 201, text.text
                request = _request(scope, "group-member", _picture())
                request["context"] = {
                    "text": "截图上下文",
                    "source": {
                        "scope_id": scope,
                        "source": {"source_type": "content", "source_id": text.json()["source_id"]},
                    },
                }
                member_capture = await http.post(CAPTURE, json=request, headers={"Authorization": "Bearer member"})
                assert member_capture.status_code == 202, member_capture.text
                app.state.access_control = None
                assert (await http.post(ASSET_GET, json=asset, headers=reader)).status_code == 503

    asyncio.run(scenario())


def test_image_domain_failures_keep_their_code_in_operation_logs(tmp_path: Path, caplog) -> None:
    async def scenario():
        async with _server(tmp_path) as (_, http):
            scope = await _scope(http)
            await _capture(http, _request(scope, "once", _picture()))
            caplog.clear()
            with caplog.at_level(logging.WARNING, logger="powercontext.server.app"):
                conflict = await http.post(CAPTURE, json=_request(scope, "once", _picture(color="blue")))
                missing = await http.post(ASSET_GET, json={"scope_id": scope, "asset_id": "ia_" + "0" * 64})
            assert conflict.status_code == 409 and missing.status_code == 404
            records = [
                record
                for record in caplog.records
                if getattr(record, "event", None) == "application.operation.completed"
            ]
            assert {record.error_code for record in records} == {"image_capture_conflict", "image_asset_not_found"}
            assert all(record.levelno == logging.WARNING for record in records)

    asyncio.run(scenario())


def test_real_loopback_cli_upload_task_and_verified_download(tmp_path: Path) -> None:
    settings = ServerSettings(
        database=SQLiteConfig(url=f"sqlite+aiosqlite:///{tmp_path / 'cli.db'}"),
        images=ImageConfig(enabled=True, asset_directory=tmp_path / "assets"),
        inference=InferenceConfig(
            vision_model="offline", vision_base_url="https://vision.example.invalid/v1", vision_deployment_id="v1"
        ),
        runtime=RuntimeConfig(artifact_processing_families=()),
        access=AccessControlConfig(mode="disabled"),
        mcp=McpConfig(enabled=False),
        metrics=MetricsConfig(enabled=False),
    )
    app = create_server_app(settings=settings, scheduler_path=tmp_path / "scheduler.db")
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    url = f"http://127.0.0.1:{listener.getsockname()[1]}"
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", access_log=False))
    worker = threading.Thread(target=lambda: server.run(sockets=[listener]), daemon=True)
    worker.start()
    try:
        for _ in range(1000):
            if server.started or not worker.is_alive():
                break
            time.sleep(0.01)
        assert server.started
        with httpx.Client(base_url=url) as http:
            scope = http.post("/v1/scopes", json={"title": "CLI", "summary": "实验", "idempotency_key": "cli"}).json()[
                "scope_id"
            ]
        input_file = tmp_path / "image.png"
        data = _picture()
        input_file.write_bytes(data)
        cli = create_cli(commands=())
        runner = CliRunner()
        invalid = runner.invoke(
            cli,
            ["image", "upload", str(input_file), "--scope-id", scope, "--capture-key", ""],
        )
        assert invalid.exit_code == 2 and "image upload" in invalid.output
        assert "Invalid value for 'file'" not in invalid.output
        upload = runner.invoke(
            cli,
            [
                "--server-url",
                url,
                "--json",
                "image",
                "upload",
                str(input_file),
                "--scope-id",
                scope,
                "--capture-key",
                "cli-upload",
                "--context-text",
                "中文说明",
            ],
        )
        assert upload.exit_code == 0, upload.output
        import json

        accepted = json.loads(upload.stdout)
        input_file.unlink()
        task = runner.invoke(
            cli, ["--server-url", url, "--json", "image", "task", accepted["task_id"], "--scope-id", scope]
        )
        assert task.exit_code == 0 and json.loads(task.stdout)["original_payload"]["context"]["text"] == "中文说明"
        output = tmp_path / "download.png"
        command = [
            "--server-url",
            url,
            "image",
            "download",
            accepted["asset"]["asset_id"],
            "--scope-id",
            scope,
            "--output",
            str(output),
        ]
        download = runner.invoke(cli, command)
        assert download.exit_code == 0, download.output
        assert output.read_bytes() == data
        assert runner.invoke(cli, command).exit_code != 0
        assert output.read_bytes() == data
    finally:
        server.should_exit = True
        worker.join(timeout=10)
        listener.close()
    assert not worker.is_alive()
