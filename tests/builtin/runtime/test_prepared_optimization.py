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

"""Observe complete delivered evidence, rather than intermediate planner states."""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Literal, cast

import pytest

from powercontext.artifacts import ArtifactAddress, ArtifactRef
from powercontext.builtin.artifacts.experience import ExperienceContent, ExperienceSearchHit
from powercontext.builtin.artifacts.topic_memory import TopicMemorySearchHit
from powercontext.builtin.inference.tokens import TokenEstimator, TokenEstimatorProfile
from powercontext.builtin.persistence.atomic_memory_index import AtomicMemoryIndexHit
from powercontext.builtin.runtime import ContextAssembly, PrepareContextRequest
from powercontext.builtin.runtime.atomic_memory import AtomicMemorySearchHit
from powercontext.builtin.runtime.prepared_context import (
    PreparedContextBuild,
    PreparedContextBuilder,
    PreparedExperienceCandidates,
    PreparedMemoryCandidates,
)
from powercontext.builtin.runtime.prepared_optimization import OptimizationResult, PreparedTokenOptimizer
from powercontext.builtin.runtime.prepared_tokens import load_prepared_token_counter


@pytest.fixture(scope="session", params=("utf8_units", "cl100k_base", "o200k_base"))
def counter(request: pytest.FixtureRequest) -> TokenEstimator:
    if request.param == "utf8_units":
        # Preserve the compiler's behavioral coverage in cache-free CI. This is
        # a deterministic test cost, not a provider-token or savings estimate.
        return TokenEstimator(
            TokenEstimatorProfile(estimator_id="test:utf8-units", version="1"),
            lambda text: len(text.encode("utf-8")),
        )
    profile = cast(Literal["cl100k_base", "o200k_base"], request.param)
    loaded = load_prepared_token_counter(profile)
    if loaded is None:
        pytest.skip(f"provide the existing pinned {profile} vocabulary in TIKTOKEN_CACHE_DIR")
    # Warm up the exact counter outside the compiler's request-time budget.
    loaded.estimate("PowerContext historical context")
    return loaded


def _request(markdown: bool, *, query: str = "retry", max_bytes: int = 32768) -> PrepareContextRequest:
    if markdown:
        return PrepareContextRequest(
            query=query,
            max_bytes=max_bytes,
            assembly=ContextAssembly.model_validate({
                "sections": [
                    {"family": "memory", "limit": 8},
                    {"family": "topic-memory", "limit": 8},
                    {"family": "experience", "limit": 2},
                ],
                "show": ["confidence", "recall_rank"],
            }),
        )
    return PrepareContextRequest(query=query, max_bytes=max_bytes)


def _experience(content: ExperienceContent, artifact_id: str = "experience-1") -> ExperienceSearchHit:
    return ExperienceSearchHit(
        artifact_ref=ArtifactRef(family="experience", artifact_id=artifact_id, revision=3), content=content
    )


def _topic(summary: str, *, snippet: str | None = None, artifact_id: str = "topic-1") -> TopicMemorySearchHit:
    return TopicMemorySearchHit(
        artifact_ref=ArtifactRef(family="topic-memory", artifact_id=artifact_id, revision=3),
        title="Retry guidance",
        summary=summary,
        snippet=snippet,
        score=1.0,
        matched_by=("topic_fts",),
    )


def _memory(artifact_id: str, text: str) -> AtomicMemorySearchHit:
    return AtomicMemorySearchHit(
        hit=AtomicMemoryIndexHit(
            artifact_ref=ArtifactRef(family="atomic-memory", artifact_id=artifact_id, revision=3),
            state_version=0,
            kind="fact",
            text=text,
            score=1.0,
        ),
        matched_by=("fts",),
    )


def _optimize(
    counter: TokenEstimator | None,
    request: PrepareContextRequest,
    *,
    mode: Literal["active", "shadow"] = "active",
    target_tokens: int = 20000,
    memory: tuple[PreparedMemoryCandidates, ...] = (),
    topics: tuple[TopicMemorySearchHit, ...] = (),
    experiences: tuple[PreparedExperienceCandidates, ...] = (),
) -> tuple[PreparedContextBuild, OptimizationResult]:
    builder = PreparedContextBuilder()
    legacy = builder.build_scopes_result(
        request=request,
        current_scope_id="current",
        memory_candidates=memory,
        topic_memory_hits=topics,
        experience_candidates=experiences,
    )
    optimized = PreparedTokenOptimizer(mode=mode, target_tokens=target_tokens, counter=counter).optimize(
        request=request,
        builder=builder,
        legacy=legacy,
        current_scope_id="current",
        memory_candidates=memory,
        topic_memory_hits=topics,
        experience_candidates=experiences,
    )
    return legacy, optimized


