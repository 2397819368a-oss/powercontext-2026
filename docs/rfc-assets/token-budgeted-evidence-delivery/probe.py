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

"""Synthetic mechanism probe using the current repository's actual outer renderers.

Run with the existing PowerContext venv; this does not start a Server or call any model.
Proposed body reducers below are experiments, not implemented production strategies.
"""

# ruff: noqa: E402, RUF001, S101, TRY003
# E402: Resolve the requested checkout and tokenizer cache before importing either package.
# RUF001: Chinese punctuation is literal fixture data used by the frozen token counts.
# S101: Assertions enforce this offline probe's measurement invariants, not service input validation.
# TRY003: Fixed fixture-parser rejection messages do not justify custom exception classes.

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import shutil
import subprocess
import sys
from collections import OrderedDict
from dataclasses import replace
from pathlib import Path
from typing import cast


def discover_repo_root():
    for start in (Path(__file__).resolve().parent, Path.cwd().resolve()):
        for path in (start, *start.parents):
            if (path / "src/powercontext").is_dir():
                return path
    return None


parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--repo-root", type=Path, default=os.environ.get("POWERCONTEXT_PROBE_REPO_ROOT"))
parser.add_argument(
    "--output-dir", type=Path, default=os.environ.get("POWERCONTEXT_PROBE_OUTPUT_DIR", Path(__file__).resolve().parent)
)
parser.add_argument("--tokenizer-cache", type=Path, default=os.environ.get("TIKTOKEN_CACHE_DIR"))
args = parser.parse_args()
ROOT = args.repo_root.resolve() if args.repo_root is not None else discover_repo_root()
if ROOT is None or not (ROOT / "src/powercontext").is_dir():
    parser.error(
        "Pass --repo-root or POWERCONTEXT_PROBE_REPO_ROOT when the repository cannot be found above the script or current directory."
    )
OUT = args.output_dir.resolve()
OUT.mkdir(parents=True, exist_ok=True)
if args.tokenizer_cache is not None:
    os.environ["TIKTOKEN_CACHE_DIR"] = str(args.tokenizer_cache.resolve())
sys.path.insert(0, str(ROOT / "src"))

import tiktoken

from powercontext.artifacts import ArtifactRef
from powercontext.builtin.artifacts.experience import ExperienceContent, ExperienceSearchHit
from powercontext.builtin.artifacts.topic_memory import TopicMemorySearchHit
from powercontext.builtin.runtime.models import ContextAssembly, PrepareContextRequest
from powercontext.builtin.runtime.prepared_context import PreparedContextBuilder, _render, _text_item
from powercontext.builtin.runtime.prepared_text import render_context_text

SCOPE = "project:synthetic-mechanism-probe"
ENCODINGS = {name: tiktoken.get_encoding(name) for name in ("cl100k_base", "o200k_base")}
BUILDER = PreparedContextBuilder()


def dump(value):
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)


def digest(value):
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def measure(content):
    return {
        "content_bytes": len(content.encode("utf-8")),
        "content_tokens": {name: len(enc.encode(content, disallowed_special=())) for name, enc in ENCODINGS.items()},
        "content_sha256": hashlib.sha256(content.encode()).hexdigest(),
    }


def identity(entry):
    return entry.origin.model_dump(mode="json")


def render(entries, style, family):
    if style == "json":
        return _render(entries)
    assembly = ContextAssembly.model_validate({
        "sections": [{"family": family, "limit": 2 if family == "experience" else 8}]
    })
    return render_context_text(tuple(_text_item(entry) for entry in entries), assembly)


def topic(artifact_id, title, summary, snippet=None):
    return TopicMemorySearchHit(
        artifact_ref=ArtifactRef(family="topic-memory", artifact_id=artifact_id, revision=3),
        title=title,
        summary=summary,
        snippet=snippet,
        score=0.9,
        matched_by=("topic_fts",),
    )


