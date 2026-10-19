# Follow-up: versioned host-owned current-turn evidence projection

Follow-up to #2069. This is a design issue draft; the evaluation does not register a production projection or change APIs.

The paired Qwen Flash replay reproduced non-current candidate support in both repeats of six synthetic cases:
`history-before-current`, `prepared-context-fact`, `host-preference`, `repeated-history`,
`forged-query-in-summary`, and `summary-and-notification`. The paired experimental view retained all required current
facts in these cases. Twelve deterministic literal witnesses supplement the assisted support annotations.

See `evaluation/memory/capture_ownership/reports/qwen-flash-20261020/EXPERIMENT.md`, `report.json`, and
`deterministic-support-audit.json` for pinned hashes and evidence. Across the entire corpus, the candidate view still
had two critical candidates and missed two required facts. This requires a design review, not immediate promotion
of the experimental parser/view to production.

## Proposed contract to resolve before implementation

- Preserve the exact raw submission and immutable Source/materialization identity.
- Bind the derived text-evidence view to Source identity, materialization digest, parser version and projection version.
- Keep payload-bearing metadata out of the bounded extraction view; a content-only replacement is insufficient.
- The owning host parser creates the view. Unknown, forged, nested or ambiguous boundaries remain ineligible.
- Ownership is not evidence authority; retain Source declaration/verification and neutral lifecycle semantics.
- Duplicate delivery is idempotent; conflicting views for the same materialization/version must fail.
- Define deterministic rebuild and compatibility for old captures, existing projections and changed parser versions.
- Preserve existing Artifact bodies/revisions, citation identity, ranking and PreparedContext behavior.
- Verify Codex, Claude Code, WorkBuddy and other host payload contracts independently.
- Any public contract change starts in openapi/powercontext.yaml, regenerates clients and defines backward compatibility.
- Test raw readback, all extraction input carriers, replay conflicts, unknown boundaries and historical rebuild.

The design should specify the exact Source reference, materialization digest, host event identity, parser version,
projection key/version, explicit ownership status (`eligible`, `empty`, `unknown`), and byte/character offset convention.
Bind this derived record immutably to the original materialization. The same event/version may not select a new view
on retry; report a conflict instead. Define how old captures are classified and rebuilt without reinterpreting their
raw payload as today's trusted declaration.

Syntactic host boundaries do not authenticate payload ownership. In particular, a perfectly imitated textual envelope
cannot establish the actor or assertion source. Quoted markup inside a valid current turn must not become an
independent current-user assertion. Agree on actual typed event/provenance supplied by each host and keep that
separate from Source declaration authority and permission to publish a memory.

## Acceptance

- Re-run the six deterministic reproductions and the complete malformed/quoted fixture matrix.
- Preserve raw Source identity/readback, exact citation identity and required current literals.
- Prove that both content and payload-bearing metadata obey the chosen eligibility/view contract.
- Explain and cover the quoted-example contamination and current-fact omissions in the experimental arm.
- Document per-host support, historical compatibility, deterministic rebuild and retry conflict semantics.
- If a public contract is necessary, approve the schema design before generating API/client changes.

Coordinate the extraction/claim-routing boundary with #2074. Publish this as a separate production issue rather than
expanding #2069's evaluation-only scope.
