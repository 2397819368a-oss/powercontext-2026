# Follow-up: versioned host-owned current-turn evidence projection

Reproduced cases and pinned inputs: see the accompanying report.json and its run/lock hashes.
This is an implementation-ready issue draft; no production registration or API change is made by the evaluation.

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

Open a separate production issue with these requirements and the specific reproduced case IDs; coordinate with #2074.
