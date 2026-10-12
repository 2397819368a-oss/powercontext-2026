# Copyright (c) 2026 OceanBase.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at http://www.apache.org/licenses/LICENSE-2.0
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""SQLite/HTTP/worker acceptance with an actual local deterministic provider."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import socket
import sqlite3
from contextlib import asynccontextmanager
from io import BytesIO
from pathlib import Path

import httpx
import pytest
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from PIL import Image
from pydantic import SecretStr
from sqlalchemy.exc import OperationalError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection

from powercontext.builtin.images.config import ImageConfig
from powercontext.builtin.images.tasks import ImageTaskQueue
from powercontext.builtin.persistence.sqlite import SQLiteConfig
from powercontext.builtin.runtime import InferenceConfig, RuntimeConfig
from powercontext.client import PowerContextClient
from powercontext.http import ExperimentalGetImageTaskRequest
from powercontext.server.factory import create_server_app
from powercontext.server.settings import AccessControlConfig, DashboardConfig, McpConfig, MetricsConfig, ServerSettings
from tests.e2e.test_image_source_product import ASSET_GET, CAPTURE, TASK_GET, _capture, _picture, _request, _scope

RETRY = "/v1/experimental/images/task/retry"
REPARSE = "/v1/experimental/images/reparse"
VISIBLE = {
    "readability": "readable",
    "ocr_text": "项目 IMG-034 错误 E429",
    "visual_description": "A visible connection error; cause unknown",
    "entities": [],
    "relations": [],
    "uncertainties": [],
}


@asynccontextmanager
async def _listen(app, *, lifespan="auto"):
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    url = f"http://127.0.0.1:{listener.getsockname()[1]}"
    server = uvicorn.Server(
        uvicorn.Config(app, log_level="error", access_log=False, timeout_graceful_shutdown=1, lifespan=lifespan)
    )
    task = asyncio.create_task(server.serve(sockets=[listener]))
    try:
        for _ in range(1000):
            if server.started or task.done():
                break
            await asyncio.sleep(0.01)
        assert server.started
        yield url
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, 10)
        listener.close()


class Backend:
    """Respond over TCP and observe the externally promised request budget."""

    def __init__(self, *script, paused=False, expected_size=(24, 16)):
        self.script = list(script)
        self.requests = []
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        if not paused:
            self.release.set()
        self.active = 0
        self.max_active = 0
        self.expected_size = expected_size
        self.app = FastAPI()
        self.app.post("/v1/chat/completions")(self.respond)

    async def respond(self, request: Request):
        payload = await request.json()
        content = [
            part
            for message in payload["messages"]
            for part in message["content"]
            if isinstance(message["content"], list)
        ]
        url = next(part["image_url"]["url"] for part in content if part["type"] == "image_url")
        data = base64.b64decode(url.split(",", 1)[1], validate=True)
        with Image.open(BytesIO(data)) as image:
            if self.expected_size is not None:
                assert image.size == self.expected_size
        self.requests.append({
            "model": payload["model"],
            "image": data,
            "max_tokens": payload["max_tokens"],
            "authorization": request.headers.get("Authorization"),
        })
        index = len(self.requests) - 1
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        self.entered.set()
        try:
            await self.release.wait()
            selected = self.script[index] if index < len(self.script) else {}
            if selected.get("delay"):
                await asyncio.sleep(selected["delay"])
            if selected.get("status", 200) != 200:
                return JSONResponse(
                    {"error": "private-provider-error"},
                    status_code=selected["status"],
                    headers={"Retry-After": str(selected.get("retry_after", 2))},
                )
            response = {
                "choices": [
                    {
                        "message": {"content": selected.get("content", json.dumps(VISIBLE, ensure_ascii=False))},
                        "finish_reason": selected.get("finish_reason", "stop"),
                    }
                ]
            }
            if "usage" in selected:
                response["usage"] = selected["usage"]
            return JSONResponse(response)
        finally:
            self.active -= 1


