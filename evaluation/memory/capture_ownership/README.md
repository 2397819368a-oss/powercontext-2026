# Capture ownership replay

Evaluation for [#2069](https://github.com/oceanbase/powercontext/issues/2069). It measures whether joined WorkBuddy
submissions re-enter Atomic Memory as new evidence and whether that produces candidates supported outside the current
user turn. Production capture, Source registration, extraction prompts, APIs, and ranking are unchanged.

## Install and validate

Run from the repository root, on Python 3.11+. SQLite needs no external database. Full replay is validated on Linux;
fixture/parser/report tests are also validated on Windows. Synthetic replay does not establish native WorkBuddy acceptance.

```bash
uv sync --project evaluation --extra capture-ownership --locked
uv run --project evaluation --extra capture-ownership capture-ownership validate \
  --manifest evaluation/memory/capture_ownership/fixtures/synthetic.v1.json
uv run --project evaluation --extra capture-ownership pytest evaluation/memory/capture_ownership/tests -q
```

The optional `capture-ownership` extra installs the real Server and inference adapters from this checkout. Pure fixture,
parser and report tests work in the normal evaluation environment; the HTTP/pipeline tests require this extra.

## Freeze before generation

The manifest labels a complete partition of each prompt with both character and UTF-8 byte offsets. Claims identify
their supporting spans, required new durable facts and exact command/path/config literals. Ownership labels and
claim expectations never enter the model request or candidate parser. All committed cases are synthetic.

Choose a new private directory **outside the checkout**. The following uses a sibling directory, not a shared home path.

```bash
mkdir -p ../capture-ownership-private
uv run --project evaluation --extra capture-ownership capture-ownership freeze \
  --manifest evaluation/memory/capture_ownership/fixtures/synthetic.v1.json \
  --config evaluation/memory/capture_ownership/config/offline.json \
  --lock ../capture-ownership-private/offline.lock.json
uv run --project evaluation --extra capture-ownership capture-ownership run \
  --manifest evaluation/memory/capture_ownership/fixtures/synthetic.v1.json \
  --lock ../capture-ownership-private/offline.lock.json \
  --private-dir ../capture-ownership-private/offline-run --mode offline
uv run --project evaluation --extra capture-ownership capture-ownership report \
  --private-dir ../capture-ownership-private/offline-run \
  --output ../capture-ownership-private/offline-report
```

Offline uses a fixed empty FunctionModel through the real structured adapter and pipeline. It verifies persistence,
readback, exact evidence IDs, pairing, input carriers, and fallback behavior. Its decision is always
`insufficient_evidence`; empty simulated output is not evidence of clean real-model extraction.

The lock binds the canonical manifest, provider/model/settings, extraction profile, built-in instructions/version,
host parser version, production and evaluation source file hashes, dependency locks, code commit, repetition count,
budgets, and decision policy. A changed dataset/code/configuration is rejected before replay. Freeze a new experiment
for intentional changes; do not reuse a reviewed output under a different lock.

## Real generation and cost

Keep the generation environment outside Git with owner-only permissions. Use the project's existing inference settings:

```dotenv
POWERCONTEXT_SERVER_INFERENCE_GENERATION_MODEL=openai-chat:qwen-flash-2025-07-28
POWERCONTEXT_SERVER_INFERENCE_GENERATION_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
POWERCONTEXT_SERVER_INFERENCE_GENERATION_MODEL_SETTINGS='{"temperature":0,"max_tokens":1024,"extra_body":{"enable_thinking":false}}'
OPENAI_API_KEY=replace-privately
```

`config/qwen-flash.json` pins this inexpensive, non-thinking snapshot. Its reference rates are 0.15 CNY per million
input tokens and 1.5 CNY per million output tokens for the short-input tier, from the
[official model page](https://help.aliyun.com/zh/model-studio/qwen-flash). Recheck rates and region availability before
using another account/date. These are configurable estimates; the provider's invoice is authoritative.

The example reserves at most 0.8 CNY for the complete run, caps each response at 1024 tokens, uses one request per
extraction with provider SDK retries disabled, runs each arm twice, and spaces requests eight seconds apart. A conservative input reservation uses UTF-8
bytes plus an 8192-token schema/framing allowance. Failed calls retain their reservation because they may be billed.
No calls are made for candidate-arm unknown/empty turns. The actual provider token counts are saved when available;
failed/timeout usage is unknown, never reported as zero. The character estimate used for input limits is separately
reported and is not presented as measured provider usage.

```bash
uv run --project evaluation --extra capture-ownership capture-ownership freeze \
  --manifest evaluation/memory/capture_ownership/fixtures/synthetic.v1.json \
  --config evaluation/memory/capture_ownership/config/qwen-flash.json \
  --lock ../capture-ownership-private/live.lock.json
uv run --project evaluation --extra capture-ownership capture-ownership run \
  --manifest evaluation/memory/capture_ownership/fixtures/synthetic.v1.json \
  --lock ../capture-ownership-private/live.lock.json \
  --private-dir ../capture-ownership-private/live-run --mode live \
  --generation-env-file ../capture-ownership-private/generation.env
```

For another provider, `capture-ownership config --generation-env-file ... --output ...` exports only allowed nonsecret
settings. Add the correct prices and budget to this configuration before freezing. Both arms must use this exact same
configuration. Evaluate another model as a separate experiment, never substitute it midway through a paired run.

## What is actually executed

The existing WorkBuddy `_capture_prompt()` constructs the Source identity and metadata. Only its network transport is
adapted to a real ASGI TestClient. Each case goes through `/v1/sources/content` twice to check idempotency, then a
file-backed SQLite repository readback. The raw captured text must match byte for byte. The registered
`powercontext.text-evidence` v1 output is checked against that persisted text.

On this revision, HTTP native capture stores a `ContentSource`. `project_atomic_memory_evidence()` therefore uses
the **native materialization** branch, not its `SourceObservation.projections` branch. The harness records this path
explicitly, checks the registered projection independently, and passes the actual native evidence into
`AtomicMemoryGenerationPipeline.extract()`. It does not claim that native capture selects a registered projection
internally. The structured adapter, extraction schema, evidence-ID validation and built-in profile are the runtime's.

- `joined-source-v1`: unchanged runtime evidence. The joined raw text occurs in content **and source_metadata**.
- `host-turn-candidate-v1`: a local whole-evidence view with the same Source reference, materialization digest,
  position, evidence ID and ordering. It replaces both raw carriers, retaining only host-parser-selected current text
  and bounded provenance. It is never registered or sent to public capture.

The candidate uses the owning WorkBuddy wrapper helpers with an additional frozen envelope-eligibility gate. Missing,
truncated, mismatched or unrecognized top-level blocks remain unknown; blank current turns remain empty. Recall's
joined-prompt fallback is never used for extraction. Valid current quoted tags remain quoted data, not instructions.
A perfectly imitated host envelope cannot be authenticated from text alone: syntactic eligibility is not trusted
authority. No production contract is proposed based on syntax alone.

## Candidate review and decision

All source text, prepared model inputs, generated candidates, the frozen lock, and `review-template.json` stay in the
private run directory (directory mode 0700). Provider exceptions are recorded by class only. Never commit these files,
even for a private corpus. Reports export only validated IDs/categories/counts/rates/digests/measurements, not raw
Source references, Scope IDs, reviewer identities, prompt text, candidate text, or credentials.

Copy `review-template.json` to `review.json`. For **each output-bound candidate**, a reviewer fills:

- `claim_ids`: frozen claims supporting every part of the candidate; use an empty list for wholly unsupported output.
- `support_span_ids`: additional frozen spans for descriptive claims outside the retention targets, for example a
  current-user introduction explaining that a quoted preference is an example. All claimed and explicit spans determine
  support origin together; quoting a preference does not by itself assert that preference as a new user fact.
- `support_complete`: all candidate claims were checked against the frozen spans.
- `unsupported_claim`: any assertion lacks support, including invented attributions or conditions.
- `current_user_attribution`: a non-current claim is represented as a new user fact/preference/constraint/decision.
- `duplicate_group`: a within-row semantic duplicate group, or null. Repeated history is not itself proof of duplication.

Set a truthful `reviewer` and `complete=true` on each row, including empty/skipped output after checking required facts.
Review hashes bind the annotation to the exact run and candidate. Labels cannot be transferred after regenerating output.
The aggregate records the frozen generation harness hash and a separate hash of the analysis implementation, so saved
outputs can be audited with an identified scorer without regenerating model responses or changing frozen ground truth.
An LLM judge may assist review but must not be the sole evidence for a critical finding: cross-check its exact candidate
against the frozen supporting span IDs, ownership categories and literal canaries. Record independent human review
when performed; agent-assisted annotation must not be described as independent human validation.

```bash
uv run --project evaluation --extra capture-ownership capture-ownership report \
  --private-dir ../capture-ownership-private/live-run \
  --review ../capture-ownership-private/live-run/review.json \
  --output ../capture-ownership-private/live-report
```

The aggregate separates UTF-8 occupancy (including repeated carriers), candidate count/kind, support origin,
unsupported/false attribution, semantic duplicates, required fact/literal retention, parser eligibility, provider usage,
and generation latency. Pending labels remain unknown. Mixed support is not automatically clean. Input occupancy and
cost/latency improvements cannot pass the quality gate.

`projection_follow_up_required` requires two repeats of a contamination case and paired candidate-arm preservation
of every required current fact with zero critical contamination/literal failures. `no_production_change` requires live,
fully reviewed valid rows, clean candidates and successful required-fact positive controls. Failed, incomplete,
unreviewed or offline evidence yields `insufficient_evidence`. Coverage applies to the frozen cases and configured model.

A positive decision emits `follow-up-issue.md`, covering raw retention, identity/materialization/parser version binding,
unknown eligibility, authority separation, idempotency, old-capture compatibility/rebuild, per-host contracts, and
design-first OpenAPI changes. Publish a separate production issue with the reproduced case IDs and aggregate hashes.

## Validation

```bash
uv run pytest tests/workbuddy_plugin/test_hook.py
uv run pytest evaluation/memory/capture_ownership/tests
uv run --project evaluation --extra capture-ownership ty check
uv run prek run -a
uv run ty check
```
