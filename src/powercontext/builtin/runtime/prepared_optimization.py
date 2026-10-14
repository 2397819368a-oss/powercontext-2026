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

"""Stateless, bounded representations of already admitted prepare evidence.

No retrieval, model call, persistence, or tokenizer initialization occurs here.
Lossy formats remain shadow-only until independent held-out acceptance promotes
their fixed policy. Exact citations and the admitted candidate set remain distinct
from representation changes.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from functools import partial
from itertools import chain
from time import perf_counter
from typing import Literal

from powercontext.artifacts import ArtifactAddress, ArtifactRef
from powercontext.builtin.artifacts.experience import ExperienceContent
from powercontext.builtin.artifacts.topic_memory import TopicMemorySearchHit
from powercontext.builtin.inference.tokens import TokenEstimator
from powercontext.builtin.runtime.models import PrepareContextRequest, PreparedContext
from powercontext.builtin.runtime.prepared_context import (
    PreparedContextBuild,
    PreparedContextBuilder,
    PreparedContextOmissions,
    PreparedExperienceCandidates,
    PreparedMemoryCandidates,
    PreparedProfileCandidate,
    _interleave,
    _interleave_groups,
    _PreparedContextEntry,
    _render,
    _text_item,
)
from powercontext.builtin.runtime.prepared_excerpts import (
    TOPIC_EXCERPT_POLICY_VERSION,
    describe_topic_coverage,
    extract_topic_excerpt,
)
from powercontext.builtin.runtime.prepared_text import _body_chunks, render_context_text

POLICY_VERSION = "prepared-evidence-v2"
# A parser is not evidence of unchanged task quality. Promotion requires a
# frozen, independently scored calibration and held-out experiment.
ACTIVE_LOSSY_POLICIES: frozenset[str] = frozenset()
_BEAM_STATES = 32
_MAX_PLANNING_SECONDS = 0.25
_OriginKey = tuple[str | None, str, str, int]


@dataclass(frozen=True)
class OptimizationResult:
    """Internal comparison; never adds fields to PreparedContext or statistics."""

    build: PreparedContextBuild
    proposed: PreparedContextBuild | None = None
    baseline_tokens: int | None = None
    proposed_tokens: int | None = None
    baseline_bytes: int | None = None
    proposed_bytes: int | None = None
    legacy_tokens: int | None = None
    legacy_bytes: int | None = None
    rules: tuple[str, ...] = ()
    fallback_reason: str | None = None
    selection_changed: bool = False
    omitted_sections: int = 0


@dataclass(frozen=True)
class _Evidence:
    entry: _PreparedContextEntry
    rank: int
    source: ExperienceContent | TopicMemorySearchHit | None = None


@dataclass(frozen=True)
class _Variant:
    entry: _PreparedContextEntry
    rules: tuple[str, ...] = ()
    omitted_sections: int = 0


class PreparedTokenOptimizer:
    """Plan at most four complete variants per selected entry, with 32 states."""

    def __init__(
        self,
        mode: Literal["shadow", "active"],
        target_tokens: int,
        counter: TokenEstimator | None,
    ) -> None:
        self.mode = mode
        self.target_tokens = target_tokens
        self.counter = counter

    def optimize(
        self,
        *,
        request: PrepareContextRequest,
        builder: PreparedContextBuilder,
        legacy: PreparedContextBuild,
        current_scope_id: str | None,
        memory_candidates: Sequence[PreparedMemoryCandidates] = (),
        topic_memory_hits: Sequence[TopicMemorySearchHit] = (),
        experience_candidates: Sequence[PreparedExperienceCandidates] = (),
        profile_candidates: Sequence[PreparedProfileCandidate] = (),
    ) -> OptimizationResult:
        if self.counter is None:
            return OptimizationResult(legacy, fallback_reason="tokenizer_unavailable")
        if request.include_code:
            return OptimizationResult(legacy, fallback_reason="code_request")
        if legacy.context.content is None or not legacy.origins:
            return OptimizationResult(legacy, fallback_reason="empty")
        deadline = perf_counter() + _MAX_PLANNING_SECONDS
        legacy_tokens = self.counter.estimate(legacy.context.content)
        legacy_bytes = legacy.context.content_bytes
        selected = _snapshot(
            builder,
            request,
            current_scope_id,
            memory_candidates,
            topic_memory_hits,
            experience_candidates,
            profile_candidates,
        )
        # Compare complete representations of the same bounded candidates. The
        # legacy delivery may already have truncated or dropped an entry; neither
        # outcome is a representation saving and neither can define this plan.
        complete = _render_plan(tuple(_Variant(item.entry) for item in selected), selected, request)
        baseline_tokens = self.counter.estimate(complete)
        baseline_bytes = len(complete.encode("utf-8"))
        comparison = OptimizationResult(
            legacy,
            baseline_tokens=baseline_tokens,
            baseline_bytes=baseline_bytes,
            legacy_tokens=legacy_tokens,
            legacy_bytes=legacy_bytes,
        )
        if any(evidence.entry.kind == "profile" for evidence in selected):
            return replace(comparison, fallback_reason="profile")
        allow_excerpts = self.mode == "shadow" or TOPIC_EXCERPT_POLICY_VERSION in ACTIVE_LOSSY_POLICIES
        variants = tuple(
            _variants(evidence, request.query, allow_excerpts, markdown=request.assembly is not None)
            for evidence in selected
        )
        try:
            tokens, best = _plan(
                self.counter, variants, selected, request, deadline, allow_selection=self.mode == "shadow"
            )
        except _PlanningError as error:
            return replace(comparison, fallback_reason=error.reason)
        selection_changed = False
        # R5 is an explicitly lossy, shadow-only experiment. Coverage is never an
        # identity; positive provenance vetoes and exact Scope/revision partitions
        # prevent similarity-based merging of sources.
        if self.mode == "shadow" and (reduced := _select_coverage(best, selected, request.query)) is not None:
            reduced_plan, reduced_evidence = reduced
            reduced_tokens = self.counter.estimate(_render_plan(reduced_plan, reduced_evidence, request))
            if reduced_tokens < tokens:
                best, selected, tokens = reduced_plan, reduced_evidence, reduced_tokens
                selection_changed = True
        content = _render_plan(best, selected, request)
        if perf_counter() > deadline:
            return replace(comparison, fallback_reason="planning_limit")
        rules = tuple(dict.fromkeys(rule for variant in best for rule in variant.rules))
        if selection_changed:
            rules = (*rules, "R5")
        content_bytes = len(content.encode("utf-8"))
        proposed_origins = tuple(evidence.entry.origin for evidence in selected)
        reason = (
            "target_unmet"
            if tokens > self.target_tokens
            else "not_smaller"
            if tokens >= baseline_tokens
            else "not_smaller_bytes"
            if content_bytes >= baseline_bytes
            else "delivery_increased"
            if tokens > legacy_tokens or content_bytes > legacy_bytes
            else "selection_changed"
            if self.mode == "active"
            and not {_origin_key(origin) for origin in legacy.origins}.issubset(
                _origin_key(origin) for origin in proposed_origins
            )
            else None
        )
        proposed = PreparedContextBuild(
            context=PreparedContext(status="ready", content=content, content_bytes=content_bytes),
            origins=proposed_origins,
            omissions=PreparedContextOmissions(truncated_items=sum(variant.entry.truncated for variant in best)),
        )
        return replace(
            comparison,
            build=proposed if self.mode == "active" and reason is None else legacy,
            proposed=proposed,
            proposed_tokens=tokens,
            proposed_bytes=content_bytes,
            rules=rules,
            fallback_reason=reason,
            selection_changed=selection_changed,
            omitted_sections=sum(variant.omitted_sections for variant in best),
        )


class _PlanningError(Exception):
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


def _plan(
    counter: TokenEstimator,
    variants: tuple[tuple[_Variant, ...], ...],
    selected: tuple[_Evidence, ...],
    request: PrepareContextRequest,
    deadline: float,
    *,
    allow_selection: bool,
) -> tuple[int, tuple[_Variant, ...]]:
    if not allow_selection and all(choices and all(not variant.rules for variant in choices) for choices in variants):
        raise _PlanningError("not_smaller")
    costs: dict[bytes, int] = {}

    count = partial(_rendered_token_count, counter, costs)

    variants = _nondominated_variants(variants, selected, request, count, deadline)
    states: list[tuple[_Variant, ...]] = [()]
    for index, choices in enumerate(variants):
        expanded: list[tuple[int, int, tuple[_Variant, ...]]] = []
        for state in states:
            for choice in choices:
                if perf_counter() > deadline:
                    raise _PlanningError("planning_limit")
                plan = (*state, choice)
                content = _render_plan(plan, selected[: index + 1], request)
                content_bytes = len(content.encode("utf-8"))
                if content_bytes <= request.max_bytes:
                    expanded.append((count(content), content_bytes, plan))
        if not expanded:
            raise _PlanningError("protected_group_unfit")
        expanded.sort(key=lambda candidate: candidate[:2])
        states = [candidate[2] for candidate in expanded[:_BEAM_STATES]]
    # Recount complete renders: BPE fragment costs are not additive.
    measured = []
    for plan in states:
        if perf_counter() > deadline:
            raise _PlanningError("planning_limit")
        measured.append((count(_render_plan(plan, selected, request)), plan))
    return min(measured, key=lambda candidate: candidate[0])


def _nondominated_variants(
    variants: tuple[tuple[_Variant, ...], ...],
    selected: tuple[_Evidence, ...],
    request: PrepareContextRequest,
    count: Callable[[str], int],
    deadline: float,
) -> tuple[tuple[_Variant, ...], ...]:
    """Keep one complete and one lossy representation using full-plan costs.

    This is bounded contextual pruning, not a proof of global BPE optimality.
    The final joint render/gain check remains authoritative. Distinct coverage
    classes are never treated as interchangeable representations.
    """

    if any(not choices for choices in variants):
        raise _PlanningError("protected_group_unfit")
    seed = [min(choices, key=lambda variant: len(variant.entry.content.encode("utf-8"))) for choices in variants]
    result = []
    for index, choices in enumerate(variants):
        best: dict[bool, tuple[int, int, _Variant]] = {}
        for variant in choices:
            if perf_counter() > deadline:
                raise _PlanningError("planning_limit")
            plan = (*seed[:index], variant, *seed[index + 1 :])
            content = _render_plan(plan, selected, request)
            cost = count(content), len(content.encode("utf-8"))
            group = variant.entry.truncated
            if group not in best or cost < best[group][:2]:
                best[group] = (*cost, variant)
        kept = tuple(candidate[2] for candidate in best.values())
        result.append(kept)
        seed[index] = min(best.values(), key=lambda candidate: candidate[:2])[2]
    return tuple(result)


def _rendered_token_count(counter: TokenEstimator, costs: dict[bytes, int], content: str) -> int:
    # Per-call digests retain no historical text. Each value was measured as
    # a complete render; fragment costs are never added together.
    key = hashlib.sha256(content.encode("utf-8")).digest()
    if key not in costs:
        costs[key] = counter.estimate(content)
    return costs[key]


def _origin_key(origin: ArtifactRef | ArtifactAddress) -> _OriginKey:
    if isinstance(origin, ArtifactAddress):
        ref = origin.artifact
        return origin.scope_id, ref.family, ref.artifact_id, ref.revision
    return None, origin.family, origin.artifact_id, origin.revision


def _snapshot(
    builder: PreparedContextBuilder,
    request: PrepareContextRequest,
    current_scope_id: str | None,
    memory: Sequence[PreparedMemoryCandidates],
    topics: Sequence[TopicMemorySearchHit],
    experiences: Sequence[PreparedExperienceCandidates],
    profiles: Sequence[PreparedProfileCandidate],
) -> tuple[_Evidence, ...]:
    """Reuse native entry construction before truncation, retaining display ranks."""

    def scope_id(value: str) -> str | None:
        return value if request.assembly is not None else None if value == current_scope_id else value or None

    memory_groups = tuple(builder._memory_entries(group.hits, scope_id=scope_id(group.scope_id)) for group in memory)
    experience_groups = tuple(
        builder._experience_entries(group.hits, scope_id=scope_id(group.scope_id)) for group in experiences
    )
    topic_entries = builder._topic_memory_entries(
        topics, scope_id=current_scope_id if request.assembly is not None else None
    )
    sources = _source_values(builder, request, current_scope_id, topics, experiences)
    if request.assembly is None:
        entries = _interleave(
            _interleave_groups(memory_groups),
            topic_entries,
            _interleave_groups(experience_groups)[: builder.experience_entry_limit],
        )
        return tuple(
            _Evidence(entry, index + 1, sources.get(_origin_key(entry.origin)))
            for index, entry in enumerate(entries[: builder.entry_limit])
        )
    result: list[_Evidence] = []
    for section in request.assembly.sections:
        if section.family == "topic-memory":
            entries = topic_entries
        elif section.family == "profile":
            entries = builder._profile_entries(profiles)
        else:
            # Interleave before deduplicating, exactly as legacy Markdown does.
            groups = (
                tuple(
                    tuple(builder._memory_entries((hit,), scope_id=group.scope_id) for hit in group.hits)
                    for group in memory
                )
                if section.family == "memory"
                else tuple(
                    tuple(builder._experience_entries((hit,), scope_id=group.scope_id) for hit in group.hits)
                    for group in experiences
                )
            )
            entries = tuple(chain.from_iterable(_interleave_groups(groups)))
        seen: set[_OriginKey] = set()
        rank = 0
        for entry in entries:
            if rank >= section.limit:
                break
            key = _origin_key(entry.origin)
            if key in seen or not entry.content.strip():
                continue
            seen.add(key)
            rank += 1
            normalized = replace(entry, content="".join(_body_chunks(entry.content)))
            result.append(_Evidence(normalized, rank, sources.get(key)))
    return tuple(result)


def _source_values(
    builder: PreparedContextBuilder,
    request: PrepareContextRequest,
    current_scope_id: str | None,
    topics: Sequence[TopicMemorySearchHit],
    experiences: Sequence[PreparedExperienceCandidates],
) -> dict[_OriginKey, ExperienceContent | TopicMemorySearchHit]:
    """Associate native source values with their existing scoped identities."""

    def scope_id(value: str) -> str | None:
        return value if request.assembly is not None else None if value == current_scope_id else value or None

    sources: dict[_OriginKey, ExperienceContent | TopicMemorySearchHit] = {}
    for group in experiences:
        for hit in group.hits:
            for entry in builder._experience_entries((hit,), scope_id=scope_id(group.scope_id)):
                sources.setdefault(_origin_key(entry.origin), hit.content)
    for hit in topics:
        for entry in builder._topic_memory_entries(
            (hit,), scope_id=current_scope_id if request.assembly is not None else None
        ):
            sources.setdefault(_origin_key(entry.origin), hit)
    return sources


def _render_plan(plan: tuple[_Variant, ...], evidence: tuple[_Evidence, ...], request: PrepareContextRequest) -> str:
    if request.assembly is None:
        return _render(tuple(variant.entry for variant in plan))
    return render_context_text(
        tuple(
            replace(_text_item(variant.entry), recall_rank=item.rank, truncated=variant.entry.truncated)
            for variant, item in zip(plan, evidence, strict=True)
        ),
        request.assembly,
    )


def _variants(evidence: _Evidence, query: str, allow_excerpts: bool, *, markdown: bool) -> tuple[_Variant, ...]:
    entry = evidence.entry
    candidates = [_Variant(entry)]
    source = evidence.source
    if isinstance(source, ExperienceContent):
        candidates.extend(_experience_variants(entry, source))
    elif isinstance(source, TopicMemorySearchHit):
        candidates.extend(_topic_variants(entry, source, query, allow_excerpts))
    result: list[_Variant] = []
    seen: set[tuple[str, bool]] = set()
    for variant in candidates:
        body = "".join(_body_chunks(variant.entry.content)) if markdown else variant.entry.content
        key = body, variant.entry.truncated
        if key not in seen and len(body.encode("utf-8")) <= PreparedContextBuilder.max_entry_content_bytes:
            seen.add(key)
            result.append(replace(variant, entry=replace(variant.entry, content=body)))
    return tuple(result[:4])


def _experience_variants(entry: _PreparedContextEntry, source: ExperienceContent) -> list[_Variant]:
    candidates: list[_Variant] = []
    grouped = _experience_body(source, group=True, tables=False)
    tabular = _experience_body(source, group=False, tables=True)
    combined = _experience_body(source, group=True, tables=True)
    for body, rules in ((grouped, ("R1",)), (tabular, ("R3",)), (combined, ("R1", "R3"))):
        if body != entry.content:
            candidates.append(_Variant(replace(entry, content=body), rules))
    return candidates


def _topic_variants(
    entry: _PreparedContextEntry,
    source: TopicMemorySearchHit,
    query: str,
    allow_excerpts: bool,
) -> list[_Variant]:
    candidates: list[_Variant] = []
    if source.snippet is not None and source.snippet == source.summary:
        body = _topic_body(
            source,
            summary=source.summary,
            snippet="Identical to the complete Summary above.",
            scoped=isinstance(entry.origin, ArtifactAddress),
        )
        candidates.append(_Variant(replace(entry, content=body), ("R2",)))
    table = _json_table(source.summary)
    if table is not None:
        body = _topic_body(
            source, summary=table, snippet=source.snippet, scoped=isinstance(entry.origin, ArtifactAddress)
        )
        candidates.append(_Variant(replace(entry, content=body), ("R3",)))
        if source.snippet is not None and source.snippet == source.summary:
            # The match covered the entire original JSON field. Every value and
            # row survives in this validated table, so its whole-field role can
            # point to that representation without inventing a substring range.
            snippet = "Matches the entire original JSON Summary, represented by the complete table above."
            body = _topic_body(source, summary=table, snippet=snippet, scoped=isinstance(entry.origin, ArtifactAddress))
            candidates.append(_Variant(replace(entry, content=body), ("R2", "R3")))
    if allow_excerpts and (excerpt := extract_topic_excerpt(source.summary, query)) is not None:
        summary = (
            f"Task-relevant excerpt; {len(excerpt.omitted_sections)} complete sections omitted.\n{excerpt.content}"
        )
        snippet = source.snippet
        if snippet == source.summary:
            snippet = "Matches the complete original Summary; sections are omitted in this excerpt."
        body = _topic_body(source, summary=summary, snippet=snippet, scoped=isinstance(entry.origin, ArtifactAddress))
        candidates.append(
            _Variant(replace(entry, content=body, truncated=True), ("R4",), len(excerpt.omitted_sections))
        )
    return candidates


def _experience_body(content: ExperienceContent, *, group: bool, tables: bool) -> str:
    values: dict[str, list[str]] = {}
    lines = []
    for name in ("situation", "action", "outcome", "lesson"):
        value = getattr(content, name)
        if group:
            values.setdefault(value, []).append(name.capitalize())
        else:
            lines.append(f"{name.capitalize()}: {(_json_table(value) or value) if tables else value}")
    if group:
        lines = [
            f"{' / '.join(roles)}: {(_json_table(value) or value) if tables else value}"
            for value, roles in values.items()
        ]
    if content.failure is not None:
        failure = content.failure
        lines.append(f"Failure cue: {failure.signature.recall_cue}")
        if failure.signature.symptom is not None:
            lines.append(f"Symptom: {failure.signature.symptom}")
        lines.extend((
            f"Repair surface: {failure.repair_surface}",
            f"Verification condition: {failure.verification.condition}",
            f"Verification check: {failure.verification.check_subject}",
        ))
    return "\n".join(lines)


def _topic_body(hit: TopicMemorySearchHit, *, summary: str, snippet: str | None, scoped: bool) -> str:
    fields = {"title": hit.title, "summary": summary}
    if snippet is not None:
        fields["snippet"] = snippet
    return (
        "\n\n".join(f"{label.capitalize()}: {value}" for label, value in fields.items())
        if scoped
        else json.dumps(fields, ensure_ascii=False, separators=(",", ":"))
    )


class _JsonNumber(str):
    """Keep numeric lexemes; never coerce a large integer or decimal to float."""


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON field")  # noqa: TRY003
        result[key] = value
    return result


def _reject_constant(value: str) -> object:
    raise ValueError("non-JSON numeric constant")  # noqa: TRY003


def _json_table(text: str) -> str | None:
    if not text.lstrip().startswith("["):
        return None
    try:
        rows = json.loads(
            text,
            object_pairs_hook=_unique_object,
            parse_int=_JsonNumber,
            parse_float=_JsonNumber,
            parse_constant=_reject_constant,
        )
    except (ValueError, RecursionError):
        return None
    if not isinstance(rows, list) or len(rows) < 2 or not all(isinstance(row, dict) and row for row in rows):
        return None
    keys = tuple(rows[0])
    if any(tuple(row) != keys for row in rows):
        return None
    if any(not isinstance(value, (str, bool, type(None))) for row in rows for value in row.values()):
        return None

    def scalar(value: object) -> str:
        return (
            str(value)
            if isinstance(value, _JsonNumber)
            else json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        )

    rendered = ["[" + ",".join(scalar(value) for value in row.values()) + "]" for row in rows]
    # Decoding every row with the same lexical parser proves type/value/order
    # preservation, including repeated rows and strings containing separators.
    decoded = [json.loads(row, parse_int=_JsonNumber, parse_float=_JsonNumber) for row in rendered]
    if decoded != [list(row.values()) for row in rows]:
        return None
    return "Complete table\nColumns: " + json.dumps(keys, ensure_ascii=False) + "\nRows:\n" + "\n".join(rendered)


def _select_coverage(
    plan: tuple[_Variant, ...], evidence: tuple[_Evidence, ...], query: str
) -> tuple[tuple[_Variant, ...], tuple[_Evidence, ...]] | None:
    covered: dict[tuple[str | None, int], set[str]] = {}
    selected_plan: list[_Variant] = []
    selected_evidence: list[_Evidence] = []
    for variant, item in zip(plan, evidence, strict=True):
        coverage = (
            describe_topic_coverage(item.source.summary, query)
            if isinstance(item.source, TopicMemorySearchHit)
            else None
        )
        key = _origin_key(item.entry.origin)
        partition = key[0], key[3]
        if coverage is None or coverage.preserve_all_sources or coverage.protected_features:
            selected_plan.append(variant)
            selected_evidence.append(item)
            continue
        known = covered.setdefault(partition, set())
        if not coverage.features.issubset(known):
            selected_plan.append(variant)
            selected_evidence.append(item)
            known.update(coverage.features)
    if len(selected_plan) == len(plan) or not selected_plan:
        return None
    return tuple(selected_plan), tuple(selected_evidence)
