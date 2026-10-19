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
from pathlib import Path

import pytest

from evaluation.memory.atomic_extraction import runner
from evaluation.memory.atomic_extraction.manifest import load_manifest

_SECRET_SENTINEL = "Bearer private-provider-secret"  # noqa: S105 - synthetic leak sentinel


def test_provider_bootstrap_failure_writes_incomplete_content_free_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_file = tmp_path / "provider.env"
    env_file.write_text(
        "\n".join((
            "POWERCONTEXT_SERVER_INFERENCE_GENERATION_MODEL=openai:test-model",
            f'POWERCONTEXT_SERVER_INFERENCE_GENERATION_HEADERS=\'{{"Authorization":"{_SECRET_SENTINEL}"}}\'',
            "POWERCONTEXT_SERVER_INFERENCE_GENERATION_MODEL_SETTINGS='{\"temperature\":0}'",
        ))
        + "\n",
        encoding="utf-8",
    )

    async def unavailable(*_args, **_kwargs):
        raise RuntimeError("private provider response")  # noqa: TRY003

    monkeypatch.setattr(runner, "_open_pydantic_ai_model", unavailable)
    output = tmp_path / "run"

    report = asyncio.run(
        runner.run_live_evaluation(
            loaded=load_manifest(),
            env_file=env_file,
            output_directory=output,
            run_id="provider-failure-fixture",
        )
    )

    assert report["status"] == "incomplete"
    assert report["factual_retention"]["false_rejections"] == 0
    assert report["instruction_leakage"]["unobserved"] == report["overall"]["total"]
    manifest = json.loads((output / "run-manifest.json").read_text(encoding="utf-8"))
    assert manifest["provider"]["header_names"] == ["Authorization"]
    assert manifest["provider"]["model_settings"] == {"temperature": 0}
    artifact_text = "\n".join(path.read_text(encoding="utf-8") for path in output.iterdir())
    assert _SECRET_SENTINEL not in artifact_text
    assert "private provider response" not in artifact_text


def test_safe_settings_preserve_token_budgets_and_redact_credentials() -> None:
    settings = {
        "max_tokens": 1024,
        "api_key": "private-api-key",
        "apiKey": "private-camel-api-key",
        "nested": {"access_token": "private-access-token", "tokenizer": "provider-default"},
    }

    safe = runner._safe_settings(settings)
    safe_nested = safe["nested"]
    original_nested = settings["nested"]

    assert safe["max_tokens"] == 1024
    assert isinstance(safe_nested, dict)
    assert isinstance(original_nested, dict)
    assert safe_nested["tokenizer"] == "provider-default"
    assert safe["api_key"] != settings["api_key"]
    assert safe["apiKey"] != settings["apiKey"]
    assert safe_nested["access_token"] != original_nested["access_token"]
    assert "private" not in json.dumps(safe)
