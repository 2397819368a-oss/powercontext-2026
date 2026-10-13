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

"""Bounded, explicitly lossy Topic projections for a limited Markdown format.

These pure policies identify known structure, not every semantic dependency. The
caller owns eligibility, source identities, complete rendering, budgets, and the
validated-policy allowlist. In particular, features are not Artifact identities
and equal features do not authorize dropping independent corroborating sources.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from urllib.parse import unquote

from powercontext.builtin.artifacts.search import analyze_fts_query, analyze_text

TOPIC_EXCERPT_POLICY_VERSION = "markdown-sections-v1"
_MAX_SUMMARY_CHARACTERS = 8_000
_MAX_HEADINGS = 64
_MAX_GROUPS = 32
_MAX_QUERY_TERMS = 64
_HEADING = re.compile(r"^[ ]{0,3}(#{1,6})[ \t]+(.+?)(?:[ \t]+#+)?[ \t]*$")
_FENCE = re.compile(r"^[ \t]*(?:`{3,}|~{3,})")
_SETEXT = re.compile(r"^[ \t]*(?:={3,}|-{3,})[ \t]*$")
_INTERNAL_LINK = re.compile(r"(?<!\\)\[[^\]\n]+\]\(#([^\s)]+)\)")
_OTHER_INTERNAL_LINK = re.compile(r"\]\(\s*#|\[\^[^\]]+\]")
_REFERENCE_DEFINITION = re.compile(r"^[ ]{0,3}\[[^\]\n]+\]:", re.MULTILINE)
_REFERENCE_USE = re.compile(r"(?<!\\)\[[^\]\n]+\]\[[^\]\n]*\]")
_LABELLED_ROLE = re.compile(r"^[ ]{0,3}(?:[-*+][ \t]+)?(?:\*\*)?([^:\n]{1,80})[:：]", re.MULTILINE)  # noqa: RUF001
_PLAIN_REFERENCE = re.compile(
    r"\b(?:above|below|aforementioned|preceding|following section)\b|"
    r"\b(?:see|refer to)\s+(?!__resolved_reference__)[\w#]|"
    r"上述|下述|前述|后述|(?:参见|见下|见上)(?![ \t]*__resolved_reference__)",
    re.IGNORECASE,
)
_SOURCE_SENSITIVE_QUERY = re.compile(
    r"\b(?:corroborat\w*|independen\w*|sources?|conflicts?|contradict\w*|compare|versions?)\b|"
    r"独立|佐证|来源|矛盾|冲突|对比|比较|版本",
    re.IGNORECASE,
)
_SOURCE_LINK = re.compile(r"https?://|\b(?:sources?|corroboration|evidence)\b|来源|佐证|证据", re.IGNORECASE)
_QUERY_STOPWORDS = frozenset({
    "a",
    "an",
    "the",
    "what",
    "how",
    "when",
    "why",
    "which",
    "is",
    "are",
    "was",
    "were",
    "and",
    "or",
    "to",
    "of",
    "for",
    "in",
    "on",
    "at",
    "with",
    "we",
    "i",
    "it",
    "can",
    "could",
    "should",
    "do",
    "does",
    "please",
    "tell",
    "give",
    "about",
})
_PROTECTED_ROLES = (
    (
        "condition",
        re.compile(
            r"\b(?:constraints?|conditions?|prerequisites?|preconditions?|requirements?|applicability|scope|limitations?)\b|条件|前提|约束|适用|范围|限制",
            re.IGNORECASE,
        ),
    ),
    ("exception", re.compile(r"\b(?:exceptions?|prohibitions?|exclusions?)\b|例外|禁止|排除", re.IGNORECASE)),
    ("version", re.compile(r"\b(?:versions?|revisions?|compatibility)\b|版本|兼容", re.IGNORECASE)),
    (
        "verification",
        re.compile(
            r"\b(?:verification|validation|checks?|failures?|evidence|sources?)\b|验证|核验|检查|失败|证据|来源",
            re.IGNORECASE,
        ),
    ),
)


@dataclass(frozen=True, slots=True)
class TopicCoverage:
    """Exact delivered-value features, with positive vetoes on source removal.

    A false ``preserve_all_sources`` is not permission to collapse identities.
    Planners must separately retain Scope/version and independent provenance.
    """

    features: frozenset[str]
    protected_features: frozenset[str]
    preserve_all_sources: bool


@dataclass(frozen=True, slots=True)
class TopicExcerpt:
    """A complete-group excerpt; omission is explicit, never semantic losslessness."""

    content: str
    omitted_sections: tuple[str, ...]
    features: frozenset[str]
    protected_features: frozenset[str]
    policy_version: str = TOPIC_EXCERPT_POLICY_VERSION

    @property
    def truncated(self) -> bool:
        return bool(self.omitted_sections)


@dataclass(frozen=True, slots=True)
class _Group:
    title: str
    content: str
    roles: frozenset[str]


@dataclass(frozen=True, slots=True)
class _Outline:
    prefix: str
    groups: tuple[_Group, ...]
    dependencies: tuple[frozenset[int], ...]


def extract_topic_excerpt(summary: str, query: str, /) -> TopicExcerpt | None:
    """Select whole Markdown groups and all explicit protections/dependencies.

    Unknown structure, unresolved references, source-comparison questions, no
    lexical match, or no actual omission return ``None``. The returned content
    excludes the omission notice: the caller must honestly mark/render truncation.
    This function does not activate a policy or evaluate model quality.
    """

    outline = _parse_outline(summary)
    terms = _query_terms(query)
    if outline is None or not terms or _SOURCE_SENSITIVE_QUERY.search(query):
        return None
    selection = _selected_groups(outline, terms)
    if selection is None:
        return None
    selected, dependencies = selection
    omitted = tuple(group.title for index, group in enumerate(outline.groups) if index not in selected)
    if not omitted:
        return None
    groups = tuple(group for index, group in enumerate(outline.groups) if index in selected)
    content = outline.prefix + "".join(group.content for group in groups)
    if len(content.encode("utf-8")) >= len(summary.encode("utf-8")):
        return None
    required = frozenset(outline.groups[index] for index in dependencies)
    features, protected = _features(outline.prefix, groups, required=required)
    return TopicExcerpt(content, omitted, features, protected)


def describe_topic_coverage(summary: str, query: str, /) -> TopicCoverage | None:
    """Describe known query/role coverage without merging or selecting sources.

    Values retain their exact text: different negation, numbers, versions, or
    whitespace yield different features. Feature-set subtraction gives marginal
    coverage; unknown structures return ``None`` and must not be pruned by R5.
    """

    outline = _parse_outline(summary)
    terms = _query_terms(query)
    if outline is None or not terms:
        return None
    selection = _selected_groups(outline, terms)
    if selection is None:
        return None
    selected, dependencies = selection
    relevant = tuple(group for index, group in enumerate(outline.groups) if index in selected)
    required = frozenset(outline.groups[index] for index in dependencies)
    features, protected = _features(outline.prefix, relevant, required=required)
    preserve = bool(_SOURCE_SENSITIVE_QUERY.search(query) or _SOURCE_LINK.search(summary))
    return TopicCoverage(features, protected, preserve)


def _query_terms(query: str) -> frozenset[str]:
    terms = frozenset(
        term for term in analyze_fts_query(query).split() if term not in _QUERY_STOPWORDS and not term.startswith("u_")
    )
    return terms if len(terms) <= _MAX_QUERY_TERMS else frozenset()


def _matches(group: _Group, terms: frozenset[str]) -> bool:
    return bool(terms.intersection(analyze_text(f"{group.title}\n{group.content}").split()))


def _selected_groups(outline: _Outline, terms: frozenset[str]) -> tuple[frozenset[int], frozenset[int]] | None:
    matching = {index for index, group in enumerate(outline.groups) if _matches(group, terms)}
    if not matching:
        return None
    selected = matching | {index for index, group in enumerate(outline.groups) if group.roles}
    required: set[int] = set()
    pending = list(selected)
    while pending:
        for dependency in outline.dependencies[pending.pop()]:
            required.add(dependency)
            if dependency not in selected:
                selected.add(dependency)
                pending.append(dependency)
    return frozenset(selected), frozenset(required)


def _features(
    prefix: str, groups: tuple[_Group, ...], *, required: frozenset[_Group]
) -> tuple[frozenset[str], frozenset[str]]:
    features: set[str] = set()
    protected: set[str] = set()
    if prefix.strip():
        feature = _feature("preamble", prefix)
        features.add(feature)
        protected.add(feature)
    for group in groups:
        value = f"{group.title}\n{group.content}"
        features.add(_feature("section", value))
        for role in group.roles:
            feature = _feature(role, value)
            features.add(feature)
            protected.add(feature)
        if group in required:
            feature = _feature("dependency", value)
            features.add(feature)
            protected.add(feature)
    return frozenset(features), frozenset(protected)


def _feature(role: str, value: str) -> str:
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
    return f"{role}:{digest}"


def _parse_outline(summary: str) -> _Outline | None:
    if _unsupported_text(summary):
        return None
    headings = _parse_heading_lines(summary)
    if headings is None:
        return None
    boundaries = _group_boundaries(headings)
    if boundaries is None:
        return None
    prefix = summary[: boundaries[0][0]]
    groups = _build_groups(summary, headings, boundaries)
    dependencies = _resolve_dependencies(prefix, groups, headings, boundaries)
    if dependencies is None:
        return None
    return _Outline(prefix, groups, dependencies)


def _unsupported_text(summary: str) -> bool:
    return (
        not summary
        or len(summary) > _MAX_SUMMARY_CHARACTERS
        or any(ord(character) < 32 and character not in "\t\r\n" for character in summary)
        or any(character in summary for character in ("\x85", "\u2028", "\u2029"))
        or _REFERENCE_DEFINITION.search(summary) is not None
        or _REFERENCE_USE.search(summary) is not None
    )


def _parse_heading_lines(summary: str) -> tuple[tuple[int, int, str], ...] | None:
    headings: list[tuple[int, int, str]] = []
    position = 0
    for line in summary.splitlines(keepends=True):
        text = line.rstrip("\r\n")
        if _FENCE.match(text) or _SETEXT.match(text) or text.startswith(("    ", "\t")):
            return None
        heading = _HEADING.match(text)
        if heading is not None:
            headings.append((position, len(heading[1]), heading[2]))
        elif text.lstrip().startswith("#"):
            return None
        position += len(line)
    return tuple(headings) if 2 <= len(headings) <= _MAX_HEADINGS else None


def _group_boundaries(headings: tuple[tuple[int, int, str], ...]) -> tuple[tuple[int, str], ...] | None:
    level = min(item[1] for item in headings)
    outer = [item for item in headings if item[1] == level]
    if len(outer) == 1 and outer[0] == headings[0]:
        children = [item for item in headings if item[1] == level + 1]
        if len(children) < 2:
            return None
        level += 1
    boundaries = tuple((start, title) for start, depth, title in headings if depth == level)
    return boundaries if 2 <= len(boundaries) <= _MAX_GROUPS else None


def _build_groups(
    summary: str,
    headings: tuple[tuple[int, int, str], ...],
    boundaries: tuple[tuple[int, str], ...],
) -> tuple[_Group, ...]:
    groups: list[_Group] = []
    for index, (start, title) in enumerate(boundaries):
        end = boundaries[index + 1][0] if index + 1 < len(boundaries) else len(summary)
        local_headings = [name for offset, _depth, name in headings if start <= offset < end]
        local_headings.extend(match[1] for match in _LABELLED_ROLE.finditer(summary[start:end]))
        roles = frozenset(role for name in local_headings for role, pattern in _PROTECTED_ROLES if pattern.search(name))
        groups.append(_Group(title, summary[start:end], roles))
    return tuple(groups)


def _map_anchors(
    headings: tuple[tuple[int, int, str], ...], boundaries: tuple[tuple[int, str], ...]
) -> dict[str, int | None] | None:
    anchors: dict[str, int | None] = {}
    for start, _depth, title in headings:
        anchor = _anchor(title)
        if not anchor or anchor in anchors:
            return None
        anchors[anchor] = next(
            (index for index, (left, _title) in reversed(tuple(enumerate(boundaries))) if start >= left), None
        )
    return anchors


def _group_dependencies(content: str, anchors: dict[str, int | None]) -> frozenset[int] | None:
    targets: set[int] = set()
    for match in _INTERNAL_LINK.finditer(content):
        anchor = unquote(match[1]).casefold()
        if anchor not in anchors:
            return None
        target = anchors[anchor]
        if target is not None:
            targets.add(target)
    plain = _INTERNAL_LINK.sub("__resolved_reference__", content)
    if _OTHER_INTERNAL_LINK.search(plain) or _PLAIN_REFERENCE.search(plain):
        return None
    return frozenset(targets)


def _resolve_dependencies(
    prefix: str,
    groups: tuple[_Group, ...],
    headings: tuple[tuple[int, int, str], ...],
    boundaries: tuple[tuple[int, str], ...],
) -> tuple[frozenset[int], ...] | None:
    # Prefix references cannot silently depend on a group later omitted.
    if _INTERNAL_LINK.search(prefix) or _OTHER_INTERNAL_LINK.search(prefix) or _PLAIN_REFERENCE.search(prefix):
        return None
    anchors = _map_anchors(headings, boundaries)
    if anchors is None:
        return None
    dependencies: list[frozenset[int]] = []
    for group in groups:
        targets = _group_dependencies(group.content, anchors)
        if targets is None:
            return None
        dependencies.append(targets)
    return tuple(dependencies)


def _anchor(title: str) -> str:
    text = title.casefold().strip()
    text = "".join(character for character in text if character.isalnum() or character in " -_")
    return re.sub(r"\s+", "-", text)


__all__ = [
    "TOPIC_EXCERPT_POLICY_VERSION",
    "TopicCoverage",
    "TopicExcerpt",
    "describe_topic_coverage",
    "extract_topic_excerpt",
]