def _content(build: PreparedContextBuild) -> str:
    assert build.context.content is not None
    return build.context.content


def _items(build: PreparedContextBuild) -> list[dict[str, object]]:
    return cast(list[dict[str, object]], json.loads(_content(build).splitlines()[-2])["items"])


def _body(build: PreparedContextBuild, markdown: bool) -> str:
    if markdown:
        return "\n".join(
            line.removeprefix(">     ") for line in _content(build).splitlines() if line.startswith(">     ")
        )
    assert len(_items(build)) == 1
    return str(_items(build)[0]["content"])


def _assert_smaller(
    counter: TokenEstimator, request: PrepareContextRequest, legacy: PreparedContextBuild, result: OptimizationResult
) -> None:
    delivered = _content(result.build)
    assert result.fallback_reason is None
    assert counter.estimate(delivered) < counter.estimate(_content(legacy))
    assert result.baseline_tokens == counter.estimate(_content(legacy))
    assert result.baseline_bytes == legacy.context.content_bytes
    assert result.proposed_tokens == counter.estimate(delivered)
    assert result.proposed_bytes == len(delivered.encode("utf-8"))
    assert result.legacy_tokens == counter.estimate(_content(legacy))
    assert result.legacy_bytes == legacy.context.content_bytes
    assert result.proposed_bytes is not None and result.baseline_bytes is not None
    assert result.proposed_bytes < result.baseline_bytes
    assert result.build.context.content_bytes == len(delivered.encode("utf-8")) <= request.max_bytes
    assert result.build.origins == legacy.origins
    assert not result.selection_changed


@pytest.mark.parametrize("markdown", [False, True])
def test_equal_experience_roles_share_complete_text_but_keep_failure_conditions(
    counter: TokenEstimator, markdown: bool
) -> None:
    shared = (
        "Retry only confirmed idempotent reads, at most three times, with jitter and a recorded attempt count. " * 2
    )
    source = ExperienceContent.model_validate({
        "situation": shared,
        "action": shared,
        "outcome": shared,
        "lesson": shared,
        "failure": {
            "signature": {"recall_cue": "Duplicate payment", "symptom": "The payment was submitted twice."},
            "repair_surface": "acceptance_check",
            "verification": {"condition": shared, "check_subject": "Confirm no duplicate payment"},
        },
    })
    snapshot = source.model_dump()
    request = _request(markdown)

    legacy, result = _optimize(
        counter, request, experiences=(PreparedExperienceCandidates("current", (_experience(source),)),)
    )

    _assert_smaller(counter, request, legacy, result)
    body = _body(result.build, markdown)
    assert f"Situation / Action / Outcome / Lesson: {shared}" in body
    assert f"Verification condition: {shared}" in body
    assert body.count(shared) == 2
    assert "Failure cue: Duplicate payment" in body
    assert "Symptom: The payment was submitted twice." in body
    assert "Repair surface: acceptance_check" in body
    assert "Verification check: Confirm no duplicate payment" in body
    assert source.model_dump() == snapshot
    assert "R1" in result.rules
    assert result.omitted_sections == 0
    if markdown:
        assert "Confidence: unknown (not assessed)" in _content(result.build)
        assert "Recall rank: 1" in _content(result.build)
        assert "Truncated: no" in _content(result.build)
    else:
        assert _items(result.build)[0]["truncated"] is False
        assert _items(result.build)[0]["citation"] == _items(legacy)[0]["citation"]


@pytest.mark.parametrize("markdown", [False, True])
def test_whitespace_numbers_and_negation_are_not_equal_experience_roles(
    counter: TokenEstimator, markdown: bool
) -> None:
    source = ExperienceContent(
        situation="Retry at most three times.",
        action="Retry at most three times. ",
        outcome="Retry at most five times.",
        lesson="Never retry at most three times.",
    )
    legacy, result = _optimize(
        counter, _request(markdown), experiences=(PreparedExperienceCandidates("current", (_experience(source),)),)
    )

    assert result.build == legacy
    assert result.fallback_reason == "not_smaller"
    assert "R1" not in result.rules
    for role in ("situation", "action", "outcome", "lesson"):
        assert f"{role.capitalize()}: {getattr(source, role)}" in _body(result.build, markdown)


