/**
 * electone-tone-studio 閳?DeepSeek Harness tool bundle for the tone pipeline.
 *
 * Two-plugin separation from `improvements.md` Stage 4 is preserved: the
 * existing `surge-osc-plugin` stays the lightweight OSC execution layer, and this
 * bundle is the high-level *studio*: it drives the deterministic Python backend
 * and returns structured results, so the AI decision layer never has to parse
 * console output.
 *
 * The seven tools, in workflow order:
 *   tone_inspect_song      read song_profile.json / stem diagnostics
 *   tone_plan_registration decide Upper / Lower / Pedal roles + engine
 *   tone_build_anchor_sfz  Golden-Anchor isolation -> sfizz per-key instrument
 *   tone_build_stem_synth  stem wavetable / String -> Vital or Surge preset
 *   tone_render_and_eval   objective scorecard + A/B audition WAVs
 *   tone_refine_patch      targeted adjustments from eval diagnostics
 *   tone_export_cantabile  generate + verify the .cantabileSong, enforce budget
 * @module @electone/tone-studio
 */
import type { Context } from '@deepseek-ai/cordis';
export declare const name = "tone-studio";
export declare const inject: string[];
export declare function apply(ctx: Context): void;
/** Directory the pipeline writes its per-song artifacts into. */
export declare function artifactsDir(song: string): string;