@asynccontextmanager
async def _running(
    root: Path,
    endpoint: str | None,
    *,
    enabled=True,
    model="test-vision",
    deployment="test-deployment",
    attempts=3,
    vision_timeout=5,
    headers=None,
    dashboard=False,
    inference=None,
    image_config=None,
    **injections,
):
    settings = ServerSettings(
        database=SQLiteConfig(url=f"sqlite+aiosqlite:///{root / 'images.db'}"),
        images=image_config or ImageConfig(enabled=enabled, asset_directory=root / "assets"),
        inference=inference
        or InferenceConfig(
            vision_model=model if endpoint else None,
            vision_base_url=endpoint,
            vision_deployment_id=deployment if endpoint else None,
            vision_max_attempts=attempts,
            vision_timeout_seconds=vision_timeout,
            vision_headers=headers or {},
        ),
        runtime=RuntimeConfig(artifact_processing_families=()),
        access=AccessControlConfig(mode="disabled"),
        mcp=McpConfig(enabled=False),
        metrics=MetricsConfig(enabled=False),
        dashboard=DashboardConfig(enabled=dashboard),
    )
    app = create_server_app(settings=settings, scheduler_path=root / "scheduler.db", **injections)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost") as http,
    ):
        yield app, http


async def _terminal(http, scope, task_id, *, deadline=10):
    async with asyncio.timeout(deadline):
        while True:
            response = await http.post(TASK_GET, json={"scope_id": scope, "task_id": task_id})
            assert response.status_code == 200, response.text
            task = response.json()
            if task["state"] in {"succeeded", "failed"}:
                return task
            await asyncio.sleep(0.02)