@pytest.mark.parametrize("markdown", [False, True])
def test_complete_equal_topic_snippet_references_the_full_delivered_summary(
    counter: TokenEstimator, markdown: bool
) -> None:
    summary = "Only confirmed idempotent reads qualify; never retry financial operations automatically. " * 6
    hit = _topic(summary, snippet=summary)
    snapshot = hit.model_dump()
    request = _request(markdown)

    legacy, result = _optimize(counter, request, topics=(hit,))

    _assert_smaller(counter, request, legacy, result)
    body = _body(result.build, markdown)
    assert summary in body
    assert body.count(summary) == 1
    assert "Identical to the complete Summary above." in body
    assert "R2" in result.rules
    assert hit.model_dump() == snapshot
    assert result.build.omissions.truncated_items == 0


@pytest.mark.parametrize("markdown", [False, True])
def test_substring_snippet_keeps_its_exact_match_even_when_summary_contains_it(
    counter: TokenEstimator, markdown: bool
) -> None:
    summary = "In revision 2, retry once. In revision 3, retry three times. Never retry payments."
    snippet = "In revision 2, retry once."
    legacy, result = _optimize(counter, _request(markdown), topics=(_topic(summary, snippet=snippet),))

    assert result.build == legacy
    assert "R2" not in result.rules
    body = _body(result.build, markdown)
    assert summary in body and snippet in body
    if not markdown:
        fields = json.loads(body)
        assert fields["summary"] == summary
        assert fields["snippet"] == snippet


@pytest.mark.parametrize("markdown", [False, True])
def test_table_retains_json_types_lexemes_row_order_and_duplicate_rows(counter: TokenEstimator, markdown: bool) -> None:
    row = (
        '{"large_integer_identifier":90071992547409931234567890,'
        '"precise_decimal_measurement":1.2300e-17,"boolean_flag_value":true,'
        '"nullable_optional_value":null,"quoted_separator_text":"a,b|\\"line\\nend",'
        '"numeric_looking_string_value":"00123"}'
    )
    summary = "[" + ",".join((row, row, row, row)) + "]"
    hit = _topic(summary)
    request = _request(markdown)

    legacy, result = _optimize(counter, request, topics=(hit,))

    _assert_smaller(counter, request, legacy, result)
    body = _body(result.build, markdown)
    table = body.split("Summary: ", 1)[1] if markdown else json.loads(body)["summary"]
    lines = table.splitlines()
    header = next(line.removeprefix("Columns: ") for line in lines if line.startswith("Columns: "))
    columns = json.loads(header)
    row_lines = lines[lines.index("Rows:") + 1 :]
    values = [json.loads(line, parse_float=Decimal) for line in row_lines]
    reconstructed = [dict(zip(columns, value, strict=True)) for value in values]
    assert reconstructed == json.loads(summary, parse_float=Decimal)
    assert len(reconstructed) == 4 and reconstructed[0] == reconstructed[1]
    assert table.count("90071992547409931234567890") == 4
    assert table.count("1.2300e-17") == 4
    assert type(values[0][0]) is int
    assert type(values[0][2]) is bool
    assert values[0][3] is None
    assert values[0][4] == 'a,b|"line\nend'
    assert values[0][5] == "00123"
    assert hit.summary == summary
    assert "R3" in result.rules


@pytest.mark.parametrize("markdown", [False, True])
def test_equal_experience_roles_can_share_a_complete_table(counter: TokenEstimator, markdown: bool) -> None:
    row = '{"component_name":"checkout","maximum_retry_attempts":3,"automatic_payment_retry":false}'
    shared = "[" + ",".join((row, row, row, row, row)) + "]"
    source = ExperienceContent(situation=shared, action=shared, outcome=shared, lesson=shared)
    request = _request(markdown)
    legacy, result = _optimize(
        counter, request, experiences=(PreparedExperienceCandidates("current", (_experience(source),)),)
    )

    _assert_smaller(counter, request, legacy, result)
    body = _body(result.build, markdown)
    assert "Situation / Action / Outcome / Lesson: Complete table" in body
    lines = body.splitlines()
    columns = json.loads(next(line.removeprefix("Columns: ") for line in lines if line.startswith("Columns: ")))
    values = [json.loads(line) for line in lines[lines.index("Rows:") + 1 :]]
    assert [dict(zip(columns, value, strict=True)) for value in values] == json.loads(shared)
    assert "R1" in result.rules and "R3" in result.rules
    assert source.action == shared and source.lesson == shared


