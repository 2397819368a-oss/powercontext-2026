# Copyright (c) 2026 OceanBase.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at http://www.apache.org/licenses/LICENSE-2.0
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Actual child-process termination/restart, plus explicitly simulated stale replies."""

import asyncio
import hashlib
import socket
import subprocess
import sys
from datetime import UTC, datetime, timedelta

import httpx
import time_machine

from powercontext.builtin.images.tasks import ImageTaskQueue
from powercontext.builtin.images.vision import VisionResult, parse_vision_output
from powercontext.builtin.inference import InferenceUsage
from tests.e2e.test_image_processing import VISIBLE, Backend, _listen, _running, _terminal
from tests.e2e.test_image_source_product import ASSET_GET, TASK_GET, _capture, _picture, _request, _scope

_CHILD = """import sys
from pathlib import Path
import uvicorn
from powercontext.builtin.images.config import ImageConfig
from powercontext.builtin.persistence.sqlite import SQLiteConfig
from powercontext.builtin.runtime import InferenceConfig, RuntimeConfig
from powercontext.server.factory import create_server_app
from powercontext.server.settings import AccessControlConfig, McpConfig, MetricsConfig, ServerSettings
root, endpoint, port = Path(sys.argv[1]), sys.argv[2], int(sys.argv[3])
settings=ServerSettings(database=SQLiteConfig(url=f"sqlite+aiosqlite:///{root/'images.db'}"),
    images=ImageConfig(enabled=True,asset_directory=root/'assets'),
    inference=InferenceConfig(vision_model="test-vision",vision_base_url=endpoint,
        vision_deployment_id="test-deployment",vision_max_attempts=2,vision_timeout_seconds=1),
    runtime=RuntimeConfig(artifact_processing_families=()),access=AccessControlConfig(mode="disabled"),
    mcp=McpConfig(enabled=False),metrics=MetricsConfig(enabled=False))
uvicorn.run(create_server_app(settings=settings,scheduler_path=root/'scheduler.db'),
    host="127.0.0.1",port=port,log_level="error",access_log=False)
"""


def test_actual_process_kill_recovers_running_attempt_after_real_lease_expiry(tmp_path):
    async def scenario():
        backend = Backend(paused=True)
        script = tmp_path / "child-image-server.py"
        script.write_text(_CHILD, encoding="utf-8")
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        async with _listen(backend.app) as endpoint:
            with (tmp_path / "child-stderr.log").open("wb") as error_log:
                child = await asyncio.create_subprocess_exec(
                    sys.executable,
                    str(script),
                    str(tmp_path),
                    endpoint + "/v1",
                    str(port),
                    stdout=subprocess.DEVNULL,
                    stderr=error_log,
                    creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
                )
                try:
                    async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", timeout=2) as http:
                        async with asyncio.timeout(20):
                            while True:
                                if child.returncode is not None:
                                    raise AssertionError("Child server exited before readiness")  # noqa: TRY003
                                try:
                                    await http.get("/health")
                                    break
                                except httpx.RequestError:
                                    await asyncio.sleep(0.05)
                        scope = await _scope(http)
                        accepted = await _capture(http, _request(scope, "abrupt-restart", _picture()))
                        await asyncio.wait_for(backend.entered.wait(), 5)
                        # Only the process launched by this test is killed. No
                        # database edits or fake clocks establish this recovery.
                        child.kill()
                        await asyncio.wait_for(child.wait(), 5)
                        backend.release.set()
                    async with _running(tmp_path, endpoint + "/v1", attempts=2, vision_timeout=1) as (_, http):
                        initial = await http.post(TASK_GET, json={"scope_id": scope, "task_id": accepted["task_id"]})
                        assert initial.json()["state"] == "running"
                        assert initial.json()["attempt_count"] == 1
                        assert initial.json()["attempts"][0]["provider_requests"] is None
                        asset = await http.post(
                            ASSET_GET, json={"scope_id": scope, "asset_id": accepted["asset"]["asset_id"]}
                        )
                        assert asset.status_code == 200
                        recovered = await _terminal(http, scope, accepted["task_id"], deadline=45)
                        assert recovered["state"] == "succeeded" and recovered["attempt_count"] == 2
                        assert [attempt["state"] for attempt in recovered["attempts"]] == ["interrupted", "succeeded"]
                        assert recovered["original"] == accepted["original"]
                        assert len(backend.requests) == 2
                finally:
                    backend.release.set()
                    if child.returncode is None:
                        child.kill()
                        await asyncio.wait_for(child.wait(), 5)

    asyncio.run(scenario())


def test_stale_reply_cannot_commit_or_release_the_new_attempt_budget(tmp_path):
    async def scenario():
        import json

        async with _running(tmp_path, "http://127.0.0.1:1/v1", attempts=2, vision_timeout=1) as (app, http):
            service = app.state.application.images
            await service.aclose()
            scope = await _scope(http)
            accepted = await _capture(http, _request(scope, "fencing", _picture()))
            first, second = ImageTaskQueue(service), ImageTaskQueue(service)
            assert await first.acquire_owner()
            stale = await first.claim()
            assert stale is not None
            digest = "sha256:" + hashlib.sha256(_picture()).hexdigest()
            assert await first.record_request(stale, digest)
            result = VisionResult(parse_vision_output(json.dumps(VISIBLE)), InferenceUsage(requests=1), 1, 200)
            # Fault injection isolates lease/fencing semantics. This simulated
            # reply is not evidence of any real provider request.
            with time_machine.travel(datetime.now(UTC) + timedelta(seconds=40), tick=False) as clock:
                assert await second.acquire_owner()
                assert await second.claim() is None
                assert not await first.finish(stale, result=result, input_digest=digest)
                clock.shift(3)
                current = await second.claim()
                assert current is not None and current.task.attempt_count == 2
                assert await second.record_request(current, digest)
                assert not await second.record_request(current, digest)
                assert not await first.finish(stale, result=result, input_digest=digest)
                assert await second.finish(current, result=result, input_digest=digest)
                assert not await second.finish(current, result=result, input_digest=digest)
                await second.release_owner()
            final = (await http.post(TASK_GET, json={"scope_id": scope, "task_id": accepted["task_id"]})).json()
            assert final["state"] == "succeeded" and final["attempt_count"] == 2
            assert final["revision"] > current.task.revision > stale.task.revision
            assert [receipt["state"] for receipt in final["attempts"]] == ["interrupted", "succeeded"]
            assert final["attempts"][0]["provider_requests"] is None
            # Exactly one terminal decrement leaves capacity usable.
            assert (await _capture(http, _request(scope, "after-fence", _picture())))["status"] == "accepted"

    asyncio.run(scenario())