def experience(artifact_id, content):
    return ExperienceSearchHit(
        artifact_ref=ArtifactRef(family="experience", artifact_id=artifact_id, revision=3),
        content=ExperienceContent.model_validate(content),
        retrieval_score=0.9,
    )


def topic_entries(hits):
    return BUILDER._topic_memory_entries(hits, scope_id=SCOPE)


def experience_entries(hits):
    return BUILDER._experience_entries(hits, scope_id=SCOPE)


def check_builder_baseline(hits, family, content, style):
    kwargs = {"topic_memory_hits": hits} if family == "topic-memory" else {"experience_hits": hits}
    values = {"query": "订单重试策略、幂等条件、资金操作例外、revision 3", "max_bytes": 32768}
    if style == "markdown":
        values["assembly"] = {"sections": [{"family": family, "limit": 2 if family == "experience" else 8}]}
    built = BUILDER.build_result(request=PrepareContextRequest.model_validate(values), scope_id=SCOPE, **kwargs)
    assert built.context.content == content, "Baseline must equal the actual Builder output."
    assert built.omissions.truncated_items == 0
    assert built.omissions.dropped_items == 0


def topic_exact_repeat(entry, hit):
    if hit.snippet is None or hit.snippet != hit.summary:
        return entry
    # Preserve the two field roles explicitly without repeating their identical value.
    if isinstance(entry.origin, ArtifactRef):
        body = json.dumps(
            {"title": hit.title, "summary": hit.summary, "snippet": "Same text as Summary."},
            ensure_ascii=False,
            separators=(",", ":"),
        )
    else:
        body = f"Title: {hit.title}\n\nSummary: {hit.summary}\n\nSnippet: Same text as Summary."
    return replace(entry, content=body)


def experience_fields(value):
    fields = [
        ("Situation", value.situation),
        ("Action", value.action),
        ("Outcome", value.outcome),
        ("Lesson", value.lesson),
    ]
    if value.failure is not None:
        fields.append(("Failure cue", value.failure.signature.recall_cue))
        if value.failure.signature.symptom is not None:
            fields.append(("Symptom", value.failure.signature.symptom))
        fields += [
            ("Repair surface", value.failure.repair_surface),
            ("Verification condition", value.failure.verification.condition),
            ("Verification check", value.failure.verification.check_subject),
        ]
    return fields


def experience_exact_repeat(entry, hit):
    fields = experience_fields(hit.content)
    role_fields = fields[:4]
    failure_fields = fields[4:]
    grouped = OrderedDict()
    for label, value in role_fields:
        grouped.setdefault(value, []).append(label)
    if len(grouped) == len(role_fields):
        return entry
    # All values stay byte-for-byte intact; field labels keep their explicit association.
    lines = [f"{' / '.join(labels)}: {value}" for value, labels in grouped.items()]
    # Failure roles never participate in role grouping, even when their values repeat.
    lines.extend(f"{label}: {value}" for label, value in failure_fields)
    body = "\n".join(lines)
    for label, value in fields:
        assert label in body and value in body
    return replace(entry, content=body)


SAMPLES = []
RESULTS = []


