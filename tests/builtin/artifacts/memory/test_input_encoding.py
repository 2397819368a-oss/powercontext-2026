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

"""Observe the actual typed model input and complete reconstructed evidence."""

from __future__ import annotations

import asyncio
import json
from contextlib import AsyncExitStack
from hashlib import sha256
from types import SimpleNamespace
from typing import Literal

import pytest
from pydantic_ai.messages import ModelResponse, TextPart, UserPromptPart
from pydantic_ai.models.function import FunctionModel

from powercontext.builtin.artifacts.atomic_memory.extraction import (
    AtomicMemoryEvidence,
    AtomicMemoryExtractionInput,
    AtomicMemoryExtractionOutput,
    AtomicMemoryGenerationPipeline,
    atomic_memory_extraction_instructions,
)
from powercontext.builtin.artifacts.memory.input_encoding import (
    MEMORY_INPUT_ENCODING_INSTRUCTIONS,
    MESSAGE_SNAPSHOT_FORMAT,
    MemoryInputOptimizer,
    SharedMemoryInput,
    compile_memory_input,
    expand_memory_input,
)
from powercontext.builtin.artifacts.memory.prompts import MemoryExtractionProfile
from powercontext.builtin.artifacts.topic_memory.generation import (
    TOPIC_MEMORY_PROBE_INSTRUCTIONS,
    BudgetedTopicMemoryGenerator,
    TopicMemoryEvidence,
    TopicMemoryProbeInput,
    TopicMemoryProbeOutput,
    topic_memory_stage_budget,
    topic_memory_stage_fixed_prompt,
)
from powercontext.builtin.inference import GenerationResult, character_token_estimator
from powercontext.builtin.inference.pydantic_ai import PydanticAIStructuredGenerator
from powercontext.builtin.runtime import InferenceConfig, RuntimeConfig
from powercontext.builtin.runtime.composition import _generation_pipelines
from powercontext.builtin.sources import BUILTIN_SOURCE_REGISTRY
from powercontext.sources import SourceRef


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _atomic(text: str, *, ordinal: int = 1, metadata=None) -> AtomicMemoryEvidence:
    metadata = {} if metadata is None else metadata
    return AtomicMemoryEvidence(
        evidence_id=f"source:{ordinal}",
        source_ref=SourceRef(source_type="content", source_id=f"s{ordinal}"),
        journal_position=ordinal,
        content={"source_id": f"s{ordinal}", "content": text, "metadata": metadata},
        source_metadata={
            "name": f"s{ordinal}",
            "materialization": "captured",
            "content": text,
            "metadata": metadata,
            "unknown_source_field": {"unit": "ms", "version": 2},
        },
    )


def _snapshot(messages, **extra) -> str:
    return _json({"format": MESSAGE_SNAPSHOT_FORMAT, "messages": messages, **extra})


def test_native_duplicate_fields_preserve_unknown_metadata_types_and_original_values() -> None:
    text = "Only read-only idempotent queries retry at most three times; funds never retry. " * 30
    metadata = {"valid_at": "2026-11-01", "unknown": [True, 1, 1.0, None, "b1", {"$ref": "b1"}]}
    original = AtomicMemoryExtractionInput(evidence=(_atomic(text, metadata=metadata),))
    before = original.model_dump_json()

    compact = compile_memory_input(original, "atomic")

    assert compact is not None
    assert expand_memory_input(compact) == original.model_dump(mode="json")
    assert original.model_dump_json() == before
    assert compact.model_dump_json().count(text) == 1
    recovered = AtomicMemoryExtractionInput.model_validate(expand_memory_input(compact))
    recovered_content = recovered.evidence[0].content
    assert isinstance(recovered_content, dict)
    recovered_metadata = recovered_content["metadata"]
    assert isinstance(recovered_metadata, dict)
    values = recovered_metadata["unknown"]
    assert isinstance(values, list)
    assert [type(value) for value in values[:4]] == [bool, int, float, type(None)]
    recovered_source = recovered.evidence[0].source_metadata
    assert isinstance(recovered_source, dict)
    assert recovered_source["unknown_source_field"] == {"unit": "ms", "version": 2}


