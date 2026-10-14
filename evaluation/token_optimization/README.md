# Prepared-context token optimization: configured-service experiment

This opt-in experiment exercises a real loopback HTTP Server, a newly created OceanBase database using the selected
configuration's account, and the configured OpenAI-compatible generation service. It does not connect an agent host
or establish production task quality, an official benchmark result, or statistical noninferiority.

The task set freezes 18 questions: 10 repository documentation questions, 4 questions over real integration/OpenAPI
catalog rows, 2 existing short FAQ paragraphs, and 2 Atomic Memory negative controls. Documents remain verbatim
paragraphs, not artificially duplicated Experience fields. Catalog projections keep the complete selected collection,
its order and original field values, recording selected columns and the collection predicate. The integration catalog
has 15 rows (`id`, `kind`, `availability`); the HTTP projection has 8 actual operations (`method`, `path`, `operationId`).
FAQ paragraphs normalize whitespace for the summary presentation while retaining source provenance. Reference
answers are specified independently and are never included in the model input. Every case has a dedicated Scope.
The public exact Artifact revision readback and documentation SHA-256 values identify the snapshot.

Three arms use the same snapshot and question:

- `off`: legacy preparation, Markdown, one selected family, limit 3, no optional metadata.
- `active`: exactly the same request with the Server compiler enabled.
- `assembly`: legacy preparation with the reasonable caller-side limit 1. With one eligible artifact this is an
  intentionally already-minimal control; any identical output is reported as no change rather than improvement.

The experiment fixes FTS retrieval, disables embeddings, reranking, the recall gate, scheduled processing and Dream,
and seeds through the existing HTTP Artifact API without generation. It retains the configured Reader LLM identity.
This isolates preparation from candidate drift and unrelated model costs. It does not evaluate hybrid retrieval or
automatic Source-to-Memory processing.

From an installed source checkout, run the bounded authenticity probe first:

```bash
python -m evaluation.token_optimization.live probe \
  --repo-root "$PWD" --env-file "$PWD/.env" \
  --output-dir /data/codex-tmp/powercontext-token-implementation-20261014 \
  --tokenizer-cache /data/codex-tmp/powercontext-token-strategy-20261012/tokenizer-cache
```

The probe performs exactly one Reader request, with no retry. It creates a new database and does not run the full
comparison. After the compiler implementation is ready, `run` performs 54 Reader requests, interleaving the three
arms and reversing their order on alternating questions. Each invocation creates its own database and artifact
directory. No cleanup deletes the retained database, existing data, or business history.

`run --prepare-only` exercises the same three HTTP arms with zero Reader calls, writing `prepared.json`; its task
quality is explicitly unmeasured. The runtime's existing content-free optimizer diagnostics are captured into each
observation to distinguish no matching rule, fallback, target fit and actual application. Raw log messages, query
text in logs, headers, credentials and provider error bodies are discarded.

Artifacts under the supplied `/data/codex-tmp` directory include:

- `manifest.json`: repository commit, material hash, tokenizer/version, isolation identity and metric boundaries.
- `cases.json`: the complete material and answer fixtures frozen before any model request.
- `snapshots.json`: verbatim material, public exact revision readback and independent reference answers.
- `answers.json`: complete preparation request/response, model input messages, answer text, raw provider usage,
  normalized input/output/cache counts, separate prepare/Reader latency, and deterministic task quality.

Reported provider `prompt_tokens` already includes cached tokens. The experiment does not add cache hits again.
Unreported counts remain `null`, not zero. Tokenizer counts cover complete returned `response.content` and separately
the text of full input messages; only the provider usage proves actual model input usage. The selected tokenizer
profile does not claim to match an undocumented provider tokenizer. All recorded attempts count, including failed
or invalid answers. Price savings remain unreported unless a verified provider price policy is supplied separately.

Quality requires the requested JSON values with exact types. An invented wrong contract value is recorded as a
serious error; missing/null values and invalid JSON still fail the task without being counted as invented contracts.
Native exact Artifact reads are rechecked after all preparation and Reader calls. Negative controls should preserve
the full necessary Atomic text; cache variation on otherwise identical prompts is not compression evidence.

The program suppresses infrastructure logs and prints only safe summary fields. It never prints model credentials,
database URLs, passwords, Bearer tokens, raw error bodies or configured secret headers. All live artifacts stay under
`/data/codex-tmp`; do not commit them.

Keep downloaded corpora, frozen run fixtures, databases, raw answers, reports, CSV files, logs, and intermediate
snapshots outside the repository. The checkout contains reusable harness source and instructions only; published
run results belong in the external artifact directory. The runners enforce external output paths.

For an independently frozen R4 qualification, `qualify calibration` plans five questions and `qualify heldout`
plans six other questions. Both default to zero model calls:

