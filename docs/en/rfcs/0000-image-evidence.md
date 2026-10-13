---
title: Image evidence and Artifact integration
---

- Proposal Name: `image_evidence_artifact_integration`
- Start Date: 2026-10-13
- RFC PR: [oceanbase/powercontext#0000](https://github.com/oceanbase/powercontext/pull/0000)
- Tracking Issue: [#1932](https://github.com/oceanbase/powercontext/issues/1932)
- Experimental implementation: [draft PR #1956](https://github.com/oceanbase/powercontext/pull/1956)
- Related RFCs: [Source definitions](1400_source_definition_and_observation_model.md),
  [Source access](1458_artifact_generation_source_access.md), [Artifact families](1549_artifact_family_unification.md),
  [processing supervisor](1515_artifact_processing_supervisor.md), [migrations](1771-unified-database-migrations.md),
  [Atomic Memory](1809-atomic-memory.md), [Artifact search](1858-artifact-search.md).

# Summary

Add optional image evidence ingestion to the normal PowerContext workflow. Retain submitted PNG/JPEG bytes as an
immutable, Scope-owned Source backed by a managed asset. Interpret the image asynchronously, and store the validated
interpretation in a new `image-analysis` Artifact family using existing revisions, lineage, authorization and read
contracts. Publish a deterministic text-evidence Source for each successful interpretation revision so existing
Source-driven Atomic Memory processing can consume it. Reuse the Artifact Processing Supervisor and propose three
image-domain tables instead of the experimental implementation's seven service-side tables.

This RFC proposes architecture and acceptance requirements. It does not release the experimental API or approve its
implementation. Automatic local-model provisioning is a separate future capability.

# Motivation

Agent conversations include screenshots, diagrams and image-based requirements. Text capture misses their contents;
an incidental Agent summary also loses the exact original material. Persisting both the image and a qualified reading
allows a later session to retrieve useful facts and a person to verify the evidence.

The capability crosses storage, background execution, host integration and public APIs. The design must fit the
existing Source/Artifact framework and explain additional persistence before implementation approval. The
[maintainer feedback](https://github.com/oceanbase/powercontext/pull/1956#issuecomment-6140580696) asks specifically for
that evaluation and for justification of every necessary new table.

# Guide-level explanation

## Normal usage

Images are disabled by default. During normal configuration, a user can enable image interpretation and explicitly
choose either a qualified multimodal generation connection or a dedicated vision endpoint. The latter can be a hosted
provider or an already-running local service. The wizard explains which service receives the image and accompanying
text. A model used by the Agent itself does not automatically provide a PowerContext inference connection.

Users start PowerContext normally. Standard host setup owns capture configuration and collector lifecycle. Initial
automatic targets are qualified Codex Desktop/CLI versions; explicit CLI/SDK upload provides an independent entry.
Host-required plugin activation and Hook trust remain first-use requirements. No separate image environment file or
manually started collector is part of normal usage.

For a runbook screenshot containing an active deployment limit, a crossed-out old value and an unconfirmed hypothesis:

1. Authorized capture preserves the submitted image representation and its session/message provenance.
2. Dashboard shows the retained image and processing state immediately after durable acceptance.
3. Vision produces visible text, visual relationships and uncertainty. An interpretation is a machine reading, not an
   approved decision. Crossed-out and uncertain statements retain those qualifications.
4. Existing Memory processing may create zero, one or several Atomic Memories according to its own policies.
5. A later session can retrieve relevant Memory. Its evidence link reaches the exact interpretation revision and,
   subject to authorization, the original submitted image.

Receipt, interpretation, Memory publication and actual fresh-session recall are distinct outcomes. Dashboard reports
them separately; successful interpretation does not imply a Memory was created. Ordinary generation/Memory processing
may remain unconfigured while vision works.

## User controls

Capture consent is persistent and specific to the project binding, Scope and Server destination. Enabling the Server
capability does not authorize reading a host's historical transcripts. Disabling capture stops new collection; retained
evidence remains readable. Re-enabling does not import messages from the disabled period.

Retry repeats a failed request's pinned interpretation profile. Reparse explicitly selects a new profile and adds a
new interpretation revision. Neither changes cited historical evidence. Configuration changes affect new requests;
they never silently substitute a different backend for accepted work. No automatic fallback sends local work to a
hosted provider.

## Initial scope

Qualify static PNG/JPEG and a file-backed SQLite deployment first. Existing text operations on other supported
backends continue normally; image admission rejects unsupported storage before accepting bytes or creating Sources.
Interpretation uses one image per request with bounded accompanying text. Initial automatic capture supports only
host versions and attachment forms exercised through an actual host process.

Video/audio/PDF ingestion, image embeddings, joint reasoning over several images, arbitrary model hosting and a new
Memory approval workflow are outside this RFC. Local weight download and an owned inference runner are future work.

# Reference-level explanation

## Current contracts and required extensions

The code baseline examined is master
[`8970a970`](https://github.com/oceanbase/powercontext/tree/8970a970c50be930fdea0e627f5299e41dcbc5ad).
The prototype examined is
[`21df99c8`](https://github.com/knqiufan/powercontext/tree/21df99c841b154975e838fff6618a91c8c191350).
RFC descriptions of future capabilities are not evidence that the baseline implements them.

| Responsibility | Existing contract | Proposed extension |
| --- | --- | --- |
| Exact acquired evidence | Source Definition registry, `pc_sources`, Scope journal, identical-write replay/conflict | Typed original-image Definition and asset descriptor; no bytes in Source JSON |
| Interpretation content/history | Artifact content, heads and exact `ArtifactRef` revisions | Registered `image-analysis` family and domain-owned publication |
| Provenance | Source/Artifact lineage and publication records | Original Source → exact analysis revision → text publication → Memory |
| Model input for Memory | Source-only extraction and named text evidence | Deterministic interpretation-publication Definition; raw images excluded from text extraction |
| Scheduling | Scope invocation intents, independent Family budgets, leadership/fencing | Image-analysis binding and worker; image request progress remains domain-owned |
| Inspection | Generic Source/Artifact reads and Dashboard evidence traversal | Image/analysis renderers and authorized asset read |
| Inference | Configured generation roles and usage reporting | Explicit vision role, actual multimodal input, validated output and request budget |

The baseline `SourceRef` is `(source_type, source_id)` within an explicit Scope, and native captures reject conflicting
payload reuse. This RFC uses that implemented contract; it does not require adding the full observation identity model
described in RFC 1400. Definition versions and original identities remain available when image processing is disabled.

Important source evidence:

- [Source models](https://github.com/oceanbase/powercontext/blob/8970a970c50be930fdea0e627f5299e41dcbc5ad/src/powercontext/sources/models.py)
  and [repository](https://github.com/oceanbase/powercontext/blob/8970a970c50be930fdea0e627f5299e41dcbc5ad/src/powercontext/builtin/persistence/sources.py).
- [Artifact models](https://github.com/oceanbase/powercontext/blob/8970a970c50be930fdea0e627f5299e41dcbc5ad/src/powercontext/artifacts/models.py)
  and [shared tables](https://github.com/oceanbase/powercontext/blob/8970a970c50be930fdea0e627f5299e41dcbc5ad/src/powercontext/builtin/persistence/tables.py).
- [Processing intents](https://github.com/oceanbase/powercontext/blob/8970a970c50be930fdea0e627f5299e41dcbc5ad/src/powercontext/builtin/persistence/processing_intents.py),
  [Supervisor](https://github.com/oceanbase/powercontext/blob/8970a970c50be930fdea0e627f5299e41dcbc5ad/src/powercontext/builtin/runtime/artifact_processing.py)
  and [Atomic extraction](https://github.com/oceanbase/powercontext/blob/8970a970c50be930fdea0e627f5299e41dcbc5ad/src/powercontext/builtin/artifacts/atomic_memory/extraction.py).

## Evaluate the Artifact boundary

| Option | Benefits | Costs and limitations |
| --- | --- | --- |
| Original and interpretation both remain Sources, as in the prototype | Smallest consumer change; preserves acquired and derived evidence | Interpretation history, task inspection and lineage need image-specific conventions; Artifact content/versioning is used only downstream |
| Image and interpretation both become Artifacts | Shared revisions and catalog throughout | Treats an acquired attachment as generated content, still needs a binary asset boundary and a Source input for current Memory extraction |
| Original Source plus image-analysis Artifact and text publication | Reuses Artifact history/lineage for derived content while preserving acquisition and Source-only Memory contracts | Adds a Family and a deterministic publication adapter; needs atomic publication and cycle prevention |

Recommend the third option. An image-analysis Artifact is not an Atomic Memory: it describes visible material, including
information that is uncertain, obsolete or not worth remembering. Reusing Memory, Profile or an unrelated Family would
mix their domain rules. Registering a new Family reuses storage and infrastructure, not another Family's meaning.

The image domain owns format validation, binary asset durability, the vision profile, bounded provider requests and
analysis publication. Transport/host adapters translate submissions. They do not define alternative authorization,
Source identity, Memory governance or another scheduler.

## Exact evidence and publication

```text
authorized attachment / explicit upload
  -> managed asset + original-image Source + durable processing request
  -> existing Supervisor -> image-analysis worker
  -> image-analysis Artifact revision + interpretation-publication Source
  -> existing Source-driven Atomic Memory extraction and reconciliation

Memory -> publication Source -> exact analysis Artifact revision -> original Source -> asset
```

The proposed original Definition is `image.original`. Its typed payload records the scoped asset descriptor, stable
capture key/attachment position, host/message provenance, actual submitted representation, bounded context snapshot
and any authorized context Source references. Byte identity and event identity are different: identical bytes can
share one scoped asset while distinct submissions retain distinct Sources.

An `image-analysis` Artifact belongs to one original Source. Revision content records that exact Source, schema/prompt
versions, backend/model deployment identity, pinned input/context digests and validated output. Output distinguishes
readability, visible text, visual description, entities, relationships and uncertainty. Secrets are absent from the
profile and operational metadata; neither embedded image instructions nor accompanying text can change execution
policy. Analysis timestamps are not fact effective dates.

The first success creates revision 1. A reparse appends a revision using the existing optimistic head precondition.
Only one nonterminal request per original image is admitted at a time. Image-analysis writes are owned by the image
domain; generic content replacement is unsupported in the initial release. A stale head or lost fence prevents the
whole publication. It cannot overwrite a newer interpretation or publish a text Source without its Artifact revision.

For each committed revision, publish one `image.interpretation` Source containing the exact scoped `ArtifactRef`,
original Source reference, validated text snapshot, projection version and content digest. The snapshot is a derived
view; the analysis Artifact is its authority. Verification rejects inconsistent snapshots. Its stable identity is
derived from the scoped Artifact ID/revision. Reading or projecting it never invokes vision or follows a moving head.

The Artifact revision, its lineage, publication Source, request outcome and Memory dirty notification commit together.
Replaying completion returns the same revision and Source. Context Sources explain interpretation but do not establish
image claims. Analysis lineage references original/context Sources, not its own publication Source; this prevents a
cycle. Memory evidence includes the publication Source and exact analysis revision. Evidence traversal checks access
at every node and never substitutes a latest revision.

Raw-image Sources are explicitly excluded from text generation input. Once an interpretation is published it enters a
later Source window, even if Memory already advanced past the original capture. Memory's existing eligibility, write
policy, reconciliation and zero-candidate behavior remain in effect. Reparse time cannot make old visible facts newer;
multiple readings of one image are not independent corroboration or repeated recall weight.
Publication preserves the original recording time when known and its original journal position in typed metadata.
Its new journal position wakes consumers; it is not the fallback age of image facts. With no explicit fact/recording
date, reconciliation preserves original evidence ordering, so delayed interpretation cannot outrank newer text solely
because it finished later.

Original-image and interpretation-publication Sources are domain-owned writes. Generic Source/observation submission
must not forge a retained asset descriptor, acceptance receipt or interpretation publication. The capture/publication
boundary validates the asset's exact Scope/state and the referenced Artifact revision/digest before committing them.

## Storage comparison and proposed three tables

The prototype has seven service-side tables. Their responsibilities map as follows; this comparison does not adopt
the prototype's physical schema as a public contract.

| Prototype table | Responsibility | Proposed disposition and reason |
| --- | --- | --- |
| `pc_experimental_image_state` | Format marker, asset/queue totals, worker owner/lease, fairness pointer | Format moves to normal migrations; worker ownership/fairness to Supervisor; capacity to the capacity table |
| `pc_experimental_image_scope_budgets` | Per-Scope pending count | Consolidate global and scoped image admission budgets in the capacity table |
| `pc_experimental_image_assets` | Scoped byte identity, reservation and retained-asset descriptor | Retain an asset table, but store bounded bytes transactionally; file reservation/orphan state is unnecessary |
| `pc_experimental_image_captures` | Durable capture digest and replay receipt | Merge into typed processing-request receipts; Source alone cannot replay the pinned task/response |
| `pc_experimental_image_tasks` | Pinned work, state and publication outcome | Processing-request table; domain progress is distinct from a coalesced Scope invocation |
| `pc_experimental_image_attempts` | Bounded provider attempts and outcomes | Bounded attempt list in each request; no separate table at the proposed maximum of five attempts |
| `pc_experimental_image_operations` | Retry/reparse key-to-request mapping | Typed keys and unique constraints in the same request table |

Proposed physical names are illustrative and require normal migration review:

1. **`pc_image_assets`**: `(scope_id, byte_digest)` identity, bounded raw bytes, media type/dimensions/size and creation
   metadata. Reuse relational binary encoding and exact digest verification. Source/Artifact JSON remains small;
   separate asset identity allows several acquired Sources to reference one byte object without repeating the bytes.
2. **`pc_image_processing_requests`**: typed capture/retry/reparse identity and request digest; original Source; pinned
   profile/context; acceptance receipt; state, attempt budget, bounded attempts, claim token and exact published result.
   Unique keys protect replay and one active request per original. Due/state/Scope indexes support bounded discovery.
   Existing processing intents deliberately contain no per-image payload, queue state or retry history; overloading
   them would change the Supervisor contract. Rows are domain command records, not another general job system.
3. **`pc_image_capacity`**: explicitly distinguished global and scoped budget rows with retained-byte and active-request
   counters. Admission locks and changes the applicable counters in the same transaction as asset/request insertion.
   Scope settings are configuration, not a transactional capacity ledger. Recomputing
   asset totals on every upload would scan a growing collection and cannot safely replace concurrent reservation.

Original/interpretation Sources, Artifact revisions/heads/lineage and processing leadership use existing tables. The
new capacity table does not store worker ownership or schema versions. Attempts stay operational, bounded data rather
than Artifact revisions; attempt details cannot include keys, raw prompts, Base64 or unconstrained provider errors.

Existing binary storage was evaluated:
[SkillPackageRepository](https://github.com/oceanbase/powercontext/blob/8970a970c50be930fdea0e627f5299e41dcbc5ad/src/powercontext/builtin/persistence/skill_packages.py)
already stores and verifies scoped immutable archive bytes. Its canonical ZIP/tree identity, required `SKILL.md`,
manifest and 4 MiB uncompressed package limit are Skill contracts, not an image asset contract. Reuse the binary
encoding/digest pattern, not `pc_skill_packages` or fake Skill packages. A shared generic asset substrate could be
extracted later if several owners have the same durability and failure semantics.

Recommend database-backed image bytes for the initial bounded SQLite deployment. Asset, Source, request and capacity
commit in one database transaction, eliminating the prototype's file-publication gap, orphan reservation and paired
DB/file-tree restore requirement. BLOB writes increase database/WAL size and can hold the SQLite write lock longer;
the choice needs measured admission/read latency and growth, not a claim that it is always faster. External managed
files/object storage are a separate alternative if those measurements justify their recovery complexity.

Initial caps are 8 MiB/image, 12 MiB JSON request, 20 million decoded pixels, four attachments/message, 1 GiB of retained
asset payloads, 256 active requests/Scope and 1,024 globally. Ingress/decode concurrency is independently bounded;
vision concurrency starts at one. Automatic attempts
default to three with a hard maximum of five. Invalid output is bounded to 64 KiB and validated before publication.
These are proposed protection defaults, not measured performance claims. Operators can lower them.

Admission uses a fixed lock order and database constraints. Duplicate bytes are not charged twice. Decoder/model work
runs outside transactions; the bounded BLOB insertion is part of the acceptance transaction. Startup reconciliation
can repair derived counters from asset/request facts before admission, using bounded batches; it never invents
new Source/Artifact identities. Routine queue/list work uses indexed keyset pages, not full Source scans.
Measure counter contention, row/file growth and recovery cost before making scalability claims.
Repair or restore may visit all asset/request records in indexed batches: bounded pages still mean O(N) total work.
That cost and the admission readiness gate must be measured separately from normal incremental uploads.

The host's local outbox is separate from these three Server tables. Its durable jobs/attachments/watches solve temporary
file survival and incremental collection on the client. Reuse host checkpoint mechanisms where their exact-input
contract fits; a remote Server cannot replace a client staging queue. Document and bound this client storage separately.

## Acceptance transaction and recovery

| Boundary | Durable fact required | Recovery behavior |
| --- | --- | --- |
| Before admission | Authorization, actual format, limits and supported storage checked | Reject without an accepted Source/request |
| Accepted response | Asset bytes, capacity, original Source/journal position, pinned request/receipt and dirty intent committed in one transaction | Rollback leaves no half-accepted asset; a lost response replays the same receipt without selecting configuration again |
| Provider start | Fenced claim and attempt increment committed before the network call | A crash/unknown response consumes that attempt; no free hidden retry |
| Successful interpretation | Revision, exact text publication, outcome and dirty intent committed atomically | Replay returns the same evidence; lost/stale claims cannot publish |
| Failed interpretation | Stable failure and actual attempt count retained | Explicit retry or bounded automatic policy; original remains readable |

Request states are `pending`, `running`, `retry_wait`, `blocked`, and terminal `succeeded`, `failed` or `superseded`.
Active quota counts all four nonterminal states. Claim/attempt updates advance a request state version. Retry is for a
failed request. An explicit reparse may supersede active work only with its exact request/state precondition; otherwise
it returns conflict. Superseding, invalidating the old claim and accepting the replacement are atomic. A late old
response cannot publish, and its already-started provider call remains counted. This permits replacing an unavailable
pinned profile without silently changing accepted work or leaving an image permanently locked.

Capture keys are qualified by Scope and attachment position. Retry/reparse keys are qualified by operation kind and
original/parent request identity. Canonical request digests include bytes, provenance and fixed context; equal keys
with different requests return conflict. Retry preserves the failed profile and context; reparse pins the selected
current profile at acceptance and checks the caller's expected Artifact revision/profile digest. Operation replay
uses its first receipt even if configuration has changed.

The image binding uses the existing single-flight Scope invocation and Family worker budget. A bounded pending provider
discovers due image requests and marks domain dirtiness; request acceptance also marks it transactionally. Invocation
generations are wakeups, not per-image task IDs. The processor preserves requests deferred until their retry time and
leaves/re-establishes their wakeup. It does not acknowledge unresolved domain work as completed.

Leadership fence and request claim are rechecked at commit. No persistent image-global worker lease or second
Supervisor is introduced. The worker can be constructed from a qualified vision connection without ordinary text
generation. Disabling capture leaves staging intact; disabling interpretation prevents new provider calls without
breaking historical reads. Lost authorization blocks publication/readback; work is not moved to another Scope or
destination. One disabled workspace cannot monopolize eligible client deliveries.

The system promises idempotent persistence, not exactly-once external inference. Provider responses can be lost after
the provider has done work. Actual request accounting, bounded retry and exact committed evidence are the guarantees.
SDK/provider automatic retries are disabled or charged through the same attempt ledger; a formatting-repair model
call is also an attempt.
Durable receipt requires a file-backed database and qualified journal/synchronous settings. A weaker or unsupported
deployment rejects image admission; normal text behavior keeps its existing contract.

## Public operations and inspection

Reuse and extend existing typed catalogs rather than reproduce image content in task-read responses:

| Public need | Proposed surface |
| --- | --- |
| Submit bytes and pin first interpretation | New image capture command, returning durable original Source and processing-request receipt |
| Read exact bytes/thumbnail | Authorized image asset read; any thumbnail is a derived representation, never the original |
| Read/list operational progress | Image processing-request read/list with bounded pagination; result is an exact Artifact reference |
| Explicit retry/reparse | One analysis-request command with a typed operation, idempotency key and relevant preconditions |
| List/read original material | Existing `list_sources` / `get_source`, with image Definition support |
| List/read analysis/history | Existing `list_artifacts`, `get_artifact`, `get_artifact_revision`, `list_artifact_revisions`, extended for the Family |
| Health and scheduling | Extend existing processing/configuration diagnostics with image binding readiness and backlog |
| Memory evidence | Extend existing Dashboard evidence renderers/traversal for original image and analysis nodes |

This proposes five image-specific operations: capture, asset read, request read, request list and analysis request.
Names/routes are selected in the implementation's OpenAPI contract; the experimental seven-operation surface is not
promoted unchanged. The new Family requires explicit runtime/HTTP schemas, reader/processor registration and SDK/JS
binding generation. Generic operations are reusable contracts, not proof that an unknown Family already works.

Admission needs Scope contribute permission; reads require the relevant existing content permissions. Retry/reparse
also needs access to the original material. An Artifact share does not grant read permission to its source Scope or
raw image. A shared Memory can remain visible while upstream evidence is explicitly unavailable. No public filesystem
path, arbitrary client path, external image URL fetch or unguarded static asset directory is accepted.

Dashboard groups original images with processing status and analysis revisions, using existing catalog/evidence
components. It supports exact original/analysis inspection from Memory. Ordinary Agent recall remains text-based and
Memory-policy governed; image-analysis is not automatically included as a second default recall family. Listing,
opening, thumbnail generation and recall never call vision.

## Configuration and host lifecycle

Use one capability switch and one explicit backend selection, separate from capture consent:

| Choice | Required configuration | Semantics |
| --- | --- | --- |
| Disabled | None | Existing text behavior; retained image evidence still readable |
| `reuse_generation` | Existing connection plus explicitly qualified multimodal capability | Reuse endpoint/model/credentials; no implicit assumption about image support |
| `dedicated_endpoint` | Vision endpoint, exact model and locally held credentials | Independent hosted or user-managed local inference connection |

The wizard and `.env` describe these optional settings; text-only installations do not gain a mandatory image decoder,
Torch/CUDA dependency or model download. Credentials are resolved at execution through a pinned connection identity;
rotating the key is allowed, silently changing model/deployment identity is not. An unavailable pinned connection
produces a visible blocked/error outcome.
Record provider-reported deployment identity when available. If a provider exposes no immutable model revision,
mark that fact as unknown rather than claim its weights are pinned. Requests pin the declared profile; historical
readback relies on stored output, not reproducible inference.

Standard setup/doctor owns Codex capture installation, consent and diagnostics. The collector stages submitted image
blocks promptly, stores stable host/session/message provenance and retries bounded delivery. It captures the bytes the
host actually submitted, which may already be resized; Dashboard labels this representation accurately. New host
formats are rejected/diagnosed until qualified. Setup must preserve ordinary conversation capture and use the existing
service lifecycle; users do not separately manage an image daemon.

## Compatibility, migration and delivery

Use the normal versioned migration mechanism for the three image-domain tables and Family registration. Do not add
an isolated schema version/auto-upgrade subsystem. An official release initializes/upgrades supported image storage
explicitly; incompatible storage rejects admission while ordinary text remains usable.

The unmerged experimental format is not a released compatibility promise. Do not initialize the new layout over it.
If experimental data is imported, keep its exact original/analysis Source IDs, bytes, payloads and journal positions;
the two experimental Definitions remain readable. New analysis Artifacts may cite those old Sources, but import must
not recast old machine readings as if a new profile generated them. Import refusal without modifying the database is
an acceptable initial policy until an explicit converter is qualified. The experiment database is kept separately.

Initial backup/restore follows the database's consistent backup boundary and includes the image BLOBs. Restore validates
asset descriptors/digests before admission. Missing/corrupt evidence produces a clear unavailable/integrity result and
never redirects to another asset. Initial storage pressure refuses new work rather than deleting historical evidence.
If external file storage is selected instead, paired DB/assets backup, durable reservation, file publication and
crash-safe orphan reclamation become additional required contracts and acceptance cases.

After RFC acceptance, deliver reviewed increments: (1) domain/storage/Artifact publication and explicit upload,
(2) standard host lifecycle and Dashboard/SDK integration, (3) real-host/model and recovery qualification. Implementation
approval is separate from RFC acceptance. #1956 remains a draft prototype pending alignment with this design.

## Acceptance criteria

| Area | Required evidence |
| --- | --- |
| Default-off and normal setup | Text-only startup unchanged; optional wizard/env, normal service lifecycle and host diagnostics; vision works with Memory processing unconfigured |
| Actual hosts | Qualified Desktop drag/file-picker/paste and CLI entries; verify host-produced attachment and Hook timing, not just invoke a Hook with synthetic input |
| Durable receipt | Exact uploaded representation survives response loss, temporary-file deletion, process kill and restart |
| Replay/concurrency | Same request returns the same receipt; conflicting reuse rejected; parallel admission respects budgets; one active analysis/original; stale fence/head publishes nothing |
| Vision failure | Invalid/oversized output, timeout and provider crash obey actual request/attempt budgets; other Scope/Family work progresses |
| Source/Artifact publication | Atomic revision/view/outcome; exact historical readback after reparse; no cycles or model calls during reads |
| Authorization | Cross-Scope denial, revoked permissions and shared Artifact without raw-image access; no hidden byte/metadata disclosure |
| Memory and fresh recall | Real generation produces governed Atomic Memories; a new actual Agent session uses retrieved evidence; zero-memory results are observable; delayed analysis does not make older facts newer |
| Client recovery | Offline backlog, capacity exhaustion, consent disable/re-enable, endpoint/Scope changes and collector replacement preserve consent and progress |
| Storage/migration | Normal fresh/upgrade paths; controlled refusal of experimental format; database backup/restore with exact BLOBs, corruption and mid-transaction crash recovery |
| Resource costs | Measure request/row/file growth, quota-lock contention and indexed page/reconciliation cost; verify all proposed caps |

Controlled providers prove request/recovery behavior. Real model execution proves inference compatibility; actual host
execution proves host input/lifecycle support. These are reported separately. Existing prototype results do not qualify
the proposed Artifact redesign, local-model runtime, every host variant or other image storage backends.

# Drawbacks

An additional Artifact family and deterministic Source publication add integration work. Binary data enlarges the
database and needs explicit quota ownership; three domain tables remain necessary in this proposal. Persisted vision output
can be wrong and can contain sensitive text. Cloud interpretation also has cost and latency. Exact provenance and
Memory policy reduce ambiguity but do not guarantee correct recognition or justify every Memory write.

# Rationale and alternatives

The comparison above favors the hybrid design because the baseline Memory extractor consumes Sources while Artifact
revisions already represent derived content. Keeping only an Agent summary loses exact visual evidence. OCR-only is a
possible future qualified interpreter but misses diagram relationships. Reprocessing images on every recall repeats
cost and can change historical meaning. Storing raw Base64 in Artifact/Source JSON grows unrelated reads and still
does not solve durable binary reservation/read authorization.

A two-table variant could derive budgets on each admission. Without an equivalent atomic reservation ledger it either
scans growing retained assets or risks oversubscription; this RFC therefore proposes a third capacity table. Separate
attempt/operation tables can be introduced later if a demonstrated retention/query requirement exceeds bounded request
records. Unrelated existing tables and Supervisor intents are not repurposed to reduce the count cosmetically.
The prototype's external-file approach keeps large bytes out of the database, but requires paired snapshots and
cross-resource crash recovery. Storing image BLOBs in a separate domain table follows the existing Skill-package binary
pattern without inheriting Skill semantics. This is the initial recommendation; neither storage medium is assumed to
have acceptable cost without measurement.

Without this feature, users continue to transcribe images manually and exact visual material is absent from the
evidence chain. The default-off initial scope limits the impact on installations that do not need it.

# Prior art

The linked PowerContext RFCs establish acquisition/derived-content separation, family-owned behavior, exact provenance,
Source-only Atomic extraction and shared scheduling. #1932 records the user need and backend alternatives. #1956 is
a feasibility prototype with two native Source Definitions, a separate image worker and seven metadata tables; its
code and acceptance records are inputs to this design review, not an approved upstream architecture.

# Unresolved questions

Resolve before merging this RFC:

1. Accept the hybrid `image-analysis` family and deterministic Source publication, or select the Source-only
   alternative with an explicit equivalent history/inspection contract.
2. Accept the three-table asset/request/capacity boundary and database-backed bytes, or identify an existing contract
   or external-storage design that provides the same atomic admission and per-image replay guarantees. Any
   consolidation must retain the invariants above.
3. Confirm file-backed SQLite and qualified Codex versions as the initial support scope, including which actual
   Desktop/CLI entry variants must pass before release.

Provider/model qualification, exact public route names and migration revision allocation are implementation review
items within the accepted contracts. They do not authorize scope expansion or weaker acceptance.

# Future possibilities

Managed local vision could add an explicit backend mode with authorized downloads, pinned model/runtime digests,
readiness, disk/resource budgets, interrupted-download recovery and an owned runner integrated with Server lifecycle.
It would use the same Artifact/evidence path. An already-running local endpoint remains `dedicated_endpoint`.
MiniCPM-V-4.7-1B is a candidate, subject to authorized available weights, licensing, runtime compatibility and measured
hardware/recognition quality; private API access does not establish a distributable default. No model download,
runner or implicit cloud fallback is included in this RFC's acceptance.
Local vision runs on the PowerContext Server machine. It does not make remote capture, ordinary generation or
embedding offline; their destinations must remain explicit.

Other backends, region-level evidence locators, OCR-only interpreters and image search require their own qualification
and any material public-contract changes require follow-up design review.