@pytest.mark.parametrize(
    "summary",
    [
        '[{"a":1,"a":2},{"a":3,"a":4}]',
        '[{"a":1,"b":2},{"a":3}]',
        '[{"a":1,"b":2},{"b":3,"a":4}]',
        '[{"a":{"nested":1}},{"a":{"nested":2}}]',
        '[{"a":[1,2]},{"a":[3,4]}]',
        '[{"a":NaN},{"a":Infinity}]',
        '[{"a":1}]',
        '[{"a":1},{"a":2}',
    ],
)
def test_unsupported_json_rows_are_delivered_unchanged(counter: TokenEstimator, summary: str) -> None:
    legacy, result = _optimize(counter, _request(False), topics=(_topic(summary),))

    assert result.build == legacy
    assert result.fallback_reason == "not_smaller"
    assert "R3" not in result.rules
    assert json.loads(_body(result.build, False))["summary"] == summary


@pytest.mark.parametrize("mode", ["active", "shadow"])
def test_unreachable_token_target_preserves_original_including_failure_tail(
    counter: TokenEstimator, mode: Literal["active", "shadow"]
) -> None:
    shared = "Only confirmed idempotent reads qualify for retries. " * 4
    source = ExperienceContent.model_validate({
        "situation": shared,
        "action": shared,
        "outcome": shared,
        "lesson": shared,
        "failure": {
            "signature": {"recall_cue": "Payment retry"},
            "repair_surface": "recall_policy",
            "verification": {"condition": "Never retry payments.", "check_subject": "No duplicate payment"},
        },
    })
    legacy, result = _optimize(
        counter,
        _request(False),
        mode=mode,
        target_tokens=1,
        experiences=(PreparedExperienceCandidates("current", (_experience(source),)),),
    )

    assert result.build == legacy
    assert result.fallback_reason == "target_unmet"
    assert result.proposed is not None
    assert "Verification condition: Never retry payments." in _body(result.proposed, False)
    assert result.baseline_tokens == counter.estimate(_content(legacy))
    assert result.proposed_tokens == counter.estimate(_content(result.proposed))
    assert result.proposed_tokens is not None and result.baseline_tokens is not None
    assert result.proposed_tokens < result.baseline_tokens


@pytest.mark.parametrize("markdown", [False, True])
def test_token_target_counts_trust_citations_and_metadata_in_addition_to_body(
    counter: TokenEstimator, markdown: bool
) -> None:
    shared = "Retry only confirmed idempotent reads at most three times with jitter. " * 3
    source = ExperienceContent(situation=shared, action=shared, outcome=shared, lesson=shared)
    experiences = (PreparedExperienceCandidates("current", (_experience(source),)),)
    request = _request(markdown)
    legacy, accepted = _optimize(counter, request, experiences=experiences)
    body_tokens = counter.estimate(_body(accepted.build, markdown))
    complete_tokens = counter.estimate(_content(accepted.build))
    assert complete_tokens > body_tokens

    _, too_small = _optimize(counter, request, experiences=experiences, target_tokens=body_tokens)
    _, exact_fit = _optimize(counter, request, experiences=experiences, target_tokens=complete_tokens)

    assert too_small.build == legacy
    assert too_small.fallback_reason == "target_unmet"
    assert too_small.proposed_tokens == complete_tokens
    assert exact_fit.build == accepted.build
    assert exact_fit.fallback_reason is None


@pytest.mark.parametrize("markdown", [False, True])
def test_complete_protected_group_that_cannot_fit_preserves_legacy_truncation(
    counter: TokenEstimator, markdown: bool
) -> None:
    shared = "Retry only confirmed idempotent reads with three attempts and jitter. " * 9
    source = ExperienceContent.model_validate({
        "situation": shared,
        "action": shared,
        "outcome": shared,
        "lesson": shared,
        "failure": {
            "signature": {"recall_cue": "Payment retry"},
            "repair_surface": "recall_policy",
            "verification": {
                "condition": "Never retry financial operations automatically. " * 8,
                "check_subject": "Confirm no duplicate payment",
            },
        },
    })
    request = _request(markdown, max_bytes=900)
    legacy, result = _optimize(
        counter, request, experiences=(PreparedExperienceCandidates("current", (_experience(source),)),)
    )

    assert legacy.origins and legacy.omissions.truncated_items == 1
    assert result.build == legacy
    assert result.fallback_reason == "protected_group_unfit"
    assert result.proposed is None
    assert result.build.context.content_bytes <= request.max_bytes


