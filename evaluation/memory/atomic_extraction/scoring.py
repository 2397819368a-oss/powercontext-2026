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

"""Deterministic retention and instruction-leakage scoring."""

from __future__ import annotations

import re

from .models import CaseObservation, CaseScore, EvaluationCase

_SECOND_PERSON = re.compile(r"\b(?:you|your|yours|yourself|yourselves)\b|你(?:们|的)?", flags=re.IGNORECASE)
_IMPERATIVE_PREFIX = re.compile(
    r"^(?:(?:(?:for|in|on|during|after|before|when|whenever)\b[^,\r\n]{1,120},\s*)?(?:and\s+)?"
    r"(?:(?:always|never)\s+)?"
    r"(?:use(?!\s+(?:of|cases?)\b)|run(?!\s+time\b)|delete|remove|retry(?!\s+count\b)|inspect|upload|"
    r"ignore|keep(?!\s+rate\b)|preserve|configure|edit|check|ensure|add|set(?!\s+(?:of|size|theory)\b)|"
    r"create|avoid)\s+|"
    r"do\s+not\b|don't\b|remember\s+to\b|make\s+sure\b|please\b|"
    r"(?:请|务必|不要|始终|记住|确保)|"
    r"使用(?![^。\uFF01\uFF1F\uFF1B\r\n]{0,40}(?:是|为|属于|作为|偏好|约束))|"
    r"运行(?!时间|时长|环境|状态)|"
    r"删除(?!操作)|移除(?!操作)|重试(?!次数|计数)|检查(?!结果|状态|报告|记录|项(?!目))|"
    r"上传(?!状态|记录)|忽略(?!列表|规则)|保留(?!率|期限|策略)|"
    r"设置(?!项(?!目)|值|名称|界面)|配置(?!项(?!目)|文件|值|路径|名称)|编辑(?!器|操作|模式))",
    flags=re.IGNORECASE,
)
_SENTENCE_BOUNDARY = re.compile(r"\A|(?<=[.!?;:—。\uFF01\uFF1F\uFF1B\n])")
_IMPERATIVE_CLAUSE_BOUNDARY = re.compile(r"[,\uFF0C]\s*(?:and|then)\s+", flags=re.IGNORECASE)
_SENTENCE_PREFIX_CHARS = " \t\r\n-_*+>`\"'0123456789.)]"


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
        folded = candidate.text.casefold()
        inspected = _mask_expected_literals(candidate.text, case.expected.required_literals)
        if (
            _SECOND_PERSON.search(inspected)
            or _has_imperative_sentence(inspected)
            or any(fragment in folded for fragment in forbidden)
        ):
            return True
    return False


def _has_imperative_sentence(text: str) -> bool:
    return any(
        _IMPERATIVE_PREFIX.match(text[boundary.end() :].lstrip(_SENTENCE_PREFIX_CHARS))
        for boundaries in (_SENTENCE_BOUNDARY.finditer(text), _IMPERATIVE_CLAUSE_BOUNDARY.finditer(text))
        for boundary in boundaries
    )


def _mask_expected_literals(text: str, literals: tuple[str, ...]) -> str:
    variants = {
        variant
        for literal in literals
        for variant in (literal, _literal_value(literal), f"`{_literal_value(literal)}`")
        if variant
    }
    for variant in sorted(variants, key=len, reverse=True):
        text = text.replace(variant, " " * len(variant))
    return text


def _contains_subject(text: str, subject: str) -> bool:
    if re.search(r"[A-Za-z0-9_]", subject):
        boundary = rf"(?<![A-Za-z0-9_]){re.escape(subject)}(?![A-Za-z0-9_])"
        return re.search(boundary, text, flags=re.IGNORECASE) is not None
    return subject.casefold() in text.casefold()


def _literal_value(literal: str) -> str:
    code_span = re.fullmatch(r"`([^`\r\n]+)`", literal)
    return code_span.group(1) if code_span is not None else literal


__all__ = ["score_case"]
