/**
 * Self-test for lib/osc-client.js — no DSH required.
 *
 * Covers: OSC encode/decode round-trip (float/int/string, padding edges),
 * address normalization, a full query round-trip against a fake Surge
 * responder over UDP loopback (including /doc noise skipping), and the
 * timeout diagnostic. Run:  node test/selftest.mjs
 */
import assert from 'node:assert/strict'
import { createSocket } from 'node:dgram'
import {
  decodeOscMessage,
  encodeOscMessage,
  normalizeParamAddress,
  queryOsc,
  sendOsc,
} from '../lib/osc-client.js'

// ---- 1. encode / decode round-trip -----------------------------------------
const msg = {
  address: '/param/filter_1/cutoff',
  args: [{ type: 'f', value: 0.25 }, { type: 's', value: '523.25 Hz' }],
}
const decoded = decodeOscMessage(encodeOscMessage(msg.address, msg.args))
assert.equal(decoded.address, msg.address)
assert.equal(decoded.args[0].type, 'f')
assert.ok(Math.abs(decoded.args[0].value - 0.25) < 1e-6, 'float round-trip')
assert.equal(decoded.args[1].type, 's')
assert.equal(decoded.args[1].value, '523.25 Hz')

const msg2 = {
  address: '/x',
  args: [{ type: 'i', value: -7 }, { type: 's', value: 'abc' }],
}
const d2 = decodeOscMessage(encodeOscMessage(msg2.address, msg2.args))
assert.equal(d2.args[0].value, -7, 'int round-trip')
assert.equal(d2.args[1].value, 'abc', 'short string padding')

assert.throws(() => encodeOscMessage('param/no-slash', []), /must start with/)

// ---- 2. address normalization ----------------------------------------------
assert.equal(normalizeParamAddress('param/a'), '/param/a')
assert.equal(normalizeParamAddress('/q/param/a'), '/param/a')
assert.equal(normalizeParamAddress('/param/a'), '/param/a')

// ---- 3. query round-trip against a fake Surge ------------------------------
const FAKE_IN = 19000
const FAKE_OUT = 19001
const fakeSurge = createSocket('udp4')
fakeSurge.on('message', (buf) => {
  const q = decodeOscMessage(buf)
  assert.equal(q.address, '/q/param/filter_1/cutoff', 'query address')
  // /doc noise arrives first in real life; the client must skip it
  fakeSurge.send(
    encodeOscMessage('/doc/param/filter_1/cutoff', [
      { type: 's', value: 'Cutoff' }, { type: 's', value: 'float' },
    ]),
    FAKE_OUT, '127.0.0.1')
  // then the value reply, exactly as Surge's sendParameter writes it
  fakeSurge.send(
    encodeOscMessage('/param/filter_1/cutoff', [
      { type: 'f', value: 0.42 }, { type: 's', value: '523.25 Hz' },
    ]),
    FAKE_OUT, '127.0.0.1')
})
await new Promise((resolve) => fakeSurge.bind(FAKE_IN, resolve))

const reply = await queryOsc({
  host: '127.0.0.1', port: FAKE_IN, listenPort: FAKE_OUT,
  address: '/param/filter_1/cutoff', timeoutMs: 2000,
})
assert.equal(reply.matchedByAddress, true, 'exact address match')
assert.ok(Math.abs(reply.message.args[0].value - 0.42) < 1e-6, 'reply value')
assert.equal(reply.message.args[1].value, '523.25 Hz', 'reply display string')

// ---- 4. sendOsc smoke test -------------------------------------------------
let received = null
const sink = createSocket('udp4')
sink.on('message', (buf) => { received = decodeOscMessage(buf) })
await new Promise((resolve) => sink.bind(19002, resolve))
await sendOsc('127.0.0.1', 19002, {
  address: '/param/a/env1/attack',
  args: [{ type: 'f', value: 0.5 }],
})
await new Promise((r) => setTimeout(r, 100))
assert.ok(received, 'sendOsc delivered')
assert.equal(received.address, '/param/a/env1/attack')
sink.close()
fakeSurge.close()

// ---- 5. timeout diagnostic -------------------------------------------------
let timedOut = false
try {
  await queryOsc({
    host: '127.0.0.1', port: 19099, listenPort: 19098,
    address: '/param/x', timeoutMs: 300,
  })
} catch (err) {
  timedOut = true
  assert.match(String(err.message), /OSC output IP\/port/, 'timeout hint')
  assert.match(String(err.message), /19098/, 'timeout names the listen port')
}
assert.ok(timedOut, 'query must time out without a responder')

console.log('selftest OK')
