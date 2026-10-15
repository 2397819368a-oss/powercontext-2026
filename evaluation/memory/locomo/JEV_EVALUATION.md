# Jev Memory reranking evidence

Task: #1937, restored from #1643.

The implementation uses the existing `MemoryReranker` injection boundary over
independent Atomic Memory Artifacts. Policy:
`powercontext.memory.rerank.jev.noul.v1`; protocol fixtures use `jev-1.13.0`.
Jev is opt-in and requires a dedicated key and pinned model. The standard
Runtime and LLM reranker retain their defaults.

## Execution coverage

SQLite acceptance uses a real local database and the actual Runtime assembly,
HTTP authorization, exact-revision read path, and PreparedContext builder.
It covers successful scoring, rate-limit fallback, total-deadline fallback,
cross-Scope rejection before provider requests, identity preservation, read-only
Memory state, UTF-8 delivery limits, and suppression of automatic recall expansion.

Provider responses in these tests use HTTPX MockTransport. They establish local
protocol handling and failure behavior, not Jev's semantic accuracy, availability,
network latency, or billed cost. A persisted-candidate evaluation test exercises
search, answer generation, and judging with deterministic local inference.

The same acceptance scenarios support an isolated real OceanBase database through
`POWERCONTEXT_TEST_OCEANBASE_URL`. The fixture creates and removes its own database;
the connection must permit those operations. OceanBase scenarios are skipped when
the setting is absent.

```bash
uv run python -m pytest \
  tests/builtin/artifacts/memory/test_jev_reranking.py \
  tests/e2e/test_jev_memory_reranking.py \
  tests/evaluation/test_jev_rerank_comparison.py
```

## Quality and cost decision

Recommendation: **revise pending live comparative evidence**.

No live three-arm run of this Noul adapter is recorded here. The execution
environment has no configured Jev credentials or OceanBase acceptance connection.
Relevant-evidence recall, ranking quality, downstream task success, real p50/p95
latency, billed input, retries, and total pipeline cost are therefore unmeasured.
There is no basis for changing the default provider or claiming a quality, latency,
or cost improvement.

The runnable comparison in [README.md](README.md#jev-memory-comparison) fixes the
inputs and delivery budget and rejects candidate/model/budget drift. The quality
regression tolerance is `0.0`, recorded before evaluation. A report with missing
cost receipts cannot recommend adoption. The bilingual/adversarial smoke cases
are separate from any future calibration and held-out evaluation.

Complete live Jev and isolated OceanBase runs, retain reviewed non-secret manifests
and comparison results, and evaluate a dedicated reranker baseline if available
before closing the issue's empirical acceptance criteria. Include the shared
ingestion cost when reporting total pipeline cost; the comparator reports the
search-to-answer evaluation portion.