@pytest.mark.parametrize("markdown", [False, True])
def test_shadow_measures_saving_but_returns_original(counter: TokenEstimator, markdown: bool) -> None:
    summary = "Only confirmed idempotent reads qualify; never retry financial operations automatically. " * 6
    legacy, result = _optimize(counter, _request(markdown), mode="shadow", topics=(_topic(summary, snippet=summary),))

    assert result.build == legacy
    assert result.proposed is not None and result.proposed != legacy
    assert counter.estimate(_content(result.proposed)) < counter.estimate(_content(legacy))
    assert result.proposed_tokens == counter.estimate(_content(result.proposed))


def test_unfit_original_candidate_cannot_be_skipped_by_the_complete_snapshot(counter: TokenEstimator) -> None:
    shared = "Retry confirmed idempotent reads at most three times with jitter. " * 2
    source = ExperienceContent(situation=shared, action=shared, outcome=shared, lesson=shared)
    request = _request(True, max_bytes=1200)
    legacy, result = _optimize(
        counter,
        request,
        experiences=(PreparedExperienceCandidates("current", (_experience(source, "🙂" * 128), _experience(source))),),
    )

    assert result.build == legacy
    assert result.fallback_reason == "protected_group_unfit"
    assert result.proposed is None
    assert len(result.build.origins) == 1
    assert legacy.omissions.dropped_items == 1
    assert "Recall rank: 2" in _content(result.build)
    assert "Recall rank: 1" not in _content(result.build)
    assert "Confidence: unknown (not assessed)" in _content(result.build)
    assert result.build.omissions.dropped_items == legacy.omissions.dropped_items


@pytest.mark.parametrize("markdown", [False, True])
def test_complete_baseline_is_measured_before_legacy_truncation(counter: TokenEstimator, markdown: bool) -> None:
    shared = "Retry only confirmed idempotent reads at most three times; never retry financial operations. " * 9
    source = ExperienceContent.model_validate({
        "situation": shared,
        "action": shared,
        "outcome": shared,
        "lesson": shared,
        "failure": {
            "signature": {"recall_cue": "Payment retry"},
            "repair_surface": "acceptance_check",
            "verification": {
                "condition": "No automatic payment retries.",
                "check_subject": "Confirm no duplicate payment",
            },
        },
    })
    request = _request(markdown)
    legacy, result = _optimize(
        counter, request, experiences=(PreparedExperienceCandidates("current", (_experience(source),)),)
    )

    assert legacy.omissions.truncated_items == 1
    assert "Confirm no duplicate payment" not in _content(legacy)
    assert result.fallback_reason is None
    assert result.baseline_tokens is not None and result.legacy_tokens is not None
    assert result.baseline_tokens > result.legacy_tokens
    assert result.baseline_bytes is not None and result.legacy_bytes is not None
    assert result.baseline_bytes > result.legacy_bytes
    assert result.proposed_tokens == counter.estimate(_content(result.build)) < result.legacy_tokens
    assert result.proposed_bytes == result.build.context.content_bytes < result.legacy_bytes
    assert result.build.origins == legacy.origins
    assert result.build.omissions.truncated_items == 0
    assert shared in _body(result.build, markdown)
    assert "Verification check: Confirm no duplicate payment" in _body(result.build, markdown)


@pytest.mark.parametrize("markdown", [False, True])
def test_lower_token_cost_cannot_adopt_a_larger_utf8_representation(markdown: bool) -> None:
    counter = TokenEstimator(
        TokenEstimatorProfile(estimator_id="test:independent-cost", version="1"),
        lambda text: 10 if "Identical to the complete Summary above." in text else 100,
    )
    legacy, result = _optimize(counter, _request(markdown), topics=(_topic("Retry.", snippet="Retry."),))

    assert result.build == legacy
    assert result.fallback_reason == "not_smaller_bytes"
    assert result.proposed is not None
    assert result.proposed_tokens is not None and result.baseline_tokens is not None
    assert result.proposed_tokens < result.baseline_tokens
    assert result.proposed_bytes is not None and result.baseline_bytes is not None
    assert result.proposed_bytes > result.baseline_bytes