def test_multimodal_worker_history_usage_and_citation_readback_survive_restart(tmp_path):
    async def scenario():
        backend = Backend({"usage": {"prompt_tokens": 785, "completion_tokens": 93}})
        async with _listen(backend.app) as endpoint:
            async with _running(tmp_path, endpoint + "/v1") as (_, http):
                scope = await _scope(http)
                accepted = await _capture(http, _request(scope, "success", _picture()))
                task = await _terminal(http, scope, accepted["task_id"])
                assert task["state"] == "succeeded" and task["attempt_count"] == 1
                assert task["analysis_payload"]["output"] == VISIBLE
                assert (
                    task["analysis_payload"]["input_digest"]
                    == "sha256:" + hashlib.sha256(backend.requests[0]["image"]).hexdigest()
                )
                attempt = task["attempts"][0]
                assert (
                    attempt["provider_requests"] == 1
                    and attempt["input_tokens"] == 785
                    and attempt["output_tokens"] == 93
                )
                assert attempt["latency_ms"] >= 0 and attempt["http_status"] == 200
                assert len(backend.requests) == 1
            async with _running(tmp_path, None, enabled=False) as (_, http):
                async with PowerContextClient("http://localhost", http_client=http) as client:
                    historical = await client.get_image_task(
                        ExperimentalGetImageTaskRequest(
                            scope_id=scope, analysis_source_id=task["analysis"]["source"]["source_id"]
                        )
                    )
                    assert historical.model_dump(mode="json") == task
                original = await http.post(
                    ASSET_GET, json={"scope_id": scope, "asset_id": accepted["asset"]["asset_id"]}
                )
                assert base64.b64decode(original.json()["bytes_base64"]) == _picture()
                assert (await http.post(TASK_GET, json={"scope_id": scope})).status_code == 422
                assert (
                    await http.post(
                        TASK_GET,
                        json={
                            "scope_id": scope,
                            "task_id": task["task_id"],
                            "analysis_source_id": task["analysis"]["source"]["source_id"],
                        },
                    )
                ).status_code == 422

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "response,code",
    [
        ({"status": 401}, "image_provider_http_401"),
        ({"content": "not JSON"}, "image_output_invalid"),
        ({"finish_reason": "length"}, "image_output_truncated"),
        ({"delay": 2}, "image_provider_timeout"),
    ],
)
def test_failed_attempt_retains_original_without_placeholder_or_hidden_retries(tmp_path, response, code, caplog):
    async def scenario():
        backend = Backend(response)
        async with (
            _listen(backend.app) as endpoint,
            _running(tmp_path, endpoint + "/v1", attempts=1, vision_timeout=1) as (_, http),
        ):
            scope = await _scope(http)
            accepted = await _capture(http, _request(scope, "failure", _picture()))
            task = await _terminal(http, scope, accepted["task_id"])
            assert task["state"] == "failed" and task["error_code"] == code and task["analysis"] is None
            assert task["attempt_count"] == 1 and len(backend.requests) == 1
            assert task["attempts"][0]["input_tokens"] is None
            assert (
                await http.post(ASSET_GET, json={"scope_id": scope, "asset_id": accepted["asset"]["asset_id"]})
            ).status_code == 200
            assert "private-provider-error" not in caplog.text

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "headers",
    [
        {"Authorization": SecretStr("Bearer 测试占位")},
        {"Invalid Header": SecretStr("test-only")},
        {"Authorization": SecretStr("Bearer test\x00only")},
    ],
    ids=["non-ascii-value", "invalid-name", "control-value"],
)
def test_invalid_vision_headers_fail_each_task_without_stopping_the_queue(tmp_path, headers):
    async def scenario():
        backend = Backend()
        async with _listen(backend.app) as endpoint, _running(tmp_path, endpoint + "/v1", headers=headers) as (_, http):
            scope = await _scope(http)
            for key in ("first-bad-config", "second-bad-config"):
                accepted = await _capture(http, _request(scope, key, _picture()))
                failed = await _terminal(http, scope, accepted["task_id"], deadline=5)
                assert failed["state"] == "failed" and failed["error_code"] == "image_vision_credentials_unavailable"
                assert failed["attempt_count"] == 1 and failed["attempts"][0]["provider_requests"] == 0
                assert (
                    await http.post(ASSET_GET, json={"scope_id": scope, "asset_id": accepted["asset"]["asset_id"]})
                ).status_code == 200
            assert not backend.requests

    asyncio.run(scenario())


@pytest.mark.parametrize("boundary", ["acquire", "renew"])
def test_transient_metadata_lock_recovers_without_server_restart(tmp_path, monkeypatch, boundary):
    async def scenario():
        failed = asyncio.Event()
        original_acquire = ImageTaskQueue.acquire_owner
        original_renew = ImageTaskQueue.renew_owner

        def lock_error():
            driver_error = sqlite3.OperationalError("database is locked")
            driver_error.sqlite_errorcode = sqlite3.SQLITE_BUSY
            return OperationalError("image ownership", {}, driver_error)

        async def acquire(queue):
            if not failed.is_set():
                failed.set()
                raise lock_error()
            return await original_acquire(queue)

        async def renew(queue, claim):
            if not failed.is_set():
                failed.set()
                raise lock_error()
            return await original_renew(queue, claim)

        monkeypatch.setattr(
            ImageTaskQueue,
            "acquire_owner" if boundary == "acquire" else "renew_owner",
            acquire if boundary == "acquire" else renew,
        )
        backend = Backend(paused=boundary == "renew")
        async with (
            _listen(backend.app) as endpoint,
            _running(tmp_path, endpoint + "/v1", vision_timeout=20) as (_, http),
        ):
            try:
                scope = await _scope(http)
                accepted = [await _capture(http, _request(scope, "lock-first", _picture()))]
                if boundary == "renew":
                    await asyncio.wait_for(backend.entered.wait(), 5)
                accepted.append(await _capture(http, _request(scope, "lock-second", _picture())))
                await asyncio.wait_for(failed.wait(), 15)
                backend.release.set()
                for item in accepted:
                    task = await _terminal(http, scope, item["task_id"])
                    assert task["state"] == "succeeded" and task["attempt_count"] == 1
                assert len(backend.requests) == 2
            finally:
                backend.release.set()

    asyncio.run(scenario())


