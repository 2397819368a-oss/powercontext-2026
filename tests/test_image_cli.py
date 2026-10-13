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

"""Image inspection commands print their result without requiring JSON mode."""

import json

import pytest
from typer.testing import CliRunner

import powercontext.client.cli as client_cli
from powercontext.cli.app import create_cli
from powercontext.http import ExperimentalImageStatus, ExperimentalImageTaskPage


@pytest.mark.parametrize("command", ["status", "list"])
def test_image_inspection_prints_result_in_default_mode(command, monkeypatch):
    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def get_image_status(self, request):
            return ExperimentalImageStatus.model_validate({
                "enabled": True,
                "available": True,
                "vision_configured": True,
                "vision_connection": "dedicated",
                "model_id": "test-model",
                "pending_count": 0,
                "worker_running": True,
                "error_code": None,
            })

        async def list_image_tasks(self, request):
            return ExperimentalImageTaskPage(items=[], next_cursor=None)

    monkeypatch.setattr(client_cli, "PowerContextClient", lambda *args, **kwargs: Client())
    result = CliRunner().invoke(create_cli([]), ["image", command, "--scope-id", "test-scope"])
    assert result.exit_code == 0, result.output
    value = json.loads(result.output)
    assert value["model_id"] == "test-model" if command == "status" else value["items"] == []