def test_complete_gain_cannot_increase_the_actual_legacy_delivery_cost() -> None:
    counter = TokenEstimator(
        TokenEstimatorProfile(estimator_id="test:delivery-cost", version="1"),
        lambda text: 5 if "…" in text else 10 if "Situation / Action / Outcome / Lesson:" in text else 100,
    )
    shared = "Retry only confirmed idempotent reads; never retry financial operations. " * 9
    source = ExperienceContent(situation=shared, action=shared, outcome=shared, lesson=shared)
    legacy, result = _optimize(
        counter, _request(False), experiences=(PreparedExperienceCandidates("current", (_experience(source),)),)
    )

    assert legacy.omissions.truncated_items == 1
    assert result.build == legacy
    assert result.fallback_reason == "delivery_increased"
    assert result.proposed is not None
    assert result.proposed_tokens is not None and result.baseline_tokens is not None
    assert result.legacy_tokens is not None
    assert result.legacy_tokens < result.proposed_tokens < result.baseline_tokens
    assert result.proposed_bytes is not None and result.legacy_bytes is not None
    assert result.proposed_bytes < result.legacy_bytes


@pytest.mark.parametrize("markdown", [False, True])
def test_original_entry_and_section_limits_bound_the_complete_plan(counter: TokenEstimator, markdown: bool) -> None:
    shared = "Only confirmed idempotent reads qualify; never retry financial operations. " * 3
    source = ExperienceContent(situation=shared, action=shared, outcome=shared, lesson=shared)
    hits = (
        *(_memory(f"memory-{index}", f"Retry constraint {index}") for index in range(7)),
        _memory("outside-limit", "This evidence lies beyond the original delivery limit. " * 100),
    )
    request = (
        PrepareContextRequest(
            query="retry",
            max_bytes=32768,
            assembly=ContextAssembly.model_validate({
                "sections": [{"family": "memory", "limit": 1}, {"family": "experience", "limit": 1}],
            }),
        )
        if markdown
        else _request(False)
    )
    legacy, result = _optimize(
        counter,
        request,
        memory=(PreparedMemoryCandidates("current", hits),),
        experiences=(PreparedExperienceCandidates("current", (_experience(source),)),),
    )

    _assert_smaller(counter, request, legacy, result)
    assert len(result.build.origins) == (2 if markdown else 8)
    assert "lies beyond the original delivery limit" not in _content(result.build)
    assert "Situation / Action / Outcome / Lesson:" in _content(result.build)


@pytest.mark.parametrize("markdown", [False, True])
def test_cross_scope_order_and_exact_atomic_text_survive_other_entry_optimization(
    counter: TokenEstimator, markdown: bool
) -> None:
    atomic = 'Constraint\r\nNever retry payments.\x00\u2028"quotation"\u202e'
    shared = "Regenerate and inspect the client before contract tests. " * 3
    experience = ExperienceContent(situation=shared, action=shared, outcome=shared, lesson=shared)
    request = _request(markdown)
    legacy, result = _optimize(
        counter,
        request,
        memory=(
            PreparedMemoryCandidates(
                "current", (_memory("a", atomic), _memory("a", atomic), _memory("c", "Local last"))
            ),
            PreparedMemoryCandidates("shared", (_memory("a", "Shared first"), _memory("b", "Shared second"))),
        ),
        experiences=(PreparedExperienceCandidates("current", (_experience(experience),)),),
    )

    _assert_smaller(counter, request, legacy, result)
    content = _content(result.build)
    assert content.index("Shared first") < content.index("Local last")
    assert len(result.build.origins) == 5
    assert (
        ArtifactAddress(scope_id="shared", artifact=ArtifactRef(family="atomic-memory", artifact_id="a", revision=3))
        in result.build.origins
    )
    if markdown:
        assert content.index("Shared second") < content.index("Local last")
        assert '    Scope: "shared"' in content
        assert "\\u0000" in content and "\\u202e" in content
        assert "\x00" not in content and "\u202e" not in content
        assert content.count("Recall rank: 1") == 2
        assert "Recall rank: 4" in content
    else:
        atoms = [item for item in _items(result.build) if item["kind"] == "atomic-memory"]
        assert [item["content"] for item in atoms] == [atomic, "Shared first", "Local last", "Shared second"]
        assert all(item["truncated"] is False for item in atoms)
        assert len(content.splitlines()) == len(_content(legacy).splitlines())


