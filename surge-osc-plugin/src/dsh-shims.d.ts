/**
 * Compile-time shims for the DSH runtime packages.
 *
 * `@deepseek-ai/cordis` and `@deepseek-ai/dsh-tools` resolve from the DSH
 * installation at runtime (installed bundles import them from the Harness
 * itself and declare no dependencies); their type declarations cannot be
 * installed into this standalone package, so the small surface used by
 * src/index.ts is declared here. Keep in sync with the tool plugin contract:
 * `export const name`, `export const inject`, `export function apply(ctx)`,
 * `ctx.tools.register(defineTool({ name, description, parameters, output,
 * execute }))`.
 */
declare module '@deepseek-ai/cordis' {
  export interface Context {
    tools: {
      register(definition: unknown): unknown
    }
  }
}

declare module '@deepseek-ai/dsh-tools' {
  export interface ToolParamSpec {
    type: 'string' | 'number' | 'integer' | 'boolean'
    required?: boolean
    description?: string
  }

  export interface ToolOutputSpec<O> {
    schema: {
      type: 'object'
      additionalProperties?: boolean
      properties: Record<string, { type: string; required?: boolean; description?: string }>
    }
    render?: (args: never, value: O) => Array<{ type: 'text'; text: string }>
  }

  export interface ToolExecContext {
    signal: AbortSignal
    agent?: unknown
  }

  export interface ToolDefinition<A, O> {
    name: string
    description: string
    parameters: Record<string, ToolParamSpec>
    output: ToolOutputSpec<O>
    execute: (args: A, exec: ToolExecContext) => Promise<O>
  }

  export function defineTool<A, O>(definition: ToolDefinition<A, O>): unknown

  export class ToolArgsError extends Error {
    constructor(messages: string[])
  }

  export class ToolContextUnavailableError extends Error {}
}
