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

"""Closed-loop real HTTP preparation load over frozen evaluation-owned OceanBase snapshots.

This program makes zero Reader calls. It measures service preparation under off/active modes, not model quality,
host integration, production capacity, or a cold database cache. Results and response fingerprints stay outside Git.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib.metadata
import json
import logging
import math
import os
import platform
import re
import secrets
import threading
import time
import uuid
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import httpx
import tiktoken
from sqlalchemy import event
from sqlalchemy.engine import Engine
from typing_extensions import override

from evaluation.token_optimization.live import api, configure, digest, rendered_origins, reuse_database, save, server
from powercontext.builtin.persistence.oceanbase import OceanBaseConfig
from powercontext.server.configuration import server_settings_context


class EventCollector(logging.Handler):
    """Thread-safe content-free diagnostics for a whole measurement window, without per-request attribution."""

    def __init__(self) -> None:
        super().__init__()
        self._events: list[dict[str, Any]] = []
        self._guard = threading.Lock()

    @override
    def emit(self, record: logging.LogRecord) -> None:
        event = getattr(record, "event", None)
        if event not in {"context.token_optimization", "context.token_optimization.failed"}:
            return
        fields = ("mode", "applied", "reason", "duration_ms", "rules", "error_type")
        value = {key: getattr(record, key) for key in fields if hasattr(record, key)}
        value["event"] = event
        with self._guard:
            self._events.append(value)

    def drain(self) -> list[dict[str, Any]]:
        with self._guard:
            values, self._events = self._events, []
        return values


class StatementCollector:
    """Count emitted SQL statements without retaining query text, parameters, or connection identities."""

    def __init__(self) -> None:
        self._counts: Counter[str] = Counter()
        self._guard = threading.Lock()
        self._enabled = False

    def before_cursor_execute(
        self, connection: Any, cursor: Any, statement: str, parameters: Any, context: Any, executemany: bool
    ) -> None:
        del connection, cursor, parameters, context, executemany
        with self._guard:
            if not self._enabled:
                return
            operation = statement.lstrip().split(None, 1)[0].upper() if statement.strip() else "other"
            self._counts[operation if operation in {"SELECT", "INSERT", "UPDATE", "DELETE"} else "other"] += 1
            if re.search(r"\bMATCH\s*\(", statement, re.IGNORECASE):
                self._counts["fts_match_statements"] += 1

    def start(self) -> None:
        with self._guard:
            self._counts.clear()
            self._enabled = True

    def stop(self) -> dict[str, Any]:
        with self._guard:
            self._enabled = False
            counts = dict(self._counts)
        return {
            "counts": counts,
            "total_statements": sum(value for key, value in counts.items() if key != "fts_match_statements"),
            "attribution": "whole window only; SQLAlchemy emitted statements, excluding native ping/internal DB work",
        }


@contextmanager
def capture_statements() -> Iterator[StatementCollector]:
    collector = StatementCollector()
    event.listen(Engine, "before_cursor_execute", collector.before_cursor_execute)
    try:
        yield collector
    finally:
        event.remove(Engine, "before_cursor_execute", collector.before_cursor_execute)


def collect_events() -> EventCollector:
    logging.disable(logging.NOTSET)
    root = logging.getLogger()
    root.handlers = [logging.NullHandler()]
    root.setLevel(logging.CRITICAL)
    collector = EventCollector()
    runtime = logging.getLogger("powercontext.builtin.runtime.application")
    runtime.handlers = [collector]
    runtime.setLevel(logging.INFO)
    runtime.propagate = False
    return collector


def requests_for(snapshots: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    requests = []
    for case_id, snapshot in sorted(snapshots.items()):
        scope_id = snapshot["scope_id"]
        if "request_payload" in snapshot:
            request = snapshot["request_payload"]
        else:
            family = snapshot["material"]["family"]
            request = {
                "scope_id": scope_id,
                "query": snapshot.get("query", snapshot.get("question")),
                "max_bytes": 16_000,
                "assembly": {
                    "format": "markdown",
                    "sections": [{"family": "memory" if family == "atomic-memory" else family, "limit": 3}],
                    "show": [],
                },
            }
        if request.get("scope_id") != scope_id or request.get("reference_scope_ids"):
            raise ValueError("Load requests must stay within their frozen evaluation Scope")  # noqa: TRY003
        requests.append((case_id, request))
    if not requests:
        raise ValueError("Load snapshot has no tasks")  # noqa: TRY003
    return requests


def exact_reads(client: httpx.Client, snapshots: dict[str, Any]) -> bool:
    for snapshot in snapshots.values():
        records = snapshot.get("artifacts") or [snapshot]
        for record in records:
            ref = record["artifact"]
            path = f"/v1/scopes/{snapshot['scope_id']}/artifacts/{ref['family']}/{ref['artifact_id']}"
            readback = api(
                client,
                "GET",
                path + f"/revisions/{ref['revision']}",
            )
            latest = api(client, "GET", path)
            if digest(readback) != record["readback_sha256"] or latest.get("revision") != ref["revision"]:
                return False
    return True


def latest_reads(client: httpx.Client, snapshots: dict[str, Any]) -> bool:
    for snapshot in snapshots.values():
        for record in snapshot.get("artifacts") or [snapshot]:
            ref = record["artifact"]
            path = f"/v1/scopes/{snapshot['scope_id']}/artifacts/{ref['family']}/{ref['artifact_id']}"
            if api(client, "GET", path).get("revision") != ref["revision"]:
                return False
    return True


async def run_batch(
    url: str,
    token: str,
    requests: list[tuple[str, dict[str, Any]]],
    concurrency: int,
    count: int,
    request_timeout: float,
) -> tuple[list[dict[str, Any]], float]:
    """Fixed outstanding-worker count; complete response decode is included, token counting is outside the timer."""
    rows: list[dict[str, Any]] = []
    limits = httpx.Limits(max_connections=concurrency, max_keepalive_connections=concurrency)
    async with httpx.AsyncClient(
        base_url=url, headers={"Authorization": "Bearer " + token}, limits=limits, timeout=request_timeout
    ) as client:

        async def worker(worker_id: int) -> None:
            for index in range(worker_id, count, concurrency):
                case_id, request = requests[index % len(requests)]
                start = time.perf_counter()
                row: dict[str, Any] = {"index": index, "case_id": case_id}
                try:
                    response = await client.post("/v1/context/prepare", json=request)
                    row["http_status"] = response.status_code
                    # Error bodies may contain remote diagnostics and are deliberately discarded.
                    if response.is_success:
                        body = response.json()
                        valid = isinstance(body, dict) and (
                            isinstance(body.get("content"), str)
                            or (body.get("status") == "empty" and body.get("content") is None)
                        )
                        if not valid:
                            row["status"] = "invalid-response"
                        else:
                            row.update(status="ok", payload=body)
                    else:
                        row["status"] = "http-error"
                except (httpx.HTTPError, ValueError) as error:
                    row.update(status="transport-error", error_type=type(error).__name__)
                row["latency_ms"] = (time.perf_counter() - start) * 1000
                rows.append(row)

        started = time.perf_counter()
        await asyncio.gather(*(worker(worker_id) for worker_id in range(concurrency)))
        elapsed = time.perf_counter() - started
    return sorted(rows, key=lambda row: row["index"]), elapsed


def fingerprint(rows: list[dict[str, Any]], encoder: Any, snapshots: dict[str, Any], mode: str) -> None:
    for row in rows:
        dataset = snapshots[row["case_id"]].get("dataset")
        row["dataset"] = dataset if isinstance(dataset, str) else "unspecified"
        payload = row.pop("payload", None)
        if payload is None:
            continue
        content = payload["content"] or ""
        origins = rendered_origins(content)
        content_sha256 = hashlib.sha256(content.encode()).hexdigest()
        expected = snapshots[row["case_id"]].get("prepared_content_sha256", {}).get(mode)
        row.update(
            prepared_status=payload.get("status"),
            content_sha256=content_sha256,
            content_tokens=len(encoder.encode(content, disallowed_special=())),
            content_bytes=len(content.encode()),
            rendered_origins=origins,
            origins_measured=bool(origins) or not content,
            response_sha256=digest(payload),
            matches_frozen_prepared_content=content_sha256 == expected if isinstance(expected, str) else None,
        )


def percentiles(values: list[float]) -> dict[str, float | None]:
    ordered = sorted(values)
    return {
        f"p{percentile}_ms": ordered[max(0, math.ceil(len(ordered) * percentile / 100) - 1)] if ordered else None
        for percentile in (50, 95, 99)
    }


def diagnostics(events: list[dict[str, Any]]) -> dict[str, Any]:
    outcomes = [event for event in events if event["event"] == "context.token_optimization"]
    return {
        "attribution": "whole window only; never matched to the last event of a concurrent request",
        "outcome_events": len(outcomes),
        "applied_events": sum(bool(event.get("applied")) for event in outcomes),
        "outcome_reasons": dict(Counter(event.get("reason", "unknown") for event in outcomes)),
        "fallback_reasons": dict(
            Counter(event.get("reason", "unknown") for event in outcomes if not event.get("applied"))
        ),
        "failure_events": sum(event["event"] == "context.token_optimization.failed" for event in events),
        "compiler_duration": percentiles([event["duration_ms"] for event in outcomes if "duration_ms" in event]),
    }


def summarize(rows: list[dict[str, Any]], elapsed: float, events: list[dict[str, Any]]) -> dict[str, Any]:
    successful = [row for row in rows if row["status"] == "ok"]
    hashes: dict[str, set[str]] = {}
    for row in successful:
        hashes.setdefault(row["case_id"], set()).add(row["content_sha256"])
    return {
        "requests": len(rows),
        "completed_successfully": len(successful),
        "errors": len(rows) - len(successful),
        "status_counts": dict(Counter(row["status"] for row in rows)),
        "prepared_status_counts": dict(Counter(row.get("prepared_status") for row in successful)),
        "elapsed_seconds": elapsed,
        "requests_per_second": len(rows) / elapsed,
        "successful_requests_per_second": len(successful) / elapsed,
        "latency_all_requests": percentiles([row["latency_ms"] for row in rows]),
        "latency_successful_requests": percentiles([row["latency_ms"] for row in successful]),
        "response_content_tokens": sum(row["content_tokens"] for row in successful),
        "response_content_bytes": sum(row["content_bytes"] for row in successful),
        "case_count": len(hashes),
        "cases_with_variable_content": {key: len(value) for key, value in hashes.items() if len(value) > 1},
        "frozen_prepare_reference_matches": sum(
            row.get("matches_frozen_prepared_content") is True for row in successful
        ),
        "frozen_prepare_reference_mismatches": sum(
            row.get("matches_frozen_prepared_content") is False for row in successful
        ),
        "frozen_prepare_reference_unmeasured": sum(
            row.get("matches_frozen_prepared_content") is None for row in successful
        ),
        "optimizer_events": diagnostics(events),
    }


def source_fingerprint(root: Path) -> dict[str, str]:
    paths = list((root / "src/powercontext/builtin/runtime").glob("prepared_*.py"))
    paths += [
        root / "src/powercontext/builtin/runtime/application.py",
        root / "evaluation/token_optimization/live.py",
        Path(__file__).resolve(),
    ]
    return {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}


def summarize_datasets(rows: list[dict[str, Any]]) -> dict[str, Any]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        groups.setdefault(row["dataset"], []).append(row)
    return {
        name: {
            "requests": len(values),
            "errors": sum(row["status"] != "ok" for row in values),
            "latency_all_requests": percentiles([row["latency_ms"] for row in values]),
            "content_tokens": sum(row.get("content_tokens", 0) for row in values),
            "content_bytes": sum(row.get("content_bytes", 0) for row in values),
        }
        for name, values in groups.items()
    }


def measure_cell(
    settings: Any,
    database: OceanBaseConfig,
    token: str,
    mode: str,
    concurrency: int,
    arguments: argparse.Namespace,
    snapshots: dict[str, Any],
    requests: list[tuple[str, dict[str, Any]]],
    encoder: Any,
    collector: EventCollector,
    statements: StatementCollector,
    directory: Path,
    boundary: str,
) -> dict[str, Any]:
    started = time.perf_counter()
    with server(configure(settings, database, token, mode), directory / f"scheduler-{mode}-{concurrency}.db") as url:
        startup_seconds = time.perf_counter() - started
        check_exact_before = arguments.snapshot_checks == "cell" or boundary == "start"
        check_exact_after = arguments.snapshot_checks == "cell" or boundary == "end"
        if check_exact_before:
            with httpx.Client(base_url=url, headers={"Authorization": "Bearer " + token}, timeout=120) as client:
                if not exact_reads(client, snapshots):
                    raise ValueError("Public exact Artifact revision changed before load")  # noqa: TRY003
        collector.drain()
        statements.start()
        first_rows, first_seconds = asyncio.run(
            run_batch(url, token, requests, concurrency, concurrency, arguments.timeout_seconds)
        )
        first_sql = statements.stop()
        first_events = collector.drain()
        fingerprint(first_rows, encoder, snapshots, mode)
        # Traverse all independent tasks before measuring steady state. This creates no model calls.
        warmup, _ = asyncio.run(run_batch(url, token, requests, 1, len(requests), arguments.timeout_seconds))
        fingerprint(warmup, encoder, snapshots, mode)
        collector.drain()
        statements.start()
        rows, elapsed = asyncio.run(
            run_batch(url, token, requests, concurrency, arguments.requests, arguments.timeout_seconds)
        )
        warm_sql = statements.stop()
        events = collector.drain()
        fingerprint(rows, encoder, snapshots, mode)
        with httpx.Client(base_url=url, headers={"Authorization": "Bearer " + token}, timeout=120) as client:
            unchanged = exact_reads(client, snapshots) if check_exact_after else latest_reads(client, snapshots)
    result = {
        "mode": mode,
        "concurrency": concurrency,
        "startup_seconds": startup_seconds,
        "unprimed_first_wave": summarize(first_rows, first_seconds, first_events),
        "unprimed_first_wave_sql": first_sql,
        "warmup_requests": len(warmup),
        "warmup_errors": sum(row["status"] != "ok" for row in warmup),
        "warm": summarize(rows, elapsed, events),
        "warm_by_dataset": summarize_datasets(rows),
        "warm_sql": warm_sql,
        "exact_reads_verified_before": check_exact_before,
        "exact_reads_verified_after": check_exact_after,
        "native_exact_reads_unchanged_after": unchanged if check_exact_after else None,
        "latest_revisions_unchanged_after": unchanged,
        "observations": {"unprimed_first_wave": first_rows, "warmup": warmup, "warm": rows},
    }
    save(directory / f"{mode}-concurrency-{concurrency}.json", result)
    if not unchanged:
        raise ValueError("Frozen Artifact snapshot changed during load")  # noqa: TRY003
    return result


def paired_summary(cells: list[dict[str, Any]]) -> list[dict[str, Any]]:
    pairs = []
    by_key = {(cell["mode"], cell["concurrency"]): cell for cell in cells}
    for concurrency in sorted({cell["concurrency"] for cell in cells}):
        off = by_key["off", concurrency]
        active = by_key["active", concurrency]
        baseline = {row["index"]: row for row in off["observations"]["warm"]}
        paired = [
            (baseline[row["index"]], row)
            for row in active["observations"]["warm"]
            if row["status"] == "ok" and baseline[row["index"]]["status"] == "ok"
        ]
        pairs.append({
            "concurrency": concurrency,
            "paired_successful_requests": len(paired),
            "changed_content_pairs": sum(left["content_sha256"] != right["content_sha256"] for left, right in paired),
            "strictly_smaller_pairs": sum(left["content_tokens"] > right["content_tokens"] for left, right in paired),
            "increased_token_pairs": sum(left["content_tokens"] < right["content_tokens"] for left, right in paired),
            "origin_comparisons_unmeasured_pairs": sum(
                not (left["origins_measured"] and right["origins_measured"]) for left, right in paired
            ),
            "origin_changed_pairs": sum(
                left["rendered_origins"] != right["rendered_origins"]
                for left, right in paired
                if left["origins_measured"] and right["origins_measured"]
            ),
            "off_content_tokens": sum(left["content_tokens"] for left, _ in paired),
            "active_content_tokens": sum(right["content_tokens"] for _, right in paired),
        })
    return pairs


def execute(arguments: argparse.Namespace) -> None:
    allowed = Path("/data/codex-tmp").resolve()
    output = arguments.output_dir.resolve()
    if not output.is_relative_to(allowed):
        raise ValueError("HTTP load artifacts must remain under /data/codex-tmp")  # noqa: TRY003
    directory = output / ("http-load-" + uuid.uuid4().hex)
    directory.mkdir(parents=True, mode=0o700)
    arguments.artifact_directory = directory
    os.environ["TMPDIR"] = str(directory)
    if arguments.tokenizer_cache:
        os.environ["TIKTOKEN_CACHE_DIR"] = str(arguments.tokenizer_cache.resolve())
    encoder = tiktoken.get_encoding(arguments.tokenizer)
    sources = source_fingerprint(arguments.repo_root.resolve())
    for relative_path in sources:
        target = directory / "source-snapshot" / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((arguments.repo_root / relative_path).read_bytes())
    manifest: dict[str, Any] = {
        "kind": "actual loopback TCP HTTP prepare load with configured OceanBase",
        "snapshot_directory": str(arguments.snapshot_dir.resolve()),
        "snapshot_checks": arguments.snapshot_checks,
        "concurrencies": arguments.concurrency,
        "warm_requests_per_cell": arguments.requests,
        "max_warm_requests": len(arguments.concurrency) * 2 * arguments.requests,
        "reader_calls": 0,
        "model_quality_measured": False,
        "tokenizer": arguments.tokenizer,
        "tokenizer_package_version": importlib.metadata.version("tiktoken"),
        "runtime_sources": sources,
        "client_platform": platform.platform(),
        "client_cpu_count": os.cpu_count(),
        "cold_definition": (
            "new Server application/runtime in the existing evaluation process, first prepare wave; "
            "OceanBase caches are not reset"
        ),
        "tail_limit": "first wave has only concurrency samples; p95/p99 are descriptive and not capacity estimates",
        "load_model": "closed loop, fixed outstanding workers; not an open-loop production traffic estimate",
        "latency_definition": "client POST through complete JSON decode; token counting outside measured batch",
        "origin_parser": "existing Markdown literal citation parser; unknown/JSON bodies marked unmeasured",
        "retrieval": "fixed FTS, no embeddings/reranker/recall gate/background processing",
        "sqlalchemy_emitted_statement_count_measured": True,
        "database_internal_work_measured": False,
        "phase": "configuration",
    }
    save(directory / "manifest.json", manifest)
    collector = collect_events()
    with (
        capture_statements() as statements,
        server_settings_context(
            host="127.0.0.1", env_file=arguments.env_file, process_environment_overrides=False
        ) as settings,
    ):
        if not isinstance(settings.database, OceanBaseConfig):
            raise TypeError("HTTP load requires configured OceanBase")  # noqa: TRY003
        database, name, snapshots = reuse_database(settings.database, arguments.snapshot_dir)
        requests = requests_for(snapshots)
        manifest.update(
            isolated_database=name,
            database_created=False,
            database_retained=True,
            snapshot_hash=digest(snapshots),
            request_hash=digest(requests),
            independent_case_count=len(requests),
            runtime_target_tokens=settings.runtime.token_optimization_target_tokens,
            runtime_tokenizer=settings.runtime.token_optimization_tokenizer,
            phase="load",
        )
        save(directory / "manifest.json", manifest)
        token = secrets.token_urlsafe(32)
        cells = []
        for index, concurrency in enumerate(arguments.concurrency):
            for mode in ("off", "active") if index % 2 == 0 else ("active", "off"):
                cell = measure_cell(
                    settings,
                    database,
                    token,
                    mode,
                    concurrency,
                    arguments,
                    snapshots,
                    requests,
                    encoder,
                    collector,
                    statements,
                    directory,
                    "start" if not cells else "end" if len(cells) == len(arguments.concurrency) * 2 - 1 else "middle",
                )
                cells.append(cell)
                summary = {key: value for key, value in cell.items() if key != "observations"}
                print(json.dumps(summary, sort_keys=True), flush=True)
        save(
            directory / "summary.json",
            {
                "cells": [{key: value for key, value in cell.items() if key != "observations"} for cell in cells],
                "paired": paired_summary(cells),
            },
        )
    manifest.update(
        phase="complete",
        runtime_sources_unchanged=source_fingerprint(arguments.repo_root.resolve()) == sources,
        native_exact_reads_unchanged=cells[-1]["native_exact_reads_unchanged_after"],
        latest_revisions_unchanged=all(cell["latest_revisions_unchanged_after"] for cell in cells),
        measured_warm_requests=sum(cell["warm"]["requests"] for cell in cells),
        total_preparation_requests=sum(
            cell["warm"]["requests"] + cell["unprimed_first_wave"]["requests"] + cell["warmup_requests"]
            for cell in cells
        ),
        warm_errors=sum(cell["warm"]["errors"] for cell in cells),
    )
    save(directory / "manifest.json", manifest)
    print(json.dumps({"artifact_directory": str(directory), "reader_calls": 0, "warm_errors": manifest["warm_errors"]}))


def positive_integer(value: str) -> int:
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("must be positive")  # noqa: TRY003 - CLI parsing validation
    return number


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot-dir", type=Path, required=True)
    parser.add_argument(
        "--snapshot-checks",
        choices=("cell", "experiment"),
        default="cell",
        help="Exact revision checks per cell, or experiment boundaries plus per-cell latest revision checks",
    )
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--tokenizer", choices=("o200k_base", "cl100k_base"), default="o200k_base")
    parser.add_argument("--tokenizer-cache", type=Path)
    parser.add_argument("--requests", type=positive_integer, default=100)
    parser.add_argument("--concurrency", type=positive_integer, nargs="+", default=[1, 4, 8])
    parser.add_argument("--timeout-seconds", type=positive_integer, default=120)
    arguments = parser.parse_args()
    if len(set(arguments.concurrency)) != len(arguments.concurrency):
        parser.error("concurrency values must be unique")
    try:
        execute(arguments)
    except Exception as error:
        directory = getattr(arguments, "artifact_directory", None)
        if directory:
            save(directory / "failure.json", {"status": "failed", "error_type": type(error).__name__})
        print(json.dumps({"status": "failed", "error_type": type(error).__name__}))
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
