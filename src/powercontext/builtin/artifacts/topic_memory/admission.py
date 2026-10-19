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

"""Conservative admission of generated Topic bodies, independent of storage validation."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping

from powercontext.builtin.artifacts.topic_memory.models import TopicMemoryContent

_TITLE_LABEL = re.compile(r"(?:t|topic|主题)[- _]?(\d+)(?:\s+coordinated)?", re.IGNORECASE)
_SECTION_LABEL = re.compile(r"^[sd][- _]?\d+\b[\s:.-]*", re.IGNORECASE)
_INTERNAL_ID = re.compile(r"\b(?:candidate|proposal|work|component|evidence|probe|temp)-\d+(?:-\d+)*\b")
_WORDS = re.compile(r"[a-z]+")
_COORDINATION_WORDS = frozenset([
    "coordinated",
    "coordination",
    "coordinating",
    "reconcile",
    "reconciled",
    "reconciliation",
    "details",
    "detail",
    "preserving",
    "preserve",
    "preserved",
    "distinct",
    "historical",
    "history",
    "identity",
    "identities",
    "without",
    "merging",
    "merge",
    "merged",
    "with",
    "of",
    "the",
    "and",
    "a",
    "an",
    "to",
    "for",
    "while",
    "only",
    "existing",
    "candidate",
    "candidates",
    "proposal",
    "proposals",
    "assigned",
    "assignment",
    "target",
    "targets",
    "unchanged",
    "separate",
    "keeping",
    "kept",
    "retain",
    "retaining",
    "retained",
    "not",
    "two",
    "all",
    "is",
    "are",
    "was",
    "were",
    "bound",
    "binding",
    "update",
    "updated",
])
_COORDINATION_ACTION = re.compile(r"\b(?:coordinat\w*|reconcil\w*|preserv\w*|merg\w*|retain\w*|keeping)\b")
_IDENTITY = re.compile(r"\b(?:historical|distinct|existing)\s+identit(?:y|ies)\b")
_CHINESE_COORDINATION_WORD = re.compile(
    r"协调后的|协调结果|协调详情|保留不同的|保持不同的|不同历史身份|独立历史身份|历史身份|现有身份|"
    r"避免合并|不合并|协调|保留|保持|候选|提案|的|并|且|与|[\s，。：]"  # noqa: RUF001 - Chinese punctuation
)
_DISCUSSION = re.compile(
    r"\b(?:example|placeholder|template|quoted|literal|investigation|protocol)\b|占位|模板|示例|排查|协议"
)


def _normalize(value: str) -> str:
    return " ".join(value.casefold().split())


def _coordination_only(value: str, internal_ids: frozenset[str]) -> bool:
    """Recognize a bounded boilerplate vocabulary, never general semantic similarity."""
    text = _SECTION_LABEL.sub("", value)
    referenced = frozenset(_INTERNAL_ID.findall(text))
    if not referenced <= internal_ids:
        return False
    text = _INTERNAL_ID.sub("", text).strip(" .,:;-#")
    if text and not _CHINESE_COORDINATION_WORD.sub("", text):
        return "身份" in text and any(word in text for word in ("协调", "保留", "保持", "不合并"))
    words = frozenset(_WORDS.findall(text))
    # Foreign scripts and extra symbols are not evidence of meaningless content.
    residue = re.sub(r"[a-z\s.,:;()\-]", "", text)
    return (
        not residue
        and bool(words)
        and words <= _COORDINATION_WORDS
        and bool(_COORDINATION_ACTION.search(text))
        and bool(_IDENTITY.search(text) or referenced)
    )


class TopicMemoryContentAdmission:
    """Reject recognizable process templates, not arbitrary unsupported assertions.

    Normalize each supplied evidence/history body once per batch. Only cited Sources
    and the selected historical Topic may support a literal discussion of a template.
    """

    def __init__(
        self,
        evidence: Mapping[str, str],
        historical: Mapping[str, TopicMemoryContent],
        *,
        internal_ids: Iterable[str] = (),
    ) -> None:
        self._evidence = {key: _normalize(value) for key, value in evidence.items()}
        self._historical = {
            key: _normalize(f"{value.title}\n{value.summary}\n{value.detail}") for key, value in historical.items()
        }
        self._internal_ids = frozenset((*evidence, *historical, *internal_ids))

    def rejection(
        self,
        content: TopicMemoryContent,
        evidence_ids: Iterable[str],
        candidate_id: str | None = None,
    ) -> str | None:
        """Return a content-free rejection code, or admit a potentially meaningful Topic."""
        title, summary, detail = (_normalize(value) for value in (content.title, content.summary, content.detail))
        numbered = _TITLE_LABEL.fullmatch(title)
        coordinated = tuple(_coordination_only(value, self._internal_ids) for value in (summary, detail))
        labels = tuple(_SECTION_LABEL.match(value) for value in (summary, detail))
        empty_sections = tuple(not _SECTION_LABEL.sub("", value) for value in (summary, detail))
        if (
            numbered
            and all(labels)
            and all(empty or coordination for empty, coordination in zip(empty_sections, coordinated, strict=True))
        ):
            # Quoting an instruction to emit a template must not make that template
            # itself a publishable Topic. An explanatory title/body remains allowed.
            return "invalid_topic_placeholder"
        if not all(coordinated):
            return None
        if _DISCUSSION.search(title):
            context = [self._evidence[key] for key in evidence_ids if key in self._evidence]
            if candidate_id in self._historical:
                context.append(self._historical[candidate_id])
            if any(_DISCUSSION.search(text) and summary in text and detail in text for text in context):
                return None
        return "invalid_topic_coordination_content"
