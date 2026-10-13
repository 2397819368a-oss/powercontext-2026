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

"""Reproducible local CPU measurements of the production context compiler.

Synthetic inputs exercise the existing Builder and exact complete-render token
counter. This is neither an HTTP stress test nor a database, model, or Agent
acceptance experiment. No network or credential loading is performed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import platform
import statistics
import subprocess
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from time import perf_counter
from typing import Any, Literal

from powercontext.artifacts import ArtifactRef
from powercontext.builtin.artifacts.experience import ExperienceContent, ExperienceSearchHit
from powercontext.builtin.artifacts.topic_memory import TopicMemorySearchHit
from powercontext.builtin.inference.tokens import TokenEstimator
from powercontext.builtin.persistence.atomic_memory_index import AtomicMemoryIndexHit
from powercontext.builtin.runtime.atomic_memory import AtomicMemorySearchHit
from powercontext.builtin.runtime.models import ContextAssembly, PrepareContextRequest
from powercontext.builtin.runtime.prepared_context import (
    PreparedContextBuild,
    PreparedContextBuilder,
    PreparedExperienceCandidates,
    PreparedMemoryCandidates,
)
from powercontext.builtin.runtime.prepared_optimization import OptimizationResult, PreparedTokenOptimizer
from powercontext.builtin.runtime.prepared_tokens import load_prepared_token_counter

Profile = Literal["cl100k_base", "o200k_base"]


@dataclass(frozen=True)
class Scenario:
    name: str
    description: str
    memories: tuple[AtomicMemorySearchHit, ...] = ()
    topics: tuple[TopicMemorySearchHit, ...] = ()
    experiences: tuple[ExperienceSearchHit, ...] = ()

    def text_bytes(self) -> int:
        texts = [hit.hit.text for hit in self.memories]
        texts.extend(value for hit in self.topics for value in (hit.title, hit.summary, hit.snippet or ""))
        texts.extend(
            getattr(hit.content, key)
            for hit in self.experiences
            for key in ("situation", "action", "outcome", "lesson")
        )
        return sum(len(text.encode("utf-8")) for text in texts)


def prose(size: int, *, seed: int) -> str:
    """Deterministic distinct sentences; sizes describe input, never gain targets."""
    sentences = []
    while len(" ".join(sentences)) < size:
        index = len(sentences)
        sentences.append(
            f"Service {seed} observation {index}: record request identity, endpoint version, "
            "retry count and measured duration; retry confirmed idempotent reads only."
        )
    return " ".join(sentences)[:size]


def memory(index: int, text: str) -> AtomicMemorySearchHit:
    return AtomicMemorySearchHit(
        hit=AtomicMemoryIndexHit(
            artifact_ref=ArtifactRef(family="atomic-memory", artifact_id=f"memory-{index}", revision=1),
            state_version=0,
            kind="fact",
            text=text,
            score=1.0,
        ),
        matched_by=("fts",),
    )


def topic(index: int, size: int, *, duplicate: bool) -> TopicMemorySearchHit:
    summary = prose(size, seed=index)
    return TopicMemorySearchHit(
        artifact_ref=ArtifactRef(family="topic-memory", artifact_id=f"topic-{index}", revision=1),
        title=f"Service {index} operating guidance",
        summary=summary,
        snippet=summary if duplicate else None,
        score=1.0,
        matched_by=("topic_fts",),
    )


def scenarios() -> tuple[Scenario, ...]:
    experiences = tuple(
        ExperienceSearchHit(
            artifact_ref=ArtifactRef(family="experience", artifact_id=f"experience-{index}", revision=1),
            content=ExperienceContent(
                situation=prose(160, seed=index + 100),
                action=prose(350, seed=index + 200),
                outcome=prose(120, seed=index + 300),
                lesson=prose(350, seed=index + 200),
            ),
        )
        for index in range(2)
    )
    return (
        Scenario(
            "small_duplicate",
            "Approximately 1 KiB of text; exact Topic summary/snippet duplicate plus one intact Atomic condition.",
            memories=(memory(0, prose(300, seed=1000)),),
            topics=(topic(0, 300, duplicate=True),),
        ),
        Scenario(
            "mixed_20k_input",
            "Approximately 20 KiB of text; unique Atomic records plus repeated Topic and Experience fields.",
            memories=tuple(memory(index, prose(1400, seed=index + 1000)) for index in range(8)),
            topics=tuple(topic(index, 800, duplicate=True) for index in range(4)),
            experiences=experiences,
        ),
        Scenario(
            "many_variants",
            "Eight distinct Topic identities with exact duplicates; exercises the bounded planner branch count.",
            topics=tuple(topic(index, 520, duplicate=True) for index in range(8)),
        ),
        Scenario(
            "overlimit_input",
            "More than 32 KiB of text; existing per-entry and total byte truncation precede protected fallback.",
            memories=tuple(memory(index, prose(3000, seed=index + 1000)) for index in range(16)),
            topics=(topic(0, 800, duplicate=True), topic(1, 800, duplicate=True)),
        ),
        Scenario(
            "unknown_prose",
            "No matching representation rule; no savings expected and original content must be returned.",
            topics=(topic(0, 900, duplicate=False),),
        ),
        Scenario(
            "protected_atomic",
            "An 8 KiB authoritative Atomic body cannot be shortened by compiler rules; fallback is expected.",
            memories=(memory(0, prose(8192, seed=1000)),),
        ),
    )


def request(markdown: bool) -> PrepareContextRequest:
    arguments: dict[str, Any] = {"query": "idempotent retry request identity", "max_bytes": 32768}
    if markdown:
        arguments["assembly"] = ContextAssembly.model_validate({
            "sections": [
                {"family": "memory", "limit": 8},
                {"family": "topic-memory", "limit": 8},
                {"family": "experience", "limit": 2},
            ],
            "show": ["confidence", "recall_rank"],
        })
    return PrepareContextRequest(**arguments)


def inputs(scenario: Scenario) -> dict[str, Any]:
    return {
        "current_scope_id": "compiler-benchmark",
        "memory_candidates": (PreparedMemoryCandidates("compiler-benchmark", scenario.memories),),
        "topic_memory_hits": scenario.topics,
        "experience_candidates": (PreparedExperienceCandidates("compiler-benchmark", scenario.experiences),),
    }


def content(build: PreparedContextBuild) -> str:
    return build.context.content or ""


def distribution(samples: list[float]) -> dict[str, float]:
    ordered = sorted(samples)
    return {
        "min_ms": min(ordered),
        "median_ms": statistics.median(ordered),
        "p95_ms": ordered[max(0, math.ceil(len(ordered) * 0.95) - 1)],
        "max_ms": max(ordered),
    }


def measure(
    optimizer: PreparedTokenOptimizer,
    builder: PreparedContextBuilder,
    query: PrepareContextRequest,
    legacy: PreparedContextBuild,
    values: dict[str, Any],
) -> tuple[float, OptimizationResult]:
    start = perf_counter()
    result = optimizer.optimize(request=query, builder=builder, legacy=legacy, **values)
    return (perf_counter() - start) * 1000, result


def summarize(measured: list[tuple[float, OptimizationResult]], counter: TokenEstimator) -> dict[str, Any]:
    return {
        "calls": len(measured),
        "compiler": distribution([duration for duration, _ in measured]),
        "accepted_calls": sum(result.build is result.proposed for _, result in measured),
        "fallback_reasons": dict(Counter(result.fallback_reason for _, result in measured if result.fallback_reason)),
        "delivered_tokens": sorted({counter.estimate(content(result.build)) for _, result in measured}),
        "proposed_tokens": sorted({
            result.proposed_tokens for _, result in measured if result.proposed_tokens is not None
        }),
        "rules": sorted({rule for _, result in measured for rule in result.rules}),
    }


def cold_measure(profile: Profile) -> dict[str, Any]:
    start = perf_counter()
    counter = load_prepared_token_counter(profile)
    loaded = perf_counter()
    if counter is None:
        raise RuntimeError("Pinned local vocabulary unavailable; benchmark performs no download")  # noqa: TRY003
    counter.estimate("PowerContext compiler initialization")
    return {
        "counter_construction_ms": (loaded - start) * 1000,
        "first_encode_ms": (perf_counter() - loaded) * 1000,
        "profile": counter.profile.model_dump(),
    }


def run(arguments: argparse.Namespace) -> dict[str, Any]:
    counter = load_prepared_token_counter(arguments.tokenizer)
    if counter is None:
        raise RuntimeError("Pinned local vocabulary unavailable; benchmark performs no download")  # noqa: TRY003
    counter.estimate("Warm encode excluded from request timing")
    output = arguments.output_dir
    output.mkdir(parents=True, exist_ok=True)
    builder = PreparedContextBuilder()
    optimizer = PreparedTokenOptimizer("active", arguments.target_tokens, counter)
    jobs = []
    rows = []
    for scenario in scenarios():
        for markdown in (False, True):
            query, values = request(markdown), inputs(scenario)
            legacy = builder.build_scopes_result(request=query, **values)
            measure(optimizer, builder, query, legacy, values)  # Warm the full path, excluded from measurements.
            baseline_times = []
            measured = []
            active_total_times = []
            for _ in range(arguments.repetitions):
                start = perf_counter()
                baseline = builder.build_scopes_result(request=query, **values)
                baseline_times.append((perf_counter() - start) * 1000)
                start = perf_counter()
                active_baseline = builder.build_scopes_result(request=query, **values)
                duration, result = measure(optimizer, builder, query, active_baseline, values)
                active_total_times.append((perf_counter() - start) * 1000)
                if content(baseline) != content(legacy) or result.build.origins != legacy.origins:
                    raise RuntimeError("Builder baseline or active origin contract changed")  # noqa: TRY003
                measured.append((duration, result))
            tokens = counter.estimate(content(legacy))
            row = {
                "scenario": scenario.name,
                "description": scenario.description,
                "format": "markdown" if markdown else "json",
                "candidate_text_bytes": scenario.text_bytes(),
                "baseline_bytes": legacy.context.content_bytes,
                "baseline_tokens": tokens,
                "baseline_items": len(legacy.origins),
                "legacy_truncated_items": legacy.omissions.truncated_items,
                "legacy_dropped_items": legacy.omissions.dropped_items,
                "baseline_builder": distribution(baseline_times),
                "active_builder_and_compiler": distribution(active_total_times),
                **summarize(measured, counter),
            }
            delivered = row["delivered_tokens"]
            row["delivered_token_savings_percent"] = (
                [100 * (tokens - value) / tokens for value in delivered] if tokens else []
            )
            rows.append(row)
            sample = measured[0][1]
            sample_name = f"{scenario.name}-{'markdown' if markdown else 'json'}"
            (output / f"{sample_name}-off.txt").write_text(content(legacy), encoding="utf-8")
            (output / f"{sample_name}-active.txt").write_text(content(sample.build), encoding="utf-8")
            jobs.append((scenario.name, markdown, query, legacy, values))
    start = perf_counter()
    with ThreadPoolExecutor(max_workers=arguments.workers) as executor:
        futures = [
            (job[0], job[1], executor.submit(measure, optimizer, builder, job[2], job[3], job[4]))
            for index in range(arguments.concurrent_calls)
            for job in [jobs[index % len(jobs)]]
        ]
        concurrent = [(name, markdown, future.result()) for name, markdown, future in futures]
    concurrent_wall_ms = (perf_counter() - start) * 1000
    return {
        "scope": "Synthetic production-compiler CPU benchmark; no HTTP, database, model or native Agent.",
        "host": {
            "python": platform.python_version(),
            "machine": platform.machine(),
            "cpu_count": os.cpu_count(),
            "load_average_at_completion": os.getloadavg() if hasattr(os, "getloadavg") else None,
        },
        "tokenizer": counter.profile.model_dump(),
        "target_tokens": arguments.target_tokens,
        "repetitions": arguments.repetitions,
        "warm_rows": rows,
        "concurrent": {
            "workers": arguments.workers,
            "wall_ms": concurrent_wall_ms,
            "calls_per_second": arguments.concurrent_calls / (concurrent_wall_ms / 1000),
            **summarize([measured for _, _, measured in concurrent], counter),
            "observations": [
                {
                    "scenario": name,
                    "format": "markdown" if markdown else "json",
                    "compiler_ms": measured[0],
                    "accepted": measured[1].build is measured[1].proposed,
                    "fallback_reason": measured[1].fallback_reason,
                }
                for name, markdown, measured in concurrent
            ],
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tokenizer", choices=("cl100k_base", "o200k_base"), default="o200k_base")
    parser.add_argument("--tokenizer-cache", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, default=Path("/data/codex-tmp/powercontext-token-performance"))
    parser.add_argument("--repo-root", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--target-tokens", type=int, default=1800)
    parser.add_argument("--repetitions", type=int, default=20)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--concurrent-calls", type=int, default=32)
    parser.add_argument("--cold-child", action="store_true", help=argparse.SUPPRESS)
    arguments = parser.parse_args()
    if min(arguments.repetitions, arguments.workers, arguments.concurrent_calls, arguments.target_tokens) < 1:
        parser.error("repetitions, workers, concurrent calls and target tokens must be positive")
    os.environ["TIKTOKEN_CACHE_DIR"] = str(arguments.tokenizer_cache.resolve())
    if arguments.cold_child:
        print(json.dumps(cold_measure(arguments.tokenizer), ensure_ascii=False))
        return
    runtime_paths = (
        "src/powercontext/builtin/runtime/prepared_context.py",
        "src/powercontext/builtin/runtime/prepared_optimization.py",
        "src/powercontext/builtin/runtime/prepared_tokens.py",
        "src/powercontext/builtin/runtime/prepared_text.py",
        "src/powercontext/builtin/runtime/prepared_excerpts.py",
    )
    runtime_fingerprints = {
        path: hashlib.sha256((arguments.repo_root / path).read_bytes()).hexdigest() for path in runtime_paths
    }
    start = perf_counter()
    # Invoke this module with validated arguments; never execute a shell string.
    child = subprocess.run(  # noqa: S603
        [
            sys.executable,
            "-m",
            "evaluation.token_optimization.performance",
            "--tokenizer",
            arguments.tokenizer,
            "--tokenizer-cache",
            str(arguments.tokenizer_cache.resolve()),
            "--cold-child",
        ],
        cwd=arguments.repo_root,
        capture_output=True,
        text=True,
        check=True,
    )
    cold = json.loads(child.stdout)
    cold["fresh_process_wall_ms_including_imports"] = (perf_counter() - start) * 1000
    results = run(arguments)
    results["cold_start"] = cold
    results["runtime_file_sha256"] = runtime_fingerprints
    results["runtime_files_unchanged_during_measurement"] = all(
        hashlib.sha256((arguments.repo_root / path).read_bytes()).hexdigest() == digest
        for path, digest in runtime_fingerprints.items()
    )
    results["benchmark_file_sha256"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    results["reproduction_argv"] = [sys.executable, "-m", "evaluation.token_optimization.performance", *sys.argv[1:]]
    destination = arguments.output_dir / "results.json"
    destination.write_text(json.dumps(results, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"results": str(destination), "warm_scenarios": len(results["warm_rows"])}, ensure_ascii=False))


if __name__ == "__main__":
    main()
