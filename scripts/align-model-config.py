"""Align a registry model's config so its chunk_size matches its STFT hop.

`bs_roformer.utils.demix_track` chunks audio with `config.inference.chunk_size`
but the model only returns `floor(chunk_size / stft_hop_length) * stft_hop_length`
samples. When chunk_size is not a multiple of the hop (the MVSep mega 53-stem
config is 882000 vs hop 512) the final `result += x * window` raises:

    RuntimeError: The size of tensor a (881664) must match the size of tensor b
    (882000) at non-singleton dimension 2

This script downloads the model if needed, then writes a sibling config with
chunk_size rounded down to a hop multiple. Prints the new config path on
success; prints nothing usable (and exits 0) when no alignment is required.
"""
import argparse
import os
import sys

import yaml


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("model", help="registry slug or friendly name")
    ap.add_argument("--models-dir", default=None)
    args = ap.parse_args()

    try:
        from bs_roformer.download import ensure_model_assets
    except Exception as e:
        print("skip: bs_roformer not importable (%s)" % e, file=sys.stderr)
        return 0

    try:
        model_path, config_path = ensure_model_assets(args.model, models_dir=args.models_dir)
    except Exception as e:
        print("skip: could not resolve model assets (%s)" % e, file=sys.stderr)
        return 0

    if not config_path or not os.path.exists(config_path):
        print("skip: no config for this model", file=sys.stderr)
        return 0

    with open(config_path, encoding="utf-8") as f:
        text = f.read()

    try:
        cfg = yaml.unsafe_load(text)
    except Exception as e:
        print("skip: config unreadable (%s)" % e, file=sys.stderr)
        return 0

    inference = cfg.get("inference") or {}
    model_cfg = cfg.get("model") or {}
    chunk = inference.get("chunk_size")
    hop = model_cfg.get("stft_hop_length")
    if not chunk or not hop:
        return 0

    if isinstance(hop, (list, tuple)):
        hop = hop[-1]
    chunk, hop = int(chunk), int(hop)
    if chunk % hop == 0:
        return 0

    aligned = (chunk // hop) * hop
    old_line = "chunk_size: %d" % chunk
    if old_line not in text:
        # tolerate spacing variants
        import re
        m = re.search(r"chunk_size:\s*%d\b" % chunk, text)
        if not m:
            print("skip: chunk_size line not found", file=sys.stderr)
            return 0
        old_line = m.group(0)

    fixed_path = os.path.splitext(config_path)[0] + ".chunk-aligned.yaml"
    with open(fixed_path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text.replace(old_line, "chunk_size: %d" % aligned, 1))

    print(fixed_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
