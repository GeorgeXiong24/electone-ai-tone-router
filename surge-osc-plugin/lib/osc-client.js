/**
 * Minimal OSC 1.0 client over UDP — zero runtime dependencies.
 *
 * Built copy of src/osc-client.ts (hand type-stripped; `tsc` regenerates it).
 * See that file for the protocol notes, including how Surge XT answers queries
 * on its configured OSC output port. @module osc-client
 */
import { createSocket } from 'node:dgram'

function pad4(n) {
  return (n + 3) & ~3
}

function writePadded(text) {
  const raw = Buffer.from(text + '\0', 'utf8')
  const buf = Buffer.alloc(pad4(raw.length))
  raw.copy(buf)
  return buf
}

/** Encode one OSC message into a datagram payload. */
export function encodeOscMessage(address, args = []) {
  if (!address.startsWith('/')) throw new Error(`OSC address must start with '/': ${address}`)
  const headers = [writePadded(address)]
  const payloads = []
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
export function decodeOscMessage(buf) {
  let off = 0
  const readString = () => {
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
  const args = []
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

function closeQuietly(socket) {
  try {
    socket.removeAllListeners()
    socket.close()
  } catch {
    /* already closed */
  }
}

/** Fire-and-forget: send one OSC message over UDP. */
export function sendOsc(host, port, message) {
  return new Promise((resolve, reject) => {
    const socket = createSocket('udp4')
    const done = (err) => {
      closeQuietly(socket)
      if (err) reject(err)
      else resolve()
    }
    socket.once('error', done)
    socket.send(encodeOscMessage(message.address, message.args), port, host, (err) =>
      done(err ?? undefined))
  })
}

/** Normalize an address: guarantee the leading slash and strip a leading "/q". */
export function normalizeParamAddress(address) {
  let a = address.trim()
  if (!a.startsWith('/')) a = '/' + a
  if (a === '/q' || a.startsWith('/q/')) a = a.slice(2) || '/'
  return a
}

/** Normalize an address for the fuzzy fallback: lowercase, drop non-alphanumerics. */
function squash(address) {
  return address.toLowerCase().replace(/[^a-z0-9]/g, '')
}

/**
 * Query one parameter: send "/q" + address to Surge's OSC input and wait for
 * the value reply on opts.listenPort. Exact address matches win; otherwise the
 * first "/param/..." message carrying a float is accepted (flagged
 * matchedByAddress: false). "/doc..." messages are skipped, "/error" for this
 * address rejects, and the deadline rejects with actionable diagnostics.
 */
export function queryOsc(opts) {
  const timeoutMs = opts.timeoutMs ?? 500
  const want = normalizeParamAddress(opts.address)
  return new Promise((resolve, reject) => {
    const socket = createSocket('udp4')
    let settled = false
    const finish = (fn) => {
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
        "exists in Surge XT's OSC specification"))),
      timeoutMs)
    const onAbort = () => finish(() => reject(new Error('aborted')))
    opts.signal?.addEventListener('abort', onAbort, { once: true })

    socket.once('error', (err) => finish(() => reject(err)))
    socket.on('message', (buf) => {
      let msg
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
