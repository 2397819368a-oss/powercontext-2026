---
title: Cindy
description: Connect Cindy 0.1.104 to the shared PowerContext MCP tools.
---

# Cindy

`experimental`

Cindy connects to the running PowerContext Server through its native custom MCP settings. This uses the same
Server tool catalog and authorization as other MCP integrations. An optional Ghost adapter provides automatic hooks.
The compatibility target is [Cindy 0.1.104](https://github.com/makecindy/cindy/releases/tag/v0.1.104).

## Connect the Server

Follow [Install and run](../get-started/install-and-run.md) to install PowerContext and
[Deploy the Server](../operate/deploy-server.md) to configure and verify it. On supported personal macOS/Linux:

```bash
powercontext service install --env-file /path/to/powercontext.env
powercontext service status
```

Load the corresponding client connection settings, then run `powercontext ready` and `powercontext capabilities`.
In Cindy settings, add a custom MCP server with these values:

| Field | Value |
| --- | --- |
| Name | `PowerContext` |
| Transport | `http` (Streamable HTTP) |
| URL | `http://127.0.0.1:8000/mcp`, with your actual port |
| Bearer token | Your Server token, without the `Bearer ` prefix; empty for anonymous local use |
| Additional headers | Empty for ordinary Bearer authentication |

Select the connection for the task and inspect its discovered tools. Use Streamable HTTP; legacy SSE is not available
through Cindy's Codex/Pi configuration path. For a Server on another machine, use a reachable HTTPS URL. Desktop
loopback is not the remote Agent's loopback; the SSH Pi path connects to custom MCP endpoints directly.
See [remote connections](../operate/connect-remote-server.md).

## Select a Scope and use explicit tools

Inspect existing Scopes through `list_scopes` and `get_scope`. Create one only when requested.
Call `resolve_scope_binding` with the selected Server-issued `explicit_scope_id` and `allow_default: false`, then
reuse the returned `scope_id`. A missing or invalid binding stops the memory operation. Do not guess a Scope from
a directory or fall back to a global Scope.

Use the actual discovered namespace for `search_memory`, `remember_memory`, and `capture_content_source`.
Memory saves require user intent and result verification. Source capture accepts original material and retains the
same `source_id` for retries; it does not prove Memory extraction has completed. Handoff and candidate-review
operations use the same Server tools and approval boundaries as other MCP hosts.

The native MCP connection does not provide a trusted Cindy session identity or workspace path. Explicit tool calls
use the selected Scope. The automatic adapter receives its trusted session ID from a Host event and resolves the
Server's Cindy session binding. A workspace binding is not inferred from Agent-authored text.

Retrieved history does not override current instructions. On failure, report the failed operation and continue
ordinary work from available context. Inspect Server state before retrying an uncertain write.

## Enable automatic recall and prompt capture

Import `powercontext-cindy-hooks-0.1.0.cindy`, enable the plugin, and open its settings. Use the same Server URL
and Scope as the native MCP connection, configure the hook credential if needed, and select **Enable automatic
memory**. Users do not need DSH or additional runtime packages.

Select an explicit Scope in settings, or leave it empty and have an operator register the Host-issued session ID
through `set_scope_binding` with `integration: "cindy"` and `kind: "session"`. The plugin `status` tool reports the
latest observed session ID and hook outcome. Missing bindings allow the original message without memory operations;
they do not trigger Scope creation or default-Scope fallback.

The `will-user-message` hook recalls bounded PreparedContext and independently captures the original prompt as a
Source before dispatch. Capture is separately selectable and does not prove extraction or Memory production.
The captured text is the input to this adapter before its rewrite; an earlier Ghost may already have rewritten it.
Cindy does not supply a separate raw-draft field.
Explicit writes, Handoffs, and review use the native MCP tools.

A ready snapshot is appended as untrusted historical evidence. Cindy's rewrite updates the message displayed,
stored, and sent to the Agent. Failed or empty recall leaves the original message
available; late replies cannot rewrite it. Context obeys its UTF-8 budget and the complete message must fit Cindy's
16,000-character rewrite limit. The adapter sends an allow verdict after a local timeout; Cindy independently
enforces its three-second deadline for the complete hook dispatch.

The same delivery reuses its Source ID; a newly submitted message has a new Host hook ID. Edited messages carrying
the adapter's historical markers are allowed without recapture or reinjection. Flagged credentials are skipped.
Uncertain writes are reported without automatic retry. Local user-message dispatch and steering are covered;
remote, worker, scheduled, goal-continuation, and pre-compaction flows are not qualified. Use the CLI for Server readiness checks.

## Experimental support

Cindy 0.1.104 provider and hook gateway tests cover MCP tools, session isolation, prompt persistence, and
message rewrites against a real Server. Host credential/message bridges are simulated; desktop installation,
credential UI, and model execution remain unverified. Cindy is outside the qualified host catalog.
