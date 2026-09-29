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

import type { Context } from '@deepseek-ai/cordis'
import type { ResolvedConfig } from './config.ts'
import { loadPeer } from './peers.ts'

export const POWERCONTEXT_MCP_SERVER_NAME = 'powercontext'

export interface DshMcpConfig {
  transport: 'streamable-http'
  serverName: string
  url: string
  headers: Record<string, string>
  failOnStartupError: boolean
  toolCallTimeoutMs: number
}

type PeerLoader = <T>(specifier: string) => Promise<T>
type DshMcpClient = { apply: (ctx: Context, config: DshMcpConfig) => Promise<void> }

export function mcpEndpoint(baseUrl: string): string {
  return `${baseUrl.replace(/\/+$/, '')}/mcp`
}

export function mcpConfig(config: Pick<ResolvedConfig, 'baseUrl' | 'authorization'>): DshMcpConfig {
  return {
    transport: 'streamable-http',
    serverName: POWERCONTEXT_MCP_SERVER_NAME,
    url: mcpEndpoint(config.baseUrl),
    headers: config.authorization ? { Authorization: config.authorization } : {},
    // DSH should keep the host usable while the Server is starting or unavailable.
    failOnStartupError: false,
    toolCallTimeoutMs: 60_000,
  }
}

export async function registerMcp(
  ctx: Context,
  config: Pick<ResolvedConfig, 'baseUrl' | 'authorization'>,
  load: PeerLoader = loadPeer,
): Promise<void> {
  const client = await load<DshMcpClient>('@deepseek-ai/dsh-mcp-client')
  await client.apply(ctx, mcpConfig(config))
}
