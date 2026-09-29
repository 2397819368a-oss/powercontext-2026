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

import { describe, expect, it, vi } from 'vitest'
import { mcpConfig, mcpEndpoint, POWERCONTEXT_MCP_SERVER_NAME, registerMcp } from '../src/mcp.ts'

describe('PowerContext MCP bridge', () => {
  it('derives the streamable HTTP endpoint from the normalized Server URL', () => {
    expect(mcpEndpoint('https://powercontext.example/')).toBe('https://powercontext.example/mcp')
    expect(mcpEndpoint('https://powercontext.example')).toBe('https://powercontext.example/mcp')
  })

  it('passes the configured authorization to the native DSH MCP client', () => {
    expect(mcpConfig({
      baseUrl: 'https://powercontext.example',
      authorization: 'Bearer test-token',
    })).toEqual({
      transport: 'streamable-http',
      serverName: POWERCONTEXT_MCP_SERVER_NAME,
      url: 'https://powercontext.example/mcp',
      headers: { Authorization: 'Bearer test-token' },
      failOnStartupError: false,
      toolCallTimeoutMs: 60_000,
    })
    expect(mcpConfig({ baseUrl: 'https://powercontext.example', authorization: undefined }).headers).toEqual({})
  })

  it('delegates registration to @deepseek-ai/dsh-mcp-client', async () => {
    const apply = vi.fn(async () => undefined)
    const load = vi.fn(async <T>(_specifier: string) => ({ apply }) as T)
    const ctx = {} as never

    await registerMcp(ctx, { baseUrl: 'https://powercontext.example', authorization: undefined }, load)

    expect(load).toHaveBeenCalledWith('@deepseek-ai/dsh-mcp-client')
    expect(apply).toHaveBeenCalledWith(ctx, expect.objectContaining({
      serverName: POWERCONTEXT_MCP_SERVER_NAME,
      url: 'https://powercontext.example/mcp',
    }))
  })
})