@pytest.mark.parametrize("family", ["atomic", "topic"])
def test_growth_snapshots_keep_complete_occurrences_roles_and_each_source_time(
    family: Literal["atomic", "topic"],
) -> None:
    messages = [
        {"id": "m1", "role": "user", "content": "Read-only queries retry three times. " * 30, "time": "2026-10-01"},
        {"id": "m2", "role": "assistant", "content": "Funds must never automatically retry. " * 30},
        {"id": "m3", "role": "user", "content": "From next week use two retries; keep other rules. " * 30},
    ]
    snapshots = (
        _snapshot(messages[:1], valid_at="2026-10-01"),
        _snapshot(messages[:2], valid_at="2026-10-02"),
        _snapshot([*messages, messages[0]], valid_at="2026-10-03", unknown="b1"),
    )
    if family == "atomic":
        original = AtomicMemoryExtractionInput(
            evidence=tuple(_atomic(text, ordinal=i) for i, text in enumerate(snapshots, 1))
        )
    else:
        original = TopicMemoryProbeInput(
            evidence=tuple(
                TopicMemoryEvidence(
                    evidence_id=f"evidence-{index}",
                    source_type="content",
                    content=_json({"content": text, "metadata": {"recorded_at": f"2026-10-0{index}"}}),
                )
                for index, text in enumerate(snapshots, 1)
            )
        )

    compact = compile_memory_input(original, family)

    assert compact is not None
    recovered = type(original).model_validate(expand_memory_input(compact))
    assert recovered.model_dump_json() == original.model_dump_json()
    # The final snapshot still has four occurrences, including the repeated m1.
    final = recovered.evidence[-1].content
    if family == "atomic":
        assert isinstance(final, dict)
        final_text = final["content"]
    else:
        assert isinstance(final, str)
        final_text = json.loads(final)["content"]
    assert isinstance(final_text, str)
    final_snapshot = json.loads(final_text)
    assert final_snapshot["messages"] == [*messages, messages[0]]
    assert [item.evidence_id for item in recovered.evidence] == [item.evidence_id for item in original.evidence]
    assert compact.model_dump_json().count(messages[0]["content"]) == 1


def test_equal_text_does_not_merge_evidence_ids_or_different_conditions() -> None:
    text = "the same complete long body with literal blocks/b1 markers " * 50
    original = AtomicMemoryExtractionInput(
        evidence=(
            _atomic(text, ordinal=1, metadata={"event_at": "2026-10-02", "count": 1, "denied": False}),
            _atomic(text, ordinal=2, metadata={"event_at": "2026-10-01", "count": 2, "denied": True}),
        )
    )
    compact = compile_memory_input(original, "atomic")
    assert compact is not None
    assert expand_memory_input(compact) == original.model_dump(mode="json")


class _Recorder:
    def __init__(self, output) -> None:
        self.inputs = []
        self.output = output

    async def generate(self, value, /):
        self.inputs.append(value)
        return GenerationResult(output=self.output)


def _optimizer(mode: Literal["off", "shadow", "active"], *, limit: int = 100_000, counter=True):
    legacy = _Recorder(AtomicMemoryExtractionOutput())
    compact = _Recorder(AtomicMemoryExtractionOutput())
    optimizer = MemoryInputOptimizer(
        legacy,
        compact,
        family="atomic",
        stage="atomic_memory.extract",
        mode=mode,
        instructions=atomic_memory_extraction_instructions(MemoryExtractionProfile.CONVERSATION),
        input_type=AtomicMemoryExtractionInput,
        output_type=AtomicMemoryExtractionOutput,
        estimator=character_token_estimator(),
        counter=character_token_estimator() if counter else None,
        input_tokens_limit=limit,
    )
    return optimizer, legacy, compact


@pytest.mark.parametrize("mode", ["off", "shadow", "active"])
def test_mode_selects_one_typed_request_without_second_extraction(
    mode: Literal["off", "shadow", "active"], caplog
) -> None:
    original = AtomicMemoryExtractionInput(evidence=(_atomic("完整条件与有效时间必须保留。" * 500),))
    optimizer, legacy, compact = _optimizer(mode)
    with caplog.at_level("INFO"):
        asyncio.run(optimizer.generate(original))
    sent = (compact if mode == "active" else legacy).inputs
    assert len(sent) == 1
    assert not (legacy if mode == "active" else compact).inputs
    if mode == "active":
        assert expand_memory_input(sent[0]) == original.model_dump(mode="json")
    else:
        assert sent[0] is original
    record = next(item for item in caplog.records if getattr(item, "event", None) == "memory.input_optimization")
    assert record.memory_input_selected_digest == sha256(sent[0].model_dump_json().encode()).hexdigest()
    original_content = original.evidence[0].content
    assert isinstance(original_content, dict)
    original_text = original_content["content"]
    assert isinstance(original_text, str)
    assert original_text not in record.getMessage()


@pytest.mark.parametrize("condition", ["short", "unavailable", "budget", "deep", "huge"])
def test_complete_cost_or_complexity_failures_send_original(condition: str) -> None:
    text = "ok" if condition == "short" else "preserve the full condition " * 500
    metadata = {}
    if condition == "deep":
        nested = "keep"
        for _ in range(30):
            nested = {"unknown": nested}
        metadata["nested"] = nested
    if condition == "huge":
        text = "x" * (2 * 1024 * 1024)
    original = AtomicMemoryExtractionInput(evidence=(_atomic(text, metadata=metadata),))
    optimizer, legacy, compact = _optimizer(
        "active", limit=50 if condition == "budget" else 100_000, counter=condition != "unavailable"
    )

    asyncio.run(optimizer.generate(original))

    assert legacy.inputs == [original]
    assert not compact.inputs