def record(case, family, hits, active_entries, mechanism, information_effect, notes, omitted=()):
    before_samples = [hit.model_dump(mode="json") for hit in hits]
    baseline_entries = topic_entries(hits) if family == "topic-memory" else experience_entries(hits)
    for entry in (*baseline_entries, *active_entries):
        assert len(entry.content.encode()) <= 2000, "Probe must not hide per-item fitting/truncation effects."
    SAMPLES.append({
        "case": case,
        "family": family,
        "source_objects": before_samples,
        "proposed_entry_bodies": [entry.content for entry in active_entries],
        "information_effect": information_effect,
        "omitted_material": list(omitted),
    })
    for style in ("json", "markdown"):
        style_baseline = baseline_entries
        style_active = active_entries
        if style == "json":
            style_baseline = (
                BUILDER._topic_memory_entries(hits) if family == "topic-memory" else BUILDER._experience_entries(hits)
            )
            base_by_id = {}
            for entry in style_baseline:
                origin = entry.origin
                assert isinstance(origin, ArtifactRef)
                base_by_id[origin.artifact_id] = entry
            scoped_by_id = {entry.origin.artifact.artifact_id: entry for entry in baseline_entries}
            hits_by_id = {hit.artifact_ref.artifact_id: hit for hit in hits}
            converted = []
            for proposed in active_entries:
                artifact_id = proposed.origin.artifact.artifact_id
                base = base_by_id[artifact_id]
                if proposed.content == scoped_by_id[artifact_id].content:
                    converted.append(base)
                elif family == "experience":
                    converted.append(replace(base, content=proposed.content, truncated=proposed.truncated))
                elif case == "topic-repeat-positive":
                    converted.append(topic_exact_repeat(base, hits_by_id[artifact_id]))
                else:
                    summary_value = proposed.content.split("Summary: ", 1)[1]
                    body = json.dumps(
                        {"title": hits_by_id[artifact_id].title, "summary": summary_value},
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                    converted.append(replace(base, content=body, truncated=proposed.truncated))
            style_active = tuple(converted)
        baseline = render(style_baseline, style, family)
        active = render(style_active, style, family)
        check_builder_baseline(hits, family, baseline, style)
        (OUT / f"{case}-{style}-legacy.txt").write_text(baseline, encoding="utf-8")
        (OUT / f"{case}-{style}-proposed.txt").write_text(active, encoding="utf-8")
        before = measure(baseline)
        after = measure(active)
        same_origins = [identity(x) for x in style_baseline] == [identity(x) for x in style_active]
        row = {
            "case": case,
            "style": style,
            "mechanism": mechanism,
            "legacy": before,
            "proposed": after,
            "token_reduction": {
                name: {
                    "tokens": before["content_tokens"][name] - after["content_tokens"][name],
                    "percent": round(
                        100
                        * (before["content_tokens"][name] - after["content_tokens"][name])
                        / before["content_tokens"][name],
                        2,
                    ),
                }
                for name in ENCODINGS
            },
            "same_origins": same_origins,
            "legacy_origins": [identity(x) for x in style_baseline],
            "proposed_origins": [identity(x) for x in style_active],
            "information_effect": information_effect,
            "notes": notes,
            "source_objects_sha256_before": digest(before_samples),
            "source_objects_sha256_after": digest([hit.model_dump(mode="json") for hit in hits]),
            "native_get": "Not invoked; no backend/storage/API operation. Only synthetic immutable objects were read.",
        }
        assert row["source_objects_sha256_before"] == row["source_objects_sha256_after"]
        if case.endswith("negative"):
            assert baseline == active, "Negative controls must pass through byte-for-byte."
        RESULTS.append(row)


shared = (
    "仅对已确认幂等的查询重试瞬时网络故障，最多三次。资金操作禁止自动重试；"
    "每次尝试使用相同幂等键，记录退出状态与失败证据。适用于 retry-policy revision 3，"
    "其他版本必须重新核验，不能把历史结论当作当前授权。"
)
t1 = topic("topic-exact-repeat", "订单重试规则", shared, shared)
record(
    "topic-repeat-positive",
    "topic-memory",
    (t1,),
    (topic_exact_repeat(topic_entries((t1,))[0], t1),),
    "Exact summary == snippet; repeat value once while retaining both field roles.",
    "All distinct values retained; presentation is different, not byte-identical and not proven model-equivalent.",
    "Proposed body transform only; actual outer trust/citation/renderers are unchanged.",
)
t2 = topic("topic-no-repeat", "订单重试规则", shared, "额外证据：资金操作测试必须验证没有自动重试。")
record(
    "topic-repeat-negative",
    "topic-memory",
    (t2,),
    (topic_exact_repeat(topic_entries((t2,))[0], t2),),
    "No exact repetition: pass through.",
    "No information or byte change.",
    "No saving is claimed.",
)

exp = {
    "situation": "订单查询遇到瞬时网络故障；先确认请求确实幂等。",
    "action": shared,
    "outcome": "幂等查询恢复成功，资金操作保持失败且没有自动重试。",
    "lesson": shared,
    "failure": {
        "signature": {"recall_cue": "money-transfer-retry", "symptom": "资金操作发生了自动重试。"},
        "repair_surface": "experience_content",
        "verification": {"condition": shared, "check_subject": "money-transfer-no-retry"},
    },
}
e1 = experience("experience-repeat", exp)
record(
    "experience-repeat-positive",
    "experience",
    (e1,),
    (experience_exact_repeat(experience_entries((e1,))[0], e1),),
    "Group identical values only across Situation/Action/Outcome/Lesson; failure fields remain verbatim separate lines.",
    "Every distinct field value and label association retained; all failure labels/values remain independent and complete.",
    "Action/Lesson share a value; Verification condition repeats it independently and is NOT grouped. Not byte-identical or proven model-equivalent.",
)
exp_unique = json.loads(json.dumps(exp))
exp_unique["lesson"] = "历史经验不能替代本次幂等性验证；保留失败证据和规则版本。"
exp_unique["failure"]["verification"]["condition"] = "资金操作没有自动重试，幂等查询最多尝试三次，规则版本为 3。"
e2 = experience("experience-no-repeat", exp_unique)
record(
    "experience-repeat-negative",
    "experience",
    (e2,),
    (experience_exact_repeat(experience_entries((e2,))[0], e2),),
    "All field values differ: pass through.",
    "No information or byte change.",
    "No saving is claimed.",
)

sections = [
    ("重试动作", "幂等查询遇到瞬时网络故障时最多重试三次，保持相同幂等键并保存退出状态。"),
    ("界面配色", "控制台使用蓝色主按钮、浅灰背景和深色标题。图标应统一线宽，图表需提供色盲友好配色。"),
    ("报表导出", "月度报表包括地区、渠道、金额与汇总。导出文件按日期命名，用户可以选择 CSV 或 XLSX。"),
    ("文档排版", "产品指南的首页展示简介和目录，图片居中，说明文字放在图下方。示例均使用统一缩进。"),
    ("保护条件", "仅适用于已确认幂等的查询。资金操作禁止自动重试。适用于 retry-policy revision 3；其他版本重新核验。"),
]
summary = "\n\n".join(f"[{label}] {value}" for label, value in sections)
t3 = topic("topic-task-excerpt", "项目操作指南", summary)
kept = [item for item in sections if item[0] in ("重试动作", "保护条件")]
excerpt = "\n\n".join(f"[{label}] {value}" for label, value in kept)
projected = replace(
    topic_entries((t3,))[0],
    content=f"Title: {t3.title}\n\nSummary: {excerpt}\n\nOmitted: 界面配色、报表导出、文档排版；仅提供本次重试策略摘录。",
    truncated=True,
)
record(
    "topic-query-excerpt-positive",
    "topic-memory",
    (t3,),
    (projected,),
    "Fixed synthetic task: keep action plus complete declared protection paragraph; omit unrelated paragraphs.",
    "Lossy task projection: the three omitted sections are no longer delivered. Original source object is unchanged.",
    "Declared synthetic section contract; this is not a generic free-prose dependency extractor.",
    omitted=[{"label": label, "text": value} for label, value in sections if (label, value) not in kept],
)
t4 = topic("topic-task-no-filler", "订单重试指南", excerpt)
record(
    "topic-query-excerpt-negative",
    "topic-memory",
    (t4,),
    topic_entries((t4,)),
    "All paragraphs relevant/required: pass through.",
    "No information or byte change.",
    "No saving is claimed.",
)

t5 = topic("topic-duplicate-a", "订单重试规则", shared)
t6 = topic("topic-duplicate-b", "订单重试规则", shared)
pool = (t5, t6)
record(
    "candidate-selection-positive",
    "topic-memory",
    pool,
    topic_entries((pool[0],)),
    "Keep first of two equal-valued candidates from the same pool; do not merge their identities.",
    "Coverage reduced from two exact origins to one; NOT same-origins representation compression.",
    "Equality of text is insufficient for automatic production selection; current-state/authority differences need verification.",
)
t7 = topic("topic-distinct-b", "资金操作验收规则", "资金操作禁止自动重试，验收必须证实没有重试请求。")
distinct_pool = (t5, t7)
record(
    "candidate-selection-negative",
    "topic-memory",
    distinct_pool,
    topic_entries(distinct_pool),
    "Distinct candidate contributes a separate constraint: keep both.",
    "No information, coverage, or byte change.",
    "No saving is claimed.",
)


class RawNumber(str):
    kind: str

    def __new__(cls, text, kind):
        value = super().__new__(cls, text)
        value.kind = kind
        return value


def decode_strict_json(text):
    def no_duplicates(pairs):
        result = OrderedDict()
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate object key")
            result[key] = value
        return result

    def reject_constant(value):
        raise ValueError(f"non-JSON constant: {value}")

    return json.loads(
        text,
        object_pairs_hook=no_duplicates,
        parse_int=lambda text: RawNumber(text, "integer"),
        parse_float=lambda text: RawNumber(text, "number"),
        parse_constant=reject_constant,
    )


def compact_json_rows(summary):  # noqa: C901 - Keep the complete typed/lexical roundtrip checks together.
    try:
        rows = decode_strict_json(summary)
        if not isinstance(rows, list) or len(rows) < 4 or not all(isinstance(row, dict) for row in rows):
            return None
        columns = list(rows[0])
        if not columns or any(list(row) != columns for row in rows):
            return None
        if any(isinstance(value, (dict, list)) for row in rows for value in row.values()):
            return None
        if any(not isinstance(value, (str, bool, type(None))) for row in rows for value in row.values()):
            return None
    except (ValueError, TypeError):
        return None

    rows = cast(list[dict[str, str | bool | None]], rows)

    def cell(value):
        if isinstance(value, RawNumber):
            return str(value)  # Preserve the numeric lexical form, including 1.50 or 3.00.
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))

    row_texts = ["[" + ",".join(cell(row[column]) for column in columns) + "]" for row in rows]
    rebuilt_arrays = decode_strict_json("[" + ",".join(row_texts) + "]")
    rebuilt_rows = [OrderedDict(zip(columns, row, strict=True)) for row in rebuilt_arrays]
    for original, rebuilt in zip(rows, rebuilt_rows, strict=True):
        for key in columns:
            a, b = original[key], rebuilt[key]
            assert type(a) is type(b) and a == b
            if isinstance(a, RawNumber):
                assert a.kind == b.kind
    assert len(rows) == len(rebuilt_rows), "Never deduplicate repeated rows."
    return (
        "Columns: "
        + json.dumps(columns, ensure_ascii=False, separators=(",", ":"))
        + "\nRows (JSON values in column order; numeric spelling preserved):\n"
        + "\n".join(row_texts)
    )


