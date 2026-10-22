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
import { combineSignals, createTimeoutSignal, type PowerContextClient } from './client.ts'
import type { ResolvedConfig } from './config.ts'
import { InvalidResponseError } from './errors.ts'

const CLOSING_BUDGET_MS = 1000

export interface FlushProgress {
  currentCursor: number
  remainingWork: boolean
}

export interface PendingFlushResult {
  acknowledged: boolean
  pendingScopes: number
}

function nonNegativeInteger(value: unknown): value is number {
  return typeof value === 'number' && Number.isInteger(value) && value >= 0
}

export function flushProgress(value: unknown): FlushProgress {
  if (!value || typeof value !== 'object') throw new InvalidResponseError('/v1/memory/flush')
  const result = value as Record<string, unknown>
  if (
    !['idle', 'processed'].includes(String(result.status))
    || !nonNegativeInteger(result.previous_cursor)
    || !nonNegativeInteger(result.current_cursor)
    || !nonNegativeInteger(result.high_watermark)
    || !nonNegativeInteger(result.processed_source_count)
    || (result.remaining_work !== undefined && typeof result.remaining_work !== 'boolean')
  ) {
    throw new InvalidResponseError('/v1/memory/flush')
  }
  return {
    currentCursor: result.current_cursor,
    remainingWork: result.remaining_work === true,
  }
}

function scopeCorrelation(scopeId: string): string {
  return createHash('sha256').update(scopeId).digest('hex').slice(0, 16)
}

export interface PendingSourceFlusher {
  record(scopeId: string, position: number): void
  flush(lifecycleEvent: string, signal?: AbortSignal): Promise<PendingFlushResult>
}

export type DiagnosticFailure = (
  event: string,
  error: unknown,
  extra?: Record<string, unknown>,
) => void

function settleWithinBoundary<T>(
  task: Promise<T>,
  signal: AbortSignal,
  onAbort: () => T,
): Promise<T> {
  if (signal.aborted) return Promise.resolve(onAbort())
  return new Promise<T>((resolve, reject) => {
    const abort = () => resolve(onAbort())
    signal.addEventListener('abort', abort, { once: true })
    task.then(
      (value) => {
        signal.removeEventListener('abort', abort)
        resolve(value)
      },
      (error) => {
        signal.removeEventListener('abort', abort)
        reject(error)
      },
    )
  })
}

export function createPendingSourceFlusher(
  client: PowerContextClient,
  config: ResolvedConfig,
  onFailure?: DiagnosticFailure,
  createBoundarySignal: () => AbortSignal = () => (
    createTimeoutSignal(Math.min(config.httpBudgetMs, CLOSING_BUDGET_MS))
  ),
): PendingSourceFlusher {
  const pending = new Map<string, number>()
  let queue = Promise.resolve<PendingFlushResult>({ acknowledged: true, pendingScopes: 0 })

  async function flushAll(lifecycleEvent: string, signal?: AbortSignal): Promise<PendingFlushResult> {
    if (pending.size === 0) return { acknowledged: true, pendingScopes: 0 }
    if (signal?.aborted) return { acknowledged: false, pendingScopes: pending.size }
    let acknowledged = true
    for (const [scopeId, position] of [...pending]) {
      let observedCursor: number | undefined
      let failure: unknown
      let scopeAcknowledged = false
      for (let attempt = 0; attempt < config.flushMaxCalls; attempt += 1) {
        try {
          const result = await client.request('flush_memory', { scope_id: scopeId }, signal)
          const progress = flushProgress(result.kind === 'json' ? result.value : undefined)
          observedCursor = progress.currentCursor
          scopeAcknowledged ||= progress.currentCursor >= position
          if (scopeAcknowledged && !progress.remainingWork) {
            break
          }
        } catch (error) {
          failure = error
          break
        }
      }
      if (scopeAcknowledged) {
        const current = pending.get(scopeId)
        if (current !== undefined && observedCursor !== undefined && observedCursor >= current) {
          pending.delete(scopeId)
        }
        continue
      }
      acknowledged = false
      failure ??= new InvalidResponseError('/v1/memory/flush')
      try {
        onFailure?.(lifecycleEvent === 'session_before_compact' ? 'pre_compaction_checkpoint' : 'flush_memory', failure, {
          host: 'pi',
          lifecycle_event: lifecycleEvent,
          scope_correlation: scopeCorrelation(scopeId),
          target_position: position,
          ...(observedCursor === undefined ? {} : { observed_cursor: observedCursor }),
        })
      } catch {
        // Diagnostics are best effort and must not affect the host boundary.
      }
    }
    return { acknowledged, pendingScopes: pending.size }
  }

  return {
    record(scopeId, position) {
      const current = pending.get(scopeId)
      pending.set(scopeId, Math.max(current ?? 0, position))
    },
    flush(lifecycleEvent, signal) {
      const boundarySignal = createBoundarySignal()
      const combined = signal ? combineSignals([boundarySignal, signal]) : boundarySignal
      const task = queue.then(() => flushAll(lifecycleEvent, combined))
      queue = task.catch(() => ({ acknowledged: false, pendingScopes: pending.size }))
      return settleWithinBoundary(task, boundarySignal, () => ({
        acknowledged: false,
        pendingScopes: pending.size,
      }))
    },
  }
}
