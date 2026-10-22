---
title: PowerContext Gateway for Controlled Model Calls and Context Reuse
description: Reuse Core context organization and privacy checks through an optional gateway for model protocols, exact context replay, native cache adaptation, and bounded execution.
---

- Proposal Name: `powercontext_gateway`
- Start Date: 2026-10-22
- RFC PR: [oceanbase/powercontext#2199](https://github.com/oceanbase/powercontext/pull/2199)
- Related RFCs: [RFC 2086](2086-core-inference-gates-and-optional-gateway.md),
  [RFC 0016](0016_pydantic_ai_inference_integration.md), [RFC 0046](0046_observability_foundations.md),
  [RFC 1345](1345_scope_organization_and_agent_integration.md), [RFC 1396](1396_handoff_access_control.md),
  [RFC 1429](1429_project_bootstrap_context.md), [RFC 1770](1770-decision-policy-governance.md),
  [RFC 1783](1783-handoff-session-rollover.md), [Working Note design](1790_source_backed_working_notes.md)

# Summary

PowerContext Gateway is an optional model-call entry point. It connects existing PowerContext organization to Agent
model traffic while clients retain their history and tool execution. It owns Scope binding, preparation at supported
boundaries, exact replay, actual-target selection, native cache adaptation, and controlled sends.
Its product goals are the same as [RFC 2086](2086-core-inference-gates-and-optional-gateway.md):
**privacy interception and token efficiency at the context layer**.

The two RFCs describe one execution path. #2086 defines Core selection, representation strategies, privacy and budget
checks, reuse constraints, and check interfaces. This RFC defines how the gateway consumes those contracts and controls
the final request, every actual send, and the first release of response content.
It adds no second ContextRuntime, GateRuntime, memory algorithm, or policy database, and does not replace Core commits.

The initial deployment is optional within the existing Python Server, sharing its process, port, authentication, and
authorization boundary. It supports full-history Chat Completions and Responses without hidden provider conversation
state. Context enhancement requires trusted conversation association.
Persistent replay introduces **one gateway-local SQLite file with two dedicated tables**, only when enabled, and no
Core tables or database service. The initial release excludes a separate administration UI, automatic tool loops,
automatic transcript ingestion, automatic summarization, billing ledgers, and semantic response caching.

Both this RFC and #2086 are proposals. The source baseline is `0bc42d0b`, and the coordination baseline is #2086. The routes, permissions, internal preparation metadata, and gateway storage below are not shipped
capabilities in that baseline.

| Decision | Contract |
| --- | --- |
| Core authority | Context selection, resource eligibility, representation algorithms, privacy decisions, reuse constraints, and domain writes. |
| Gateway authority | Protocols, trusted conversations, injection positions, immutable replay, actual targets, final rendering, sends, buffering, and release. |
| Host authority | User history, response acceptance, client tools and side effects, task completion, and Handoff timing. |
| Model-service authority | Native KV storage, reuse, eviction, and provider-defined usage. |
| API additions | Model-protocol ingress and two conversation-metadata operations; Core checks reuse the two operations from #2086. |
| Storage additions | Two dedicated optional gateway tables; no transcript store, response-recovery store, or complete attempt ledger. |
| Default deployment | Embedded in Server and disabled by default; standalone deployment is a later delivery using the same contracts. |
| Safety default | Withhold when required checks are unavailable; strict mode releases no model content before approval. |

# Motivation

Calling `/v1/context/prepare` lets PowerContext establish what it delivered, but does not reveal subsequent host
instructions, tool results, or the final model target. Preparing again every turn can also change historical prefixes,
increase retrieval work, and reduce opportunities for native model-cache reuse.

The gateway supplies execution-side visibility and ownership:

1. Inspect complete requests at the actual model boundary, including host history and tools outside PowerContext.
2. Prepare at supported new-user-message boundaries and preserve injections through ordinary tool rounds.
3. Replay adopted context at its original positions with unchanged content, avoiding unnecessary representation changes.
4. Adapt native caching after the actual model/API/account/region is resolved, measuring real usage and complete-task cost.
5. Reuse deployment and domain APIs so each Agent does not rebuild retrieval, cache settings, and failure handling.

Cache stability follows authorization, resource eligibility, privacy, and correctness. The promise is improved reuse
conditions, not guaranteed hits, savings percentages, or model-quality improvement.

# Guide-level explanation

## One coding Agent

The user has registered `project:powercontext`. The deployment permits selected Principals and model profiles for this
Scope. The client points its model base URL at the gateway and uses a credential accepted by PowerContext.
A supported host also sends a stable conversation ID through extra headers, using different conversations for different
work.

This full-history example uses **proposed interfaces**. `coding-default` is a deployment model alias, not a provider:

```bash
curl https://pc.example/gateway/v1/chat/completions \
  -H "Authorization: Bearer $POWERCONTEXT_TOKEN" \
  -H "X-PowerContext-Scope-ID: project:powercontext" \
  -H "X-PowerContext-Conversation-ID: coding-client-001" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "coding-default",
    "messages": [
      {"role": "user", "content": "Fix the API client and preserve contract-generation requirements"}
    ],
    "stream": false,
    "max_tokens": 2048
  }'
```

1. The gateway authenticates the caller and checks current Scope read access, paid execution permission, and profile use.
2. Core selects cited context through existing prepare/assembly. The gateway does not search databases or rank Memory.
3. The gateway appends the complete PreparedContext to the latest user content where the protocol permits, retaining
   its historical-material trust wrapper. A Core-approved exact whole-value duplicate may instead reuse the historical injection.
4. It persists the exact injection, position, and reuse dependencies before rendering, obtaining #2086 final admission,
   and sending the actual request.
5. After the complete raw response passes output-release checks, it delivers through the client's protocol.
6. The client executes tools and submits its history and results. The gateway restores existing injections exactly,
   performs no new retrieval for this round, and still checks the complete input.
7. A new user message can trigger one new bounded preparation. Refresh, revocation, or resource invalidation establishes
   a new boundary; invalid content is not retained for caching.

An empty preparation is normal. Privacy rejection, invalid reuse eligibility, and unavailable policy have explicit
states. A delivered answer does not mean tools executed, Memory was written, or the task completed.

## Three integration modes

| Mode | Usage | Boundary |
| --- | --- | --- |
| Embedded Server gateway | Enable the optional component and send model requests to `/gateway`; the initial recommended deployment. | Reach Core through the same authentication/authorization executor and reuse in-process preparation metadata and checks. |
| Standalone PowerContext Gateway | A later delivery calls existing prepare and #2086 checks over HTTP. | Requires trusted delegation; remote persistent replay also requires the verifiable preparation binding defined below. |
| Third-party gateway | Integrate #2086 at its final send/release boundary, or use it as a validated upstream path. | Does not require this gateway's tables; actual targets, mutations, retries, and visibility determine coverage. |

Changing only a base URL does not necessarily enable reliable conversation enhancement. Clients without stable
conversation association initially use `check_only`: complete input/output inspection remains, but no project inference,
old-conversation inheritance, or automatic injection occurs.
The supported-client list must record actual versions, extra-header support, full history, cancellation, streaming
timeouts, and native signature behavior.

## How the two RFCs work together

| Question | #2086 contract | This RFC's implementation responsibility |
| --- | --- | --- |
| Which context to include | Existing Scoped prepare, assembly, Builder, resource eligibility, and approved representation. | Call once at a new-message boundary and preserve the result; do not duplicate selection. |
| Whether reuse remains valid | Authorization, freshness, retention, complete provenance dependencies, and reuse restrictions. | Verify before sending; persist exact blocks and stop an invalid replay epoch. |
| How to inspect | The same input/output algorithms, modes, failure actions, and transient bindings. | Call after final rendering, re-admit every actual attempt, and check before first strict release. |
| How to use native caching | Core supplies reusable boundaries; actual hits belong to the model service. | Native adapter maps target capabilities, breakpoints, TTL, and routing hints and parses available usage. |
| Who stores execution state | Core adds no inference database; Source/Artifact retain their domain contracts. | Store only two tables needed to restore injections, without a general model transcript. |
| How knowledge is written | Existing Source/Artifact, Work/Handoff, and their validation. | No automatic capture initially; later explicit workflows use existing APIs and commits. |

# Reference-level explanation

## 1. Scope, layers, and non-goals

The gateway is an execution adapter, with implementation under `src/powercontext/gateway/`. Existing Runtime code must
not depend on it. Protocol handling, final rendering, and transport belong to the gateway. Shared privacy and
representation algorithms remain in the context/inference/policy modules identified by #2086.

The initial scope is one host, one gateway process, a single Scope without context references, full text, and validated
structured-tool carriers. Enhancement explicitly selects
`assembly.sections=[{"family":"memory","limit":6},{"family":"experience","limit":2}]` and `include_code=false`.
Other families, code, and cross-Scope content enter persistent replay only after complete reuse dependencies and
acceptance exist. Existing prepare users keep their defaults.

The initial release excludes arbitrary protocol conversion, dynamic cheap-model escalation, load balancing, a general
reverse proxy, global hard cost reservations, shared multi-host SQLite, automatic lossy compaction, response/semantic
caches, secret restoration, and arbitrary Agent tool execution.
Core background Embedding/rerank/extraction retain their own inference boundaries; they are not forced through this
gateway or automatically given Agent context.

## 2. Components and calls

```mermaid
%%{init: {"theme":"base","look":"classic","themeVariables":{"fontFamily":"Arial, Noto Sans CJK SC, sans-serif","fontSize":"15px","lineColor":"#64748b","edgeLabelBackground":"#ffffff"},"flowchart":{"curve":"basis","nodeSpacing":28,"rankSpacing":24,"wrappingWidth":210,"padding":16,"htmlLabels":true}}}%%
flowchart TB
    H["<b>Agent / SDK</b><br/>Client history and tools"]
    T["<b>Third-party gateway (optional)</b><br/>Final-boundary integration"]
    subgraph PC["PowerContext Server · one process by default"]
        A["<b>Existing authentication and authorization</b><br/>Principal / Scope / execution permission"]
        subgraph GW["Optional Gateway · protocol and execution"]
            subgraph GW_FLOW[" "]
                direction LR
                P["<b>Protocol ingress</b><br/>Chat / Responses<br/>Validated carriers"]
                L["<b>Conversation handling</b><br/>Classify · inject · exact replay<br/>Single writer"]
                N["<b>Upstream adapter</b><br/>Approved route · cache parameters<br/>Final rendering"]
                X["<b>Execution boundary</b><br/>Admit → send<br/>Buffer → release"]
                S[("Gateway-local SQLite<br/>Conversations + replay blocks")]
                O["<b>Existing observations</b><br/>Bounded metadata<br/>Incomplete usage"]
                P --> L
                L <--> S
                L --> N
                N --> X
                X -.-> O
            end
        end
        subgraph CORE["Existing Core + RFC 2086 · context and checks"]
            subgraph CORE_FLOW[" "]
                direction LR
                C["<b>Scoped context application</b><br/>prepare / assembly<br/>PreparedContextBuilder"]
                I["<b>Input and output checks</b><br/>Privacy · target · full budget<br/>Output release"]
                D[("Existing Scope / Source / Artifact<br/>Work / Bootstrap / Handoff")]
                Q["<b>Existing policy and inference</b><br/>Rules and algorithms"]
                C --> D
                Q -.-> C
                Q -.-> I
            end
        end
        A --> GW
    end
    U["<b>Approved actual model endpoint</b><br/>Native KV cache · provider usage"]
    H --> A
    GW <-->|"Send boundary: admitted send / raw response"| U
    T -. "Authenticated prepare / check contracts" .-> CORE
    GW <-->|"Conversation handling → prepare<br/>Send / release boundary ⇄ input / output checks"| CORE

    classDef client fill:#f1f5f9,stroke:#94a3b8,color:#0f172a,stroke-width:1.2px
    classDef auth fill:#334155,stroke:#334155,color:#ffffff,stroke-width:1.5px
    classDef gateway fill:#ffffff,stroke:#60a5fa,color:#1e3a5f,stroke-width:1.5px
    classDef core fill:#ffffff,stroke:#5eaaa0,color:#164e48,stroke-width:1.5px
    classDef store fill:#fff8eb,stroke:#d4a65a,color:#684b1b,stroke-width:1.2px
    classDef support fill:#f8fafc,stroke:#94a3b8,color:#475569,stroke-dasharray:4 3
    class H,T,U client
    class A auth
    class P,L,N,X gateway
    class C,I core
    class S,D store
    class O,Q support
    style PC fill:#f8fafc,stroke:#94a3b8,stroke-width:1.5px
    style GW fill:#eff6ff,stroke:#93c5fd,stroke-width:1.5px
    style CORE fill:#f0fdfa,stroke:#83c5b9,stroke-width:1.5px
    style GW_FLOW fill:transparent,stroke:transparent
    style CORE_FLOW fill:transparent,stroke:transparent
```

Blue groups show Gateway protocol and execution; green groups show Core context and checks; warm nodes show persistent state. Dashed lines mark policy, observations, and optional third-party integration. Links between groups name the actual callers and entry points.

Core and Gateway are responsibility boundaries, not two required services. Embedded calls use the same application and
check algorithms through Server's authenticated authorization executor. Sharing a process does not permit repository
access that bypasses resource authorization. Standalone and third-party integrations use public HTTP contracts, without
private Server handlers or direct Core database access.

Split code around cohesive protocol, upstream, replay/store, and request-pipeline responsibilities; do not begin with a
generic plugin framework or a new runtime facade.
Native adapters must reach the final actual send. A high-level SDK with hidden retries or mutations cannot establish
complete coverage.

## 3. Identity, Scope, and execution permission

Reuse `AuthenticationProvider` and its `AuthenticationResult`, Principal/actor/credential identity, the current Scope
catalog, and `AuthorizationProvider`. Add no gateway user database, separate tenant hierarchy, or gateway-key table.
The existing static Bearer maps to one fixed Principal and is not multi-user isolation.

| Constraint | Design |
| --- | --- |
| Scope selection | `X-PowerContext-Scope-ID`, or a trusted deployment binding from a credential to exactly one Scope; reject ambiguity. |
| Scope access | Check current `scope.read` on every request and required replay validation; resources retain their own access profiles. |
| Paid execution | Propose Scope action `gateway.invoke` and minimal role `gateway.user` in existing authorization vocabulary; read access does not imply model spending. |
| Profile permission | Only deployment-approved profiles/model aliases are usable by the authenticated caller; client model values request selection. |
| State partition | Complete `deployment + principal + scope_id + conversation_id + epoch` partition; another caller's IDs cannot be read or adopted. |
| Management | Reuse operational configuration and `server.admin`; conversation read/delete also require owner checks. |
| Credentials | Never forward PowerContext credentials to models; approved profiles resolve provider credentials through secret references. |

`gateway.invoke` and `gateway.user` are proposed additions, not existing enum values. Existing roles do not silently gain
execution permission. Any administrator execution grant is an explicit role mapping documented for upgrades.
A conversation cannot change Scope in place; changing projects creates a new conversation. Client user/metadata fields,
prompt tags, and provider session IDs confer no authority. `for_scope` only binds a partition, not authorization.

Standalone deployment forwards accepted user authentication or supported trusted delegation, allowing Core to identify
subject and actor. One privileged service token plus caller-controlled principal/scope is insufficient. PDP unavailability
must not allow reads or paid sends. The initial release shares no replay between users; even same-Scope collaborators
cannot read another Principal's injection copies.

## 4. Configuration, routing, and target profiles

Extend existing Server configuration with an optional `gateway` section. The following values are proposed; acceptance
must fix the measured thresholds:

```yaml
gateway:
  enabled: false
  context_owner: gateway
  privacy_mode: enforcing
  output_mode: strict
  replay:
    enabled: true
    idle_ttl_seconds: 3600
    max_age_seconds: 86400
    max_context_blocks: 128
    max_context_bytes: 262144
    max_reuse_dependencies: 64
  limits:
    max_request_bytes: 8388608
    max_response_bytes: 2097152
    max_inflight: 16
  upstream_profiles:
    coding-default:
      protocol: openai-chat
      model: deployment-model-id
      base_url: https://approved-model.example/v1
      credential_ref: env:MODEL_API_KEY
      cache_adapter: validated-deployment-profile
```

Trusted configuration selects `gateway` or `host` as `context_owner`. When the host already manages
prepare/bootstrap/Handoff, the gateway neither prepares again nor injects a second copy; complete-request checks still
apply. A matching prompt tag does not prove host ownership or disable checks.

`UpstreamProfile` is the same contract as #2086, not a second cache-capability model: exact target, API/protocol and renderer
versions, account/region, permitted data processing and retention, model window/output limits, tool/schema/reasoning
capabilities, native cache modes/boundaries/TTL semantics, and usage mappings.
Profiles and configuration snapshots are versioned. Ordinary changes apply to new epochs; revocation/security changes
apply through current checks immediately.

Routing resolves one target from the trusted profile, requested protocol, and allowed model alias, remaining stable
within an epoch. Equal-priority ambiguity fails configuration loading; no automatic fallback is provided initially.
A disabled target or invalid credential fails explicitly without switching accounts, regions, models, or protocols.
Future target switches require new planning, history-compatibility checks, and complete admission, not old cache bindings.

Disallow provider redirects, caller-supplied base URLs, arbitrary forwarded authorization/cookies, unknown query
parameters, and credentials embedded in URLs. Endpoint resolution, DNS/IP checks, and egress policies must keep
configuration changes within approved targets; string-prefix matching is not destination proof.
Third-party upstream profiles also require validation of hidden routing/retries. Unknown actual targets are not covered.

## 5. Protocols, carriers, and conversations

| Path/protocol | Initial behavior |
| --- | --- |
| `POST /gateway/v1/chat/completions` | Complete history and validated text/tool requests; same-protocol output. |
| `POST /gateway/v1/responses` | Complete input, `store:false`, and no hidden provider history; same-protocol output. |
| `GET /gateway/v1/models` | Only configured model aliases the caller may execute; no broad probing of arbitrary upstreams. |
| `POST /gateway/v1/messages` | Later independent adaptation and acceptance, not an initial supported route. |
| Embeddings, batch jobs, files, WebSockets, audiovisual carriers, and unknown routes | Reject initially; never become an unchecked general proxy. |

Responses `previous_response_id`, `conversation`, `background`, item references, and opaque reasoning/signature blocks
require a profile that can supply inspection visibility while preserving native constraints.
The initial enforcing path rejects hidden history it cannot inspect. It must not silently pass it through and claim
complete protection. Chat and Responses are not converted. Native signed/reasoning content is neither migrated between
models nor automatically modified or discarded.

A supported host supplies the stable conversation ID; the gateway creates the epoch. Clients cannot select an old epoch
to evade validation. Even with an ID, validate history prefixes, roles, tool call/result pairing, and complete association
with previous replies. Matching opening text or query hashes do not establish conversation identity.
The initial implementation performs no fuzzy history lookup or cross-conversation ancestry inference. Unbound requests
use `check_only`.

Request classification affects optional enhancement only:

| Type | Prepare | Replay existing injection | Input/output checks |
| --- | --- | --- | --- |
| Initial/new user message | At most one bounded preparation | After validation | Required |
| Tool result, continuation, or provably unsent retry | No new preparation | Exact validated replay | Required for every actual attempt |
| Recognized title/housekeeping requests | No enhancement or automatic capture | Only as required by a validated protocol | Separate complete checks |
| No trusted conversation or host ownership | No automatic preparation | No inherited gateway blocks | Required |
| Unrecognized history edit, client compaction, or deletion | Stop the old epoch | Do not guess missing history | Explicit conflict or new-boundary requirement |

Unknown classification may stop enhancement, but not weaken safety. Reject if signatures or history integrity cannot be
preserved. Tool definitions, arguments, results, error text, and new host material remain in the complete check.

## 6. Preparation, exact injection, and trusted reuse dependencies

New messages obtain one PreparedContext through existing `ScopedContextApplication.prepare`, retaining assembly and
Builder citations, trust wrapping, UTF-8 limits, and omissions.
Do not assemble a separate gateway Memory catalog or call prepare again as a safety preflight.
Read existing blocks by the same anchor first; validate and reuse an existing block without another preparation.
Persist a content-free marker for empty results to avoid repeated retrieval on retry.
An uncommitted candidate can be recomputed after a crash: the guarantee is one exact committed representation, not
exactly-once preparation computation.

Append the complete context block to the **latest user content** where the protocol permits, without raising its
instruction priority. Do not modify historical system/developer/tools or insert random IDs, timestamps, or diagnostics
into stable segments. Do not truncate completed citation wrappers to make space. Disable this enhancement capability or
reject a profile requiring it when safe append is unsupported.

A replay record contains exact prepared text, position, native block and renderer version, content digest, message anchor,
provenance/reuse binding, retention constraints, and creation/expiry times. Persist it with put-if-absent before the first
send. Different values for one partition/epoch/anchor stop the send; never overwrite or re-retrieve to repair an old block.
Storage failure prevents that enhanced send. If release-state persistence fails after sending, withhold content and
retain Unknown semantics.
Client history remains authoritative. The gateway stores its own derived additions, not the full user transcript.

Anchors use a versioned protocol canonicalizer and an incremental HMAC chain over ordered original client messages,
including message ordinal, role, supported content, and tool IDs, without gateway injections.
Identical text at different positions, edited history, and changed tool association cannot accidentally share an anchor.
Compute the chain linearly over a complete request rather than rescanning history for every block.

New user messages can still select existing content. Core's optional whole-value delivery-reuse strategy may omit a
duplicate append only when exact prepared text, complete dependencies/versions, and trust/placement constraints match,
and the old block is actually present in this outbound history and currently eligible.
Keep the original block in place and persist a bounded reuse marker at the new anchor.
Existing context/inference reuse constraints own this decision; Gateway cannot infer it from a hash alone.
Initially, partial overlap never permits splitting blocks, deleting Memory items, or rewriting wrappers.
Deliver the complete new block or withhold if the full budget does not fit.
This optional strategy follows #2086 off/shadow/active modes and independent quality acceptance.
A disabled strategy or unproven equivalence cannot be reported as deduplication.

### 6.1 Preparation results are not credentials

The four-field `PreparedContext`, rendered citations, `PreparedContextBuild.lineages`, and Bootstrap receipts cannot alone
authorize persistent replay. At the actual selected-and-rendered path, Core must produce **complete bounded reuse
dependencies**: exact resources/versions, owning Scopes, content digests, eligibility/freshness rules, maximum retention,
and output-content binding. Existing origins/lineages can contribute facts but cannot simply be renamed authorization
proof.

The initial embedded gateway obtains this internal metadata in the same authorized application call.
This is an optional internal extension of existing preparation, not a new prepare algorithm or public access to private
`_prepare_build`, and does not change the public four-field PreparedContext.
Families/code/references without complete dependencies cannot enter that enhancement profile. Missing dependencies do
not mean no validation is needed.

Before each send, Core validates these bounded dependencies through existing domain read/authorization capabilities:
current Scope/resource permissions, whether each exact version remains eligible for this historical use, active/retired
status, required head/freshness checks, and current privacy/retention policy. Validation does not run semantic search or
Embedding again. Even one Scope requires resource-lifecycle validation; readable Scope status does not authorize
indefinite egress of revoked material.

An invalid, expired, or unverifiable dependency yields `context_refresh_required` / withhold and stops the old epoch.
The host establishes a new conversation or explicit Handoff boundary. Never replace an old block in place, splice
semantic windows, or break native signatures.
Newly added material does not require repeating all old queries on every call. Profiles define maximum freshness ages,
new-user-message boundaries, and explicit refresh behavior.

### 6.2 Verifiable binding for standalone deployment

Remote enhancement is a later delivery. Ordinary prepare content cannot establish its provenance.
Propose negotiated optional response header `X-PowerContext-Reuse-Binding` on the **existing prepare operation**:
return it only when requested and Core has complete dependencies. The JSON body remains the original four fields.

Use a versioned, authenticated and encrypted short-lived envelope containing deployment/subject/Scope, exact content
digest, complete dependencies, policy/preparation profile, and expiry, without content. Bound its total length and
dependency count. Reject tampering, expiry, wrong audience/subject/Scope, and unknown key versions.
This is neither bearer permission nor injection proof. Every #2086 `input/check` authenticates again and validates current
dependencies; final admission binds only the actual request and target.

This extension adds no third Core operation or permanent receipt table. Verify rotation, expiry, size, and revocation
latency. Unnegotiated clients retain their contract. A remote gateway without this capability offers `check_only` instead
of unsafe frozen replay. Third parties may use the same binding without deploying this gateway's state database.

## 7. Native caching and complete budgets

Gateway state reconstructs exact injected model input; provider KV is separate state. Every actual call still generates
a new answer. The initial release caches neither answers, semantic results, nor tool side effects.

```text
Request 1: unchanged instructions/tools | user-1 + exact context-A
Request 2: same native prefix           | accepted answer-1 | tool results
Request 3: same historical placement    | user-2 + context-B
```

This is a logical illustration. Target roles, content blocks, token boundaries, and deployment rules determine actual
native qualification.

Native adapters consume #2086 `reuse_constraints` to choose supported automatic/explicit modes, eligible breakpoints,
TTL, and routing hints within approved profiles. Core does not emit universal `cache_control`, and Gateway does not
decide independently that invalid resources remain usable.
Cache settings precede final rendering; checks then cover the exact body, relevant headers, target, and output limits.

| Deployment cache capability | Native adapter behavior | Support boundary |
| --- | --- | --- |
| Implicit prefix caching | Preserve the exact native prefix, provide stable routing hints only where supported, and map actual cached-input usage. | No explicit breakpoint does not remove history/target validation or guarantee a hit. |
| Explicit breakpoints | Set native breakpoints and retention parameters on profile-approved tools/system/message blocks. | Breakpoints are not universal cross-protocol fields; preserve corresponding native boundaries. |
| Separate remote cache resource | Requires additional create/reference/delete operations, cost, and retention lifecycle. | Excluded initially; independently validate full-input visibility and resource governance without advance storage approval. |

Model aliases do not imply these capabilities. Each approved profile fixes measured support, minimum prefix conditions,
and usage definitions. Where routing hints are needed, derive them from stable deployment/Principal/Scope/epoch/target
profile partitions rather than the growing full-request digest, without exposing raw business identity.
Hints affect only routing mechanisms supported by the target.

| Change | Required behavior |
| --- | --- |
| Ordinary new tool results/user messages | Preserve exact historical blocks and native positions; recheck the complete extended request. |
| Tools/schema, system, or reasoning settings change | Validate protocol continuation; do not present new cache qualification as an old binding. |
| Model/API/account/region/renderer changes | Replan and re-admit, creating a new epoch where needed; no private KV migration. |
| Access revocation, resource invalidation, or required policy change | Stop invalid reuse even when cache eligibility is lost. |
| Optional cache parameter unsupported | Omit only when the original request remains approved and within budget, then recheck the changed final payload. |
| Cache miss | Generate normally, without retries or default padding to reach a cache minimum. |

Omitting cache parameters does not establish that provider implicit caching is disabled. Reject profiles that cannot meet
required retention, regional, or isolation constraints. Scope/route keys are not physical cache isolation; cache TTL is not
a deletion deadline. Stable request bytes are a locally testable invariant, not proof of identical provider token/KV
rendering. Validate native compatibility, actual usage, and latency separately.

Complete token admission includes instructions, original history, replayed blocks, tools/schema, protocol overhead, and
output reserve using a target estimator/profile with stated uncertainty. `prepare.max_bytes` bounds supplementary text
only. Withhold when the request does not fit; the host explicitly prepares less context or creates a Handoff.
Do not silently trim history or approved context. Core representation strategies run during preparation; Gateway must not
compress already frozen prepared text a second time.

## 8. Sends, output, and failure states

```mermaid
sequenceDiagram
    participant H as Host
    participant G as Gateway
    participant C as Core / RFC 2086
    participant S as Gateway store
    participant U as Actual upstream
    H->>G: Complete request + Scope + conversation
    G->>G: Authenticate / authorize / lock / classify
    G->>C: Validate current reuse dependencies
    opt New supported user message
        G->>C: Existing prepare with internal reuse metadata
        C-->>G: PreparedContext + complete bounded dependencies
        G->>S: Put immutable injection or reuse marker before send
    end
    G->>G: Exact replay + actual target + native rendering
    opt Reuse constraints not yet available
        G->>C: input/check preflight
        C-->>G: withhold + bounded constraints
        G->>G: At most one native re-render
    end
    G->>C: Final input/check or same in-process algorithm
    C-->>G: Current payload-bound and target-bound admission
    G->>S: Persist sending marker
    G->>U: One admitted attempt
    U-->>G: Complete raw response + available usage
    G->>C: output/check before any model content release
    C-->>G: Current content-bound release decision
    G->>S: Persist release authorization and reply anchor
    G-->>H: Checked result in original protocol
```

Checks retain #2086 `allow/block/withhold`, current authorization, trusted stage/profile, coverage, final content/target
binding, and expiry. A check-only operation does not prove what an executor actually sent.
Use the same rules and conformance samples for embedded/remote execution, not a more permissive gateway privacy engine.

First bound raw bytes, strictly parse UTF-8/JSON, and reject duplicate keys and unsupported structures before optional
enhancement. Retain original carriers until checks finish and inspect them alongside final payloads so serialization
normalization cannot hide prohibited values. Target token budgets apply only to the actual final outbound payload.
Embedding/rerank needed by prepare retain their own Core pre-send checks, rather than waiting for primary-model admission.

Required inspection covers instructions, history, context, tool definitions/arguments/results, supported structured
content, raw output, exposable errors, and completion state. Duplicate JSON keys, escaped/numeric representations, and
unknown fields cannot hide sensitive data.
Do not send prohibited material to a hosted classifier/summarizer before deciding whether egress is allowed.
Disabling optional optimization cannot disable required rules.

### 8.1 Strict output and streaming

`strict` buffers complete supported output and assembles ordinary responses or SSE only after approval.
Before approval, protocol heartbeats may contain no model content. Never release answer fragments, tool arguments, or
raw provider errors. This is not real-time token relay; it increases first-content-release latency and requires client and
reverse-proxy timeout validation.
`observe` measures behavior without claiming disclosure prevention. No enforcing bounded-streaming claim is made before
independent acceptance.

Limits cover response bytes, time, concurrency, parse depth, and total memory. 2 MiB × 16 is only the body lower bound,
not an RSS cap. Overflow, incomplete JSON/tool arguments, interrupted upstream streams, failed checks, or unavailable
release decisions stop content release and trigger best-effort cancellation.
Never spill sensitive responses into SQLite or temporary files.
Check current policy before send and release; measure revocation latency at the actual boundaries.
After SSE headers have been sent, terminate through supported error events rather than claiming an HTTP-status change.

### 8.2 Bounded sending and restart

Connection-pool and storage waits count against admission expiry. Revalidate binding validity immediately before network
write and check again after expiry. A durable sending marker cannot extend permission, policy, or target binding or
become permanent send permission.

One owner controls retries. Disable hidden SDK retries or integrate every actual attempt at the final boundary.
Only provably unsent connection failures permit bounded retries, with new admission.
Timeouts, lost connections, and crashes that may have reached the upstream yield `outcome_unknown`; do not automatically
pay to resend or fall back.

Allow one in-flight request per conversation, using an in-process lock and database version CAS.
Lease expiry does not imply that the upstream did not execute. No distributed exactly-once claim is made.
Return 409 on conversation concurrency conflicts; clients retry after the original call resolves.
Cancellation preserves known/uncertain send state.
Pre-send block/withhold does not advance sent state. A complete known terminal result whose release is rejected is a
known failure, not an uncertain send, and never triggers another automatic model call.

```mermaid
stateDiagram-v2
    [*] --> Ready
    Ready --> Prepared: immutable injection stored
    Prepared --> Sending: final admission and durable marker
    Ready --> Sending: existing replay validated and admitted
    Sending --> ReleaseAuthorized: complete checked response and reply anchor
    ReleaseAuthorized --> Ready: local delivery finished
    Sending --> Unknown: timeout or process loss
    Sending --> Ready: known terminal rejection without release
    ReleaseAuthorized --> Unknown: delivery interrupted or process loss
    Prepared --> Ready: provably no send
    Unknown --> Closed: explicit new boundary
    Ready --> Closed: invalidation or expiry
    Closed --> [*]
```

After restart, `Sending` and unconfirmed `ReleaseAuthorized` become Unknown, not replayed model calls.
There is no persisted response body, so an identical input cannot recover the previous answer.
`release authorized` means local checks approved release, not client acceptance, network delivery, or task completion.

The initial release does not promise model idempotency for client duplicates. Regeneration can incur another charge.
An unresolved call in Unknown returns 409; the host explicitly opens a new boundary and decides whether to execute again.
#2086's lack of durable response recovery remains. Gateway state prevents its own blind resends after restart only.

## 9. Minimal API inventory

Core requests retain `scope_id` and existing flat operation IDs. Model-protocol bodies do not acquire vendor-incompatible
domain fields.

| Category | Interface/operation | Status and ownership |
| --- | --- | --- |
| Context | `prepare_context`, with Bootstrap/Work/Handoff under host workflows | Existing Core APIs; no duplicate preparation algorithm. |
| Input/output | `POST /v1/inference/input/check` / `check_inference_input`; `POST /v1/inference/output/check` / `check_inference_output` | Proposed in #2086; consumed here without replacement checks. |
| Model ingress | `/gateway/v1/chat/completions`, `/gateway/v1/responses`, `/gateway/v1/models` | Initially proposed here; protocol-native success responses. |
| Conversation status | `GET /v1/scopes/{scope_id}/gateway/conversations/{conversation_id}` / `get_gateway_conversation` | Proposed here; owner-visible metadata only. |
| Delete derived copies | `DELETE /v1/scopes/{scope_id}/gateway/conversations/{conversation_id}` / `delete_gateway_conversation` | Proposed here; close epoch, prevent further use, and delete gateway replay blocks. |
| Capability discovery | Existing `get_capabilities` | Extend declarations for protocol, carrier, strict mode, enhancement, replay, remote binding, and versions. |
| Remote preparation binding | Optional `X-PowerContext-Reuse-Binding` response header on existing prepare | Later negotiated extension; unchanged default body and no additional operation. |

The first valid enhanced request creates a conversation; no start/plan/prepare/finalize gateway CRUD family is needed.
GET returns no context/response content, credentials, or matched secrets. Missing resources retain non-disclosure behavior
without revealing another owner's state.
DELETE acts on gateway-derived copies, not Source/Artifact, and cannot recall material already sent to models.
Order deletion with in-flight send/release in the same conversation state machine. Return 409/retry-later while a request
is being cancelled. Confirm success only after preventing subsequent local use and deleting visible rows, without
promising physical erasure from SQLite pages, backups, or providers.

`X-PowerContext-Scope-ID` must match trusted bindings. Domain conversation routes use the path Scope; conflicting inputs
cannot change resource ownership. When clients cannot send the header, a trusted one-credential/one-Scope binding may
supply it. A global default Scope is not isolation.

Model paths preserve native envelopes. Errors use protocol-supported shapes with stable content-free reasons and request
correlation. Use `401/403` for authentication/authorization, `409` for history/in-flight/Unknown conflicts, `422` for
unsupported carriers or windows, `429` for gateway admission capacity, `503` for required policy/dependency unavailability,
and 403 for explicit policy prohibition. Do not collapse withhold, empty, upstream errors, and local rejection into success.
Strip all PowerContext control headers, reuse bindings, and caller credentials from final upstream requests; send only
approved provider headers.

These are proposals; this RFC does not change `openapi/powercontext.yaml`.
Implement Core checks and management metadata in that source contract, regenerate clients, and run `make api-generate` /
`make contract-test`. Version model wire schemas separately, document their OpenAPI entry points and conformance fixtures,
and do not present ordinary Core ErrorResponse as the provider protocol.

## 10. Persistence, costs, and lifecycle

### 10.1 Why two dedicated tables

Exact injections must survive retries and gateway restart; memory alone cannot preserve history integrity.
Source is domain evidence, Artifact is versioned knowledge, Bootstrap receipts address specific delivery deduplication,
and daily usage aggregates permit loss. None has the state machine for undelivered model calls or immutable injections.
Reducing table count does not justify storing private copies in Source/Artifact, processing leases, or decision observations.

Create `gateway.sqlite3` only when optional persistent gateway replay is enabled:

| Table | Key and required contents | Mutation contract |
| --- | --- | --- |
| `gateway_conversations` | deployment/Principal/Scope/conversation/epoch; config/renderer/target profiles; client-history digest and latest reply anchor; latest attempt state/digest; version and expiry. | CAS updates; latest attempt is a bounded blind-resend guard, not a full ledger; no complete history or response body. |
| `gateway_replay_blocks` | Complete conversation partition plus message anchor; exact injection/native position, reuse binding, content digest, and creation/expiry. | Put-if-absent; conflicts fail; delete by conversation/expiry; store this gateway's injections only. |

Initially add no Core tables and no profile/key/log/response/capture/tool/summary tables.
Use trusted files/secret references for configuration and existing observations for low-sensitivity diagnostics.
Selected versions and commits retain Core domain storage.
Without persistent replay, use check_only without automatic cross-request enhancement. Never silently lose injections
inside an already enhanced conversation.

Use local single-host SQLite WAL with strict directory/file permissions, not a shared filesystem, and one gateway
process owner. Hold an exclusive process-lifetime lock when opening the state store; a second owner fails startup.
Request-level CAS is not a substitute for single-process sending.
A different Core backend does not imply multi-host gateway recovery.
The new file has explicit independent schema migration. Reject unknown schemas, back up before migration, and verify
exact-block readback. Do not assign today's provenance/permissions to old blocks. Close epochs missing dependencies
required by the new version.

### 10.2 Bounds, expiry, and cleanup

Persist content only within deployment retention policy, using authenticated encryption with managed keys.
Unavailable keys prevent sensitive writes. Keys are not stored in SQLite. Encryption does not replace Scope/owner
authorization, backup protection, or current policy.
Conversation/message digests use deployment HMAC; low-entropy content hashes are not public diagnostics.

Proposed defaults are one-hour idle expiry, 24-hour absolute expiry, 128 blocks, and 256 KiB total payload per conversation.
Effective expiry is the minimum of configuration, Core binding, and permitted resource retention.
Stop reads/sends on expiry before asynchronous cleanup; delayed cleanup never extends eligibility.

128 is a record-count bound; 256 KiB is cumulative, not per block.
At 100 active enhanced conversations per Principal, content alone can reach approximately 25 MiB, or 25 GiB for 1,000
Principals, before indexes, encryption, WAL, and backups.
Deployments must bound per-Principal and global bytes/conversation admission, not rely on TTL alone.
At capacity, reject new enhancement instead of evicting required blocks and continuing with broken replay.

Batch reads/writes by complete conversation keys and bounded anchors; each preparation retains its original entry
limit. Bound the deduplicated dependencies for the entire conversation to 64, not merely this turn's eight candidates.
At that limit, withhold and require a new boundary rather than truncating the dependency list.
A new message must not scan all conversations, Sources, or Memory.
Cleanup uses indexed bounded batches. Measure hot-conversation concurrency, WAL growth, restart, and deletion latency.

Although client history is not persisted, each request still requires parsing/scanning with linear cost in request size.
Complete privacy inspection and strict buffering remain real CPU/memory costs despite retrieval occurring only on new
messages.

## 11. Usage, operational controls, and observations

Reuse existing telemetry, usage wrappers, statistics/sinks, and content-free operational conventions.
Register new operation/unit/outcome fields, protocol values, finite reasons, and coverage; update both language guides.
Use `log_safely` and existing request/trace identity, excluding prompts, cited content, responses, credentials, and
sensitive paths. Logs and lossy usage are not response recovery, audit, or billing ledgers.

Gateway emits typed attempt metadata: actual target/profile, preparation/check/send/buffer/release latency,
available ordinary-input/cache-read/cache-write/output/reasoning usage, known/estimated/unknown coverage, and incomplete
reasons. Reuse #2086's optional inference/usage metadata rather than introducing another usage model.
Daily aggregates cannot reconstruct missing cache or failed-attempt fields. Unknown is not zero.
A task correlation ID is observation, not proof of host task success.

Initial execution controls are bounded concurrency, input/output sizes, timeouts, cancellation, and backpressure, not
complex rate limiting or hard cost reservations. Distinguish upstream first token, first approved content release, and
delivery completion. Cancel on client disconnect where possible without assuming upstream billing stops immediately.
Required dependency failures surface through readiness/operations; safety failure cannot degrade into bare forwarding.

## 12. Later features and domain composition

| Capability | Composition | Required independent acceptance |
| --- | --- | --- |
| Standalone deployment | Same Core prepare/check, trusted delegation, and verifiable reuse binding. | Real HTTP path, current authorization, rotation, binding size/expiry, cancellation, and faults. |
| Read-only memory tools | Explicitly allowed MCP/HTTP tools retaining evidence/permissions; tool rounds enter complete checks and exact replay. | Model-visible catalog, host-visible behavior, round/cost bounds, native tool/reasoning pairing. |
| Write tools/side effects | Disabled by default; explicit user permission with domain approval, idempotency, and commits. | Execution permission is not tool authorization; hidden loops never approve writes by default. |
| Explicit conversation-fragment capture | User/host-requested `/v1/sources/content` or working-notes with stable source_id and exact-value retries. | Strip injected blocks only from trusted injection records; verify privacy/retention, receipts, conflicts, and readback. |
| Long-task continuity | Prefer explicit host Handoff/rollover through existing prepare/finalize/commit/continue. | Real persistence/readback; temporary summaries are not Handoffs or evidence of acceptance/completion. |
| Automatic compaction | Define new windows, summary evidence, and signature compatibility only with independent quality and complete-cost evidence. | Check and bill summarizer calls, account for lost cache qualification and irreversible information loss. |

A completed model response is not user acceptance; repetition in the next turn is not knowledge-capture authorization.
The initial gateway does not automatically ingest based on these heuristics.
Future explicit capture uses the existing identical-value contract: one complete Source identity and exact
content/metadata return the same receipt, while different values conflict.
Do not generate a new ID after timeout to hide uncertain writes or treat Source 202 as extracted Memory.
Working Notes are explicit temporary evidence, not automatic Memory promotion.

Gateway does not reimplement background write gates. Required privacy failure/unknown cannot become write HOLD or empty
extraction success and must not advance unprocessed Source cursors.
Existing domain consumers retain recovery permissions and retry budgets.

## 13. Phases and acceptance

### P0: the smallest complete Server deployment

Deliver one Scope, explicit conversation IDs, complete memory/experience reuse dependencies, Chat and full-history
Responses, one approved deployment profile, strict text/structured-tool output, persistent exact replay, and bounded
execution. Exclude opaque carriers and unsupported families through capability declarations, not permissive forwarding.

#2086 shared input/output checks and complete internal reuse dependencies are safety prerequisites.
Its gateway-free vertical slice can still ship independently. Enable this component's enforcing/enhancement declarations
only after prerequisites pass. Embedded P0 needs no HTTP check round trips, standalone deployment, or remote preparation
binding. Check-only users can leave replay disabled without creating gateway tables.

### P1: remote access and more protocols

Validate the optional binding header on existing prepare and the two #2086 remote checks, then deliver standalone and
third-party conformance examples.
Add Messages, its native signatures, and additional validated carriers/families, publishing real client/deployment
versions for each. P0 acceptance does not establish these paths.

### P2: evidence-driven extensions

Add read-only tools, explicit capture, larger deployments, or quality-validated compaction only after demonstrated demand
and independent review. They are not P0 requirements or advance approval for tables, automatic knowledge writes, or
dynamic model scheduling.

### Required validation

| Dimension | Reproducible acceptance |
| --- | --- |
| Coordination between RFCs | Identical embedded/HTTP policy samples, including unknown/unavailable, authorization, actual targets, content binding, and expiry. |
| Privacy | Seed prohibited values in host history, context, tool structures, escaping/duplicate keys, and raw errors; prove controlled upstreams/hosts do not receive blocked content. |
| Reuse and access | Scope denial, retired/deleted Artifacts, head/freshness changes, tampered/expired bindings, and another user/Scope ID cannot preserve invalid replay. |
| History integrity | Initial/new messages, tool rounds, exact whole-value duplicates versus partial overlap, regeneration, client edits/compaction, identical questions in different conversations, and host/gateway integration together. |
| Native carriers | Stable roles/tool IDs/order/content blocks, explicit renderer changes, and rejection of unsupported signatures/hidden history. |
| State and restart | Block conflicts/disk full prevent sends; real process crashes before/after send and release leave Unknown without automatic resend. |
| Concurrency/cancellation | One in-flight call per conversation; PDP/policy failure, disconnect, SSE backpressure, limits, and cleanup do not leak. |
| Deployment/migration | Disabled means no state file; enabled restart reads exact blocks; unknown schemas/keys fail; deletion prevents later local use. |
| Domain reuse | Four-field prepare unchanged, no duplicate injection, no automatic Source/Memory/Handoff, and no successful Source-window consumption after privacy failure. |

Validate actual protocol support with supported clients and real providers. Mock upstreams establish local bytes and
failure behavior, not real cache hits, signature compatibility, or host acceptance.

### Efficiency experiments and launch criteria

Fix task/repository/model/protocol/tools/schema, evidence, policy, output budget, and quality criteria, then compare:

1. Current direct execution with the same required privacy checks.
2. Gateway execution without optional enhancement, measuring gateway/strict overhead.
3. The same prepared content with exact replay, measuring preparation frequency, final native prefixes, and actual cache usage.
4. The same reuse with validated representation/native-cache strategies, isolating their incremental value.

Separate cold/warm/multi-turn conditions, context refresh, parameter changes, revocation, and window boundaries.
Providers may cache by default; absence of cache parameters is not an uncached baseline.
Preserve existing Core deduplication and recall optimizations in every group instead of manufacturing repeated retrieval.

Measure complete-task quality, failures/repairs/retries, primary/auxiliary model cost, retrieval/Embedding/rerank/decisions,
cache writes/reads, unknown coverage, CPU/RSS/disk, upstream first token/first release/total time, and P95/P99.
Paid paired experiments require a separate experiment budget; shadow candidates do not add model calls by default.
Enable an optimization by default only when privacy, quality, resource constraints, and measured gains meet
predeclared thresholds. Publish no savings numbers without experiments.

# Drawbacks

A gateway on the critical model path adds failure modes, authorization/scanning cost, strict-release latency, and
responsibility for retained private copies.
Persistent replay needs local state, keys, cleanup, and migrations; single-host SQLite has no multi-host failover.
Reliable enhancement requires conversation/full-history/protocol capabilities and cannot cover every base-URL-only client.

Cache stability, real-time streaming, immediate resource changes, and automatic summarization have tradeoffs.
Current authorization, safety, and accurate history take priority, so refresh can reduce reuse and strict mode can delay
content delivery substantially.

# Rationale and alternatives

| Alternative | Decision |
| --- | --- |
| Put all algorithms in Gateway | Duplicates #2086/Core and forces background/embedded users through a gateway; shared domain algorithms stay in Core. |
| Stateless prepare proxy only | Cannot restore client-invisible historical injections; suitable for check_only, not stable enhancement. |
| Repeat all retrieval each turn | Adds calls and changes historical prefixes; prepare only for new messages and validate eligibility without semantic search. |
| Put full prompts in Source/Artifact | Confuses evidence with private derived copies and changes retention/extraction behavior; use two bounded dedicated tables. |
| Full transcripts plus response/attempt ledger | No initial recovery/financial guarantee requires them; retain only injections and the latest unresolved state. |
| New gateway identity/tenant/policy platform | Duplicates Principal/Scope/Access/Policy; reuse existing boundaries and trusted configuration. |
| Claim complete safety through opaque downstreams | Actual targets/mutations/retries are unknown; require a final-boundary plugin or validated transparent path. |
| Prefer cache stability over current eligibility | Risks sending revoked content; current safety and resource eligibility take priority. |

# Prior art

Existing scoped context/assembly/Builder, Source/Artifact, Bootstrap/Work/Handoff, Server authentication/authorization,
DecisionPolicy, inference adapters, and usage wrappers provide the foundation.
[RFC 2086](2086-core-inference-gates-and-optional-gateway.md) supplies the context-protection and efficiency contracts
consumed here, not a separate algorithm design to reimplement in Gateway.
Concrete deployment adapters and acceptance fixtures describe model protocols and native provider caching.

# Unresolved questions

Before merge, fix the initial real client/provider/SDK combinations, configuration entry point, deployment retention
requirements, family-specific complete dependencies, key facilities, and resource/SLO thresholds.
Execution action/role, two dedicated tables, embedded default, final-boundary checks, and no automatic uncertain-send
retry are explicit decisions.

The remote-header envelope encoding and encryption-key rotation must be fixed before P1 implementation.
Until then, P1 must not claim remote persistent enhancement; embedded P0 uses the same internal dependency contract.
Multi-host execution, financial ledgers, automatic compaction, and write tools are not unfinished initial requirements.

# Future possibilities

Extend the validated vertical slice with more protocols, controlled read-only tools, explicit knowledge capture, and host
continuity. Multi-host or durable response recovery requires a separate justification for shared state, single execution
and delivery, uncertain results, and data deletion. Two local tables do not constitute a distributed execution platform.