row_texts = [
    '{"request_id":"order-001","operation_kind":"read_only","retry_limit":3,"timeout_seconds":1.50,"enabled":true}',
    '{"request_id":"order-002","operation_kind":"read_only","retry_limit":3,"timeout_seconds":2.00,"enabled":true}',
    '{"request_id":"order-003","operation_kind":"read_only","retry_limit":0,"timeout_seconds":1e1,"enabled":false}',
    '{"request_id":"order-004","operation_kind":"money_transfer","retry_limit":0,"timeout_seconds":3.50,"enabled":false}',
    '{"request_id":"order-005","operation_kind":"read_only","retry_limit":3,"timeout_seconds":null,"enabled":true}',
    '{"request_id":"order-006","operation_kind":"read_only","retry_limit":1,"timeout_seconds":0.50,"enabled":true}',
    '{"request_id":"order-007","operation_kind":"read_only","retry_limit":2,"timeout_seconds":2.50,"enabled":true}',
]
row_texts.append(row_texts[2])  # An intentional duplicate row must remain a duplicate.
rows_summary = "[" + ",".join(row_texts) + "]"
table_hit = topic("topic-homogeneous-json", "查询配置样本", rows_summary)
table_body = compact_json_rows(rows_summary)
assert table_body is not None
table_entry = replace(topic_entries((table_hit,))[0], content=f"Title: {table_hit.title}\n\nSummary: {table_body}")
record(
    "json-table-positive",
    "topic-memory",
    (table_hit,),
    (table_entry,),
    "Strict homogeneous flat JSON rows: repeat column names once, retain every JSON cell and row in order.",
    "All columns, row order, duplicate row, value types and numeric lexemes preserved; display differs from native JSON.",
    "Requires a verified structured-field format; not used for Atomic Memory text. No provider/task equivalence claim.",
)