def test_automatic_budget_manual_retry_and_reparse_preserve_old_records(tmp_path):
    async def scenario():
        backend = Backend({"status": 503}, {"status": 503})
        async with _listen(backend.app) as endpoint:
            async with _running(tmp_path, endpoint + "/v1", attempts=2) as (_, http):
                scope = await _scope(http)
                accepted = await _capture(http, _request(scope, "retry", _picture()))
                failed = await _terminal(http, scope, accepted["task_id"])
                assert failed["state"] == "failed" and failed["attempt_count"] == 2 and len(backend.requests) == 2
                request = {"scope_id": scope, "task_id": failed["task_id"], "retry_key": "manual-one"}
                retry = await http.post(RETRY, json=request)
                assert retry.status_code == 202, retry.text
                new = await _terminal(http, scope, retry.json()["task_id"])
                assert new["state"] == "succeeded" and new["task_id"] != failed["task_id"]
                assert new["profile_digest"] == failed["profile_digest"] and new["original"] == failed["original"]
                assert (
                    await http.post(TASK_GET, json={"scope_id": scope, "task_id": failed["task_id"]})
                ).json() == failed
                assert (await http.post(RETRY, json=request)).json()["task_id"] == new["task_id"]
                assert (await http.post(RETRY, json=request | {"task_id": new["task_id"]})).status_code == 409
                assert (
                    await http.post(
                        RETRY, json=request | {"task_id": new["task_id"], "retry_key": "cannot-retry-success"}
                    )
                ).status_code == 409
                original = new["original"]["source"]
                reparse = await http.post(
                    REPARSE,
                    json={
                        "scope_id": scope,
                        "original": original,
                        "reparse_key": "parse-one",
                        "expected_profile_digest": new["profile_digest"],
                    },
                )
                assert reparse.status_code == 202, reparse.text
                parsed = await _terminal(http, scope, reparse.json()["task_id"])
                assert parsed["state"] == "succeeded" and parsed["analysis"] != new["analysis"]
                assert (
                    await http.post(
                        TASK_GET, json={"scope_id": scope, "analysis_source_id": new["analysis"]["source"]["source_id"]}
                    )
                ).json() == new
            async with _running(tmp_path, endpoint + "/v1", model="changed-model", attempts=2) as (_, http):
                replay = await http.post(
                    REPARSE,
                    json={
                        "scope_id": scope,
                        "original": original,
                        "reparse_key": "parse-one",
                        "expected_profile_digest": new["profile_digest"],
                    },
                )
                assert replay.status_code == 202 and replay.json()["task_id"] == parsed["task_id"]
                mismatch = await http.post(
                    REPARSE,
                    json={
                        "scope_id": scope,
                        "original": original,
                        "reparse_key": "changed-with-old-profile",
                        "expected_profile_digest": new["profile_digest"],
                    },
                )
                assert mismatch.status_code == 409
                retry = await http.post(RETRY, json=request | {"retry_key": "old-profile"})
                old = await _terminal(http, scope, retry.json()["task_id"])
                assert old["state"] == "failed" and old["error_code"] == "image_vision_configuration_unavailable"
                assert old["attempts"][0]["provider_requests"] == 0
                reparse = await http.post(
                    REPARSE, json={"scope_id": scope, "original": original, "reparse_key": "current-profile"}
                )
                changed = await _terminal(http, scope, reparse.json()["task_id"])
                assert changed["state"] == "succeeded" and changed["profile_digest"] != new["profile_digest"]
                assert backend.requests[-1]["model"] == "changed-model"

    asyncio.run(scenario())


