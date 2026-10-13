# Configured-service acceptance results

The implemented ordinary active compiler reduced actual provider input by **1.13% across all 18 questions** and
**3.93% across the six questions with smaller prepared bodies**. Every ordinary answer passed its independently
specified checks. An experimental Markdown policy reduced input by **25.17% in three eligible held-out questions**;
that policy remains outside ordinary active mode. These observations establish a working HTTP/database/model path
and limited applicability, not universal savings or a cheaper bill.

## Implementation and measurement boundary

The compiler runs inside the existing preparation operation, after retrieval/admission and before delivery. No public
request/response field, HTTP/MCP operation, database table, or migration was added. The default deployment mode is
`off`; `shadow` returns the legacy body while evaluating proposals. Ordinary `active` enables exact Experience-field
deduplication, exact Topic summary/snippet association, and complete flat JSON-row representations. Markdown section
selection and coverage-based removal remain shadow candidates; the production lossy-policy allowlist is empty.

Acceptance used a real loopback TCP HTTP Server, an isolated newly created OceanBase database under the configured
account, and the generation service configured in `.env`. It used fixed FTS retrieval, with embeddings, extraction,
reranking, recall expansion, and background processing disabled. Public repository documents supplied the material.
Scope/query/revisions, independent gold answers, and material hashes were frozen before calls. Exact Artifact readback
and rendered origins stayed unchanged. Native coding-agent hosts and customer histories were not evaluated.

## Ordinary active: full cohort and eligible subset

The main dataset has ten repository-documentation questions, four questions about actual integration/OpenAPI rows,
two existing FAQ paragraphs, and two Atomic negative controls. It contains no artificially repeated Experience fields.
`off`, `active`, and a reasonable legacy assembly limit of one were compared. The latter is already minimal when only
one Artifact is eligible, so identical output counts as no improvement.

The final cohort below uses the complete eight-call paired catalog confirmation together with 46 unchanged observations
from the original 54-call batch. All 54 final-cohort prompts were matched to actual paid observations by full prompt
and body SHA-256. This is a disclosed two-batch composite, not a newly executed uninterrupted 54-call experiment.

| Metric, 18 questions per arm | Off | Active | Assembly limit 1 |
|---|---:|---:|---:|
| Correct answers | 18/18 | 18/18 | 18/18 |
| Serious wrong-contract answers | 0 | 0 | 0 |
| Prepared-body tokens, `o200k_base` | 9,096 | 8,900 | 9,096 |
| Actual provider input tokens | 11,413 | 11,284 | 11,413 |
| Actual provider output tokens | 10,933 | 10,639 | 10,698 |
| Observed input + output | 22,346 | 21,923 | 22,111 |
| Cached input tokens, already included in input | 1,024 | 0 | 1,536 |
| Uncached input tokens | 10,389 | 11,284 | 9,877 |
| Mean HTTP prepare seconds | 0.2623 | 0.2316 | 0.3308 |
| Mean Reader seconds | 11.1018 | 10.8777 | 10.8103 |

Six of 18 prepared bodies became smaller; the other twelve were no-ops. In the six-case subset, actual input was
3,286 → 3,157, output was 4,070 → 3,732, and observed input + output was 7,356 → 6,889. The complete-body reduction
across all cases was 2.15%, while actual full-model-input reduction was 1.13%. They are different measurements.

The four actual catalog questions saved only 14 provider input tokens in aggregate: two integration questions saved
11 each, while each of two OpenAPI questions **increased by four** despite smaller `o200k_base` bodies. The two exact
FAQ associations saved 29 and 86 input tokens. The selected profile does not match every provider tokenizer.

An alternative source-matched view retaining all original baselines and replacing only four active observations
(50 original + four confirmation) has the same input totals; its output totals are 10,716 → 10,639 and observed
input + output is 22,129 → 21,923. The difference demonstrates why output variation must not be attributed entirely
to compression. The paired view is used in the table, and neither view supports a billing-savings claim: cache heat
differs and active uncached input increased. No provider price policy was used.

[Per-case aggregate CSV](results.csv) contains only safe counts and rule names, without prompts or credentials.

## Experimental Markdown policy through real HTTP

Six independent held-out questions were frozen. Three had eligible complete Markdown groups and received six actual
paired Reader calls. Three documents containing unsupported code-block structures retained legacy content and made
no Reader calls. The experiment temporarily enabled `markdown-sections-v1` only in its evaluation process and restored
the previous value. Deployment settings and the production empty allowlist were not changed.

| Eligible question | Provider input, off → candidate | Provider output, off → candidate | Correct, off → candidate |
|---|---:|---:|---|
| Unsupported integration contract | 726 → 519 | 508 → 826 | Yes → Yes |
| Request logs | 739 → 603 | 616 → 571 | Yes → Yes |
| Team dashboard access conditions | 704 → 501 | 501 → 580 | No → Yes |
| Total, three questions | 2,169 → 1,623 | 1,625 → 1,977 | 2/3 → 3/3 |

Input fell 25.17%; output rose 21.66%; observed input + output fell 3,794 → 3,600 (5.11%). There were no serious
wrong-contract answers and no cache hits. The legacy per-item byte fitter cut off the dashboard's later access
conditions, so the baseline returned null; selection of the complete relevant group retained both conditions.

Separate offline calibration/held-out qualification had three eligible questions each, with both arms correct in all
six. It compared full source rendering against excerpts, a different baseline from the capped HTTP preparation.
Its larger gains must not be reported as product gains. These small sets do not establish statistical noninferiority
for arbitrary Markdown; the policy is not promoted by these runners.

## Compiler performance and acceptance limits

The reproducible CPU benchmark exercises the production Builder and compiler with synthetic inputs and no model,
database, or HTTP. With 20 warm repetitions per scenario and 32 calls across four threads per target, all 480 warm
calls and 64 threaded calls completed without a planning-deadline fallback. The large default-target candidates
still correctly returned legacy content when they could not meet 1,800 tokens.

At a relaxed 20,000-token target, approximately 20 KiB mixed inputs changed JSON 2,600 → 2,004 tokens (22.92%) and
Markdown 4,871 → 4,119 (15.44%), with median compiler costs of 10.90 ms and 39.06 ms respectively. Eight-topic
inputs changed JSON 2,130 → 1,362 (36.06%), median 9.95 ms, and Markdown 2,345 → 1,577 (32.75%), median 16.92 ms.
These are synthetic preparation gains, not additional provider or task results. Shared-host timings are not SLAs.
Contextual pruning is bounded and can miss a feasible or globally cheaper representation; final complete-render
token and byte checks remain authoritative.

Validation passed the relevant 395-test regression batch, the final 192-test compiler/configuration/HTTP batch,
five evaluation-metric regressions, `make check`, and `make docs-test` (920 exported public pages and internal links).
The HTTP E2E unit fixtures use ASGI/SQLite; the configured-service experiments above separately establish actual
TCP/OceanBase/model execution. They do not establish an installed native host workflow or automatic memory ingestion.

Across the complete configured Reader experiment, 81 persisted actual attempts reported 53,467 input and 48,113
output tokens (101,580 combined); no attempt had missing usage or a transport/HTTP failure. The 81 calls comprise
one authenticity probe, 12 offline qualification calls, 54 ordinary calls, six experimental HTTP calls, and eight
paired confirmations. Mock tests and CPU runs are excluded. No monetary cost estimate is claimed.

See [README.md](README.md) for explicit call counts, snapshot reuse, policy boundaries, and reproduction commands.
Raw paid answers, frozen fixtures, source snapshots, and per-observation SHA evidence are retained locally under the
evaluation artifact directory; they are not committed.
