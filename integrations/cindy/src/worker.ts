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
import { createHash } from 'node:crypto'
import { createInterface } from 'node:readline'
import { PowerContextClient, combineSignals, type FetchFn } from '../../dsh/plugins/powercontext/src/client.ts'
import { RequestNotSentError, ServerResponseError } from '../../dsh/plugins/powercontext/src/errors.ts'
import { validatePreparedContext } from '../../dsh/plugins/powercontext/src/prepared-context.ts'
import { containsSecret } from '../../dsh/plugins/powercontext/src/secrets.ts'
import { normalizeServerUrl } from '../../dsh/plugins/powercontext/src/transport.ts'

export const CONTEXT_START = '<!-- powercontext:cindy-context:v1 -->'
export const CONTEXT_END = '<!-- /powercontext:cindy-context:v1 -->'
export const HOOK_BUDGET_MS = 1800

type Config = {
  enabled?: boolean
  server_url?: string
  auth_mode?: 'none' | 'bearer'
  scope_id?: string
  allow_insecure_http?: boolean
  capture_prompts?: boolean
  max_bytes?: number
}
type HookInput = { config: Config; session_id: string; hook_id: string; text: string; deadline_at?: number }
type Outcome = {
  ok: boolean
  code?: string
  scope_id?: string
  prepare: string
  capture: string
  content?: string
  source_id?: string
  source_position?: number
}

function authorization(config: Config, raw?: string): { baseUrl: string; header?: string } {
  const baseUrl = normalizeServerUrl(config.server_url ?? 'http://127.0.0.1:8000', config.allow_insecure_http ?? false)
  if (config.auth_mode === undefined || config.auth_mode === 'none') return { baseUrl }
  if (config.auth_mode !== 'bearer' || !raw) throw new Error('credential_missing')
  const credential = JSON.parse(raw)
  if (!credential || typeof credential !== 'object'
    || normalizeServerUrl(credential.server_url, config.allow_insecure_http ?? false) !== baseUrl
    || typeof credential.authorization !== 'string' || !/^Bearer [^\s]+$/.test(credential.authorization)) {
    throw new Error('credential_mismatch')
  }
  return { baseUrl, header: credential.authorization }
}

function failureCode(error: unknown): string {
  if (error instanceof ServerResponseError) {
    if (error.statusCode === 401) return 'authentication_failed'
    if (error.statusCode === 403) return 'authorization_failed'
    if (error.code === 'scope_binding_target_missing') return 'scope_binding_target_missing'
    if (error.code === 'scope_not_found') return 'scope_not_found'
  }
  return 'operation_unavailable'
}

