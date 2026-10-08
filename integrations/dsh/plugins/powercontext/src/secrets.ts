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

const SECRET_MARKERS = ['sk-', 'api_key', 'BEGIN PRIVATE']
const CONTENT_WRITES = new Set(['remember_memory', 'capture_content_source', 'revise_memory_entry'])

export function containsSecret(text: string): boolean {
  return SECRET_MARKERS.some((marker) => text.includes(marker))
}

export function hasSecretContent(operation: string, payload: unknown): boolean {
  if (!CONTENT_WRITES.has(operation) || !payload || typeof payload !== 'object') return false
  const content = payload as { text?: unknown; content?: unknown }
  return [content.text, content.content].some(value => typeof value === 'string' && containsSecret(value))
}
