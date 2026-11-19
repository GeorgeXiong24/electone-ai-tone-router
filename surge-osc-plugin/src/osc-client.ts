/**
 * Minimal OSC 1.0 client over UDP — zero runtime dependencies.
 *
 * Encoding follows the OSC 1.0 spec: a NUL-terminated, 4-byte-padded address
 * string, a padded type-tag string ("," + one tag per argument), then
 * big-endian payloads (float32 "f", int32 "i", padded string "s"). Only what
 * Surge XT's parameter interface needs is implemented: one message per
 * datagram.
 *
 * Query flow (verified against Surge XT's src/surge-xt/osc/OpenSoundControl.cpp):
 *   - a query "/q/param/..." is sent to Surge's OSC **input** port;
 *   - Surge answers on its configured OSC **output** port/IP (not back to the
 *     sender's port) with, first, the value message:
 *     "/param/..." + normalized float32 + display string (e.g. "523.25 Hz"),
 *     followed by extension and "/doc/param/..." info messages;
 *   - an unknown address is answered with "/error" + one string.
 * @module osc-client
 */
import { createSocket, type Socket } from 'node:dgram'

/** Supported OSC argument kinds. */
export type OscArg =
  | { type: 'f'; value: number }
  | { type: 'i'; value: number }
  | { type: 's'; value: string }

/** One decoded OSC message. Bundles are not produced (Surge replies are plain messages). */
export interface OscMessage {
  address: string
  args: OscArg[]
}

function pad4(n: number): number {
  return (n + 3) & ~3
}

function writePadded(text: string): Buffer {
  const raw = Buffer.from(text + '\0', 'utf8')
  const buf = Buffer.alloc(pad4(raw.length))
  raw.copy(buf)
  return buf
}

/** Encode one OSC message into a datagram payload. */
export function encodeOscMessage(address: string, args: OscArg[] = []): Buffer {
  if (!address.startsWith('/')) throw new Error(`OSC address must start with '/': ${address}`)
  const headers: Buffer[] = [writePadded(address)]
  const payloads: Buffer[] = []
  let tags = ','
  for (const arg of args) {
    if (arg.type === 'f' || arg.type === 'i') {
      tags += arg.type
      const b = Buffer.alloc(4)
      if (arg.type === 'f') b.writeFloatBE(arg.value, 0)
      else b.writeInt32BE(arg.value, 0)
      payloads.push(b)
    } else {
      tags += 's'
      payloads.push(writePadded(arg.value))
    }
  }
  headers.push(writePadded(tags))
  return Buffer.concat([...headers, ...payloads])
}

/** Decode one OSC message datagram. */
export function decodeOscMessage(buf: Buffer): OscMessage {
  let off = 0
  const readString = (): string => {
    const end = buf.indexOf(0, off)
    if (end < 0) throw new Error('OSC: unterminated string')
    const s = buf.toString('utf8', off, end)
    off = off + pad4(end - off + 1)
    return s
  }
  const address = readString()
  const tags = readString()
  if (!address.startsWith('/')) throw new Error(`OSC: bad address '${address}'`)
  if (!tags.startsWith(',')) throw new Error(`OSC: bad type-tag string '${tags}'`)
  const args: OscArg[] = []
  for (const tag of tags.slice(1)) {
    if (tag === 'f') {
      args.push({ type: 'f', value: buf.readFloatBE(off) })
      off += 4
    } else if (tag === 'i') {
      args.push({ type: 'i', value: buf.readInt32BE(off) })
      off += 4
    } else if (tag === 's') {
      args.push({ type: 's', value: readString() })
    } else {
      throw new Error(`OSC: unsupported type tag '${tag}'`)
    }
  }
  return { address, args }
}

function closeQuietly(socket: Socket): void {
  try {
    socket.removeAllListeners()
    socket.close()
  } catch {
    /* already closed */
  }
}

/** Fire-and-forget: send one OSC message over UDP. */
export function sendOsc(host: string, port: number, message: OscMessage): Promise<void> {
  return new Promise((resolve, reject) => {
    const socket = createSocket('udp4')
    const done = (err?: Error): void => {
      closeQuietly(socket)
      if (err) reject(err)
      else resolve()
    }
    socket.once('error', done)
    socket.send(encodeOscMessage(message.address, message.args), port, host, (err) =>
      done(err ?? undefined))
  })
}

