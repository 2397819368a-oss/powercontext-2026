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

"""Summarize frozen live results without treating missing provider usage as zero."""

from __future__ import annotations

import argparse
import json
import statistics
from pathlib import Path
from typing import Any

from evaluation.token_optimization.live import ARMS, rendered_origins, save


def summarize(directory: Path) -> dict[str, Any]:
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    rows = json.loads((directory / "answers.json").read_text(encoding="utf-8"))
    grouped = {arm: [row for row in rows if row["arm"] == arm] for arm in ARMS}
    arms: dict[str, Any] = {}
    for arm, selected in grouped.items():
        if not selected:
            continue
        arm_result: dict[str, Any] = {
            "attempts": len(selected),
            "valid_answers": sum(row["reader"]["status"] == "ok" for row in selected),
            "correct_answers": sum(row["quality"]["correct"] for row in selected),
            "serious_contract_errors": sum(row["quality"]["serious_error"] for row in selected),
            "complete_response_content_tokens": sum(row["content_tokens"] for row in selected),
            "complete_response_content_bytes": sum(row["content_bytes"] for row in selected),
            "prepare_latency_mean_seconds": statistics.mean(row["prepare_latency_seconds"] for row in selected),
            "reader_latency_mean_seconds": statistics.mean(row["reader"]["latency_seconds"] for row in selected),
            "prepare_latency_median_seconds": statistics.median(row["prepare_latency_seconds"] for row in selected),
            "reader_latency_median_seconds": statistics.median(row["reader"]["latency_seconds"] for row in selected),
        }
        for metric in ("input_tokens", "output_tokens", "cached_input_tokens", "uncached_input_tokens"):
            values = [row["reader"]["usage"].get(metric) for row in selected]
            arm_result[metric] = {
                "confirmed_total": sum(value for value in values if value is not None),
                "reported_attempts": sum(value is not None for value in values),
                "unknown_attempts": sum(value is None for value in values),
            }
        arms[arm] = arm_result
    by_case: dict[str, dict[str, Any]] = {}
    for row in rows:
        by_case.setdefault(row["case_id"], {})[row["arm"]] = row
    pairs = []
    for identity, case_rows in by_case.items():
        if "off" not in case_rows or "active" not in case_rows:
            continue
        off, active = case_rows["off"], case_rows["active"]
        off_origins = off.get("rendered_origins") or rendered_origins(off["prepare"]["response"].get("content") or "")
        active_origins = active.get("rendered_origins") or rendered_origins(
            active["prepare"]["response"].get("content") or ""
        )
        off_input = off["reader"]["usage"]["input_tokens"]
        active_input = active["reader"]["usage"]["input_tokens"]
        off_output = off["reader"]["usage"]["output_tokens"]
        active_output = active["reader"]["usage"]["output_tokens"]
        pairs.append({
            "case_id": identity,
            "negative_control": off["negative_control"],
            "same_scope_and_query": all(
                off["prepare"]["request"][key] == active["prepare"]["request"][key] for key in ("scope_id", "query")
            ),
            "same_rendered_origins": off_origins == active_origins,
            "off_content_tokens": off["content_tokens"],
            "active_content_tokens": active["content_tokens"],
            "off_provider_input_tokens": off_input,
            "active_provider_input_tokens": active_input,
            "off_provider_output_tokens": off_output,
            "active_provider_output_tokens": active_output,
            "response_token_reduction": off["content_tokens"] - active["content_tokens"],
            "provider_input_token_reduction": off_input - active_input
            if off_input is not None and active_input is not None
            else None,
            "provider_output_token_reduction": off_output - active_output
            if off_output is not None and active_output is not None
            else None,
            "strict_response_token_gain": off["content_tokens"] > active["content_tokens"],
            "body_identical": off["content_sha256"] == active["content_sha256"],
            "off_correct": off["quality"]["correct"],
            "active_correct": active["quality"]["correct"],
            "quality_regression": off["quality"]["correct"] and not active["quality"]["correct"],
        })
    return {
        "task_set": manifest["task_set"],
        "repo_head": manifest["repo_head"],
        "case_count": manifest["case_count"],
        "expected_attempts": manifest["max_reader_calls"],
        "actual_attempts": len(rows),
        "all_expected_attempts_recorded": len(rows) == manifest["max_reader_calls"],
        "arms": arms,
        "paired_cases": pairs,
        "paired_quality_regressions": sum(pair["quality_regression"] for pair in pairs),
        "strict_gain_case_count": sum(pair["strict_response_token_gain"] for pair in pairs),
        "negative_control_body_changes": sum(pair["negative_control"] and not pair["body_identical"] for pair in pairs),
        "native_exact_reads_unchanged": manifest.get("native_exact_reads_unchanged"),
        "limits": [
            "Repository-documentation task experiment, not a native coding-agent or customer-history evaluation",
            "Small purposive task set does not establish statistical noninferiority",
            "FTS fixed; no embedding, automatic extraction, rerank or recall gate evaluated",
            "Provider cache heat is observed, not guaranteed cold or hot; no unverified price savings",
            "The tokenizer profile measures returned text; provider input usage includes its actual message framing",
            "Assembly limit 1 is an already-minimal control where one eligible artifact exists",
        ],
        "official_benchmark": False,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    arguments = parser.parse_args()
    directory = arguments.directory.resolve()
    if not directory.is_relative_to(Path("/data/codex-tmp")):
        raise ValueError("Live reports must remain under /data/codex-tmp")  # noqa: TRY003
    result = summarize(directory)
    save(directory / "report.json", result)
    print(
        json.dumps({
            "report": str(directory / "report.json"),
            "case_count": result["case_count"],
            "actual_attempts": result["actual_attempts"],
            "paired_quality_regressions": result["paired_quality_regressions"],
        })
    )


if __name__ == "__main__":
    main()
