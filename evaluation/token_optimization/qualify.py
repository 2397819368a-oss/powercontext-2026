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

"""Offline R4 format qualification using real frozen documents and an optional live Reader.

The existing pure policy and renderer are exercised over a full-material comparator. This does not invoke HTTP
preparation or use backend admission, and therefore does not establish production savings or activate a policy.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import uuid
from pathlib import Path
from typing import Any

import tiktoken

from evaluation.token_optimization.cases import R4_CALIBRATION_CASES, R4_HELDOUT_CASES, Case
from evaluation.token_optimization.live import SYSTEM, digest, grade, read_answer, save
from powercontext.artifacts import ArtifactAddress, ArtifactRef
from powercontext.builtin.runtime.models import ContextAssembly
from powercontext.builtin.runtime.prepared_excerpts import TOPIC_EXCERPT_POLICY_VERSION, extract_topic_excerpt
from powercontext.builtin.runtime.prepared_text import ContextTextItem, _body_chunks, render_context_text
from powercontext.server.configuration import server_settings_context


def plan(case: Case, material: dict[str, Any], scope_id: str) -> dict[str, Any]:
    content = material["content"]
    original = content["summary"]
    query = case.retrieval_query or case.question
    excerpt = extract_topic_excerpt(original, query)
    artifact = ArtifactAddress(
        scope_id=scope_id,
        artifact=ArtifactRef(family="topic-memory", artifact_id="offline-" + case.case_id, revision=1),
    )
    assembly = ContextAssembly.model_validate({"sections": [{"family": "topic-memory", "limit": 1}], "show": []})

    def render(summary: str, truncated: bool) -> str:
        body = "Title: " + content["title"] + "\n\nSummary: " + summary
        body = "".join(_body_chunks(body))
        return render_context_text(
            (ContextTextItem(artifact=artifact, content=body, recall_rank=1, truncated=truncated),), assembly
        )

    baseline = render(original, False)
    proposed = render(excerpt.content, True) if excerpt else baseline
    return {
        "case_id": case.case_id,
        "query": query,
        "question": case.question,
        "expected": case.expected,
        "policy_version": TOPIC_EXCERPT_POLICY_VERSION,
        "applicable": excerpt is not None,
        "omitted_sections": list(excerpt.omitted_sections) if excerpt else [],
        "baseline_content": baseline,
        "proposed_content": proposed,
        "origin": artifact.model_dump(mode="json"),
        "origin_kind": "offline synthetic citation; not a committed backend artifact",
    }


def execute(arguments: argparse.Namespace) -> None:
    output = arguments.output_dir.resolve()
    if not output.is_relative_to(Path("/data/codex-tmp")):
        raise ValueError("Qualification outputs must stay under /data/codex-tmp")  # noqa: TRY003
    run_id = uuid.uuid4().hex
    directory = output / ("qualification-" + arguments.phase + "-" + run_id)
    directory.mkdir(parents=True, mode=0o700)
    if arguments.tokenizer_cache:
        os.environ["TIKTOKEN_CACHE_DIR"] = str(arguments.tokenizer_cache.resolve())
    encoder = tiktoken.get_encoding("o200k_base")
    materials, plans = frozen_plans(arguments, run_id)
    manifest = {
        "phase": arguments.phase,
        "kind": "offline format qualification",
        "http_product_measurement": False,
        "backend_admission_exercised": False,
        "policy_activated": False,
        "tokenizer": "o200k_base",
        "tokenizer_version": importlib.metadata.version("tiktoken"),
        "fixture_hash": digest({"materials": materials, "plans": plans}),
        "case_count": len(plans),
        "maximum_reader_calls": sum(candidate["applicable"] for candidate in plans) * 2 if arguments.call_reader else 0,
        "comparator": "complete material in existing Markdown renderer; NOT legacy HTTP's 2000-byte per-item fitted output",
        "reader_calls": 0,
        "pre_frozen_fixture_directory": str(arguments.fixture_dir) if arguments.fixture_dir else None,
    }
    save(directory / "manifest.json", manifest)
    save(directory / "fixtures.json", {"materials": materials, "plans": plans})
    results = []
    with server_settings_context(
        host="127.0.0.1", env_file=arguments.env_file, process_environment_overrides=False
    ) as settings:
        for candidate in plans:
            result = {key: value for key, value in candidate.items() if not key.endswith("_content")}
            for arm in ("baseline", "proposed"):
                content = candidate[arm + "_content"]
                messages = [
                    {"role": "system", "content": SYSTEM + content},
                    {"role": "user", "content": candidate["question"]},
                ]
                observation: dict[str, Any] = {
                    "content_tokens": len(encoder.encode(content)),
                    "content_bytes": len(content.encode()),
                    "content_sha256": hashlib.sha256(content.encode()).hexdigest(),
                    "messages": messages,
                }
                observation["reader_called"] = False
                if arguments.call_reader and candidate["applicable"]:
                    reader = read_answer(settings, messages, arguments.max_output_tokens)
                    observation["reader"] = reader
                    observation["quality"] = grade(reader.get("answer") or "", candidate["expected"])
                    observation["reader_called"] = True
                    manifest["reader_calls"] += 1
                result[arm] = observation
            results.append(result)
            save(directory / "results.json", results)
            save(directory / "manifest.json", manifest)
            print(
                json.dumps({
                    "case_id": candidate["case_id"],
                    "applicable": candidate["applicable"],
                    "reader_calls_so_far": manifest["reader_calls"],
                }),
                flush=True,
            )
    manifest["applicable_cases"] = sum(candidate["applicable"] for candidate in plans)
    manifest["completed"] = True
    save(directory / "manifest.json", manifest)
    print({
        "artifact_directory": str(directory),
        "reader_calls": manifest["reader_calls"],
        "applicable_cases": manifest["applicable_cases"],
    })


def frozen_plans(arguments: argparse.Namespace, run_id: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if not arguments.fixture_dir:
        cases = R4_CALIBRATION_CASES if arguments.phase == "calibration" else R4_HELDOUT_CASES
        materials = {case.case_id: case.material(arguments.repo_root) for case in cases}
        return materials, [plan(case, materials[case.case_id], "offline-qualification-" + run_id) for case in cases]
    frozen = json.loads((arguments.fixture_dir / "fixtures.json").read_text(encoding="utf-8"))
    frozen_manifest = json.loads((arguments.fixture_dir / "manifest.json").read_text(encoding="utf-8"))
    if frozen_manifest["phase"] != arguments.phase:
        raise ValueError("Frozen qualification phase does not match")  # noqa: TRY003
    materials = frozen["materials"]
    plans = []
    for original in frozen["plans"]:
        case = Case(
            case_id=original["case_id"],
            source_path=materials[original["case_id"]]["source_path"],
            heading=materials[original["case_id"]]["content"]["title"],
            question=original["question"],
            expected=original["expected"],
            retrieval_query=original["query"],
        )
        plans.append(plan(case, materials[case.case_id], original["origin"]["scope_id"]))
    return materials, plans


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("calibration", "heldout"))
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--tokenizer-cache", type=Path)
    parser.add_argument("--max-output-tokens", type=int, default=1024)
    parser.add_argument(
        "--fixture-dir", type=Path, help="Use material, question and gold from a pre-frozen zero-call plan"
    )
    parser.add_argument(
        "--call-reader", action="store_true", help="Opt in to one live model attempt per comparator arm"
    )
    arguments = parser.parse_args()
    try:
        execute(arguments)
    except Exception as error:
        print({"status": "failed", "error_type": type(error).__name__})
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
