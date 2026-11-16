"""Visible progress reporting for the separation pipeline.

Why this exists: the underlying tools (bs-roformer, audio-separator) either
print nothing for minutes or emit tqdm bars that are lost when stdout is not a
terminal. A run that is simply slow then looks identical to a run that crashed.

What it provides:

  stage(i, n, text)          a numbered stage header, e.g. [2/3] Separating stems
  Heartbeat(label)           context manager printing elapsed time while a long
                             in-process call runs
  run(cmd, ...)              runs a child process, streams its output, and shows
                             elapsed time + % parsed from the child's own output

Progress percentages are parsed from what the tools already emit:

  bs-roformer      "Estimated time remaining: 12.5 seconds"  (with a total)
  audio-separator  tqdm bars such as " 40%|####  | 66/165 [00:49<01:07]"

Usage from the shell:

  python progress.py run --stage 2 --of 3 --label "Separating stems" -- <cmd...>
"""
import argparse
import os
import queue
import re
import subprocess
import sys
import threading
import time

# Progress sources, in priority order.
RE_BSROF_TOTAL = re.compile(r"Estimated total processing time for this track:\s*([\d.]+)")
RE_BSROF_REMAIN = re.compile(r"Estimated time remaining:\s*([\d.]+)")
RE_TQDM_PCT = re.compile(r"\s*(\d{1,3})%\|")
RE_TQDM_FRAC = re.compile(r"\|\s*(\d+)/(\d+)\s*\[")

HEARTBEAT_SEC = 5.0
# Minimum gap between status lines when stdout is not a terminal.
STATUS_SEC = 3.0
_STATUS_LAST = {}


