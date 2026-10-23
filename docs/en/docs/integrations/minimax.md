---
status: community
title: MiniMax Code
description: Install the native MiniMax plugin and distinguish package discovery from Server access.
---

# MiniMax Code

`community` · experimental

PowerContext installs a native MiniMax plugin containing the `powercontext-project-context` Skill and a streamable HTTP MCP connection. MiniMax owns the MCP session. The plugin does not start the Server or automatically capture conversations.

## Prerequisites and versions

Use Python 3.11+, the PowerContext CLI, and a running PowerContext Server. Install the CLI, Server, and plugin from the same repository revision. For a pinned release, use its matching repository tag with `--ref`; the plugin manifest version is independent of the Python package version. Remote refs follow the existing branch/tag convention. To install a specific commit, check it out locally and use `--source`.

Native qualification uses the official npm CLI `@minimax-ai/code@0.6.9` on Linux x86_64 with Node.js 22.23.3. Desktop discovery and other platforms are not qualified by that CLI run. See the [acceptance record](https://github.com/oceanbase/powercontext/blob/master/integrations/minimax/acceptance.md) for the commands and evidence boundary.

## Install and select a target

```bash
powercontext setup minimax

# Repository root or the native plugin directory
powercontext setup minimax --source /path/to/powercontext

# A branch or tag containing the plugin, matched to your Server revision
powercontext setup minimax --source oceanbase/powercontext --ref <matching-tag>

# Explicit MiniMax data root and executable launcher
powercontext setup minimax --source /path/to/powercontext \
  --minimax-data-dir "/path/to/MiniMax data" \
  --minimax-command /path/to/mcode
```

The destination is `<data root>/plugins/powercontext`, including hidden files, Skill references, icon, and license. `--minimax-data-dir` takes precedence over nonempty `MINIMAX_DATA_DIR`, then `MAVIS_DATA_DIR`, then the saved installation target. Without these choices, the installer asks the selected CLI's `plugin marketplace list --json` for its local directory, including native profile/build selection. If the CLI is missing, the default is `~/.minimax`; a selected `MAVIS_PROFILE` requires an explicit data root in that case. Older or custom builds whose marketplace response cannot be interpreted require `--minimax-data-dir`.

The launcher comes from `--minimax-command`, the saved launcher for the same data root, or `mcode` on PATH. An invalid explicit or saved launcher fails before installation. The selected data root is passed to native verification as `MINIMAX_DATA_DIR`; launch MiniMax with the same directory. Setup saves the target for later `doctor minimax` and `config agent minimax` commands. Environment directory overrides still take precedence over that saved target.

Interactive `powercontext setup` includes MiniMax. Multiple agents share the same setup behavior:

```bash
powercontext setup codex minimax --source /path/to/powercontext --json
powercontext doctor minimax --json
```

## Configure a protected or remote Server

Setup defaults to the local Server or preserves an existing native MCP endpoint. To select a different endpoint:

```bash
powercontext setup minimax --source /path/to/powercontext \
  --server-url https://memory.example
```

Supply credentials through `POWERCONTEXT_MINIMAX_AUTHORIZATION`, or the shared `POWERCONTEXT_CLIENT_API_TOKEN`, in the process environment or setup `--env-file`. The installer stores a literal Bearer header only in `<data root>/mcp.json`. It never writes credentials into the distributed plugin or installation record. Enter secrets without exposing them in shell history. On Linux/macOS, the private file has owner-only permissions; protect it with an appropriate ACL on Windows.

Setup merges only the PowerContext server entry, preserving other MCP servers, unknown fields, headers, and an existing disabled preference. Existing credentials are retained. Moving an authenticated entry to another endpoint requires an explicit replacement credential; setup will not silently forward the old credential. Malformed private configuration fails before package installation.

MiniMax uses its enabled private `powercontext` entry before the bundled connection. Configure literal URLs and headers; the supported private configuration does not expand `${TOKEN}` placeholders. Setup's remote HTTP consent governs PowerContext's own configuration and diagnostic requests; it does not bypass MiniMax's native HTTP/TLS restrictions. Prefer HTTPS. Doctor reads the native endpoint and headers, rather than treating a global PowerContext token as the host's credentials.

## Reinstall, upgrade, and recover

Repeat the setup command with the desired source/ref. The installer records its managed file hashes under the PowerContext data root, keyed by MiniMax data directory. It updates those files, removes unchanged obsolete managed files, and preserves user-created files and unrelated plugins. A modified managed file or a new package file colliding with user content causes a conflict before replacement.

An identical manually copied package can be adopted while keeping extra user files. A different unmanaged package is rejected. Back it up outside the MiniMax `plugins` directory, preserve its private connection settings, and reconcile custom changes before retrying. Do not leave a second PowerContext package under another immediate plugin directory.

Package publication restores the prior package and ownership record on failure. Connection publication restores the prior private/shared configuration on failure. Setup reports a completed package installation separately if subsequent connection configuration fails. If interrupted publication leaves a recovery backup, restore the indicated backup before retrying.

## Understand verification results

- `package`: the native manifest and referenced local resources are present.
- `native`: MiniMax reports the local `powercontext` plugin installed, enabled, at the expected version, with at least the required Skill/MCP counts. Counts do not establish resource names or an active MCP session. A successful command that omits the plugin fails this check.
- `server_liveness` / `server_readiness`: the selected Server responds and reports its readiness.
- `server_access`: the CLI accesses Server info with the effective native headers. It does not prove that MiniMax established a connection or invoked a tool.

Files copied with no CLI are reported as installed with degraded native verification and a nonzero overall exit. For a desktop installation, install the supported CLI or pass an existing executable launcher with `--minimax-command`; package copying alone is not desktop qualification. Doctor does not install packages, rewrite configuration, or enable plugins. Native CLI commands may maintain their own runtime bookkeeping.

Refresh the Local catalog with `Ctrl+R` in `/plugins`, or restart MiniMax, using the same data directory. Preserve user disablement during upgrades. To enable it deliberately, run the selected launcher's `plugin enable powercontext@local --json`, with the target data directory in `MINIMAX_DATA_DIR`, then rerun doctor. Restart after changing a connection that has already been used. Verify a real MCP tool result in MiniMax separately from the CLI checks.
