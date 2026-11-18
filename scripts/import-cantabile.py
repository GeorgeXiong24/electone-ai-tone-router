"""Import the generated tone files into Cantabile.

Builds the .cantabileSong (via build-cantabile.py) and, when the user agrees,
opens it with Cantabile so the plugins and patches are loaded in one step.

The song file always lives inside the project workspace
(workspace/cantabile/); nothing is written into Cantabile's own user folders.

Usage:
  python import-cantabile.py --song <title substring>
  python import-cantabile.py --latest
  python import-cantabile.py --latest --yes     # no prompt
  python import-cantabile.py --latest --no      # build only, never launch
  python import-cantabile.py --latest --dry-run
"""
import argparse
import glob
import json
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import cantabile_format as cf  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKSPACE = os.environ.get("ELECTONE_WORKSPACE") or os.path.join(ROOT, "workspace")
TONE_ROOT = os.path.join(WORKSPACE, "tone")
SONG_ROOT = os.path.join(WORKSPACE, "cantabile")

# Cantabile registers .cantabileSong with this command; fall back to the
# default install path when the association is missing.
CANTABILE_EXE_CANDIDATES = [
    r"C:\Program Files\Topten Software\Cantabile 4.0\Cantabile.exe",
    r"C:\Program Files (x86)\Topten Software\Cantabile 4.0\Cantabile.exe",
]


def find_cantabile():
    """Return the Cantabile executable path, or None."""
    for path in CANTABILE_EXE_CANDIDATES:
        if os.path.isfile(path):
            return path
    try:
        import winreg
        key = winreg.OpenKey(
            winreg.HKEY_CLASSES_ROOT,
            r"Cantabile3.Song\Shell\open\command")
        command, _ = winreg.QueryValueEx(key, "")
        winreg.CloseKey(key)
        # "\"C:\\...\\Cantabile.exe\" \"%1\""
        exe = command.split('"')[1] if '"' in command else command.split()[0]
        if os.path.isfile(exe):
            return exe
    except OSError:
        pass
    return None


def find_song(song_filter=""):
    """Return (song_name, song_path) of the newest generated song file."""
    cands = glob.glob(os.path.join(SONG_ROOT, "*.cantabileSong"))
    if song_filter:
        cands = [p for p in cands if song_filter in os.path.basename(p)]
    if not cands:
        return None, None
    path = max(cands, key=os.path.getmtime)
    return os.path.splitext(os.path.basename(path))[0], path


def build_song(song_filter=""):
    """Run build-cantabile.py and return its output path."""
    script = os.path.join(ROOT, "scripts", "build-cantabile.py")
    cmd = [sys.executable, script]
    if song_filter:
        cmd += ["--song", song_filter]
    else:
        cmd += ["--latest"]
    result = subprocess.run(cmd, capture_output=True, text=True,
                            encoding="utf-8", errors="replace")
    if result.returncode != 0:
        sys.stderr.write(result.stdout or "")
        sys.stderr.write(result.stderr or "")
        raise SystemExit("build-cantabile.py failed")
    return result.stdout


def verify_song(path):
    """Check that every plugin blob in the song decodes to a Surge patch."""
    with open(path, encoding="utf-8") as f:
        song = json.load(f)
    plugins = 0
    for obj in song.get("objects", []):
        if obj.get("kind") != "Plugin":
            continue
        plugins += 1
        class_id, state = cf.decode_plugin_data(obj["pluginData"])
        if class_id != cf.SURGE_CLASS_ID:
            raise SystemExit(f"{obj['name']}: unexpected plugin class id")
        xml, _ = cf.parse_surge_component_state(cf.parse_vst3_state(state)[1])
        if b"<patch" not in xml[:200]:
            raise SystemExit(f"{obj['name']}: state is not a Surge patch")
    return plugins


def launch(path):
    """Open the song with Cantabile. Returns the process or None."""
    exe = find_cantabile()
    if exe is None:
        return None, "找不到 Cantabile.exe"
    try:
        proc = subprocess.Popen([exe, path], close_fds=True)
    except OSError as exc:
        return None, f"启动失败：{exc}"
    return proc, exe


def ask_yes_no(prompt, default=False):
    suffix = "[Y/n]" if default else "[y/N]"
    while True:
        try:
            answer = input(f"{prompt} {suffix} ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return False
        if not answer:
            return default
        if answer in ("y", "yes"):
            return True
        if answer in ("n", "no"):
            return False
        print("请输入 Y 或 N。")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--song", default="", help="substring of the song name")
    ap.add_argument("--latest", action="store_true")
    ap.add_argument("--yes", action="store_true", help="import without asking")
    ap.add_argument("--no", action="store_true", help="never launch Cantabile")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--wait", type=float, default=0.0,
                    help="seconds to wait after launching, then report status")
    args = ap.parse_args()

    if args.yes and args.no:
        sys.exit("--yes and --no are mutually exclusive")

    print("=" * 64)
    print("生成 Cantabile 工程 / building Cantabile song")
    print("=" * 64)
    output = build_song(args.song)
    sys.stdout.write(output)

    song, path = find_song(args.song)
    if path is None:
        sys.exit("没有生成任何 .cantabileSong 文件")

    plugins = verify_song(path)
    print(f"工程文件: {path}")
    print(f"插件数量: {plugins}")

    if args.dry_run:
        print("[dry-run] 不启动 Cantabile")
        return

    if args.no:
        print("已跳过自动导入（--no）。")
        return

    if args.yes:
        do_import = True
    else:
        print()
        do_import = ask_yes_no("需要自动导入 Cantabile 吗？", default=True)

    if not do_import:
        print("已取消。工程文件保留在：")
        print(f"  {path}")
        return

    proc, info = launch(path)
    if proc is None:
        print(f"自动导入失败：{info}")
        print("可以手动打开工程文件：")
        print(f"  {path}")
        sys.exit(1)

    print(f"已用 Cantabile 打开：{info}")
    print(f"  {path}")

    if args.wait > 0:
        time.sleep(args.wait)
        code = proc.poll()
        if code is None:
            print("Cantabile 正在运行。")
        else:
            print(f"Cantabile 已退出（exit {code}）。")


if __name__ == "__main__":
    main()