export async function runHook(input: HookInput, credential?: string, fetchImpl?: FetchFn): Promise<Outcome> {
  const outcome: Outcome = { ok: true, prepare: 'skipped', capture: 'skipped' }
  if (!input?.config || input.config.enabled !== true) return outcome
  const remaining = input.deadline_at === undefined ? HOOK_BUDGET_MS : Math.min(HOOK_BUDGET_MS, input.deadline_at - Date.now())
  if (!Number.isFinite(remaining) || remaining <= 0) {
    return { ...outcome, code: 'hook_deadline', capture: 'not_sent' }
  }
  if (typeof input.text !== 'string' || !input.text.trim()
    || typeof input.session_id !== 'string' || !input.session_id.trim() || input.session_id.length > 256
    || typeof input.hook_id !== 'string' || !input.hook_id.trim() || input.hook_id.length > 256) {
    return { ...outcome, ok: false, code: 'invalid_hook' }
  }
  // A rewritten bubble may be edited and submitted again. Do not recapture its historical evidence.
  if (input.text.includes(CONTEXT_START) || input.text.includes(CONTEXT_END)) {
    return { ...outcome, code: 'prepared_context_present' }
  }
  let connection: { baseUrl: string; header?: string }
  try { connection = authorization(input.config, credential) }
  catch { return { ...outcome, ok: false, code: 'invalid_connection' } }
  if (containsSecret(input.text) || (connection.header && input.text.includes(connection.header.slice(7)))
    || /\b(?:Bearer\s+\S+|gh[pousr]_\S+)/.test(input.text)) {
    return { ...outcome, code: 'sensitive_content' }
  }
  const maxBytes = input.config.max_bytes ?? 6000
  if (!Number.isInteger(maxBytes) || maxBytes < 512 || maxBytes > 8192
    || (input.config.capture_prompts !== undefined && typeof input.config.capture_prompts !== 'boolean')
    || (input.config.scope_id !== undefined && (typeof input.config.scope_id !== 'string' || !input.config.scope_id.trim()))) {
    return { ...outcome, ok: false, code: 'invalid_configuration' }
  }
  const client = new PowerContextClient({
    baseUrl: connection.baseUrl,
    authorization: connection.header,
    allowInsecureHttp: input.config.allow_insecure_http ?? false,
    requestTimeoutMs: HOOK_BUDGET_MS,
    userAgent: 'powercontext-cindy-hooks/0.1.0',
    fetch: fetchImpl,
  })
  const controller = new AbortController()
  const timer = setTimeout(() => controller.abort(), remaining)
  try {
    let scopeId: string
    try {
      const resolved = await client.request('resolve_scope_binding', {
        allow_default: false,
        ...(input.config.scope_id ? { explicit_scope_id: input.config.scope_id } : {}),
        binding_keys: [{ integration: 'cindy', kind: 'session', external_id: input.session_id }],
      }, controller.signal)
      const value = resolved.kind === 'json' ? resolved.value as { scope_id?: unknown } : undefined
      if (!value || typeof value.scope_id !== 'string' || !value.scope_id.trim()) throw new Error('invalid_scope')
      scopeId = value.scope_id
      outcome.scope_id = scopeId
    } catch (error) { return { ...outcome, ok: false, code: failureCode(error) } }

    if (input.text.length > 8192) outcome.prepare = 'query_too_long'
    else try {
      // Reserve part of the host's three-second window for independent original-prompt capture.
      const signal = combineSignals([controller.signal, AbortSignal.timeout(800)])
      const response = await client.request('prepare_context', { scope_id: scopeId, query: input.text, max_bytes: maxBytes }, signal)
      const prepared = validatePreparedContext(response.kind === 'json' ? response.value : undefined, undefined, maxBytes)
      outcome.prepare = prepared.status
      if (prepared.content) outcome.content = prepared.content
    } catch { outcome.prepare = 'unavailable' }

    if (input.config.capture_prompts === false) outcome.capture = 'disabled'
    else if (input.text.length > 200_000) outcome.capture = 'source_too_long'
    else {
      const identity = [scopeId, input.session_id, input.hook_id, input.text].join('\0')
      outcome.source_id = `cindy-user-prompt:${createHash('sha256').update(identity).digest('hex')}`
      try {
        const response = await client.request('capture_content_source', {
          scope_id: scopeId,
          source_id: outcome.source_id,
          content: input.text,
          metadata: {
            origin: 'cindy', event: 'user_prompt_submit', capture_stage: 'will-user-message-input',
            session_id: input.session_id, hook_id: input.hook_id,
          },
        }, controller.signal)
        const value = response.kind === 'json' ? response.value as { position?: unknown } : undefined
        if (!value || typeof value.position !== 'number' || !Number.isInteger(value.position) || value.position < 1) {
          throw new Error('capture_not_confirmed')
        }
        outcome.capture = 'accepted'
        outcome.source_position = value.position
      } catch (error) {
        outcome.capture = error instanceof RequestNotSentError ? 'not_sent'
          : error instanceof ServerResponseError && error.statusCode < 500 ? 'rejected' : 'unknown'
      }
    }
    return outcome
  } finally { clearTimeout(timer) }
}

if (require.main === module) {
  const lines = createInterface({ input: process.stdin, crlfDelay: Infinity })
  lines.on('line', async (line) => {
    let request: { jsonrpc?: string; id?: unknown; method?: string; params?: HookInput; cindy?: { secrets?: Record<string, string> } }
    try { request = JSON.parse(line) }
    catch { return }
    if (request.id === undefined) return
    let result: Outcome
    try {
      if (request.jsonrpc !== '2.0' || !['powercontext/hook', 'powercontext/authenticated-hook'].includes(request.method ?? '')) {
        throw new Error('invalid_request')
      }
      const input = request.params!
      if (input.config.auth_mode === 'bearer' && request.method !== 'powercontext/authenticated-hook') throw new Error('invalid_request')
      result = await runHook(input, request.cindy?.secrets?.server_credential)
    } catch { result = { ok: false, code: 'hook_unavailable', prepare: 'skipped', capture: 'skipped' } }
    process.stdout.write(`${JSON.stringify({ jsonrpc: '2.0', id: request.id, result })}\n`)
  })
}
