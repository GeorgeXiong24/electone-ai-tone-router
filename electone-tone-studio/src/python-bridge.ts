/**
 * Clean Python worker bridge for the electone tone pipeline.
 *
 * The plugin's whole job is to run the deterministic Python backend
 * (`scripts/build_song_tone.py`, `eval_tones.py`, `disk_budget.py`, …) and hand
 * the AI a structured JSON result instead of raw console text. Nothing here does
 * DSP: the audio work stays in Python, where it is testable on its own.
 * @module @electone/tone-studio
 */
import { spawn } from 'node:child_process'
import { existsSync } from 'node:fs'
import path from 'node:path'

/** Absolute path of the repository root (the plugin lives one level below it). */
export function repoRoot(): string {
  return path.resolve(__dirname, '..', '..')
}

export function workspaceDir(): string {
  return process.env.ELECTONE_WORKSPACE || path.join(repoRoot(), 'workspace')
}

function pythonExe(): string {
  const candidates = [
    path.join(repoRoot(), '.venv', 'Scripts', 'python.exe'),
    path.join(repoRoot(), '.venv', 'bin', 'python'),
  ]
  for (const c of candidates) if (existsSync(c)) return c
  return process.platform === 'win32' ? 'python' : 'python3'
}

export function scriptPath(name: string): string {
  return path.join(repoRoot(), 'scripts', name)
}

export interface RunOptions {
  /** Script file name inside scripts/ (e.g. 'build_song_tone.py'). */
  script: string
  args?: string[]
  /** Milliseconds before the child is killed. Default 30 min. */
  timeoutMs?: number
  /** Abort signal from the tool execution context. */
  signal?: AbortSignal
  /** Extra environment variables. */
  env?: Record<string, string>
}

export interface RunResult {
  ok: boolean
  code: number | null
  /** Parsed JSON from stdout when the script prints one, else null. */
  json: unknown
  stdout: string
  stderr: string
  /** Wall-clock duration in milliseconds. */
  ms: number
  error: string
}

/** Parse the last complete JSON object/array printed on stdout. */
export function parseTrailingJson(text: string): unknown {
  const trimmed = text.trim()
  if (!trimmed) return null
  const tryParse = (s: string): unknown => {
    try {
      return JSON.parse(s)
    } catch {
      return null
    }
  }
  const direct = tryParse(trimmed)
  if (direct !== null) return direct
  // Scan for balanced JSON blocks, keeping the last one that parses.
  let depth = 0
  let start = -1
  let last: unknown = null
  let inString = false
  let escaped = false
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
        const candidate = tryParse(trimmed.slice(start, i + 1))
        if (candidate !== null) last = candidate
        start = -1
      }
    }
  }
  return last
}

/** Run one Python script and capture its structured result. */
export async function runPython(opts: RunOptions): Promise<RunResult> {
  const script = scriptPath(opts.script)
  const started = Date.now()
  if (!existsSync(script)) {
    return {
      ok: false, code: null, json: null, stdout: '', stderr: '', ms: 0,
      error: `missing script: ${script}`,
    }
  }
  return await new Promise<RunResult>((resolve) => {
    const child = spawn(pythonExe(), [script, ...(opts.args ?? [])], {
      cwd: repoRoot(),
      env: {
        ...process.env,
        PYTHONIOENCODING: 'utf-8',
        PYTHONUTF8: '1',
        ...(opts.env ?? {}),
      },
      stdio: ['ignore', 'pipe', 'pipe'],
    })
    let stdout = ''
    let stderr = ''
    let settled = false
    const timeoutMs = opts.timeoutMs ?? 30 * 60 * 1000

    const finish = (code: number | null, error?: string) => {
      if (settled) return
      settled = true
      clearTimeout(timer)
      resolve({
        ok: (code === 0) && !error,
        code,
        json: parseTrailingJson(stdout),
        stdout,
        stderr,
        ms: Date.now() - started,
        error: error ?? '',
      })
    }

    const timer = setTimeout(() => {
      child.kill('SIGKILL')
      finish(null, `timed out after ${Math.round(timeoutMs / 1000)}s`)
    }, timeoutMs)

    opts.signal?.addEventListener('abort', () => {
      child.kill('SIGKILL')
      finish(null, 'aborted by the agent')
    })

    child.stdout?.on('data', (d: Buffer) => { stdout += d.toString('utf8') })
    child.stderr?.on('data', (d: Buffer) => { stderr += d.toString('utf8') })
    child.on('error', (err) => finish(null, String(err.message ?? err)))
    child.on('close', (code) => finish(code))
  })
}

/** Tail of a stream, for compact error messages. */
export function tail(text: string, lines = 12): string {
  const parts = text.trimEnd().split(/\r?\n/)
  return parts.slice(-lines).join('\n')
}