@pytest.mark.parametrize("markdown", [False, True])
def test_unpromoted_excerpt_can_be_measured_in_shadow_but_never_delivered_in_active(
    counter: TokenEstimator, markdown: bool
) -> None:
    summary = (
        "## Retry\nRetry confirmed idempotent reads at most three times.\n\n"
        "## Dashboard colors\n"
        + "The dashboard uses blue headers and amber cards with consistent margins. " * 10
        + "\n\n## Exceptions\nNever retry financial operations automatically.\n"
    )
    request = _request(markdown)
    topics = (_topic(summary),)
    legacy, active = _optimize(counter, request, topics=topics)
    _, shadow = _optimize(counter, request, topics=topics, mode="shadow")

    assert active.build == legacy
    assert "R4" not in active.rules
    assert shadow.build == legacy
    assert shadow.proposed is not None
    assert "R4" in shadow.rules and shadow.omitted_sections == 1
    body = _body(shadow.proposed, markdown)
    assert "three times" in body and "Never retry financial operations automatically." in body
    assert "dashboard uses blue" not in body
    assert "complete sections omitted" in body
    assert shadow.proposed.omissions.truncated_items == 1


def test_unpromoted_coverage_selection_keeps_native_origins_in_active(counter: TokenEstimator) -> None:
    summary = "## Retry\nRetry reads at most three times.\n\n## Theme\n" + "Use blue headers and amber cards. " * 10
    topics = (_topic(summary, artifact_id="first"), _topic(summary, artifact_id="second"))
    request = _request(False)
    legacy, active = _optimize(counter, request, topics=topics)
    _, shadow = _optimize(counter, request, topics=topics, mode="shadow")

    assert active.build == legacy
    assert len(active.build.origins) == 2
    assert not active.selection_changed
    assert shadow.build == legacy
    assert shadow.proposed is not None and len(shadow.proposed.origins) == 1
    assert shadow.selection_changed and "R5" in shadow.rules
    assert shadow.proposed.origins[0] == legacy.origins[0]


@pytest.mark.parametrize("change", ["scope", "revision", "independent_source", "exception", "comparison_query"])
def test_shadow_coverage_cannot_drop_distinct_versions_provenance_or_constraints(
    counter: TokenEstimator, change: str
) -> None:
    summary = "## Retry\nRetry reads at most three times.\n\n## Theme\n" + "Use blue headers and amber cards. " * 4
    first = _topic(summary, artifact_id="first")
    second = _topic(summary, artifact_id="second")
    query = "retry"
    if change == "scope":
        # Topic candidates are scoped to the current Scope. Cross-Scope evidence
        # enters through distinct Memory addresses and is never an R5 candidate.
        legacy, result = _optimize(
            counter,
            _request(False),
            mode="shadow",
            memory=(
                PreparedMemoryCandidates("current", (_memory("same", summary),)),
                PreparedMemoryCandidates("shared", (_memory("same", summary),)),
            ),
        )
    else:
        if change == "revision":
            second = second.model_copy(update={"artifact_ref": second.artifact_ref.model_copy(update={"revision": 4})})
        elif change == "independent_source":
            second = second.model_copy(
                update={"summary": summary + "\n## Evidence\nhttps://example.invalid/incident\n"}
            )
        elif change == "exception":
            second = second.model_copy(update={"summary": summary + "\n## Exceptions\nNever retry payments.\n"})
        else:
            query = "Compare independent sources for retry"
        legacy, result = _optimize(counter, _request(False, query=query), mode="shadow", topics=(first, second))

    assert result.build == legacy
    assert result.proposed is not None
    assert result.proposed.origins == legacy.origins
    assert not result.selection_changed
    assert "R5" not in result.rules


def test_unavailable_tokenizer_preserves_legacy_without_claiming_savings() -> None:
    legacy, result = _optimize(None, _request(False), topics=(_topic("Use three attempts."),))

    assert result.build == legacy
    assert result.fallback_reason == "tokenizer_unavailable"
    assert result.baseline_tokens is None and result.proposed_tokens is None
    assert result.proposed is None