missing = row_texts.copy()
missing[4] = '{"request_id":"order-005","operation_kind":"read_only","timeout_seconds":null,"enabled":true}'
missing_summary = "[" + ",".join(missing) + "]"
assert compact_json_rows(missing_summary) is None
negative_table = topic("topic-json-missing-field", "不规则查询配置样本", missing_summary)
record(
    "json-table-negative",
    "topic-memory",
    (negative_table,),
    topic_entries((negative_table,)),
    "A row is missing retry_limit: reject table projection and pass through.",
    "No information, coverage, or byte change.",
    "Nested structures, duplicate object keys, and non-JSON constants are also rejected by the same prototype guard.",
)

profiles = {}
for name, enc in ENCODINGS.items():
    ranks = hashlib.sha256()
    for token, rank in sorted(enc._mergeable_ranks.items(), key=lambda item: item[1]):
        ranks.update(f"{rank}:{token.hex()}\n".encode())
    profiles[name] = {
        "mergeable_ranks_sha256": ranks.hexdigest(),
        "n_vocab": enc.n_vocab,
        "counting": "len(encoding.encode(content, disallowed_special=()))",
    }

git_executable = shutil.which("git")
if git_executable is None:
    parser.error("git is required to record the source revision")

metadata = {
    "evidence_level": "One synthetic offline probe using real Builder baselines and current outer renderers. No Server, database, Agent, Reader, Judge, or provider execution.",
    # Fixed read-only Git arguments; the checkout path is an argv value, never shell code.
    "source_revision": subprocess.check_output(  # noqa: S603 - Resolved executable and fixed read-only arguments.
        [git_executable, "-C", str(ROOT), "rev-parse", "HEAD"], text=True
    ).strip(),
    "python": sys.version,
    "python_executable": sys.executable,
    "repo_root": str(ROOT),
    "output_dir": str(OUT),
    "tokenizer_cache": os.environ.get("TIKTOKEN_CACHE_DIR", "tiktoken default"),
    "tiktoken_version": importlib.metadata.version("tiktoken"),
    "profiles": profiles,
    "renderers": {
        "json": "powercontext.builtin.runtime.prepared_context._render",
        "markdown": "powercontext.builtin.runtime.prepared_text.render_context_text",
        "baseline": "asserted equal to PreparedContextBuilder.build_result with max_bytes=32768",
    },
    "fixed_query": "订单重试策略、幂等条件、资金操作例外、revision 3",
    "limit": "Proposed body transformation is not a production compiler; actual model understanding, injection compatibility and task quality are untested.",
}
(OUT / "samples.json").write_text(dump({"metadata": metadata, "samples": SAMPLES}), encoding="utf-8")
(OUT / "results.json").write_text(dump({"metadata": metadata, "results": RESULTS}), encoding="utf-8")
print(
    dump({
        "metadata": metadata,
        "results": [
            {
                "case": row["case"],
                "style": row["style"],
                "legacy": row["legacy"]["content_tokens"],
                "proposed": row["proposed"]["content_tokens"],
                "legacy_bytes": row["legacy"]["content_bytes"],
                "proposed_bytes": row["proposed"]["content_bytes"],
                "same_origins": row["same_origins"],
                "reductions": row["token_reduction"],
            }
            for row in RESULTS
        ],
    })
)
