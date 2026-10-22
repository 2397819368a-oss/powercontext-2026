/*
 * Copyright (c) 2026 OceanBase.
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 * http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */
// Exercise the pinned upstream gateway. Database/continuation modules are unused boundary stubs;
// the plugin message bridge and credential injection are simulated, not Electron acceptance.
import { readFile } from 'node:fs/promises';
import { readFileSync } from 'node:fs';
import { stripTypeScriptTypes } from 'node:module';
import { join } from 'node:path';
import { spawn } from 'node:child_process';
import { createInterface } from 'node:readline';
import { runInNewContext } from 'node:vm';
import { once } from 'node:events';

const [source, plugin] = process.argv.slice(2);
const input = JSON.parse(readFileSync(0, 'utf8'));
const dataUrl = text => `data:text/javascript;base64,${Buffer.from(text).toString('base64')}`;
const brain = join(source, 'apps/desktop/src/main/cindy-brain');
const shared = await readFile(join(source, 'apps/desktop/src/shared/ghost.ts'), 'utf8');
const constants = ['GHOST_ASSISTANT_HOOK_TIMEOUT_MS', 'GHOST_HOOK_FUSE_THRESHOLD', 'GHOST_HOOK_REWRITE_MAX_CHARS', 'GHOST_HOOK_TIMEOUT_MS', 'GHOST_SUB_QUEUE_MAX']
  .map(name => shared.match(new RegExp(`export const ${name} = [^;]+;`))[0]).join('\n');
const owner = stripTypeScriptTypes(await readFile(join(brain, 'ghostOwnerScope.ts'), 'utf8'), { mode: 'transform' });
const unused = 'export function eq(){throw new Error("unexpected database access")};export function getDbClient(){throw new Error("unexpected database access")};export const sessions={};export function isTurnContinuationBoundaryEvent(){throw new Error("unexpected continuation access")};';
const replacements = {
  '../../shared/ghost.js': dataUrl(constants),
  './ghostOwnerScope.js': dataUrl(owner),
  '@cindy/maker-shared/turn-continuation': dataUrl(unused),
  'drizzle-orm': dataUrl(unused),
  '../localDb/client/current.js': dataUrl(unused),
  '../localDb/schema.js': dataUrl(unused),
};
const gatewaySource = stripTypeScriptTypes(await readFile(join(brain, 'subscriptionGateway.ts'), 'utf8'), { mode: 'transform' })
  .replace(/from ['"]([^'"]+)['"]/g, (original, path) => replacements[path] ? `from '${replacements[path]}'` : original);
const { GhostSubscriptionGateway } = await import(dataUrl(gatewaySource));

const manifest = JSON.parse(await readFile(join(plugin, 'ghost.json'), 'utf8'));
const worker = spawn(process.execPath, ['--experimental-permission', `--allow-fs-read=${plugin}`, join(plugin, manifest.node.entry)], {
  cwd: plugin,
  env: Object.fromEntries(Object.entries(process.env).filter(([key]) => !['NODE_PATH', 'NODE_OPTIONS'].includes(key) && !key.startsWith('POWERCONTEXT_'))),
  stdio: ['pipe', 'pipe', 'pipe'],
});
worker.stderr.resume();
const pending = new Map();
let sequence = 0;
createInterface({ input: worker.stdout }).on('line', line => {
  const reply = JSON.parse(line);
  pending.get(reply.id)?.(reply.result);
  pending.delete(reply.id);
});
const settings = { enabled: true, capture_prompts: true, server_url: input.url, auth_mode: 'bearer', max_bytes: 6000 };
let dispatch;
let hookSequence = 0;
const gateway = new GhostSubscriptionGateway({
  listGhosts: () => [{ manifest, enabled: true }],
  isRunning: () => true,
  wake: async () => {},
  now: Date.now,
  resolveMessageHookContext: () => ({}),
  newHookId: () => `delivery-${++hookSequence}`,
  sendToGhost: (_id, payload) => { void dispatch(payload); },
});
runInNewContext(await readFile(join(plugin, manifest.entry), 'utf8'), {
  setTimeout, clearTimeout,
  fetch: async () => ({ ok: true, json: async () => settings }),
  cindy: {
    onHostMessage: handler => { dispatch = handler; },
    send: async value => { gateway.handleVerdict(manifest.id, value); },
    node: { request: request => new Promise((resolve, reject) => {
      const id = ++sequence;
      const timer = setTimeout(() => { pending.delete(id); reject(new Error('worker timeout')); }, request.timeoutMs);
      pending.set(id, result => { clearTimeout(timer); resolve({ ok: true, result }); });
      worker.stdin.write(JSON.stringify({
        jsonrpc: '2.0', id, method: request.method, params: request.params,
        cindy: { secrets: { server_credential: JSON.stringify({ server_url: input.url, authorization: `Bearer ${input.token}` }) } },
      }) + '\n');
    }) },
  },
});
try {
  const first = await gateway.screenUserMessage({ sessionId: 'session-a', text: input.prompt });
  const second = await gateway.screenUserMessage({ sessionId: 'session-b', text: input.prompt });
  const missing = await gateway.screenUserMessage({ sessionId: 'unbound', text: input.prompt });
  const edited = await gateway.screenUserMessage({ sessionId: 'session-a', text: first.text ?? input.prompt });
  process.stdout.write(JSON.stringify({ first, second, missing, edited }));
} finally {
  const exited = once(worker, 'exit');
  worker.kill('SIGTERM');
  const timer = setTimeout(() => worker.kill('SIGKILL'), 3000);
  try { await exited; } finally { clearTimeout(timer); }
}