```bash
python -m evaluation.token_optimization.qualify calibration \
  --repo-root "$PWD" --env-file "$PWD/.env" \
  --output-dir /data/codex-tmp/powercontext-token-implementation-20261014 \
  --tokenizer-cache /data/codex-tmp/powercontext-token-strategy-20261012/tokenizer-cache
```

Adding `--call-reader` explicitly opts in to at most two live attempts per question: complete-material baseline and
the existing pure parser's proposal. Full material is rendered through the existing Markdown renderer; its synthetic
citations are marked as offline and it is not submitted to backend recall or HTTP preparation. This baseline is
deliberately distinct from the legacy HTTP fitter's 2000-byte item cap. Qualification savings must therefore not be
reported as product savings. Unsupported real documents with code blocks are preserved as fallbacks. The policy
allowlist remains a runtime decision; this tool neither edits it nor asserts unchanged quality.

Pass `--fixture-dir` pointing at an earlier zero-call qualification directory to reuse the exact pre-frozen material,
question, retrieval query and independent reference answer. The current pure parser computes its proposal again.
Questions with no eligible excerpt make zero model requests. Both calibration and held-out result files preserve
raw usage and answers for every attempted comparison.

After a policy is qualified, the separate real HTTP runs can use `--case-set r4-heldout` or `r4-calibration`.
Some tasks freeze a short retrieval query alongside the complete answer question; both are kept identical across
arms, and model answer requirements never replace the retrieval query. These small sets screen concrete behavior
and do not establish statistically powered quality noninferiority.

`--snapshot-dir` reuses the exact evaluation-owned database, Scope IDs and committed Artifact revisions from an
earlier live run. The material, question and independently specified answers must match, and public exact reads
are verified before reuse. A later `run --prepare-only --snapshot-dir ...` supplies content and full prompt SHA
evidence for replay without billing the model again. The runner may select a documented case subset from a snapshot.

`--case-set r3-confirmation` selects the four integration/OpenAPI catalog tasks and runs only `off`/`active` (eight
Reader calls, or zero with `--prepare-only`). Keep the original run intact. A final report that reuses fourteen
unchanged tasks and supplements four changed tasks must identify both batches; it is not a newly executed,
uninterrupted full comparison. Complete prompt SHA matching proves which paid observation supports each final view.

The separate `experimental` module evaluates the independently frozen R4 held-out materials through real HTTP.
It temporarily enables `markdown-sections-v1` only in its own Python process. It does not edit the production policy
allowlist, deployment settings, or public API, and restores the previous process value on exit. The default is zero
Reader calls; `--call-reader` calls the model only for pairs with strictly fewer returned-text tokens. Any result is
explicitly experimental and cannot be reported as ordinary product active behavior:

```bash
python -m evaluation.token_optimization.experimental \
  --fixture-dir /data/codex-tmp/.../qualification-heldout-... \
  --env-file "$PWD/.env" \
  --output-dir /data/codex-tmp/powercontext-token-implementation-20261014 \
  --tokenizer-cache /data/codex-tmp/powercontext-token-strategy-20261012/tokenizer-cache
```

The selected tokenizer can shrink returned text while a configured provider reports unchanged or increased input
tokens. Preserve and report that disagreement. Prefix cache hits and model reasoning/output counts also vary across
attempts; lower tokenizer counts do not establish lower provider input, lower total task cost, or a lower bill.

Run `python -m evaluation.token_optimization.report /data/codex-tmp/.../run-...` to produce `report.json` from the
recorded live answers. It reports all attempts, usage coverage, paired quality regressions, origin changes, negative
control changes and actual text/model token totals; it does not assert that active output must be shorter.

## Independent external corpora

`corpus prepare` accepts an externally frozen JSON list of cases. Each case has a stable `case_id`, `dataset`, original
`question`, complete `documents`, independent scoring-only `gold`, and a preselected `reader_selected` boolean. A
document has `family: "topic-memory"` and existing `content` fields `title`, `summary`, and `detail`. An optional
`compact_control.documents` supplies a separately labeled reasonable serialization of the same complete material.
Keep input manifests, licensed datasets, selection scripts, and scoring programs outside this checkout.

```bash
python -m evaluation.token_optimization.corpus prepare \
  --fixture-file /data/codex-tmp/.../frozen-cases/cases.json \
  --env-file "$PWD/.env" --repo-root "$PWD" \
  --output-dir /data/codex-tmp/.../runs \
  --tokenizer-cache /data/codex-tmp/.../tokenizer-cache
```

