/**
 * Offline self-test for the electone-tone-studio bundle.
 *
 * Verifies the parts that do not need DeepSeek Harness running:
 *   1. the bundle metadata is installable (package.json dsh.bundle + patch),
 *   2. the Python bridge locates the interpreter and the scripts,
 *   3. every tool registers with a valid name/parameters/output contract,
 *   4. the trailing-JSON parser handles real script output.
 *
 * Run with:  node test/selftest.mjs
 */
import { readFileSync, existsSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const here = path.dirname(fileURLToPath(import.meta.url))
const root = path.resolve(here, '..')
const repo = path.resolve(root, '..')

let failures = 0
const ok = (label) => console.log(`  ok   ${label}`)
const bad = (label, detail) => {
  failures++
  console.log(`  FAIL ${label}${detail ? `: ${detail}` : ''}`)
}
const check = (cond, label, detail) => (cond ? ok(label) : bad(label, detail))

console.log('electone-tone-studio selftest')

// 1. bundle metadata ---------------------------------------------------------
const pkg = JSON.parse(readFileSync(path.join(root, 'package.json'), 'utf8'))
check(pkg.name === '@electone/tone-studio', 'package name')
check(pkg.dsh?.bundle?.patch === './cordis.patch.yml', 'dsh bundle patch declared')
check(existsSync(path.join(root, 'cordis.patch.yml')), 'cordis.patch.yml exists')
const patch = readFileSync(path.join(root, 'cordis.patch.yml'), 'utf8')
check(patch.includes('@electone/tone-studio'), 'patch references the bundle')
check(existsSync(path.join(root, 'tsconfig.json')), 'tsconfig.json exists')
check(existsSync(path.join(root, 'src', 'index.ts')), 'src/index.ts exists')
check(existsSync(path.join(root, 'src', 'python-bridge.ts')), 'python-bridge exists')

// 2. the Python backend this bundle drives --------------------------------
const scripts = [
  'build_song_tone.py',
  'eval_tones.py',
  'disk_budget.py',
  'analyze_song.py',
  'build-cantabile.py',
  'piano_seed.py',
  'vital_synth.py',
  'surge_synth.py',
  'anchor_extract.py',
  'anchor_resynth.py',
]
for (const s of scripts)
  check(existsSync(path.join(repo, 'scripts', s)), `scripts/${s}`)

const venv = process.platform === 'win32'
  ? path.join(repo, '.venv', 'Scripts', 'python.exe')
  : path.join(repo, '.venv', 'bin', 'python')
check(existsSync(venv), 'venv interpreter present')

// 3. tool contract ---------------------------------------------------------
const index = readFileSync(path.join(root, 'src', 'index.ts'), 'utf8')
const expected = [
  'tone_inspect_song',
  'tone_plan_registration',
  'tone_build_anchor_sfz',
  'tone_build_stem_synth',
  'tone_render_and_eval',
  'tone_refine_patch',
  'tone_export_cantabile',
]
for (const tool of expected) {
  check(index.includes(`name: '${tool}'`), `tool ${tool} registered`)
  check(index.includes(tool) && index.includes('execute('),
    `tool ${tool} has execute`)
}
check((index.match(/ctx\.tools\.register\(defineTool\(/g) ?? []).length
  === expected.length, 'exactly 7 tools registered')
check(index.includes("export const inject = ['tools']"), 'injects tools service')
check(index.includes('ToolArgsError'), 'validates arguments')

// 4. trailing-JSON parser (duplicated from the bridge so this runs standalone)
function parseTrailingJson(text) {
  const trimmed = text.trim()
  if (!trimmed) return null
  try { return JSON.parse(trimmed) } catch { /* fall through */ }
  let depth = 0, start = -1, last = null, inString = false, escaped = false
  for (let i = 0; i < trimmed.length; i++) {
    const ch = trimmed[i]
    if (inString) {
      if (escaped) escaped = false
      else if (ch === '\\') escaped = true
      else if (ch === '"') inString = false
      continue
    }
    if (ch === '"') inString = true
    else if (ch === '{' || ch === '[') {
      if (depth === 0) start = i
      depth++
    } else if (ch === '}' || ch === ']') {
      depth--
      if (depth === 0 && start >= 0) {
        try { last = JSON.parse(trimmed.slice(start, i + 1)) } catch { /* ignore */ }
        start = -1
      }
    }
  }
  return last
}

const sample = 'analyzing...\n{"song":"韦礼安 - 女孩","keyboards":{"右手":1}}\ndone\n'
const parsed = parseTrailingJson(sample)
check(parsed?.song === '韦礼安 - 女孩', 'parser handles CJK + trailing text')
check(parseTrailingJson('no json here') === null, 'parser returns null without json')
check(parseTrailingJson('') === null, 'parser handles empty input')

console.log(failures === 0
  ? '\nselftest PASS'
  : `\nselftest FAIL (${failures} problem${failures === 1 ? '' : 's'})`)
process.exit(failures === 0 ? 0 : 1)
