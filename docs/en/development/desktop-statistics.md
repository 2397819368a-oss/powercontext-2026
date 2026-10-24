---
title: "Desktop Scope statistics"
description: "Exact-Scope native statistics, isolation and qualification."
---

# Desktop Scope statistics

Desktop consumes `POST /v1/stats` through the native `get_stats` command. The
renderer supplies only the active generation and `today`, `7d` or `30d`; native
code obtains the current Scope and constructs an exact, single-Scope selection.
There is no renderer-supplied endpoint, Scope list or all/subtree mode.

The connection must have a matching qualified operation profile. Each read
rechecks identity using the existing connection policy, preserves Server
authorization failures, and applies the existing transport timeout and 1 MiB
response bound. A denial never retries against a broader selection. Responses
must retain the requested selection, Scope IDs and period. Aggregate and per-Scope
recall estimators may be null, as permitted by the public contract.

Statistics use their own cancellation channel. A replacement statistics read,
`cancel_stats_reads`, or a generation change cancels pending statistics; Memory
read cancellation does not cancel them. Submitted Memory writes retain their
existing original-target behavior.

The operation catalog and reachable wire models come from
`pnpm --dir desktop generate`; IPC declarations come from the Rust `export_ipc`
example. The native exact-selection projection reuses concrete generated field
types while preserving the nullable estimator and prohibiting broader selections.
Neither Server APIs nor historical qualification records are changed.

See [the qualification record](../../../desktop/VALIDATION.md#current-scope-statistics-qualification)
for the tested Server/contract/wheel identities and actual validation limits.
Synthetic tests establish local handling; the built-wheel harness separately
establishes the observed SQLite protocol behavior. Installed Windows/macOS page
acceptance remains a platform gate.
