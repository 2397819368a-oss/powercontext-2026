# Copyright (c) 2026 OceanBase.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0

"""Deterministic retention and instruction-leakage scoring."""

from __future__ import annotations

import re

from .models import CaseObservation, CaseScore, EvaluationCase

_SECOND_PERSON = re.compile(r"\b(?:you|your|yours|yourself|yourselves)\b|你(?:们|的)?", flags=re.IGNORECASE)
_IMPERATIVE_PREFIX = re.compile(
    r"^(?:(?:always|never)\s+(?:use|run|delete|remove|retry|inspect|upload|ignore|keep|preserve|configure|edit)\s+|"
    r"(?:use(?!\s+of\b)|run|delete|remove|retry|inspect|upload|ignore|keep|preserve|configure|edit)\s+|"
    r"do\s+not\b|don't\b|remember\s+to\b|make\s+sure\b|please\b|"
    r"(?:请|务必|不要|始终|使用|运行|删除|移除|重试|检查|上传|忽略|记住|确保|保留|设置|配置|编辑))",
    flags=re.IGNORECASE,
)


def score_case(case: EvaluationCase, observation: CaseObservation) -> CaseScore:
    """Score one exact observation without exposing candidate content."""

    if observation.case_id != case.case_id:
        raise ValueError("observation case ID does not match the evaluation case")  # noqa: TRY003

    failures: set[str] = set()
    if observation.status == "failed":
        failures.add("generation_error")

    leakage_passed = observation.status == "succeeded" and not _has_instruction_leakage(case, observation)
    if not leakage_passed and observation.status == "succeeded":
        failures.add("instruction_leakage")

    retention_applicable = case.expected.disposition == "atomic_memory"
    retention_passed = True
    if retention_applicable and observation.status == "succeeded":
        retention_failures: set[str] = set()
        retention_passed = _score_retention(case, observation, retention_failures)
        failures.update(retention_failures)
    elif retention_applicable:
        retention_passed = False
    elif observation.status == "succeeded" and observation.candidates:
        failures.add("unexpected_candidate")

    passed = observation.status == "succeeded" and leakage_passed and retention_passed and not failures
    return CaseScore(
        case_id=case.case_id,
        disposition=case.expected.disposition,
        passed=passed,
        factual_retention_applicable=retention_applicable,
        factual_retention_passed=retention_passed if observation.status == "succeeded" else False,
        instruction_leakage_passed=leakage_passed,
        failure_classes=tuple(sorted(failures)),
    )


def _score_retention(case: EvaluationCase, observation: CaseObservation, failures: set[str]) -> bool:
    candidates = observation.candidates
    if not candidates:
        failures.add("false_rejection")
        return False

    subjects = case.expected.required_subjects
    combined = "\n".join(candidate.text for candidate in candidates)
    if any(not any(_contains_subject(candidate.text, subject) for subject in subjects) for candidate in candidates) or (
        not all(_contains_subject(combined, subject) for subject in subjects)
    ):
        failures.add("subject_missing")
    if any(_literal_value(literal) not in combined for literal in case.expected.required_literals):
        failures.add("literal_loss")
    actual_kinds = {candidate.kind for candidate in candidates}
    if case.expected.required_kinds and actual_kinds != set(case.expected.required_kinds):
        failures.add("kind_mismatch")
    cited = {evidence_id for candidate in candidates for evidence_id in candidate.evidence_ids}
    if cited != set(case.expected.required_evidence_ids):
        failures.add("evidence_mismatch")
    supplied = {item.evidence_id for item in case.evidence}
    if any(not candidate.evidence_ids or not set(candidate.evidence_ids) <= supplied for candidate in candidates):
        failures.add("evidence_mismatch")
    if case.expected.max_candidates is not None and len(candidates) > case.expected.max_candidates:
        failures.add("candidate_overproduction")
    return not failures


def _has_instruction_leakage(case: EvaluationCase, observation: CaseObservation) -> bool:
    if observation.status != "succeeded":
        return False
    forbidden = tuple(fragment.casefold() for fragment in case.expected.forbidden_output_fragments)
    for candidate in observation.candidates:
        stripped = candidate.text.lstrip(" \t\r\n-_*`\"'")
        folded = candidate.text.casefold()
        if (
            _SECOND_PERSON.search(candidate.text)
            or _IMPERATIVE_PREFIX.match(stripped)
            or any(fragment in folded for fragment in forbidden)
        ):
            return True
    return False


def _contains_subject(text: str, subject: str) -> bool:
    if re.search(r"[A-Za-z0-9_]", subject):
        boundary = rf"(?<![A-Za-z0-9_]){re.escape(subject)}(?![A-Za-z0-9_])"
        return re.search(boundary, text, flags=re.IGNORECASE) is not None
    return subject.casefold() in text.casefold()


def _literal_value(literal: str) -> str:
    code_span = re.fullmatch(r"`([^`\r\n]+)`", literal)
    return code_span.group(1) if code_span is not None else literal


__all__ = ["score_case"]