def _fmt_dur(seconds):
    seconds = int(max(0, seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return "%d:%02d:%02d" % (h, m, s)
    return "%02d:%02d" % (m, s)


def _is_tty():
    try:
        return sys.stdout.isatty()
    except Exception:
        return False


def _safe_print(text):
    """Print without dying on a console codepage that cannot encode the text.

    yt-dlp and the separator emit CJK titles; on a GBK console a plain print()
    raises UnicodeEncodeError and would abort the pipeline.
    """
    try:
        print(text, flush=True)
    except UnicodeEncodeError:
        enc = getattr(sys.stdout, "encoding", None) or "utf-8"
        print(text.encode(enc, "replace").decode(enc, "replace"), flush=True)


def stage(index, total, text):
    """Print a numbered stage header."""
    print("")
    print("=" * 64)
    _safe_print("[%d/%d] %s" % (index, total, text))
    print("=" * 64, flush=True)


def _status_line(label, elapsed, pct=None, note="", force=False):
    """Single-line status that overwrites itself on a terminal.

    Outside a terminal every redraw would become a new log line, so print at
    most once per STATUS_SEC there (and always on a milestone percentage).
    """
    parts = ["  %s" % label, "elapsed %s" % _fmt_dur(elapsed)]
    if pct is not None:
        pct = max(0, min(100, pct))
        width = 24
        filled = int(width * pct / 100.0)
        bar = "#" * filled + "-" * (width - filled)
        parts.append("[%s] %3d%%" % (bar, int(pct)))
    if note:
        parts.append(note)
    line = " | ".join(parts)

    if _is_tty():
        sys.stdout.write("\r" + line.ljust(78))
        sys.stdout.flush()
        return line

    if not force:
        now = time.time()
        last_t = _STATUS_LAST.get("t", 0.0)
        last_pct = _STATUS_LAST.get("pct")
        # Outside a terminal, only log on a >=10% jump or every STATUS_SEC.
        small_step = (last_pct is not None and pct is not None
                      and abs(pct - last_pct) < 10.0)
        if now - last_t < STATUS_SEC and small_step:
            return line
    _STATUS_LAST["t"] = time.time()
    _STATUS_LAST["pct"] = pct
    _safe_print(line)
    return line


def _end_status():
    if _is_tty():
        sys.stdout.write("\n")
        sys.stdout.flush()


class ProgressReader:
    """Tracks percentage from a child process's own output."""

    def __init__(self):
        self.total_est = None
        self.pct = None

    def feed(self, line):
        m = RE_TQDM_PCT.match(line)
        if m:
            self.pct = float(m.group(1))
            return
        m = RE_TQDM_FRAC.search(line)
        if m:
            done, tot = int(m.group(1)), int(m.group(2))
            if tot > 0:
                self.pct = 100.0 * done / tot
            return
        m = RE_BSROF_TOTAL.search(line)
        if m:
            self.total_est = float(m.group(1))
            return
        m = RE_BSROF_REMAIN.search(line)
        if m and self.total_est and self.total_est > 0:
            remain = float(m.group(1))
            self.pct = 100.0 * (1.0 - remain / self.total_est)


def run(cmd, label, index=None, total=None, quiet_patterns=None, echo=True,
        collapse_tqdm=True):
    """Run *cmd*, streaming output with a live elapsed-time / % status line."""
    if index is not None and total is not None:
        stage(index, total, label)
    else:
        print("")
        print("--- %s" % label, flush=True)

    env = dict(os.environ)
    env.setdefault("PYTHONIOENCODING", "utf-8")
    env.setdefault("PYTHONUTF8", "1")

    started = time.time()
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        bufsize=0,
        env=env,
    )

    q = queue.Queue()

    def reader():
        buf = b""
        while True:
            chunk = proc.stdout.read(256)
            if not chunk:
                break
            buf += chunk
            # Treat both \n and lone \r as line breaks so tqdm redraws are seen.
            while True:
                idx_n = buf.find(b"\n")
                idx_r = buf.find(b"\r")
                cands = [i for i in (idx_n, idx_r) if i >= 0]
                if not cands:
                    break
                cut = min(cands)
                line, buf = buf[:cut], buf[cut + 1:]
                q.put(line.decode("utf-8", "replace"))
        if buf:
            q.put(buf.decode("utf-8", "replace"))
        q.put(None)

    threading.Thread(target=reader, daemon=True).start()

    reader_progress = ProgressReader()
    last_beat = time.time()
    printed_any = False
    last_status_len = 0

    while True:
        try:
            line = q.get(timeout=0.4)
        except queue.Empty:
            if time.time() - last_beat >= HEARTBEAT_SEC:
                _status_line(label, time.time() - started, reader_progress.pct,
                             note="working...")
                last_beat = time.time()
            continue

        if line is None:
            break

        stripped = line.strip()
        if not stripped:
            continue

        reader_progress.feed(stripped)

        skip = False
        if quiet_patterns and any(p in stripped for p in quiet_patterns):
            skip = True
        if collapse_tqdm and (RE_TQDM_PCT.match(stripped) or RE_TQDM_FRAC.search(stripped)):
            # Replace the live status line instead of spamming the log.
            if _is_tty():
                sys.stdout.write("\r" + " " * last_status_len + "\r")
            last_status_len = len(_status_line(label, time.time() - started,
                                               reader_progress.pct))
            last_beat = time.time()
            skip = True
        if RE_BSROF_REMAIN.search(stripped):
            if _is_tty():
                sys.stdout.write("\r" + " " * last_status_len + "\r")
            last_status_len = len(_status_line(label, time.time() - started,
                                               reader_progress.pct))
            last_beat = time.time()
            skip = True

        if not skip and echo:
            if _is_tty() and last_status_len:
                sys.stdout.write("\r" + " " * last_status_len + "\r")
                last_status_len = 0
            _safe_print("  " + stripped[:150])
            printed_any = True

    code = proc.wait()
    elapsed = time.time() - started
    _end_status()

    if code == 0:
        _safe_print("  done: %s in %s" % (label, _fmt_dur(elapsed)))
    else:
        _safe_print("  FAILED: %s (exit %d) after %s"
                    % (label, code, _fmt_dur(elapsed)))
    return code


class Heartbeat:
    """Print elapsed time while a blocking in-process call runs.

    Used where the work happens inside this Python process rather than in a
    child we can stream (e.g. audio-separator's model download and inference).
    """

    def __init__(self, label, interval=HEARTBEAT_SEC):
        self.label = label
        self.interval = interval
        self.started = None
        self._stop = threading.Event()
        self._thread = None

    def __enter__(self):
        self.started = time.time()
        if _is_tty():
            _status_line(self.label, 0)

        def beat():
            while not self._stop.wait(self.interval):
                _status_line(self.label, time.time() - self.started, note="working...")

        self._thread = threading.Thread(target=beat, daemon=True)
        self._thread.start()
        return self

    def note(self, pct):
        _status_line(self.label, time.time() - self.started, pct)

    def __exit__(self, exc_type, exc, tb):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=1.0)
        _end_status()
        elapsed = time.time() - self.started
        if exc_type is None:
            print("  done: %s in %s" % (self.label, _fmt_dur(elapsed)), flush=True)
        else:
            print("  FAILED: %s after %s" % (self.label, _fmt_dur(elapsed)), flush=True)
        return False


def main():
    if len(sys.argv) < 2 or sys.argv[1] != "run":
        print("usage: progress.py run --stage N --of M --label TEXT -- cmd ...")
        return 2

    argv = sys.argv[2:]
    if "--" not in argv:
        print("missing -- before the command")
        return 2
    split = argv.index("--")
    opts, cmd = argv[:split], argv[split + 1:]
    if not cmd:
        print("no command given")
        return 2

    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", type=int, default=None)
    ap.add_argument("--of", type=int, default=None)
    ap.add_argument("--label", default="working")
    ap.add_argument("--quiet", action="append", default=[])
    a = ap.parse_args(opts)

    return run(cmd, a.label, index=a.stage, total=a.of, quiet_patterns=a.quiet)


if __name__ == "__main__":
    sys.exit(main())