def test_two_runtime_workers_share_one_lease_and_keep_network_outside_writer_transaction(tmp_path):
    async def scenario():
        backend = Backend(paused=True)
        async with (
            _listen(backend.app) as endpoint,
            _running(tmp_path, endpoint + "/v1") as (_, first),
            _running(tmp_path, endpoint + "/v1") as (_, second),
        ):
            scope = await _scope(first)
            one = await _capture(first, _request(scope, "one", _picture()))
            await asyncio.wait_for(backend.entered.wait(), 5)
            # A second writer accepts work while the first provider is blocked.
            two = await asyncio.wait_for(_capture(second, _request(scope, "two", _picture())), 2)
            backend.release.set()
            assert (await _terminal(first, scope, one["task_id"]))["state"] == "succeeded"
            assert (await _terminal(second, scope, two["task_id"]))["state"] == "succeeded"
            assert len(backend.requests) == 2 and backend.max_active == 1

    asyncio.run(scenario())


def test_graceful_close_finishes_current_task_and_disabled_restart_leaves_queue_readable(tmp_path):
    async def scenario():
        backend = Backend(paused=True)
        async with _listen(backend.app) as endpoint:
            async with _running(tmp_path, endpoint + "/v1") as (app, http):
                scope = await _scope(http)
                one = await _capture(http, _request(scope, "active", _picture()))
                await asyncio.wait_for(backend.entered.wait(), 5)
                two = await _capture(http, _request(scope, "queued", _picture()))
                closing = asyncio.create_task(app.state.application.images.aclose())
                await asyncio.sleep(0)
                backend.release.set()
                await asyncio.wait_for(closing, 10)
                assert (await _terminal(http, scope, one["task_id"]))["state"] == "succeeded"
                pending = (await http.post(TASK_GET, json={"scope_id": scope, "task_id": two["task_id"]})).json()
                assert pending["state"] == "pending" and pending["attempt_count"] == 0
            async with _running(tmp_path, None, enabled=False) as (_, http):
                assert (
                    await http.post(TASK_GET, json={"scope_id": scope, "task_id": two["task_id"]})
                ).json() == pending
                assert (await http.post(CAPTURE, json=_request(scope, "disabled", _picture()))).status_code == 503
                assert len(backend.requests) == 1

    asyncio.run(scenario())


def test_new_endpoint_credentials_are_never_sent_to_old_tasks(tmp_path):
    async def scenario():
        old_backend, new_backend = Backend(), Backend()
        async with _listen(old_backend.app) as old_url, _listen(new_backend.app) as new_url:
            async with _running(tmp_path, old_url + "/v1") as (app, http):
                await app.state.application.images.aclose()
                scope = await _scope(http)
                accepted = await _capture(http, _request(scope, "pinned", _picture()))
            async with _running(
                tmp_path, new_url + "/v1", headers={"Authorization": SecretStr("Bearer test-only-new-secret")}
            ) as (_, http):
                task = await _terminal(http, scope, accepted["task_id"])
                assert task["error_code"] == "image_vision_configuration_unavailable"
                assert not old_backend.requests and not new_backend.requests
                new = await http.post(
                    REPARSE,
                    json={"scope_id": scope, "original": task["original"]["source"], "reparse_key": "new-connection"},
                )
                assert (await _terminal(http, scope, new.json()["task_id"]))["state"] == "succeeded"
                assert new_backend.requests[0]["authorization"] == "Bearer test-only-new-secret"
                assert "test-only-new-secret" not in json.dumps(task)

    asyncio.run(scenario())


