import { defineTool, ToolArgsError } from '@deepseek-ai/dsh-tools';
import { runPython, tail, workspaceDir } from './python-bridge.js';
export const name = 'tone-studio';
export const inject = ['tools'];
const DEFAULT_SONG = '';
const EXPORT_TIMEOUT_MS = 30 * 60 * 1000;
function requireSong(song) {
    const s = (song ?? '').trim();
    if (!s)
        throw new ToolArgsError(['song is required, e.g. "闂婏妇銇滅€?- 婵傚啿顒?']);
    return s;
}
function failText(label, r) {
    const reason = r.error || tail(r.stderr) || 'no output';
    return `${label} failed: ${reason}`;
}
/* ------------------------------------------------------------------ tools */
export function apply(ctx) {
    // 1 -------------------------------------------------------------------
    ctx.tools.register(defineTool({
        name: 'tone_inspect_song',
        description: 'Read a song\'s MIR analysis (song_profile.json + stem analysis): per-stem '
            + 'polyphony, clean single-note counts, vocal contour (structural only 閳?'
            + 'vocals are never sampled), backing LTAS and the open spectral pockets '
            + 'where a right-hand lead blends cleanly. Start every session with this.',
        parameters: {
            song: {
                type: 'string',
                required: true,
                description: 'Song title as it appears under workspace/stems.',
            },
        },
        output: {
            schema: {
                type: 'object',
                properties: {
                    ok: { type: 'boolean', required: true, description: 'Analysis found.' },
                    song: { type: 'string', required: true, description: 'Song title.' },
                    profile: { type: 'object', required: true,
                        description: 'song_profile.json (stems, vocal contour, LTAS).' },
                    error: { type: 'string', required: true, description: 'Empty on success.' },
                },
            },
            render: (_args, v) => [{
                    type: 'text',
                    text: v.ok
                        ? `${v.song}: ${describeProfile(v.profile)}`
                        : `inspect failed: ${v.error}`,
                }],
        },
        async execute(args, exec) {
            const song = requireSong(args.song);
            const r = await runPython({
                script: 'analyze_song.py',
                args: ['--song', song],
                signal: exec?.signal,
                timeoutMs: EXPORT_TIMEOUT_MS,
            });
            if (!r.ok) {
                return { ok: false, song, profile: {}, error: failText('tone_inspect_song', r) };
            }
            const profile = r.json ?? {};
            return { ok: true, song, profile, error: '' };
        },
    }));
    // 2 -------------------------------------------------------------------
    ctx.tools.register(defineTool({
        name: 'tone_plan_registration',
        description: 'Lock the Upper / Lower / Pedal registration and the creation engine per '
            + 'keyboard. Applies the Stage-3 decision hierarchy (instrumental lead -> '
            + 'that instrument; Mandopop/acoustic/ballad -> Piano by default; sustained '
            + 'legato over sparse backing or electronic mix -> a crafted blending lead) '
            + 'and returns a rationale the user can override in one phrase.',
        parameters: {
            song: {
                type: 'string',
                required: true,
                description: 'Song title.',
            },
            force_upper: {
                type: 'string',
                description: 'Optional override: "piano" or "stem_anchor".',
            },
        },
        output: {
            schema: {
                type: 'object',
                properties: {
                    ok: { type: 'boolean', required: true, description: 'Plan produced.' },
                    plan: { type: 'object', required: true,
                        description: 'Per-keyboard voice/engine plus the rationale.' },
                    error: { type: 'string', required: true, description: 'Empty on success.' },
                },
            },
            render: (_args, v) => [{
                    type: 'text',
                    text: v.ok
                        ? `${v.plan.upper_decision?.voice}: ${v.plan.rationale}`
                        : `plan failed: ${v.error}`,
                }],
        },
        async execute(args, exec) {
            const song = requireSong(args.song);
            const extra = args.force_upper ? ['--force-upper', args.force_upper] : [];
            const r = await runPython({
                script: 'build_song_tone.py',
                args: ['--song', song, '--plan-only', ...extra],
                signal: exec?.signal,
                timeoutMs: EXPORT_TIMEOUT_MS,
            });
            if (!r.ok) {
                return { ok: false, plan: {}, error: failText('tone_plan_registration', r) };
            }
            return { ok: true, plan: r.json ?? {}, error: '' };
        },
    }));
    // 3 -------------------------------------------------------------------
    ctx.tools.register(defineTool({
        name: 'tone_build_anchor_sfz',
        description: 'Build a per-key sfizz instrument with Engine A: harmonic-comb isolation of '
            + 'Golden Anchors from a stem, formant-preserving resynthesis across the '
            + 'manual, and a 3-band Match EQ. Writes one 16-bit sample per key plus the '
            + '.sfz Cantabile loads. Choose which keyboard to build.',
        parameters: {
            song: { type: 'string', required: true, description: 'Song title.' },
            keyboard: {
                type: 'string',
                description: 'right | left | pedal (default: all three).',
            },
            n_anchors: {
                type: 'number',
                description: 'Golden Anchors to extract, 3-6 (default 4).',
            },
        },
        output: {
            schema: {
                type: 'object',
                properties: {
                    ok: { type: 'boolean', required: true, description: 'Instrument built.' },
                    instruments: { type: 'object', required: true,
                        description: 'Per-keyboard .sfz paths, key counts, sample MB.' },
                    error: { type: 'string', required: true, description: 'Empty on success.' },
                },
            },
            render: (_args, v) => [{
                    type: 'text',
                    text: v.ok
                        ? `instruments built: ${describeInstruments(v.instruments)}`
                        : `build failed: ${v.error}`,
                }],
        },
        async execute(args, exec) {
            const song = requireSong(args.song);
            const extra = [];
            if (args.keyboard)
                extra.push('--keyboard', args.keyboard);
            if (typeof args.n_anchors === 'number')
                extra.push('--n-anchors', String(args.n_anchors));
            const r = await runPython({
                script: 'build_song_tone.py',
                args: ['--song', song, '--sfz-only', ...extra],
                signal: exec?.signal,
                timeoutMs: EXPORT_TIMEOUT_MS,
            });
            if (!r.ok) {
                return { ok: false, instruments: {}, error: failText('tone_build_anchor_sfz', r) };
            }
            return { ok: true, instruments: r.json ?? {}, error: '' };
        },
    }));
    // 4 -------------------------------------------------------------------
    ctx.tools.register(defineTool({
        name: 'tone_build_stem_synth',
        description: 'Build a synthesised instrument with Engine B/C: a stem-extracted wavetable '
            + 'plus attack transient for Vital (.vital), or a Surge XT String / wavetable '
            + 'patch (.fxp). Used for pads, leads and electronic timbres where no clean '
            + 'sampled note exists.',
        parameters: {
            song: { type: 'string', required: true, description: 'Song title.' },
            stem: {
                type: 'string',
                required: true,
                description: 'Source stem name, e.g. guitar / other / piano.',
            },
            engine: {
                type: 'string',
                description: 'vital | surge_string | surge_wavetable (default vital).',
            },
        },
        output: {
            schema: {
                type: 'object',
                properties: {
                    ok: { type: 'boolean', required: true, description: 'Preset written.' },
                    preset: { type: 'string', required: true, description: 'Preset path.' },
                    detail: { type: 'object', required: true,
                        description: 'Detected f0, engine, wavetable path.' },
                    error: { type: 'string', required: true, description: 'Empty on success.' },
                },
            },
            render: (_args, v) => [{
                    type: 'text',
                    text: v.ok ? `preset written: ${v.preset}` : `synth failed: ${v.error}`,
                }],
        },
        async execute(args, exec) {
            const song = requireSong(args.song);
            const stem = (args.stem ?? '').trim();
            if (!stem)
                throw new ToolArgsError(['stem is required']);
            const r = await runPython({
                script: 'build_song_tone.py',
                args: ['--song', song, '--synth-stem', stem,
                    '--synth-engine', args.engine || 'vital'],
                signal: exec?.signal,
                timeoutMs: EXPORT_TIMEOUT_MS,
            });
            if (!r.ok) {
                return { ok: false, preset: '', detail: {},
                    error: failText('tone_build_stem_synth', r) };
            }
            const d = r.json ?? {};
            return { ok: true, preset: d.preset ?? '', detail: d, error: '' };
        },
    }));
    // 5 -------------------------------------------------------------------
    ctx.tools.register(defineTool({
        name: 'tone_render_and_eval',
        description: 'Run the objective scorecard (Stage 6): per-key pitch audit, key-to-key '
            + 'coherence, octave-band spectral match, polyphonic chord clarity, null '
            + 'test and the per-song disk budget. Returns pass/fail per check and the '
            + 'audition WAV paths so the user can listen.',
        parameters: {
            song: { type: 'string', required: true, description: 'Song title.' },
        },
        output: {
            schema: {
                type: 'object',
                properties: {
                    ok: { type: 'boolean', required: true, description: 'All gates passed.' },
                    report: { type: 'object', required: true,
                        description: 'Per-keyboard check results and disk budget.' },
                    error: { type: 'string', required: true, description: 'Empty on success.' },
                },
            },
            render: (_args, v) => [{
                    type: 'text',
                    text: `${v.ok ? 'PASS' : 'FAIL'}: ${describeReport(v.report)}`,
                }],
        },
        async execute(args, exec) {
            const song = requireSong(args.song);
            const r = await runPython({
                script: 'eval_tones.py',
                args: ['--song', song],
                signal: exec?.signal,
                timeoutMs: EXPORT_TIMEOUT_MS,
            });
            // eval_tones exits 1 when a gate fails; that is a result, not a crash.
            const report = r.json ?? {};
            if (!r.json) {
                return { ok: false, report: {}, error: failText('tone_render_and_eval', r) };
            }
            return { ok: Boolean(report.all_pass), report, error: '' };
        },
    }));
    // 6 -------------------------------------------------------------------
    ctx.tools.register(defineTool({
        name: 'tone_refine_patch',
        description: 'Apply targeted musical adjustments to a built instrument after the user '
            + 'auditions it (e.g. "make the right hand brighter", "softer left hand"). '
            + 'Rebuilds the affected keyboard with the requested adjustments and returns '
            + 'a diff log.',
        parameters: {
            song: { type: 'string', required: true, description: 'Song title.' },
            keyboard: {
                type: 'string',
                required: true,
                description: 'right | left | pedal.',
            },
            brightness_db: {
                type: 'number',
                description: 'Presence/air change in dB (negative = warmer).',
            },
            attack_ms: { type: 'number', description: 'New attack time in ms.' },
            release_ms: { type: 'number', description: 'New release time in ms.' },
            gain_db: { type: 'number', description: 'Output level change in dB.' },
        },
        output: {
            schema: {
                type: 'object',
                properties: {
                    ok: { type: 'boolean', required: true, description: 'Rebuild succeeded.' },
                    applied: { type: 'object', required: true, description: 'Applied deltas.' },
                    error: { type: 'string', required: true, description: 'Empty on success.' },
                },
            },
            render: (_args, v) => [{
                    type: 'text',
                    text: v.ok
                        ? `refined ${JSON.stringify(v.applied)}`
                        : `refine failed: ${v.error}`,
                }],
        },
        async execute(args, exec) {
            const song = requireSong(args.song);
            const keyboard = (args.keyboard ?? '').trim();
            if (!keyboard)
                throw new ToolArgsError(['keyboard is required']);
            const applied = { keyboard };
            const argv = ['--song', song, '--sfz-only', '--keyboard', keyboard];
            for (const k of ['brightness_db', 'attack_ms', 'release_ms', 'gain_db']) {
                const v = args[k];
                if (typeof v === 'number') {
                    argv.push(`--${k.replace(/_/g, '-')}`, String(v));
                    applied[k] = v;
                }
            }
            const r = await runPython({
                script: 'build_song_tone.py', args: argv,
                signal: exec?.signal, timeoutMs: EXPORT_TIMEOUT_MS,
            });
            if (!r.ok)
                return { ok: false, applied, error: failText('tone_refine_patch', r) };
            return { ok: true, applied, error: '' };
        },
    }));
    // 7 -------------------------------------------------------------------
    ctx.tools.register(defineTool({
        name: 'tone_export_cantabile',
        description: 'Generate and byte-verify the .cantabileSong for a song: serialize every '
            + 'keyboard\'s instrument into the state Cantabile expects, verify the '
            + 'round-trip (class id, path, checksum) and hard-assert the per-song disk '
            + 'budget stays under 1.2 GB.',
        parameters: {
            song: { type: 'string', required: true, description: 'Song title.' },
            open: {
                type: 'boolean',
                description: 'Open the generated song in Cantabile when available.',
            },
        },
        output: {
            schema: {
                type: 'object',
                properties: {
                    ok: { type: 'boolean', required: true, description: 'Export verified.' },
                    path: { type: 'string', required: true, description: '.cantabileSong path.' },
                    verified: { type: 'object', required: true,
                        description: 'Round-trip and budget verification results.' },
                    error: { type: 'string', required: true, description: 'Empty on success.' },
                },
            },
            render: (_args, v) => [{
                    type: 'text',
                    text: v.ok
                        ? `exported and verified: ${v.path}`
                        : `export failed: ${v.error}`,
                }],
        },
        async execute(args, exec) {
            const song = requireSong(args.song);
            const argv = ['--song', song, '--verify'];
            if (args.open)
                argv.push('--open');
            const r = await runPython({
                script: 'build-cantabile.py', args: argv,
                signal: exec?.signal, timeoutMs: EXPORT_TIMEOUT_MS,
            });
            const d = r.json ?? {};
            if (!r.ok) {
                return { ok: false, path: d.path ?? '', verified: d,
                    error: failText('tone_export_cantabile', r) };
            }
            return { ok: true, path: d.path ?? '', verified: d, error: '' };
        },
    }));
}
/* ------------------------------------------------------------------ render helpers */
function describeProfile(profile) {
    const stems = Object.keys(profile?.stems ?? {});
    const pockets = profile?.backing_ltas?.open_spectral_pockets_hz ?? [];
    const v = profile?.vocal_melody_profile;
    const bits = [`${stems.length} stems`];
    if (v?.mean_note_dur_s != null)
        bits.push(`vocal ${v.mean_note_dur_s}s notes`);
    if (pockets.length)
        bits.push(`open pockets ${pockets.join('/')} Hz`);
    return bits.join(', ');
}
function describeInstruments(instruments) {
    const kb = instruments?.keyboards ?? {};
    const parts = Object.entries(kb).map(([k, v]) => `${k} ${v.n_keys} keys/${v.samples_mb} MB`);
    return parts.length ? parts.join(', ') : 'nothing built';
}
function describeReport(report) {
    const kbs = report?.keyboards ?? {};
    const parts = Object.entries(kbs).map(([k, v]) => `${k} ${v.passed}/${v.total}`);
    const b = report?.disk_budget;
    if (b)
        parts.push(`disk ${b.total_mb}MB/${b.budget_mb}MB`);
    return parts.join(', ') || 'no results';
}
/** Directory the pipeline writes its per-song artifacts into. */
export function artifactsDir(song) {
    return `${workspaceDir()}/poc/${song}`;
}
