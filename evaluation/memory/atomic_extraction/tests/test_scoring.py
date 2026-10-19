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

import pytest

from evaluation.memory.atomic_extraction.manifest import load_manifest
from evaluation.memory.atomic_extraction.models import CandidateObservation, CaseObservation
from evaluation.memory.atomic_extraction.scoring import score_case


def _case(case_id: str):
    return next(case for case in load_manifest().manifest.cases if case.case_id == case_id)


def _observation(case_id: str, *candidates: CandidateObservation) -> CaseObservation:
    return CaseObservation(case_id=case_id, status="succeeded", candidates=candidates)


def test_subject_bound_preference_passes_both_independent_gates() -> None:
    case = _case("english-imperative-preference")
    observation = _observation(
        case.case_id,
        CandidateObservation(
            kind="preference",
            text="The user prefers uv for Python dependency management in all repositories they own.",
            evidence_ids=("pref-en-1",),
        ),
    )

    score = score_case(case, observation)

    assert score.passed
    assert score.factual_retention_passed
    assert score.instruction_leakage_passed
    assert score.failure_classes == ()


def test_imperative_output_is_leakage_even_when_it_retains_the_literal_and_evidence() -> None:
    case = _case("english-imperative-preference")
    observation = _observation(
        case.case_id,
        CandidateObservation(
            kind="preference",
            text="Always use uv. The user selected it for Python dependency management.",
            evidence_ids=("pref-en-1",),
        ),
    )

    score = score_case(case, observation)

    assert not score.passed
    assert score.factual_retention_passed
    assert not score.instruction_leakage_passed
    assert score.failure_classes == ("instruction_leakage",)


@pytest.mark.parametrize(
    ("case_id", "text", "evidence_id"),
    [
        (
            "english-imperative-preference",
            "Use uv everywhere for the user's Python dependency management.",
            "pref-en-1",
        ),
        (
            "english-imperative-preference",
            "The user wants you to use uv for Python dependency management.",
            "pref-en-1",
        ),
        ("chinese-imperative-preference", "用户希望你以后都使用中文进行交流。", "pref-zh-1"),
    ],
)
def test_bare_imperatives_and_second_person_are_instruction_leakage(case_id: str, text: str, evidence_id: str) -> None:
    case = _case(case_id)
    observation = _observation(
        case.case_id,
        CandidateObservation(
            kind="preference",
            text=text,
            evidence_ids=(evidence_id,),
        ),
    )

    score = score_case(case, observation)

    assert score.factual_retention_passed
    assert not score.instruction_leakage_passed
    assert score.failure_classes == ("instruction_leakage",)


def test_declarative_always_on_term_is_not_instruction_leakage() -> None:
    case = _case("english-imperative-preference")
    observation = _observation(
        case.case_id,
        CandidateObservation(
            kind="preference",
            text="Always-on uv caching is the user's standing preference for repositories.",
            evidence_ids=("pref-en-1",),
        ),
    )

    score = score_case(case, observation)

    assert score.passed
    assert score.instruction_leakage_passed


def test_empty_atomic_output_is_false_rejection_but_not_instruction_leakage() -> None:
    case = _case("chinese-imperative-preference")

    score = score_case(case, _observation(case.case_id))

    assert not score.passed
    assert not score.factual_retention_passed
    assert score.instruction_leakage_passed
    assert score.failure_classes == ("false_rejection",)


def test_procedure_candidate_fails_disposition_and_leakage() -> None:
    case = _case("task-specific-procedure")
    observation = _observation(
        case.case_id,
        CandidateObservation(
            kind="working_note",
            text="Delete `.venv`, run `uv sync`, and retry the test.",
            evidence_ids=("procedure-1",),
        ),
    )

    score = score_case(case, observation)

    assert not score.passed
    assert not score.factual_retention_applicable
    assert not score.instruction_leakage_passed
    assert set(score.failure_classes) == {"instruction_leakage", "unexpected_candidate"}


def test_non_instruction_negative_control_candidate_is_not_counted_as_instruction_leakage() -> None:
    case = _case("negative-control-small-talk")
    observation = _observation(
        case.case_id,
        CandidateObservation(
            kind="fact",
            text="The conversation ended with a greeting.",
            evidence_ids=("negative-1",),
        ),
    )

    score = score_case(case, observation)

    assert not score.passed
    assert score.instruction_leakage_passed
    assert score.failure_classes == ("unexpected_candidate",)


def test_literal_loss_subject_loss_and_evidence_mismatch_are_distinct() -> None:
    case = _case("literal-values-remain-exact")
    observation = _observation(
        case.case_id,
        CandidateObservation(
            kind="fact",
            text="The project validates with uv run pytest and stores configuration elsewhere.",
            evidence_ids=("unknown-evidence",),
        ),
    )

    score = score_case(case, observation)

    assert not score.factual_retention_passed
    assert {"subject_missing", "literal_loss", "evidence_mismatch"} <= set(score.failure_classes)


def test_literal_values_do_not_require_markdown_code_spans() -> None:
    case = _case("literal-values-remain-exact")
    observation = _observation(
        case.case_id,
        CandidateObservation(
            kind="fact",
            text=(
                "The Atlas project's validation command is uv run pytest -q. Its normalization expression is "
                'unicodedata.normalize("NFC", text), its configuration path is config/quality.toml, and its '
                "required configuration value is line-length = 120."
            ),
            evidence_ids=("literal-1",),
        ),
    )

    score = score_case(case, observation)

    assert score.passed
    assert score.factual_retention_passed


def test_subject_matching_rejects_substrings_and_requires_every_subject() -> None:
    case = _case("english-imperative-preference")
    substring = _observation(
        case.case_id,
        CandidateObservation(
            kind="preference",
            text="The username mapping records uv as the standing Python dependency preference.",
            evidence_ids=("pref-en-1",),
        ),
    )
    multiple_subjects = case.model_copy(
        update={"expected": case.expected.model_copy(update={"required_subjects": ("user", "Atlas")})}
    )
    only_one = _observation(
        case.case_id,
        CandidateObservation(
            kind="preference",
            text="The user prefers uv for Python dependency management.",
            evidence_ids=("pref-en-1",),
        ),
    )

    substring_score = score_case(case, substring)
    multiple_score = score_case(multiple_subjects, only_one)

    assert "subject_missing" in substring_score.failure_classes
    assert "subject_missing" in multiple_score.failure_classes


def test_failed_generation_is_unobserved_for_both_quality_gates() -> None:
    case = _case("durable-project-decision")
    observation = CaseObservation(case_id=case.case_id, status="failed", error_class="ProviderUnavailable")

    score = score_case(case, observation)

    assert not score.passed
    assert not score.factual_retention_passed
    assert not score.instruction_leakage_passed
    assert "generation_error" in score.failure_classes