def test_known_phase_two_metadata_is_migrated_without_changing_receipts(tmp_path):
    async def scenario():
        backend = Backend()
        async with _listen(backend.app) as endpoint:
            async with _running(tmp_path, endpoint + "/v1") as (app, http):
                await app.state.application.images.aclose()
                scope = await _scope(http)
                accepted = await _capture(http, _request(scope, "legacy", _picture()))
            # Recreate the known phase-two layout; this is a compatibility fixture,
            # not a claim that a disk failure was reproduced.
            _legacy_layout(tmp_path / "images.db")
            async with _running(tmp_path, endpoint + "/v1") as (_, http):
                task = await _terminal(http, scope, accepted["task_id"])
                assert task["state"] == "succeeded" and task["original"] == accepted["original"]
                assert await _capture(http, _request(scope, "legacy", _picture())) == accepted | {"replayed": True}

    asyncio.run(scenario())


def _legacy_layout(path):
    with sqlite3.connect(path) as database:
        for name in (
            "pc_experimental_image_task_ready",
            "pc_experimental_image_task_leases",
            "pc_experimental_image_analysis_lookup",
        ):
            database.execute(f'DROP INDEX "{name}"')
        for table, columns in {
            "pc_experimental_image_state": ("worker_owner", "worker_lease_until", "last_scope"),
            "pc_experimental_image_tasks": (
                "original_source_id",
                "analysis_source_id",
                "claim_token",
                "lease_until",
                "available_at",
            ),
        }.items():
            for column in columns:
                database.execute(f'ALTER TABLE "{table}" DROP COLUMN "{column}"')
        for table in ("pc_experimental_image_attempts", "pc_experimental_image_operations"):
            database.execute(f'DROP TABLE "{table}"')


def test_interrupted_schema_upgrade_rolls_back_and_preserves_historical_readback(tmp_path, monkeypatch):
    async def scenario():
        backend = Backend()
        async with _listen(backend.app) as endpoint:
            async with _running(tmp_path, endpoint + "/v1") as (app, http):
                await app.state.application.images.aclose()
                scope = await _scope(http)
                accepted = await _capture(http, _request(scope, "migration-rollback", _picture()))
            _legacy_layout(tmp_path / "images.db")
            original_exec = AsyncConnection.exec_driver_sql

            async def fail_upgrade(connection, statement, *args, **kwargs):
                if 'ADD COLUMN "worker_lease_until"' in statement:
                    raise SQLAlchemyError("forced migration failure")  # noqa: TRY003
                return await original_exec(connection, statement, *args, **kwargs)

            with monkeypatch.context() as patch:
                patch.setattr(AsyncConnection, "exec_driver_sql", fail_upgrade)
                async with _running(tmp_path, endpoint + "/v1") as (_, http):
                    failed = await http.post(TASK_GET, json={"scope_id": scope, "task_id": accepted["task_id"]})
                    assert failed.status_code == 503
                    assert (await http.get("/health/live")).status_code == 200
                    assert await _scope(http, "text-runtime-after-image-migration-failure")
            async with _running(tmp_path, endpoint + "/v1") as (_, http):
                recovered = await _terminal(http, scope, accepted["task_id"])
                assert recovered["state"] == "succeeded"
                assert recovered["original"] == accepted["original"]
                assert recovered["original_payload"]["asset"] == accepted["asset"]

    asyncio.run(scenario())


def test_scope_rotation_prevents_one_scope_backlog_from_starving_another(tmp_path):
    async def scenario():
        backend = Backend(paused=True)
        async with _listen(backend.app) as endpoint, _running(tmp_path, endpoint + "/v1") as (_, http):
            first, second = await _scope(http), await _scope(http, "second")
            running = await _capture(http, _request(first, "first-running", _picture()))
            await asyncio.wait_for(backend.entered.wait(), 5)
            await _capture(http, _request(first, "backlog-one", _picture()))
            await _capture(http, _request(first, "backlog-two", _picture()))
            later = await _capture(http, _request(second, "fair-turn", _picture(color="blue")))
            backend.release.set()
            assert (await _terminal(http, first, running["task_id"]))["state"] == "succeeded"
            assert (await _terminal(http, second, later["task_id"]))["state"] == "succeeded"
            assert backend.requests[1]["image"] == _picture(color="blue")

    asyncio.run(scenario())
