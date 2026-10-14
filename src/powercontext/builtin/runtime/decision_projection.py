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

"""Deterministic, request-only projection safety for decision backends.

The projection boundary is deliberately separate from decision policy. A sanitizer can remove
unsafe text or withhold a request, but it cannot decide whether a Memory write is valid and it
never changes the authoritative object that produced the request.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol, cast

from powercontext.builtin.decision_projection_contract import DecisionProjectionFact
from powercontext.builtin.runtime.decision_model import DecisionRequest
from powercontext.client.capture import REDACTED, known_secret_values, redact_known_secrets

_FACT_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
_MAX_FACTS = 8
_MAX_FACT_LENGTH = 64
_MAX_SUBJECT_LENGTH = 4000
_MAX_EVIDENCE_ITEMS = 32
_MAX_EVIDENCE_LENGTH = 4000
_PROVIDER_SECRET_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:"
    r"sk-[A-Za-z0-9_-]{10,}|ghp_[A-Za-z0-9]{10,}|github_pat_[A-Za-z0-9_]{10,}|"
    r"gh[ourst]_[A-Za-z0-9]{10,}|xapp-\d+-[A-Za-z0-9-]{10,}|xox[baprs]-[A-Za-z0-9-]{10,}|"
    r"AIza[A-Za-z0-9_-]{30,}|AKIA[A-Z0-9]{16}|glpat-[A-Za-z0-9_-]{10,}"
    r")"
)


class DecisionProjectionReason(StrEnum):
    """Stable, content-free reasons for refusing a decision projection."""

    MATERIAL_REMOVED = "projection_material_removed"
    SANITIZER_FAILED = "projection_sanitizer_failed"
    SANITIZER_INVALID = "projection_sanitizer_invalid"


_ALLOWED_FACTS = frozenset(fact.value for fact in DecisionProjectionFact)


class InvalidDecisionProjectionError(ValueError):
    """A sanitizer returned a result outside the projection contract."""


@dataclass(frozen=True, slots=True)
class DecisionRequestProjection:
    """The only two outcomes a decision request sanitizer may produce.

    A permitted projection carries a replacement request and bounded machine-readable facts.
    A withheld projection carries no request at all. ``decision_kind`` and ``question`` remain
    owned by the Runtime policy; sanitizers may only replace the subject and evidence values.
    """

    request: DecisionRequest | None
    redaction_facts: tuple[str, ...] = ()
    reason_code: str | None = None

    def __post_init__(self) -> None:
        facts = self.redaction_facts
        if not isinstance(facts, tuple) or len(facts) > _MAX_FACTS:
            raise InvalidDecisionProjectionError("decision projection redaction facts are not bounded")  # noqa: TRY003
        for fact in facts:
            if (
                not isinstance(fact, str)
                or len(fact) > _MAX_FACT_LENGTH
                or _FACT_RE.fullmatch(fact) is None
                or fact not in _ALLOWED_FACTS
            ):
                raise InvalidDecisionProjectionError("decision projection redaction facts must be content-free codes")  # noqa: TRY003
        if self.request is None:
            if not isinstance(self.reason_code, str) or self.reason_code not in _WITHHELD_REASONS:
                raise InvalidDecisionProjectionError("withheld decision projections require a stable reason code")  # noqa: TRY003
            if facts:
                raise InvalidDecisionProjectionError("withheld decision projections cannot carry redaction facts")  # noqa: TRY003
        elif self.reason_code is not None:
            raise InvalidDecisionProjectionError("permitted decision projections cannot carry a reason code")  # noqa: TRY003

    @classmethod
    def permitted(
        cls,
        request: DecisionRequest,
        *,
        redaction_facts: tuple[str, ...] = (),
    ) -> DecisionRequestProjection:
        """Construct a projection that may be sent after Runtime validation."""

        return cls(request=request, redaction_facts=redaction_facts)

    @classmethod
    def withheld(cls, reason_code: DecisionProjectionReason | str) -> DecisionRequestProjection:
        """Construct a no-call projection with one stable reason code."""

        value = reason_code.value if isinstance(reason_code, DecisionProjectionReason) else reason_code
        return cls(request=None, reason_code=value)

    @property
    def is_permitted(self) -> bool:
        return self.request is not None


class DecisionRequestProjectionSanitizer(Protocol):
    """Runtime-owned synchronous port for one bounded decision projection."""

    def sanitize(self, request: DecisionRequest, /) -> DecisionRequestProjection:
        """Return a permitted replacement request or withhold it."""

        ...


DecisionRequestSanitizer = DecisionRequestProjectionSanitizer
DecisionRequestSanitizerCallable = Callable[[DecisionRequest], DecisionRequestProjection]

_WITHHELD_REASONS = frozenset(reason.value for reason in DecisionProjectionReason)


class KnownSecretDecisionRequestSanitizer:
    """Sanitize known secrets and common provider-key prefixes on a bounded request.

    This helper is intentionally conservative about semantic loss: a subject or an entire
    evidence set that becomes only a redaction marker cannot support an honest decision and is
    withheld. It never reads a Source, Memory, Artifact, Scope, or provider.

    The secret-value set is snapshotted when the sanitizer is constructed. Coverage is limited
    to sensitive environment values, Codex auth values, and the provider-key prefixes listed
    above; it is not a general PII or arbitrary password detector.
    """

    def __init__(self, *, codex_home: Path | None = None) -> None:
        self._known_secrets = tuple(sorted(known_secret_values(codex_home=codex_home), key=len, reverse=True))

    def sanitize(self, request: DecisionRequest, /) -> DecisionRequestProjection:
        subject, subject_has_material = self._sanitize_subject(request.subject)
        evidence = tuple(self._sanitize_text(item) for item in request.evidence)
        subject_changed = subject != request.subject
        evidence_changed = evidence != request.evidence
        if not subject_has_material or (evidence and not any(_has_decision_material(item) for item in evidence)):
            return DecisionRequestProjection.withheld(DecisionProjectionReason.MATERIAL_REMOVED)
        facts = tuple(
            fact
            for changed, fact in (
                (subject_changed, "subject_redacted"),
                (evidence_changed, "evidence_redacted"),
            )
            if changed
        )
        return DecisionRequestProjection.permitted(
            DecisionRequest(
                decision_kind=request.decision_kind,
                question=request.question,
                subject=subject,
                evidence=evidence,
            ),
            redaction_facts=facts,
        )

    def _sanitize_text(self, value: str) -> str:
        value = redact_known_secrets(value, secrets=self._known_secrets)
        return _PROVIDER_SECRET_RE.sub(REDACTED, value)

    def _sanitize_subject(self, value: str) -> tuple[str, bool]:
        candidates = _candidate_subject_items(value)
        if candidates is None:
            sanitized = self._sanitize_text(value)
            return sanitized, _has_decision_material(sanitized)
        sanitized_candidates: list[dict[str, int | str]] = []
        has_material = True
        for candidate, text in candidates:
            sanitized_text = self._sanitize_text(text)
            sanitized_candidates.append({"candidate": candidate, "text": sanitized_text})
            has_material = has_material and _has_decision_material(sanitized_text)
        return (
            json.dumps(sanitized_candidates, ensure_ascii=False, separators=(",", ":")),
            has_material,
        )


RedactingDecisionRequestSanitizer = KnownSecretDecisionRequestSanitizer


def apply_decision_request_sanitizer(
    sanitizer: DecisionRequestProjectionSanitizer | DecisionRequestSanitizerCallable,
    request: DecisionRequest,
    /,
) -> DecisionRequestProjection:
    """Invoke one sanitizer without providing it any data beyond the bounded request."""

    sanitize = getattr(sanitizer, "sanitize", None)
    if callable(sanitize):
        result = sanitize(request)
    elif callable(sanitizer):
        result = cast(DecisionRequestSanitizerCallable, sanitizer)(request)
    else:
        raise InvalidDecisionProjectionError("decision projection sanitizer is not callable")  # noqa: TRY003
    if not isinstance(result, DecisionRequestProjection):
        raise InvalidDecisionProjectionError("decision projection sanitizer returned an invalid result")  # noqa: TRY003
    _validate_projection(result, request)
    return result


def _validate_projection(projection: DecisionRequestProjection, original: DecisionRequest, /) -> None:
    if projection.request is None:
        return
    replacement = projection.request
    if replacement.decision_kind != original.decision_kind or replacement.question != original.question:
        raise InvalidDecisionProjectionError(  # noqa: TRY003
            "decision projection sanitizer may only replace subject and evidence"
        )
    if (
        not isinstance(replacement.subject, str)
        or not replacement.subject
        or len(replacement.subject) > _MAX_SUBJECT_LENGTH
    ):
        raise InvalidDecisionProjectionError("decision projection subject is not bounded")  # noqa: TRY003
    if not isinstance(replacement.evidence, tuple) or len(replacement.evidence) > _MAX_EVIDENCE_ITEMS:
        raise InvalidDecisionProjectionError("decision projection evidence is not bounded")  # noqa: TRY003
    if any(not isinstance(item, str) or len(item) > _MAX_EVIDENCE_LENGTH for item in replacement.evidence):
        raise InvalidDecisionProjectionError("decision projection evidence is not bounded")  # noqa: TRY003


def _has_decision_material(value: str) -> bool:
    return bool(value.replace(REDACTED, "").replace("<redacted>", "").strip())


def _candidate_subject_items(value: str) -> list[tuple[int, str]] | None:
    """Decode the exact Memory gate subject envelope without treating it as decision material."""

    try:
        parsed = json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(parsed, list) or not parsed:
        return None
    candidates: list[tuple[int, str]] = []
    for index, item in enumerate(parsed, start=1):
        if not isinstance(item, dict) or set(item) != {"candidate", "text"}:
            return None
        candidate = item.get("candidate")
        text = item.get("text")
        if type(candidate) is not int or candidate != index or not isinstance(text, str):
            return None
        candidates.append((candidate, text))
    return candidates


__all__ = [
    "DecisionProjectionFact",
    "DecisionProjectionReason",
    "DecisionRequestProjection",
    "DecisionRequestProjectionSanitizer",
    "DecisionRequestSanitizer",
    "DecisionRequestSanitizerCallable",
    "InvalidDecisionProjectionError",
    "KnownSecretDecisionRequestSanitizer",
    "RedactingDecisionRequestSanitizer",
    "apply_decision_request_sanitizer",
]
