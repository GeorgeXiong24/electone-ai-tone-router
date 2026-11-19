"""Redirect every pipeline cache into the project workspace.

Import this module BEFORE any third-party library (torch, librosa/numba,
HuggingFace, ...) so their caches land under <workspace>/cache — inside the
project directory — instead of the user profile on C:. The PowerShell entry
points set the same variables; this module covers direct
`python scripts/<script>.py` invocations documented in the README.

Override points (all optional):
  ELECTONE_WORKSPACE  workspace root (default <project>/workspace)
  ELECTONE_CACHE      cache root (default <workspace>/cache)
Explicitly set variables are respected (setdefault semantics).
"""
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKSPACE = os.environ.get("ELECTONE_WORKSPACE") or os.path.join(ROOT, "workspace")
CACHE_ROOT = os.environ.get("ELECTONE_CACHE") or os.path.join(WORKSPACE, "cache")

os.environ.setdefault("ELECTONE_CACHE", CACHE_ROOT)
# XDG-style caches (yt-dlp, assorted CLI tools)
os.environ.setdefault("XDG_CACHE_HOME", CACHE_ROOT)
# model / JIT caches
os.environ.setdefault("TORCH_HOME", os.path.join(CACHE_ROOT, "torch"))
os.environ.setdefault("HF_HOME", os.path.join(CACHE_ROOT, "huggingface"))
os.environ.setdefault("NUMBA_CACHE_DIR", os.path.join(CACHE_ROOT, "numba"))
os.environ.setdefault("MPLCONFIGDIR", os.path.join(CACHE_ROOT, "matplotlib"))
os.environ.setdefault("PIP_CACHE_DIR", os.path.join(CACHE_ROOT, "pip"))

os.makedirs(CACHE_ROOT, exist_ok=True)