Preparation creates an isolated evaluation-owned database, ingests every published document unit, and freezes ordinary
off/active HTTP output. It makes zero Reader calls. Compact controls use off mode and separate Scope/Artifact identities;
their costs and retrieval behavior must be reported separately. The existing eight-entry Topic section limit applies:
ingesting all documents does not imply that every unit is selected or delivered. Do not filter material by the gold
answer, reference SQL, supporting-paragraph annotations, or measured compression success.

`corpus read --snapshot-dir /data/codex-tmp/.../corpus-prepare-...` explicitly calls the configured Reader for every
preselected observation, including no-op and failed preparations. It consumes the exact frozen prompts without further
database work, so it can run separately from HTTP load. Preserve the independent selection count and all attempts;
raw usage and latency are durably written before any answer scoring. Score outside the checkout with the appropriate
upstream normalization, units/scales and numeric rules rather than the documentation workload's strict Python equality.

For example, WikiSQL can use actual read-only complete `SELECT *` row mappings with independent reference SQL executed
only for scoring. Retain a matched header/rows serialization as a compact control. TAT-QA should retain its original
two-dimensional table and all published paragraph units, without inventing repeated keys to manufacture gains.
These are compiler-specific QA adaptations and not official benchmark leaderboard submissions. Record dataset
revisions, download hashes, transformation rules, licenses, unsupported input counts and exact case-selection seeds in
the external manifest. Run-output reports and CSV files are not repository deliverables.

The separate `load` module measures real loopback TCP HTTP preparation backed by the configured OceanBase, using a
previously frozen evaluation-owned snapshot. It makes **zero Reader calls** and measures neither answer quality nor
provider token usage. Run it against an independent corpus snapshot rather than treating synthetic compiler CPU
measurements as service capacity:

```bash
python -m evaluation.token_optimization.load \
  --snapshot-dir /data/codex-tmp/.../independent-corpus-snapshot \
  --repo-root "$PWD" --env-file "$PWD/.env" \
  --output-dir /data/codex-tmp/powercontext-token-1991-followup-20261014 \
  --tokenizer-cache /data/codex-tmp/powercontext-token-strategy-20261012/tokenizer-cache \
  --concurrency 1 4 8 --requests 100
```

The default measures 600 warm requests: 100 per mode/concurrency combination. Each cell uses a new Server runtime,
records its first unprimed preparation wave, traverses the corpus once to warm preparation, and then measures the
closed-loop batch. First-wave sample counts are only 1/4/8 and cannot establish tail behavior. OceanBase caches are
never flushed; "unprimed" describes the Server's first preparation requests, not a cold database. Client connection
setup remains included in the measured POST latency. Report p50/p95/p99 with sample counts, errors, requests/sec,
response text token counts, content variability and paired citation origins. Complete JSON decoding is inside the
timer; token counting is outside it. No internal database query count is claimed.
An evaluation-process SQLAlchemy listener aggregates emitted SELECT/INSERT/UPDATE/DELETE and FTS MATCH statement
counts per measurement window. It records no SQL text or parameters and does not count native pool ping or database
internal work. It is removed when the harness exits and changes no runtime endpoint or database schema.

`manifest.json` and `snapshots.json` must identify a retained `pc_token_ab_<32hex>` database. Each snapshot contains
`scope_id`, `artifact`, `readback_sha256`, `material`, and `query`/`question`. An optional `request_payload` supplies
the exact existing preparation request, including assembly limits for multiple candidates; its Scope must match.
For multi-candidate tasks, include `artifacts: [{artifact, readback_sha256}, ...]` for every frozen revision. Exact
public reads are checked before and after every cell, and both modes use the same request and snapshot hashes.
For larger corpora, `--snapshot-checks experiment` verifies all exact readback hashes and latest revisions at the
experiment's start and end, and all latest revisions after every cell. Those checks stay outside the latency and SQL
measurement windows. Set `--requests` high enough to traverse every independent case; 150 cases need at least 150
measured requests per cell (use 300 for two complete traversals). Optional snapshot `dataset` labels produce separate
response latency, error and text-token aggregates; optimizer and SQL diagnostics remain whole-window aggregates.
Optional `prepared_content_sha256: {"off": "...", "active": "..."}` records pre-frozen HTTP preparation body hashes.
Every successful observation is checked against its mode's hash; missing references remain explicitly unmeasured.
The literal citation parser supports the existing Markdown renderer; unknown or JSON bodies are marked unmeasured
for origin comparison rather than counted as unchanged.
The manifest records warm, warmup and first-wave request boundaries and preserves the source files outside Git.
The runtime's content-free diagnostics are collected with a thread-safe handler over a whole measurement window;
they are never attributed to an individual concurrent request using a shared last-event variable. All measurements,
response fingerprints, manifests, downloaded corpora and intermediate files remain under `/data/codex-tmp` and must
not be committed to the repository.
