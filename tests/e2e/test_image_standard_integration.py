# Copyright (c) 2026 OceanBase. Licensed under the Apache License, Version 2.0.
"""Ordinary settings, model reuse and scoped image directory through real HTTP/SQLite."""

import asyncio
import sqlite3
from io import BytesIO

import pytest
from PIL import Image
from pydantic import AnyHttpUrl, SecretStr

from powercontext.builtin.images.config import ImageConfig, parse_profile
from powercontext.builtin.images.tasks import ImageTaskQueue
from powercontext.builtin.images.vision import VisionFailure, VisionParser
from powercontext.builtin.runtime import InferenceConfig
from powercontext.client import PowerContextClient
from powercontext.http import ExperimentalGetImageStatusRequest, ExperimentalListImageTasksRequest
from tests.e2e.test_image_processing import Backend, _listen, _running, _terminal
from tests.e2e.test_image_source_product import _capture, _picture, _request, _scope


def test_generation_reuse_sends_multimodal_bytes_and_matching_credentials(tmp_path):
    async def scenario():
        backend = Backend({})
        async with _listen(backend.app) as endpoint:
            config = InferenceConfig(
                vision_use_generation=True,
                generation_model="openai-chat:reused-vision",
                generation_base_url=endpoint + "/v1",
                generation_headers={"Authorization": SecretStr("Bearer reused-test")},
                vision_model="unused-vision",
                vision_deployment_id="stale-dedicated-deployment",
                vision_base_url="https://unused.invalid/v1",
                vision_headers={"Authorization": SecretStr("Bearer must-not-send")},
            )
            async with _running(tmp_path, endpoint, inference=config) as (_, http):
                scope = await _scope(http)
                capture = await _capture(http, _request(scope, "reuse", _picture()))
                saved = await _terminal(http, scope, capture["task_id"])
                assert saved["state"] == "succeeded"
                assert backend.requests[0]["model"] == "reused-vision"
                assert backend.requests[0]["authorization"] == "Bearer reused-test"
                assert saved["analysis_payload"]["profile"]["model_id"] == "reused-vision"
                assert saved["analysis_payload"]["profile"]["deployment_id"] == "generation:reused-vision"
                async with PowerContextClient("http://localhost", http_client=http) as client:
                    status = await client.get_image_status(ExperimentalGetImageStatusRequest(scope_id=scope))
                    assert status.vision_connection.value == "generation" and status.worker_running

    asyncio.run(scenario())


def test_unspecified_asset_directory_preserves_retained_historical_root(tmp_path):
    async def scenario():
        backend = Backend({})
        async with _listen(backend.app) as endpoint:
            async with _running(tmp_path, endpoint + "/v1") as (_, http):
                scope = await _scope(http)
                accepted = await _capture(http, _request(scope, "retained-root", _picture()))
                await _terminal(http, scope, accepted["task_id"])
            async with _running(tmp_path, None, image_config=ImageConfig()) as (_, http):
                material = await http.post(
                    "/v1/experimental/images/asset/get",
                    json={"scope_id": scope, "asset_id": accepted["asset"]["asset_id"]},
                )
                assert material.status_code == 200, material.text
                page = await http.post("/v1/experimental/images/tasks/list", json={"scope_id": scope})
                assert page.json()["items"][0]["task"]["task_id"] == accepted["task_id"]

    asyncio.run(scenario())


def test_recoverable_sqlite_io_failure_resumes_durable_image_processing(tmp_path, monkeypatch):
    from sqlalchemy.exc import OperationalError

    claim = ImageTaskQueue.claim
    injected = False

    async def fail_once(queue):
        nonlocal injected
        if not injected:
            injected = True
            failure = sqlite3.OperationalError("simulated IO failure")
            failure.sqlite_errorcode = sqlite3.SQLITE_IOERR
            raise OperationalError("claim", {}, failure)
        return await claim(queue)

    monkeypatch.setattr(ImageTaskQueue, "claim", fail_once)

    async def scenario():
        backend = Backend({})
        async with _listen(backend.app) as endpoint, _running(tmp_path, endpoint + "/v1") as (_, http):
            scope = await _scope(http)
            accepted = await _capture(http, _request(scope, "io-recovery", _picture()))
            task = await _terminal(http, scope, accepted["task_id"])
            assert task["state"] == "succeeded" and task["attempt_count"] == 1
            status = await http.post("/v1/experimental/images/status", json={"scope_id": scope})
            assert status.json()["worker_running"] and status.json()["error_code"] is None

    asyncio.run(scenario())


def test_unknown_worker_invariant_rejects_new_capture_and_reports_unavailable(tmp_path, monkeypatch):
    async def unavailable(queue):
        raise ValueError("invalid retained task invariant")  # noqa: TRY003 - injected persisted invariant failure

    monkeypatch.setattr(ImageTaskQueue, "claim", unavailable)

    async def scenario():
        backend = Backend({})
        async with _listen(backend.app) as endpoint, _running(tmp_path, endpoint + "/v1") as (_, http):
            scope = await _scope(http)
            async with asyncio.timeout(5):
                while True:
                    status = await http.post("/v1/experimental/images/status", json={"scope_id": scope})
                    if status.json()["error_code"]:
                        break
                    await asyncio.sleep(0.02)
            assert status.json()["error_code"] == "image_worker_unavailable"
            response = await http.post("/v1/experimental/images/capture", json=_request(scope, "stop", _picture()))
            assert response.status_code == 503 and "image_worker_unavailable" in response.text
            assert (await http.post("/v1/experimental/images/tasks/list", json={"scope_id": scope})).status_code == 200

    asyncio.run(scenario())


