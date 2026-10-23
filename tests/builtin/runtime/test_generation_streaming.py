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
import logging
from contextlib import AsyncExitStack

import pytest

from powercontext.builtin.inference.pydantic_ai import StreamingAggregateModel
from powercontext.builtin.runtime.composition import _open_pydantic_ai_model

_EVENT = "inference.generation.streaming_not_applied"


def _warnings(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [record for record in caplog.records if getattr(record, "event", None) == _EVENT]


def test_auto_streaming_warns_when_the_provider_prefix_is_not_supported(caplog) -> None:
    async def scenario() -> object:
        async with AsyncExitStack() as resources:
            _provider, model = await _open_pydantic_ai_model(
                "test",
                base_url=None,
                headers={},
                resources=resources,
                instrumentation=None,
                streaming="auto",
            )
            return model

    with caplog.at_level(logging.WARNING, logger="powercontext.builtin.runtime.composition"):
        model = asyncio.run(scenario())

    assert not isinstance(model, StreamingAggregateModel)
    warnings = _warnings(caplog)
    assert len(warnings) == 1
    assert warnings[0].operation == "generate"
    assert warnings[0].unit == "inference"


def test_disabled_streaming_does_not_warn_for_an_unsupported_prefix(caplog) -> None:
    async def scenario() -> None:
        async with AsyncExitStack() as resources:
            await _open_pydantic_ai_model(
                "test",
                base_url=None,
                headers={},
                resources=resources,
                instrumentation=None,
                streaming="disabled",
            )

    with caplog.at_level(logging.WARNING, logger="powercontext.builtin.runtime.composition"):
        asyncio.run(scenario())

    assert _warnings(caplog) == []
