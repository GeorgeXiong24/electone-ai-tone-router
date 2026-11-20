/**
 * Surge XT parameter control over OSC — DeepSeek Harness tool bundle.
 *
 * The execution layer of the "Surge XT timbre replication" workflow: the agent
 * (AI decision layer) asks surgext-mcp for parameter semantics, decides values,
 * and this plugin writes/reads them reliably through Surge XT's native OSC
 * interface. Nothing here renders or analyzes audio — that stays in the
 * electone-ai-tone-router pipeline.
 *
 * Addresses are forwarded verbatim; only the `/param/` prefix is enforced. The
 * authoritative parameter address list is Surge XT's built-in OSC
 * specification (Surge XT menu), e.g. `/param/filter_1/cutoff`.
 * @module @electone/surge-osc-tools
 */
import type { Context } from '@deepseek-ai/cordis'
import { defineTool, ToolArgsError } from '@deepseek-ai/dsh-tools'
import { normalizeParamAddress, queryOsc, sendOsc, type OscArg } from './osc-client.js'

/** Cordis plugin name. */
export const name = 'surge-osc-tools'
/** Services used by the tools. */
export const inject = ['tools']

const DEFAULT_HOST = '127.0.0.1'
const DEFAULT_PORT = 9000
const DEFAULT_OUTPUT_PORT = 9001
const DEFAULT_TIMEOUT_MS = 500

interface SetParamArgs {
  address: string
  value: number
  host?: string
  port?: number
}

interface SetParamResult {
  ok: boolean
  address: string
  value: number
  host: string
  port: number
}

interface GetParamArgs {
  address: string
  host?: string
  port?: number
  outputPort?: number
  timeoutMs?: number
}

interface GetParamResult {
  ok: boolean
  address: string
  value: number
  display: string
  error: string
}

function checkParamAddress(address: string): string {
  const normalized = normalizeParamAddress(address)
  if (!normalized.startsWith('/param/'))
    throw new ToolArgsError([
      `address must start with /param/ (got '${address}'); ` +
      "Surge XT's OSC specification lists the valid parameter addresses",
    ])
  return normalized
}

function checkNormalized(value: number | undefined): number {
  if (typeof value !== 'number' || !Number.isFinite(value) || value < 0 || value > 1)
    throw new ToolArgsError(['value must be a number between 0 and 1 (normalized)'])
  return value
}

/**
 * Register the surge_set_param / surge_get_param tools.
 * @param ctx - context providing the DSH tool registry.
 */
export function apply(ctx: Context): void {
  ctx.tools.register(defineTool<SetParamArgs, SetParamResult>({
    name: 'surge_set_param',
    description:
      'Set one Surge XT parameter to a normalized value (0-1) over OSC. ' +
      "Surge XT must have OSC enabled with the target input port. Values are the " +
      'normalized 0-1 form Surge\'s OSC layer accepts; convert physical targets ' +
      '(Hz, ms, dB) to normalized values before calling.',
    parameters: {
      address: {
        type: 'string',
        required: true,
        description:
          'Surge XT OSC parameter address, e.g. /param/filter_1/cutoff. ' +
          'Must start with /param/. The authoritative address list is the OSC ' +
          'specification inside Surge XT.',
      },
      value: {
        type: 'number',
        required: true,
        description: 'Normalized value between 0 and 1.',
      },
      host: {
        type: 'string',
        description: 'Host running Surge XT. Default 127.0.0.1.',
      },
      port: {
        type: 'integer',
        description: "Surge XT's OSC input port. Default 9000.",
      },
    },
    output: {
      schema: {
        type: 'object',
        additionalProperties: false,
        properties: {
          ok: { type: 'boolean', required: true, description: 'True when the datagram was sent.' },
          address: { type: 'string', required: true, description: 'Normalized parameter address.' },
          value: { type: 'number', required: true, description: 'Normalized value sent.' },
          host: { type: 'string', required: true },
          port: { type: 'integer', required: true },
        },
      },
      render: (_args, value) => [{
        type: 'text',
        text: `${value.address} <- ${value.value} (UDP ${value.host}:${value.port})`,
      }],
    },
    async execute(args): Promise<SetParamResult> {
      const address = checkParamAddress(args.address)
      const value = checkNormalized(args.value)
      const host = args.host || DEFAULT_HOST
      const port = args.port ?? DEFAULT_PORT
      const payload: OscArg[] = [{ type: 'f', value }]
      await sendOsc(host, port, { address, args: payload })
      return { ok: true, address, value, host, port }
    },
  }))

  ctx.tools.register(defineTool<GetParamArgs, GetParamResult>({
    name: 'surge_get_param',
    description:
      'Query one Surge XT parameter\'s current normalized value (0-1) over OSC. ' +
      'Surge replies on its configured OSC output IP/port rather than back to the ' +
      'sender, so Surge\'s OSC output must target this machine and outputPort, ' +
      'otherwise the query times out. The reply also carries Surge\'s own display ' +
      'string (e.g. "523.25 Hz").',
    parameters: {
      address: {
        type: 'string',
        required: true,
        description:
          'Surge XT OSC parameter address, e.g. /param/filter_1/cutoff. ' +
          'Must start with /param/.',
      },
      host: {
        type: 'string',
        description: 'Host running Surge XT. Default 127.0.0.1.',
      },
      port: {
        type: 'integer',
        description: "Surge XT's OSC input port (where the query is sent). Default 9000.",
      },
      outputPort: {
        type: 'integer',
        description:
          "UDP port to listen on for the reply; must equal Surge XT's OSC output " +
          'port. Default 9001.',
      },
      timeoutMs: {
        type: 'number',
        description: 'Reply deadline in milliseconds. Default 500.',
      },
    },
    output: {
      schema: {
        type: 'object',
        additionalProperties: false,
        properties: {
          ok: { type: 'boolean', required: true, description: 'True when a reply arrived.' },
          address: { type: 'string', required: true, description: 'Address the reply carried.' },
          value: { type: 'number', required: true, description: 'Normalized value 0-1 (0 when ok is false).' },
          display: { type: 'string', required: true, description: "Surge's display string, empty when unavailable." },
          error: { type: 'string', required: true, description: 'Empty on success; otherwise the failure and what to check.' },
        },
      },
      render: (_args, value) => [{
        type: 'text',
        text: value.ok
          ? `${value.address} = ${value.value}${value.display ? ` (${value.display})` : ''}`
          : `query failed for ${value.address}: ${value.error}`,
      }],
    },
    async execute(args, exec): Promise<GetParamResult> {
      const address = checkParamAddress(args.address)
      const host = args.host || DEFAULT_HOST
      const port = args.port ?? DEFAULT_PORT
      const outputPort = args.outputPort ?? DEFAULT_OUTPUT_PORT
      const timeoutMs = args.timeoutMs ?? DEFAULT_TIMEOUT_MS
      try {
        const reply = await queryOsc({
          host, port, listenPort: outputPort, address, timeoutMs,
          signal: exec?.signal,
        })
        const float = reply.message.args.find((a) => a.type === 'f')
        const str = reply.message.args.find((a) => a.type === 's')
        return {
          ok: true,
          address: reply.message.address,
          value: float && float.type === 'f' ? float.value : 0,
          display: str && str.type === 's' ? str.value : '',
          error: '',
        }
      } catch (err) {
        return {
          ok: false,
          address,
          value: 0,
          display: '',
          error: String(err instanceof Error ? err.message : err),
        }
      }
    },
  }))
}