def test_changed_reused_endpoint_cannot_silently_run_an_accepted_profile():
    config = InferenceConfig(
        vision_use_generation=True,
        generation_model="openai-chat:model",
        generation_base_url=AnyHttpUrl("https://one.invalid/v1"),
    )
    profile = parse_profile(config)
    assert profile is not None
    config = InferenceConfig(
        vision_use_generation=True,
        generation_model="openai-chat:model",
        generation_base_url=AnyHttpUrl("https://two.invalid/v1"),
    )
    with pytest.raises(VisionFailure, match="image_vision_configuration_unavailable"):
        VisionParser(config).headers_for(profile)
    with pytest.raises(ValueError, match="openai-chat"):
        parse_profile(InferenceConfig(vision_use_generation=True, generation_model="anthropic:vision"))


def test_reuse_resolves_generation_environment_key_without_leaking_it_to_dedicated_vision(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-environment-key")
    config = InferenceConfig(
        vision_use_generation=True,
        generation_model="openai-chat:multimodal",
        generation_base_url=AnyHttpUrl("https://generation.invalid/v1"),
        generation_headers={"X-Tenant": SecretStr("tenant")},
    )
    profile = parse_profile(config)
    assert profile is not None
    assert VisionParser(config).headers_for(profile) == {
        "X-Tenant": "tenant",
        "Authorization": "Bearer synthetic-environment-key",
    }
    explicit = config.model_copy(update={"generation_headers": {"authorization": SecretStr("Bearer role-key")}})
    assert VisionParser(explicit).headers_for(profile) == {"authorization": "Bearer role-key"}
    dedicated = InferenceConfig(
        vision_model="vision", vision_base_url="https://vision.invalid/v1", vision_deployment_id="v1"
    )
    separate = parse_profile(dedicated)
    assert separate is not None
    with pytest.raises(VisionFailure, match="image_vision_credentials_unavailable"):
        VisionParser(dedicated).headers_for(separate)


def test_image_directory_paginates_reads_exact_materials_and_remains_readable_when_disabled(tmp_path):
    async def scenario():
        backend = Backend({})
        async with _listen(backend.app) as endpoint:
            async with _running(tmp_path, endpoint + "/v1", dashboard=True) as (_, http):
                scope = await _scope(http)
                other = await _scope(http, "other")
                captures = [await _capture(http, _request(scope, str(i), _picture(size=(800, 600)))) for i in range(4)]
                async with PowerContextClient("http://localhost", http_client=http) as client:
                    first = await client.list_image_tasks(ExperimentalListImageTasksRequest(scope_id=scope, limit=2))
                    assert len(first.items) == 2 and first.next_cursor
                    second = await client.list_image_tasks(
                        ExperimentalListImageTasksRequest(scope_id=scope, cursor=first.next_cursor, limit=2)
                    )
                    ids = [item.task.task_id for item in first.items + second.items]
                    assert len(set(ids)) == 4 and set(ids) == {item["task_id"] for item in captures}
                    assert all(item.created_at is not None for item in first.items + second.items)
                    assert (
                        await client.list_image_tasks(ExperimentalListImageTasksRequest(scope_id=other))
                    ).items == []
                invalid = await http.post(
                    "/v1/experimental/images/tasks/list", json={"scope_id": other, "cursor": first.next_cursor}
                )
                assert invalid.status_code == 422
                page = await http.get("/dashboard/images", params={"scope": scope, "lang": "en"})
                assert page.status_code == 200 and "View original and analysis" in page.text
                assert page.text.count('class="card h-100"') == 4
                asset = captures[0]["asset"]["asset_id"]
                thumb = await http.get("/dashboard/image-thumbnails/" + asset, params={"scope": scope})
                assert thumb.status_code == 200 and thumb.headers["cache-control"] == "no-store"
                with Image.open(BytesIO(thumb.content)) as image:
                    assert image.width <= 480 and image.height <= 320
                assert (
                    await http.get("/dashboard/image-thumbnails/" + asset, params={"scope": other})
                ).status_code == 404
            async with _running(tmp_path, None, enabled=False, dashboard=True) as (_, http):
                page = await http.get("/dashboard/images", params={"scope": scope, "lang": "en"})
                assert page.status_code == 200 and "retained materials remain readable" in page.text
                assert page.text.count('class="card h-100"') == 4

    asyncio.run(scenario())


def test_pre_directory_database_upgrade_preserves_sources_and_legacy_date(tmp_path):
    async def scenario():
        async with _running(tmp_path, None) as (_, http):
            # The enabled-but-unconfigured deployment accepts no new material.
            scope = await _scope(http)
        # Build a retained record with a controlled provider, then remove only the additive display column.
        backend = Backend({})
        async with _listen(backend.app) as endpoint, _running(tmp_path, endpoint + "/v1") as (_, http):
            accepted = await _capture(http, _request(scope, "legacy", _picture()))
            await _terminal(http, scope, accepted["task_id"])
        with sqlite3.connect(tmp_path / "images.db") as db:
            before = db.execute(
                "select scope_id, source_type, source_id, payload, journal_position from pc_sources order by journal_position"
            ).fetchall()
            db.execute("drop index pc_experimental_image_task_list")
            db.execute("alter table pc_experimental_image_tasks drop column created_at")
        async with _running(tmp_path, None, enabled=False) as (_, http):
            result = await http.post("/v1/experimental/images/tasks/list", json={"scope_id": scope})
            assert result.status_code == 200, result.text
            assert result.json()["items"][0]["created_at"] is None
        with sqlite3.connect(tmp_path / "images.db") as db:
            after = db.execute(
                "select scope_id, source_type, source_id, payload, journal_position from pc_sources order by journal_position"
            ).fetchall()
            assert after == before and db.execute("pragma integrity_check").fetchone()[0] == "ok"

    asyncio.run(scenario())
