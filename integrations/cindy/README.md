# Cindy integration

`experimental`

Connect [Cindy 0.1.104](https://github.com/makecindy/cindy/releases/tag/v0.1.104) to PowerContext
through its native custom MCP settings. The optional `powercontext-hooks` Ghost adds automatic recall
and prompt capture before local user messages are dispatched.

See the [English guide](../../docs/en/docs/integrations/cindy.md) or
[中文指南](../../docs/zh/docs/integrations/cindy.md) for connection settings, Scope selection, and hook behavior.

## Build the hook archive

```bash
uv sync --locked
pnpm --dir integrations/dsh/plugins/powercontext install --frozen-lockfile --config.auto-install-peers=false
pnpm --dir integrations/dsh/plugins/powercontext exec tsdown --config ../../../cindy/tsdown.config.ts
uv run python integrations/cindy/scripts/package.py
```

Import `dist/powercontext-cindy-hooks-0.1.0.cindy` into Cindy. The archive is standalone;
users do not need DSH or additional runtime packages.

The worker bundles the shared HTTP client, PreparedContext validation, and credential checks from the DSH
integration at build time. Explicit business tools use native MCP; the hook uses the shared HTTP API for
context preparation and Source capture.

## Run behavior checks

```bash
node --test integrations/cindy/tests/*.test.cjs
uv run pytest tests/e2e/test_mcp_transport.py tests/e2e/test_mcp_full_profile.py -q
```

For Cindy provider and hook gateway acceptance:

```bash
git clone --depth 1 --branch v0.1.104 https://github.com/makecindy/cindy.git /tmp/cindy-validation
POWERCONTEXT_CINDY_SOURCE=/tmp/cindy-validation \
  uv run pytest tests/e2e/test_cindy_mcp_chain.py tests/e2e/test_cindy_hooks_chain.py -q
```

These tests use Cindy's provider and subscription gateway source with a real authenticated TCP Server.
They cover MCP discovery, Scope isolation, Memory readback, prompt capture, and rewrite verdicts.
The packaged worker runs with access limited to its installation directory to verify standalone operation.
Host credential/message bridges are simulated; desktop installation, credential UI, and model execution
remain unverified.
