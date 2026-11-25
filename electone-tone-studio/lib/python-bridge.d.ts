/** Absolute path of the repository root (the plugin lives one level below it). */
export declare function repoRoot(): string;
export declare function workspaceDir(): string;
export declare function scriptPath(name: string): string;
export interface RunOptions {
    /** Script file name inside scripts/ (e.g. 'build_song_tone.py'). */
    script: string;
    args?: string[];
    /** Milliseconds before the child is killed. Default 30 min. */
    timeoutMs?: number;
    /** Abort signal from the tool execution context. */
    signal?: AbortSignal;
    /** Extra environment variables. */
    env?: Record<string, string>;
}
export interface RunResult {
    ok: boolean;
    code: number | null;
    /** Parsed JSON from stdout when the script prints one, else null. */
    json: unknown;
    stdout: string;
    stderr: string;
    /** Wall-clock duration in milliseconds. */
    ms: number;
    error: string;
}
/** Parse the last complete JSON object/array printed on stdout. */
export declare function parseTrailingJson(text: string): unknown;
/** Run one Python script and capture its structured result. */
export declare function runPython(opts: RunOptions): Promise<RunResult>;
/** Tail of a stream, for compact error messages. */
export declare function tail(text: string, lines?: number): string;