def test_private_model_binding_serializes_the_same_selected_typed_view() -> None:
    actual_prompts = []
    actual_instructions = []

    async def model(messages, info):
        actual_prompts.extend(
            part.content for message in messages for part in message.parts if isinstance(part, UserPromptPart)
        )
        actual_instructions.append(info.instructions)
        return ModelResponse(parts=[TextPart('{"candidates":[]}')])

    instructions = atomic_memory_extraction_instructions(MemoryExtractionProfile.CONVERSATION)
    raw_model = FunctionModel(model)
    legacy = PydanticAIStructuredGenerator(
        model=raw_model,
        instructions=instructions,
        input_type=AtomicMemoryExtractionInput,
        output_type=AtomicMemoryExtractionOutput,
    )
    compact = PydanticAIStructuredGenerator(
        model=raw_model,
        instructions=f"{instructions}\n{MEMORY_INPUT_ENCODING_INSTRUCTIONS}",
        input_type=SharedMemoryInput,
        output_type=AtomicMemoryExtractionOutput,
    )
    optimizer = MemoryInputOptimizer(
        legacy,
        compact,
        family="atomic",
        stage="atomic_memory.extract",
        mode="active",
        instructions=instructions,
        input_type=AtomicMemoryExtractionInput,
        output_type=AtomicMemoryExtractionOutput,
        estimator=character_token_estimator(),
        counter=character_token_estimator(),
        input_tokens_limit=100_000,
    )
    value = AtomicMemoryExtractionInput(evidence=(_atomic("a durable rule with an important exception " * 500),))
    plan = optimizer.plan(value)
    assert plan.compact
    asyncio.run(optimizer.generate_plan(plan))
    assert actual_prompts == [plan.selected.model_dump_json()]
    assert MEMORY_INPUT_ENCODING_INSTRUCTIONS in actual_instructions[0]
    assert sha256(actual_prompts[0].encode()).hexdigest() == plan.selected_digest


def test_unsent_input_schema_cannot_make_a_larger_actual_request_eligible() -> None:
    actual = []

    async def model(messages, info):
        prompts = [part.content for message in messages for part in message.parts if isinstance(part, UserPromptPart)]
        actual.append((info.instructions, prompts))
        return ModelResponse(parts=[TextPart('{"candidates":[]}')])

    instructions = atomic_memory_extraction_instructions(MemoryExtractionProfile.CONVERSATION)
    raw_model = FunctionModel(model)
    legacy = PydanticAIStructuredGenerator(
        model=raw_model,
        instructions=instructions,
        input_type=AtomicMemoryExtractionInput,
        output_type=AtomicMemoryExtractionOutput,
    )
    compact = PydanticAIStructuredGenerator(
        model=raw_model,
        instructions=f"{instructions}\n{MEMORY_INPUT_ENCODING_INSTRUCTIONS}",
        input_type=SharedMemoryInput,
        output_type=AtomicMemoryExtractionOutput,
    )
    optimizer = MemoryInputOptimizer(
        legacy,
        compact,
        family="atomic",
        stage="atomic_memory.extract",
        mode="active",
        instructions=instructions,
        input_type=AtomicMemoryExtractionInput,
        output_type=AtomicMemoryExtractionOutput,
        estimator=character_token_estimator(),
        counter=character_token_estimator(),
        input_tokens_limit=100_000,
    )
    value = AtomicMemoryExtractionInput(evidence=(_atomic("x" * 840),))
    plan = optimizer.plan(value)
    assert plan.candidate is not None
    assert not plan.compact
    assert plan.reason == "not_smaller"

    async def scenario():
        await legacy.generate(value)
        await optimizer.generate_plan(plan)

    asyncio.run(scenario())
    assert actual[0] == actual[1]


def test_injected_atomic_pipeline_without_compact_binding_keeps_original_contract() -> None:
    original = AtomicMemoryExtractionInput(evidence=(_atomic("hello"),))
    legacy = _Recorder(AtomicMemoryExtractionOutput())
    pipeline = AtomicMemoryGenerationPipeline(
        extractor=legacy, reconciler=legacy, estimator=character_token_estimator()
    )
    assert asyncio.run(pipeline.extract(original)) == ()
    assert legacy.inputs == [original]