/** Options for {@link queryOsc}. */
export interface QueryOptions {
  /** Host running Surge XT. */
  host: string
  /** Surge XT's OSC input port (where the /q query is sent). */
  port: number
  /**
   * Local UDP port to wait on for the reply. Must equal Surge XT's configured
   * OSC output port, and Surge's OSC output IP must point at this machine.
   */
  listenPort: number
  /** Parameter address to query, e.g. "/param/filter_1/cutoff". */
  address: string
  /** Reply deadline in milliseconds (default 500). */
  timeoutMs?: number
  /** Cooperative cancellation. */
  signal?: AbortSignal
}

/** Query outcome. */
export interface QueryResult {
  /** The matching reply message (normalized float first, display string second). */
  message: OscMessage
  /** True when the reply address equals the queried address exactly. */
  matchedByAddress: boolean
}

/** Normalize an address for the fuzzy fallback: lowercase, drop non-alphanumerics. */
function squash(address: string): string {
  return address.toLowerCase().replace(/[^a-z0-9]/g, '')
}

/**
 * Query one parameter: send "/q" + address to Surge's OSC input and wait for
 * the value reply on `listenPort`.
 *
 * Matching: an exact address match wins; otherwise the first "/param/..."
 * message carrying a float is accepted (flagged `matchedByAddress: false`)
 * because Surge may answer under its canonical address spelling. "/doc..." and
 * unrelated messages are skipped, "/error" for this address rejects.
 */
export function queryOsc(opts: QueryOptions): Promise<QueryResult> {
  const timeoutMs = opts.timeoutMs ?? 500
  const want = normalizeParamAddress(opts.address)
  return new Promise((resolve, reject) => {
    const socket = createSocket('udp4')
    let settled = false
    const finish = (fn: () => void): void => {
      if (settled) return
      settled = true
      clearTimeout(timer)
      opts.signal?.removeEventListener('abort', onAbort)
      closeQuietly(socket)
      fn()
    }
    const timer = setTimeout(
      () => finish(() => reject(new Error(
        `no reply from Surge XT within ${timeoutMs}ms on UDP port ${opts.listenPort}; ` +
        'check that OSC is enabled in Surge XT, that its OSC output IP/port ' +
        `target this machine and port ${opts.listenPort}, and that ${want} ` +
        'exists in Surge XT\'s OSC specification'))),
      timeoutMs)
    const onAbort = (): void => finish(() => reject(new Error('aborted')))
    opts.signal?.addEventListener('abort', onAbort, { once: true })

    socket.once('error', (err) => finish(() => reject(err)))
    socket.on('message', (buf) => {
      let msg: OscMessage
      try {
        msg = decodeOscMessage(buf)
      } catch {
        return
      }
      if (msg.address === '/error') {
        const text = msg.args.map((a) => String(a.value)).join(' ')
        if (squash(text).includes(squash(want)))
          finish(() => reject(new Error(`Surge XT error: ${text}`)))
        return
      }
      if (msg.address.startsWith('/doc')) return
      const hasFloat = msg.args.some((a) => a.type === 'f')
      const exact = msg.address === want
      const fuzzy = hasFloat && msg.address.startsWith('/param/') && squash(msg.address) === squash(want)
      const fallback = hasFloat && msg.address.startsWith('/param/')
      if (exact || fuzzy || fallback) {
        finish(() => resolve({ message: msg, matchedByAddress: exact || fuzzy }))
      }
    })

    socket.bind(opts.listenPort, () => {
      const queryAddress = want.startsWith('/q') ? want : '/q' + want
      socket.send(encodeOscMessage(queryAddress, []), opts.port, opts.host, (err) => {
        if (err) finish(() => reject(err))
      })
    })
  })
}

/** Strip a leading "/q" and guarantee the leading slash of a parameter address. */
export function normalizeParamAddress(address: string): string {
  let a = address.trim()
  if (!a.startsWith('/')) a = '/' + a
  if (a === '/q' || a.startsWith('/q/')) a = a.slice(2) || '/'
  return a
}
