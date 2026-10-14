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

"""Observable offline evaluation behavior for Memory write-gate promotion evidence."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import cast

import pytest

from evaluation.memory.write_gate.dataset import (
    ArtifactRef,
    Case,
    Dataset,
    Label,
    SourceRef,
    validate_split_pair,
)
from evaluation.memory.write_gate.report import calibration_report, report
from evaluation.memory.write_gate.resolver import AuthorizedClientResolver, MissingInput, ResolvedCase
from evaluation.memory.write_gate.runner import EvaluationStatus, evaluate_dataset
from powercontext.builtin.decision_observations import DecisionCoverage
from powercontext.builtin.inference import InferenceUsage
from powercontext.builtin.runtime.decision_model import DecisionOutcome, DecisionResult
from powercontext.client.client import PowerContextClient
from powercontext.client.errors import ForbiddenResponseError

DIGEST = "sha256:" + "a" * 64


class _Resolver:
    async def resolve(self, case: Case) -> ResolvedCase:
        return ResolvedCase(candidate=f"Candidate {case.case_id}", evidence=("会议决定在周五发布。",))


class _Model:
    policy_id = "test.write-gate"
    is_local_only = True

    def __init__(self, *, used_fallback: bool = False):
        self.used_fallback = used_fallback
        self.outcomes = iter(("no", "yes", "yes", "yes", "no", "yes", "no", "abstain"))

    async def evaluate(self, request):
        return DecisionResult(
            outcome=DecisionOutcome(next(self.outcomes)),
            confidence=0.8,
            policy_id=self.policy_id,
            used_fallback=self.used_fallback,
            usage=InferenceUsage(requests=1),
        )


def _datasets():
    datasets = []
    for split in ("calibration", "held_out"):
        cases = tuple(
            Case(
                case_id=f"{split}-{index}",
                group_id=f"{split}-{index}",
                scope_id="test-scope",
                language="en" if index < 4 else "zh",
                label=Label.ACCEPT if index % 2 == 0 else Label.HOLD,
                label_reason="test_support",
                subject=ArtifactRef(artifact_id=f"{split}-{index}", revision=1, content_digest=DIGEST),
                evidence=(ArtifactRef(artifact_id="evidence", revision=1, content_digest=DIGEST),),
            )
            for index in range(8)
        )
        datasets.append(Dataset(dataset_id="test", split=split, cases=cases))
    validate_split_pair(*datasets)
    return *datasets, {}


def test_calibration_and_held_out_report_fallback_separately() -> None:
    calibration, held_out, hashes = _datasets()
    resolver = _Resolver()

    async def scenario():
        return (
            await evaluate_dataset(calibration, resolver, model=_Model()),
            await evaluate_dataset(held_out, resolver, model=_Model()),
        )

    calibration_results, held_results = asyncio.run(scenario())
    result = report(
        calibration_results,
        held_results,
        dataset_id=calibration.dataset_id,
        dataset_hashes=hashes,
        code_commit="test",
        execution="controlled_model",
        selected_configuration="hold_on=yes;threshold=none",
    )

    assert result["selected_configuration"] == "hold_on=yes;threshold=none"
    assert result["held_out"]["overall"]["status_counts"] == {"evaluated": 7, "evaluation_fallback": 1}
    assert result["held_out"]["overall"]["false_holds"] == 1
    assert result["held_out"]["overall"]["effective_false_passes_including_fallback"] == 1
    assert result["held_out"]["overall"]["cost"]["model_requests"] == 8
    assert result["held_out"]["en"]["false_holds"] == 1
    assert result["held_out"]["zh"]["false_holds"] == 0
    assert result["promotion_decision"] == "not_assessed"
    assert result["held_out"]["overall"]["hold_count"] == 4
    assert result["held_out"]["overall"]["action_denominator"] == 8
    serialized = json.dumps(result, ensure_ascii=False)
    assert "Candidate held_out-0" not in serialized
    assert "会议决定" not in serialized


def test_calibration_requires_an_explicit_configuration_before_held_out_scoring() -> None:
    calibration, held_out, hashes = _datasets()
    resolver = _Resolver()

    async def scenario():
        return (
            await evaluate_dataset(calibration, resolver, model=_Model()),
            await evaluate_dataset(held_out, resolver, model=_Model()),
        )

    calibration_results, held_results = asyncio.run(scenario())
    only = calibration_report(
        calibration_results,
        dataset_id=calibration.dataset_id,
        dataset_hashes=hashes,
        code_commit="test",
        execution="controlled_model",
    )
    assert only["phase"] == "calibration_only"
    assert "held_out" not in only
    with pytest.raises(ValueError, match="declare a calibration configuration"):
        report(
            calibration_results,
            held_results,
            dataset_id=calibration.dataset_id,
            dataset_hashes=hashes,
            code_commit="test",
            execution="controlled_model",
        )


def test_missing_input_is_not_scored_as_negative() -> None:
    class MissingResolver:
        async def resolve(self, case):
            raise MissingInput

    calibration, _, _ = _datasets()
    result = asyncio.run(evaluate_dataset(calibration, MissingResolver(), model=_Model()))
    assert {row.status for row in result} == {EvaluationStatus.MISSING_INPUT}
    assert all(row.actions is None for row in result)


def test_no_call_and_backend_fallback_are_not_counted_as_adjudication() -> None:
    calibration, _, _ = _datasets()
    resolver = _Resolver()
    no_call = asyncio.run(evaluate_dataset(calibration, resolver))
    assert {row.status for row in no_call} == {EvaluationStatus.EVALUATION_FALLBACK}
    assert all(row.coverage is DecisionCoverage.UNADJUDICATED and row.used_fallback for row in no_call)
    no_call_report = report(
        no_call,
        no_call,
        dataset_id=calibration.dataset_id,
        dataset_hashes={},
        code_commit="test",
        execution="no_call",
    )
    assert no_call_report["selected_configuration"] is None
    assert no_call_report["selected_policy_version"] is None
    assert no_call_report["held_out"]["overall"]["adjudication_coverage"] == 0

    backend_fallback = asyncio.run(evaluate_dataset(calibration, resolver, model=_Model(used_fallback=True)))
    assert backend_fallback[0].status is EvaluationStatus.EVALUATION_FALLBACK
    assert backend_fallback[0].coverage is DecisionCoverage.UNADJUDICATED
    assert backend_fallback[0].used_fallback is True


def test_rejects_scenario_group_leakage_between_splits() -> None:
    calibration, held_out, _ = _datasets()
    copied = held_out.cases[0].model_copy(update={"group_id": calibration.cases[0].group_id})
    bad = held_out.model_copy(update={"cases": (copied, *held_out.cases[1:])})
    with pytest.raises(ValueError, match="scenario groups"):
        validate_split_pair(calibration, bad)


def _remote_case(*, evidence: tuple[SourceRef | ArtifactRef, ...]) -> Case:
    return Case(
        case_id="remote-1",
        group_id="remote-group",
        scope_id="scope-a",
        language="en",
        label=Label.ACCEPT,
        label_reason="direct_support",
        subject=ArtifactRef(artifact_id="memory-1", revision=1, content_digest=DIGEST),
        evidence=evidence,
    )


def test_current_authorization_failure_is_reported_separately() -> None:
    class DenyingClient:
        async def get_artifact_revision(self, scope_id, family, artifact_id, revision):
            assert scope_id == "scope-a"
            raise ForbiddenResponseError(status_code=403, request_id=None)

    case = _remote_case(evidence=(ArtifactRef(artifact_id="evidence", revision=1, content_digest=DIGEST),))
    dataset = Dataset(dataset_id="remote", split="calibration", cases=(case,))
    result = asyncio.run(evaluate_dataset(dataset, AuthorizedClientResolver(cast(PowerContextClient, DenyingClient()))))
    assert result[0].status is EvaluationStatus.UNAUTHORIZED
    assert result[0].actions is None


def test_exact_revision_and_citations_are_required_before_model_call() -> None:
    class ChangedClient:
        async def get_artifact_revision(self, scope_id, family, artifact_id, revision):
            assert scope_id == "scope-a"
            return SimpleNamespace(
                scope_id=scope_id,
                family=family,
                artifact_id=artifact_id,
                revision=revision,
                content_digest="sha256:" + "b" * 64,
                content={"text": "changed"},
                sources=[],
                artifacts=[],
            )

    case = _remote_case(evidence=(ArtifactRef(artifact_id="evidence", revision=1, content_digest=DIGEST),))
    dataset = Dataset(dataset_id="remote", split="calibration", cases=(case,))
    result = asyncio.run(evaluate_dataset(dataset, AuthorizedClientResolver(cast(PowerContextClient, ChangedClient()))))
    assert result[0].status is EvaluationStatus.MISSING_INPUT


def test_current_scope_and_exact_citations_cannot_be_substituted() -> None:
    class IncorrectClient:
        def __init__(self, scope_id: str) -> None:
            self.scope_id = scope_id

        async def get_artifact_revision(self, scope_id, family, artifact_id, revision):
            return SimpleNamespace(
                scope_id=self.scope_id,
                family=family,
                artifact_id=artifact_id,
                revision=revision,
                content_digest=DIGEST,
                content={"text": "candidate"},
                sources=[],
                artifacts=[SimpleNamespace(family="atomic-memory", artifact_id="other", revision=1)],
            )

    case = _remote_case(evidence=(ArtifactRef(artifact_id="evidence", revision=1, content_digest=DIGEST),))
    dataset = Dataset(dataset_id="remote", split="calibration", cases=(case,))
    wrong_scope = asyncio.run(
        evaluate_dataset(dataset, AuthorizedClientResolver(cast(PowerContextClient, IncorrectClient("scope-b"))))
    )
    wrong_citation = asyncio.run(
        evaluate_dataset(dataset, AuthorizedClientResolver(cast(PowerContextClient, IncorrectClient("scope-a"))))
    )
    assert wrong_scope[0].status is EvaluationStatus.MISSING_INPUT
    assert wrong_citation[0].status is EvaluationStatus.UNSUPPORTED_REFERENCE


def test_source_read_without_exact_gate_projection_is_incomplete() -> None:
    source = SourceRef(source_id="source-1", content_digest=DIGEST)

    class SourceClient:
        async def get_artifact_revision(self, scope_id, family, artifact_id, revision):
            return SimpleNamespace(
                scope_id=scope_id,
                family=family,
                artifact_id=artifact_id,
                revision=revision,
                content_digest=DIGEST,
                content={"text": "candidate"},
                sources=[SimpleNamespace(source_type="content", source_id="source-1")],
                artifacts=[],
            )

        async def get_source(self, scope_id, source_type, source_id):
            return SimpleNamespace(
                scope_id=scope_id,
                source_type=source_type,
                source_id=source_id,
                content_digest=DIGEST,
                content="source text",
            )

    case = _remote_case(evidence=(source,))
    dataset = Dataset(dataset_id="remote", split="calibration", cases=(case,))
    result = asyncio.run(evaluate_dataset(dataset, AuthorizedClientResolver(cast(PowerContextClient, SourceClient()))))
    assert result[0].status is EvaluationStatus.UNSUPPORTED_REFERENCE


def test_uncommitted_candidate_reference_is_incomplete() -> None:
    case = Case(
        case_id="pending",
        group_id="pending",
        scope_id="scope-a",
        language="en",
        label=Label.HOLD,
        label_reason="unsupported",
        subject=SourceRef(source_id="candidate:1", content_digest=DIGEST),
        evidence=(SourceRef(source_id="source-1", content_digest=DIGEST),),
    )
    dataset = Dataset(dataset_id="remote", split="calibration", cases=(case,))
    result = asyncio.run(evaluate_dataset(dataset, AuthorizedClientResolver(cast(PowerContextClient, object()))))
    assert result[0].status is EvaluationStatus.UNSUPPORTED_REFERENCE