def test_default_off_builtin_pipeline_sends_original_input_without_optimization_event(monkeypatch, caplog) -> None:
    actual_prompts = []

    async def model(messages, info):
        actual_prompts.extend(
            part.content for message in messages for part in message.parts if isinstance(part, UserPromptPart)
        )
        return ModelResponse(parts=[TextPart('{"candidates":[]}')])

    raw_model = FunctionModel(model)

    async def open_model(*args, **kwargs):
        return raw_model, raw_model

    monkeypatch.setattr("powercontext.builtin.runtime.composition._open_pydantic_ai_model", open_model)
    value = AtomicMemoryExtractionInput(evidence=(_atomic("durable rule with every condition " * 500),))

    async def scenario():
        async with AsyncExitStack() as resources:
            pipelines = await _generation_pipelines(
                InferenceConfig(generation_model="openai-chat:test-model"),
                RuntimeConfig(),
                resources,
                None,
                BUILTIN_SOURCE_REGISTRY,
            )
            pipeline = pipelines[1]
            assert pipeline is not None
            assert await pipeline.extract(value) == ()

    with caplog.at_level("INFO"):
        asyncio.run(scenario())
    assert actual_prompts == [value.model_dump_json()]
    assert not any(getattr(record, "event", None) == "memory.input_optimization" for record in caplog.records)


@pytest.mark.parametrize("counter", [True, False])
def test_custom_prompt_keeps_original_typed_input(monkeypatch, counter: bool) -> None:
    monkeypatch.setattr(
        "powercontext.builtin.artifacts.memory.input_encoding.current_prompt",
        lambda key: SimpleNamespace(selection="artifact", compiled_instructions="Preserve my original input contract."),
    )
    value = AtomicMemoryExtractionInput(evidence=(_atomic("durable rule with every exception " * 500),))
    optimizer, legacy, compact = _optimizer("active", counter=counter)
    plan = optimizer.plan(value)
    assert plan.reason == "custom_prompt"
    assert plan.would_noop is None
    asyncio.run(optimizer.generate_plan(plan))
    assert legacy.inputs == [value]
    assert not compact.inputs


@pytest.mark.parametrize("text,expected", [("hello", True), ("好的", False), ("改成周五", False), ("失败了", False)])
def test_noop_candidate_is_diagnostic_and_always_runs_the_original_model(text: str, expected: bool) -> None:
    evidence = _atomic(text)
    source_metadata = evidence.source_metadata
    assert isinstance(source_metadata, dict)
    evidence = evidence.model_copy(
        update={
            "source_metadata": {key: value for key, value in source_metadata.items() if key != "unknown_source_field"}
        }
    )
    value = AtomicMemoryExtractionInput(evidence=(evidence,))
    optimizer, legacy, compact = _optimizer("active")
    plan = optimizer.plan(value)
    assert plan.would_noop is expected
    asyncio.run(optimizer.generate_plan(plan))
    assert legacy.inputs == [value]
    assert not compact.inputs


def test_topic_selected_view_is_checked_with_its_actual_schema_and_instructions() -> None:
    legacy = _Recorder(TopicMemoryProbeOutput())
    compact = _Recorder(TopicMemoryProbeOutput())
    estimator = character_token_estimator()
    budget = topic_memory_stage_budget(context_window_tokens=100_000, max_requests=1, model_settings={})
    shared_instructions = f"{TOPIC_MEMORY_PROBE_INSTRUCTIONS}\n{MEMORY_INPUT_ENCODING_INSTRUCTIONS}"
    optimizer = MemoryInputOptimizer(
        BudgetedTopicMemoryGenerator(
            legacy,
            estimator=estimator,
            budget=budget,
            fixed_prompt=topic_memory_stage_fixed_prompt(
                TOPIC_MEMORY_PROBE_INSTRUCTIONS, TopicMemoryProbeInput, TopicMemoryProbeOutput
            ),
        ),
        BudgetedTopicMemoryGenerator(
            compact,
            estimator=estimator,
            budget=budget,
            fixed_prompt=topic_memory_stage_fixed_prompt(
                shared_instructions, SharedMemoryInput, TopicMemoryProbeOutput
            ),
        ),
        family="topic",
        stage="topic_memory.probe",
        mode="active",
        instructions=TOPIC_MEMORY_PROBE_INSTRUCTIONS,
        input_type=TopicMemoryProbeInput,
        output_type=TopicMemoryProbeOutput,
        estimator=estimator,
        counter=estimator,
        input_tokens_limit=budget.input_tokens_limit,
    )
    text = _json({
        "content": "All original roles, conditions and dates remain. " * 500,
        "metadata": {"recorded_at": "2026-10-01"},
    })
    original = TopicMemoryProbeInput(
        evidence=tuple(TopicMemoryEvidence(evidence_id=f"e{i}", source_type="content", content=text) for i in range(3))
    )
    asyncio.run(optimizer.generate(original))
    assert not legacy.inputs
    assert len(compact.inputs) == 1
    assert expand_memory_input(compact.inputs[0]) == original.model_dump(mode="json")
