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

"""Explicit experimental R4 HTTP comparison, distinct from ordinary product active mode.

Only this process's internal policy allowlist is temporarily overridden. No runtime source, deployment configuration,
public endpoint or field is changed. Reader calls require --call-reader and only occur for strictly shorter pairs.
"""

from __future__ import annotations

import argparse
import json
import secrets
import uuid
from pathlib import Path
from typing import Any

import httpx
import tiktoken

from evaluation.token_optimization.cases import Case
from evaluation.token_optimization.live import (
    answer_row,
    capture_optimizer_diagnostics,
    configure,
    digest,
    exact_reads_unchanged,
    prepared,
    save,
    seed,
    select_snapshot_database,
    server,
)
from evaluation.token_optimization.qualify import frozen_plans
from powercontext.builtin.persistence.oceanbase import OceanBaseConfig
from powercontext.builtin.runtime import prepared_optimization
from powercontext.builtin.runtime.prepared_excerpts import TOPIC_EXCERPT_POLICY_VERSION
from powercontext.server.configuration import server_settings_context


def execute(arguments: argparse.Namespace) -> None:
    directory = arguments.output_dir.resolve() / ("experimental-r4-http-" + uuid.uuid4().hex)
    if not directory.is_relative_to(Path("/data/codex-tmp")):
        raise ValueError("Experimental results must stay under /data/codex-tmp")  # noqa: TRY003
    directory.mkdir(parents=True, mode=0o700)
    arguments.phase = "heldout"
    run_id = uuid.uuid4().hex
    materials, plans = frozen_plans(arguments, run_id)
    cases = tuple(
        Case(
            case_id=plan["case_id"],
            source_path=materials[plan["case_id"]]["source_path"],
            heading=materials[plan["case_id"]]["content"]["title"],
            question=plan["question"],
            expected=plan["expected"],
            retrieval_query=plan["query"],
        )
        for plan in plans
    )
    manifest: dict[str, Any] = {
        "run_id": run_id,
        "kind": "experimental R4 real HTTP",
        "ordinary_product_active": False,
        "policy_override_process_only": TOPIC_EXCERPT_POLICY_VERSION,
        "policy_source_modified": False,
        "formal_noninferiority": False,
        "case_count": len(cases),
        "max_reader_calls": len(cases) * 2 if arguments.call_reader else 0,
        "reader_calls": 0,
        "pre_frozen_fixture_directory": str(arguments.fixture_dir),
        "material_hash": digest(materials),
    }
    save(directory / "manifest.json", manifest)
    save(directory / "fixtures.json", {"materials": materials, "plans": plans})
    # This evaluation intentionally uses the same validated file settings and existing tokenizer cache as main.
    import os

    if arguments.tokenizer_cache:
        os.environ["TIKTOKEN_CACHE_DIR"] = str(arguments.tokenizer_cache.resolve())
    encoder = tiktoken.get_encoding("o200k_base")
    capture_optimizer_diagnostics()
    with server_settings_context(
        host="127.0.0.1", env_file=arguments.env_file, process_environment_overrides=False
    ) as settings:
        if not isinstance(settings.database, OceanBaseConfig):
            raise TypeError("Experimental HTTP evaluation requires configured OceanBase")  # noqa: TRY003
        database, name, reused = select_snapshot_database(settings.database, arguments, materials, cases, run_id)
        manifest["isolated_database"] = name
        manifest["runtime_target_tokens"] = settings.runtime.token_optimization_target_tokens
        manifest["runtime_tokenizer"] = settings.runtime.token_optimization_tokenizer
        save(directory / "manifest.json", manifest)
        token = secrets.token_urlsafe(32)
        with (
            server(configure(settings, database, token, "off"), directory / "scheduler-off.db") as off_url,
            httpx.Client(base_url=off_url, headers={"Authorization": "Bearer " + token}, timeout=120) as off_client,
        ):
            snapshots = reused or {
                case.case_id: seed(off_client, case, materials[case.case_id], run_id) for case in cases
            }
            if not exact_reads_unchanged(off_client, snapshots):
                raise ValueError("Experimental exact snapshot changed")  # noqa: TRY003
            save(directory / "snapshots.json", snapshots)
            results = compare(arguments, directory, settings, database, token, cases, snapshots, off_client, encoder)
            manifest["native_exact_reads_unchanged"] = exact_reads_unchanged(off_client, snapshots)
    manifest["reader_calls"] = sum(row["reader"]["called"] for row in results)
    manifest["strict_gain_cases"] = sum(row["strict_gain_pair"] for row in results) // 2
    manifest["completed"] = True
    save(directory / "manifest.json", manifest)
    print(
        json.dumps({
            "artifact_directory": str(directory),
            "reader_calls": manifest["reader_calls"],
            "strict_gain_cases": manifest["strict_gain_cases"],
        })
    )


def compare(
    arguments: argparse.Namespace,
    directory: Path,
    settings: Any,
    database: OceanBaseConfig,
    token: str,
    cases: tuple[Case, ...],
    snapshots: dict[str, Any],
    off_client: httpx.Client,
    encoder: Any,
) -> list[dict[str, Any]]:
    previous = prepared_optimization.ACTIVE_LOSSY_POLICIES
    prepared_optimization.ACTIVE_LOSSY_POLICIES = frozenset({TOPIC_EXCERPT_POLICY_VERSION})
    results: list[dict[str, Any]] = []
    try:
        with (
            server(
                configure(settings, database, token, "active"), directory / "scheduler-experimental.db"
            ) as active_url,
            httpx.Client(
                base_url=active_url, headers={"Authorization": "Bearer " + token}, timeout=120
            ) as active_client,
        ):
            for index, case in enumerate(cases):
                off, off_latency = prepared(off_client, case, snapshots[case.case_id], "off")
                proposed, proposed_latency = prepared(active_client, case, snapshots[case.case_id], "active")
                strict = len(encoder.encode(proposed["response"].get("content") or "")) < len(
                    encoder.encode(off["response"].get("content") or "")
                )
                observations = {"off": (off, off_latency), "experimental-r4": (proposed, proposed_latency)}
                order = ("off", "experimental-r4") if index % 2 == 0 else ("experimental-r4", "off")
                for arm in order:
                    preparation, latency = observations[arm]
                    row = answer_row(
                        case,
                        arm,
                        preparation,
                        latency,
                        settings,
                        encoder,
                        arguments.max_output_tokens,
                        call_reader=bool(arguments.call_reader and strict),
                    )
                    row["strict_gain_pair"] = strict
                    results.append(row)
                save(directory / "answers.json", results)
                print(
                    json.dumps({
                        "case_id": case.case_id,
                        "strict_gain": strict,
                        "reader_calls_so_far": sum(row["reader"]["called"] for row in results),
                    }),
                    flush=True,
                )
    finally:
        prepared_optimization.ACTIVE_LOSSY_POLICIES = previous
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture-dir", type=Path, required=True)
    parser.add_argument("--snapshot-dir", type=Path)
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--tokenizer-cache", type=Path)
    parser.add_argument("--max-output-tokens", type=int, default=1024)
    parser.add_argument(
        "--call-reader", action="store_true", help="Compare models only for HTTP pairs with strict token gain"
    )
    arguments = parser.parse_args()
    try:
        execute(arguments)
    except Exception as error:
        print(json.dumps({"status": "failed", "error_type": type(error).__name__}))
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
