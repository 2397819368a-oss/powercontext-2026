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

"""Protect missing usage accounting and independently graded contract answers."""

# ruff: noqa: S101 - pytest assertions protect externally reported evaluation metrics.

import httpx
import pytest
from pydantic import AnyHttpUrl

from evaluation.token_optimization.live import grade, read_answer, usage_metrics
from powercontext.builtin.runtime import InferenceConfig
from powercontext.server.settings import ServerSettings


def test_missing_provider_usage_is_unknown_and_cache_is_not_added_twice():
    absent = usage_metrics(None)
    assert absent["input_tokens"] is None
    assert absent["output_tokens"] is None
    assert absent["cached_input_tokens"] is None
    actual = usage_metrics({
        "prompt_tokens": 100,
        "completion_tokens": 20,
        "prompt_tokens_details": {"cached_tokens": 60},
    })
    assert actual["input_tokens"] == 100
    assert actual["uncached_input_tokens"] == 40
    assert actual["cached_input_tokens"] == 60


def test_boolean_usage_and_integer_answer_do_not_pass_for_boolean_contract():
    assert usage_metrics({"prompt_tokens": True})["input_tokens"] is None
    incorrect = grade('{"enabled": 0}', {"enabled": False})
    assert not incorrect["correct"]
    assert incorrect["serious_error"]
    missing = grade('{"enabled": null}', {"enabled": False})
    assert not missing["correct"]
    assert not missing["serious_error"]


@pytest.mark.parametrize("choices", [[{"message": None}], None, [{"message": {"content": {"bad": "shape"}}}]])
def test_malformed_http_200_answer_still_accounts_reported_tokens(monkeypatch, choices):
    monkeypatch.setenv("OPENAI_API_KEY", "unit-test-credential")
    settings = ServerSettings(
        inference=InferenceConfig(
            generation_model="openai:test", generation_base_url=AnyHttpUrl("https://provider.example.test/v1")
        )
    )
    raw_usage = {"prompt_tokens": 100, "completion_tokens": 20}
    transport = httpx.MockTransport(lambda _: httpx.Response(200, json={"choices": choices, "usage": raw_usage}))
    response = read_answer(settings, [{"role": "user", "content": "A test question"}], 64, transport=transport)
    assert response["status"] == "invalid-answer"
    assert response["raw_usage"] == raw_usage
    assert response["usage"]["input_tokens"] == 100
    assert response["usage"]["output_tokens"] == 20
    assert not grade(response["answer"] or "", {"answer": "ok"})["correct"]
