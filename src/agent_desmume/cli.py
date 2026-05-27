"""
agent-desmume CLI: thin client that auto-spawns agent-desmume-daemon and
dispatches verbs over a Unix socket. One CLI invocation == one request
(or a batch).
"""

import argparse
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


def state_dir(session: str) -> Path:
    base = Path(os.environ.get("XDG_RUNTIME_DIR")
                or os.path.expanduser("~/.cache"))
    d = base / "agent-desmume" / session
    d.mkdir(parents=True, exist_ok=True)
    return d


def socket_path(session: str) -> Path:
    return state_dir(session) / "sock"


def pid_file(session: str) -> Path:
    return state_dir(session) / "pid"


def log_file(session: str) -> Path:
    return state_dir(session) / "daemon.log"


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError):
        return False
    return True


def read_pid(session: str) -> int | None:
    pf = pid_file(session)
    if not pf.exists():
        return None
    try:
        pid = int(pf.read_text().strip())
    except (ValueError, OSError):
        return None
    return pid if alive(pid) else None


def ensure_daemon(session: str) -> None:
    if read_pid(session) is not None:
        return
    sock = socket_path(session)
    if sock.exists():
        sock.unlink()
    log = open(log_file(session), "ab")
    proc = subprocess.Popen(
        [sys.executable, "-m", "agent_desmume.daemon", "--socket", str(sock)],
        stdin=subprocess.DEVNULL, stdout=log, stderr=log,
        start_new_session=True, close_fds=True,
    )
    pid_file(session).write_text(str(proc.pid))
    # Wait for the socket to appear (daemon ready).
    deadline = time.monotonic() + 8.0
    while time.monotonic() < deadline:
        if sock.exists() and alive(proc.pid):
            return
        if not alive(proc.pid):
            raise RuntimeError(
                f"daemon exited before socket appeared; see {log_file(session)}"
            )
        time.sleep(0.05)
    raise TimeoutError(f"daemon failed to listen on {sock} within 8s")


def stop_daemon(session: str) -> dict:
    pid = read_pid(session)
    if pid is None:
        return {"running": False}
    try:
        send(session, "shutdown", {}, autostart=False)
    except Exception:
        pass
    # Wait for graceful exit
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline and alive(pid):
        time.sleep(0.05)
    if alive(pid):
        os.kill(pid, 15)
        time.sleep(0.2)
    pid_file(session).unlink(missing_ok=True)
    socket_path(session).unlink(missing_ok=True)
    return {"stopped": pid}


def send(session: str, verb: str, args: dict, *, autostart: bool = True) -> dict:
    if autostart:
        ensure_daemon(session)
    sock = socket_path(session)
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.connect(str(sock))
        req = {"id": 1, "verb": verb, "args": args}
        s.sendall((json.dumps(req) + "\n").encode("utf-8"))
        # Read one line of response.
        f = s.makefile("rb")
        line = f.readline()
        if not line:
            raise RuntimeError("daemon closed connection without responding")
        return json.loads(line)


def parse_addr(s: str) -> int:
    return int(s, 0)  # supports 0x.. and decimal


def emit(resp: dict, as_json: bool) -> int:
    if as_json:
        json.dump(resp, sys.stdout)
        sys.stdout.write("\n")
    else:
        if resp.get("ok"):
            r = resp.get("result") or {}
            if r:
                for k, v in r.items():
                    print(f"{k}: {v}")
            else:
                print("ok")
        else:
            print(f"error: {resp.get('error')}", file=sys.stderr)
    return 0 if resp.get("ok") else 1


# ── verb handlers ────────────────────────────────────────────────────────────

def cmd_boot(session, ns):       return send(session, "boot", {"rom": os.path.abspath(ns.rom)})
def cmd_close(session, ns):      return send(session, "close", {})
def cmd_reset(session, ns):      return send(session, "reset", {})
def cmd_step(session, ns):       return send(session, "step", {"n": ns.n})
def cmd_press(session, ns):      return send(session, "press", {"key": ns.key})
def cmd_release(session, ns):    return send(session, "release", {"key": ns.key})
def cmd_keys(session, ns):       return send(session, "keys", {"pressed": ns.keys})
def cmd_untouch(session, ns):    return send(session, "touch_release", {})
def cmd_mic(session, ns):        return send(session, "mic_blow", {"on": ns.state == "on"})
def cmd_ping(session, ns):       return send(session, "ping", {})
def cmd_info(session, ns):       return send(session, "info", {})
def cmd_status(session, ns):     return send(session, "is_running", {})


def cmd_screenshot(session, ns):
    return send(session, "screenshot", {"path": os.path.abspath(ns.path),
                                        "screen": ns.screen,
                                        "overlay": ns.overlay})


def cmd_touch(session, ns):
    args = {"x": ns.x, "y": ns.y}
    if ns.pixels:
        args["mode"] = "pixels"
    return send(session, "touch", args)


def cmd_tap(session, ns):
    # Convenience: press + step + release.
    cmds = [
        {"verb": "press",   "args": {"key": ns.key}},
        {"verb": "step",    "args": {"n": ns.frames}},
        {"verb": "release", "args": {"key": ns.key}},
        {"verb": "step",    "args": {"n": 1}},
    ]
    return send(session, "batch", {"cmds": cmds})


def cmd_state(session, ns):
    if ns.action == "save":      return send(session, "state_save", {"slot": ns.slot})
    if ns.action == "load":      return send(session, "state_load", {"slot": ns.slot})
    if ns.action == "save-file": return send(session, "state_save_file", {"path": ns.path})
    if ns.action == "load-file": return send(session, "state_load_file", {"path": ns.path})
    raise ValueError(ns.action)


def cmd_read_mem(session, ns):
    return send(session, "read_mem", {"addr": parse_addr(ns.addr), "len": ns.length})


def cmd_write_mem(session, ns):
    return send(session, "write_mem", {"addr": parse_addr(ns.addr), "hex": ns.hex})


def cmd_read_string(session, ns):
    return send(session, "read_string", {"addr": parse_addr(ns.addr),
                                          "codec": ns.codec, "max": ns.max})


def cmd_regs(session, ns):
    if ns.regs_action == "read":
        return send(session, "regs_read", {"cpu": ns.cpu})
    if ns.regs_action == "write":
        updates = {}
        for assignment in ns.assignments:
            if "=" not in assignment:
                raise ValueError(f"expected NAME=VALUE, got {assignment!r}")
            name, val = assignment.split("=", 1)
            updates[name.strip()] = parse_addr(val.strip())
        return send(session, "regs_write", {"cpu": ns.cpu, "updates": updates})
    raise ValueError(ns.regs_action)


def cmd_break(session, ns):
    if ns.break_action == "add":
        return send(session, "break_add", {"addr": parse_addr(ns.addr), "size": ns.size})
    if ns.break_action == "clear":
        return send(session, "break_clear", {"addr": parse_addr(ns.addr)})
    if ns.break_action == "clear-all":
        return send(session, "break_clear_all", {})
    if ns.break_action == "list":
        return send(session, "break_list", {})
    raise ValueError(ns.break_action)


def cmd_watch(session, ns):
    if ns.watch_action == "add":
        return send(session, "watch_add",
                    {"addr": parse_addr(ns.addr), "mode": ns.mode, "size": ns.size})
    if ns.watch_action == "clear":
        return send(session, "watch_clear", {"addr": parse_addr(ns.addr), "mode": ns.mode})
    if ns.watch_action == "clear-all":
        return send(session, "watch_clear_all", {})
    if ns.watch_action == "list":
        return send(session, "watch_list", {})
    raise ValueError(ns.watch_action)


def cmd_hits(session, ns):
    return send(session, "hits", {"drain": not ns.peek})


def cmd_backup(session, ns):
    if ns.backup_action == "import":
        return send(session, "backup_import",
                    {"path": os.path.abspath(ns.path), "force_size": ns.force_size})
    if ns.backup_action == "export":
        return send(session, "backup_export", {"path": os.path.abspath(ns.path)})
    raise ValueError(ns.backup_action)


def cmd_movie(session, ns):
    if ns.movie_action == "record":
        return send(session, "movie_record",
                    {"path": os.path.abspath(ns.path), "author": ns.author})
    if ns.movie_action == "play":
        return send(session, "movie_play", {"path": os.path.abspath(ns.path)})
    if ns.movie_action == "stop":
        return send(session, "movie_stop", {})
    if ns.movie_action == "status":
        return send(session, "movie_status", {})
    raise ValueError(ns.movie_action)


def cmd_gpu(session, ns):
    # agent-desmume gpu layer SCREEN INDEX on|off
    return send(session, "gpu_layer",
                {"screen": ns.screen, "index": ns.index, "on": ns.state == "on"})


def cmd_batch(session, ns):
    if ns.file == "-":
        payload = json.load(sys.stdin)
    else:
        with open(ns.file) as f:
            payload = json.load(f)
    if isinstance(payload, dict) and "cmds" in payload:
        cmds = payload["cmds"]
    elif isinstance(payload, list):
        cmds = payload
    else:
        raise ValueError("batch payload must be a list or {\"cmds\":[...]}")
    return send(session, "batch", {"cmds": cmds})


def cmd_start(session, ns):
    ensure_daemon(session)
    if ns.rom:
        return send(session, "boot", {"rom": os.path.abspath(ns.rom)},
                    autostart=False)
    return {"ok": True, "result": {"socket": str(socket_path(session)),
                                   "pid": read_pid(session)}}


def cmd_stop(session, ns):
    return {"ok": True, "result": stop_daemon(session)}


# ── argparse plumbing ────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="agent-desmume",
        description="Headless DeSmuME daemon client [Python]",
    )
    p.add_argument("--session", default=os.environ.get("AGENT_DESMUME_SESSION", "default"))
    p.add_argument("--json", action="store_true", help="Emit raw JSON response")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("start"); s.add_argument("rom", nargs="?"); s.set_defaults(fn=cmd_start)
    s = sub.add_parser("stop"); s.set_defaults(fn=cmd_stop)
    s = sub.add_parser("ping"); s.set_defaults(fn=cmd_ping)
    s = sub.add_parser("info"); s.set_defaults(fn=cmd_info)
    s = sub.add_parser("status"); s.set_defaults(fn=cmd_status)

    s = sub.add_parser("boot"); s.add_argument("rom"); s.set_defaults(fn=cmd_boot)
    s = sub.add_parser("close"); s.set_defaults(fn=cmd_close)
    s = sub.add_parser("reset"); s.set_defaults(fn=cmd_reset)

    s = sub.add_parser("step"); s.add_argument("n", nargs="?", type=int, default=1); s.set_defaults(fn=cmd_step)

    s = sub.add_parser("screenshot")
    s.add_argument("path")
    s.add_argument("--screen", choices=["top", "bottom", "both"], default="both")
    s.add_argument("--overlay", action="store_true",
                   help="Pad image with rulers on all 4 sides (pixel + percent labels) "
                        "to help locate touch coords. Each side has a 52-px margin "
                        "(advertised as 'pad=52px' in every corner) so the agent can "
                        "subtract it when mapping canvas pixels back to image pixels. "
                        "For --screen both, a red line marks the boundary at y=192 "
                        "below which is the touch screen. If a touch is currently "
                        "active, a magenta dot is drawn at the contact point "
                        "(bottom/both only).")
    s.set_defaults(fn=cmd_screenshot)

    s = sub.add_parser("press"); s.add_argument("key"); s.set_defaults(fn=cmd_press)
    s = sub.add_parser("release"); s.add_argument("key"); s.set_defaults(fn=cmd_release)
    s = sub.add_parser("keys"); s.add_argument("keys", nargs="*"); s.set_defaults(fn=cmd_keys)

    s = sub.add_parser("tap")
    s.add_argument("key")
    s.add_argument("--frames", type=int, default=2)
    s.set_defaults(fn=cmd_tap)

    s = sub.add_parser("touch",
        help="Touch the bottom (touch) screen. Default: normalized 0.0-1.0 coords. "
             "With --pixels: integer pixel coords clamped to (0..255, 0..191).")
    s.add_argument("x", type=float)
    s.add_argument("y", type=float)
    s.add_argument("--pixels", action="store_true",
                   help="Interpret x,y as integer pixel coords (clamped to 0..255, 0..191) "
                        "instead of normalized 0.0-1.0.")
    s.set_defaults(fn=cmd_touch)
    s = sub.add_parser("untouch"); s.set_defaults(fn=cmd_untouch)

    s = sub.add_parser("mic"); s.add_argument("state", choices=["on", "off"]); s.set_defaults(fn=cmd_mic)

    s = sub.add_parser("state")
    ssub = s.add_subparsers(dest="action", required=True)
    sa = ssub.add_parser("save"); sa.add_argument("slot", type=int)
    sa = ssub.add_parser("load"); sa.add_argument("slot", type=int)
    sa = ssub.add_parser("save-file"); sa.add_argument("path")
    sa = ssub.add_parser("load-file"); sa.add_argument("path")
    s.set_defaults(fn=cmd_state)

    s = sub.add_parser("read-mem"); s.add_argument("addr"); s.add_argument("length", type=int); s.set_defaults(fn=cmd_read_mem)
    s = sub.add_parser("write-mem"); s.add_argument("addr"); s.add_argument("hex"); s.set_defaults(fn=cmd_write_mem)

    s = sub.add_parser("read-string",
                       help="Read a NUL-terminated string from RAM")
    s.add_argument("addr")
    s.add_argument("--codec", default="shift_jis",
                   help="Text encoding to decode with (default: shift_jis)")
    s.add_argument("--max", type=int, default=256,
                   help="Max bytes to read (default 256)")
    s.set_defaults(fn=cmd_read_string)

    s = sub.add_parser("regs", help="Read or write CPU registers")
    ssub = s.add_subparsers(dest="regs_action", required=True)
    sa = ssub.add_parser("read")
    sa.add_argument("cpu", nargs="?", default="arm9", choices=["arm9", "arm7"])
    sa = ssub.add_parser("write")
    sa.add_argument("cpu", choices=["arm9", "arm7"])
    sa.add_argument("assignments", nargs="+",
                    help="NAME=VALUE assignments (e.g. pc=0x022f8818 r0=42)")
    s.set_defaults(fn=cmd_regs)

    s = sub.add_parser("break", help="Manage code (exec) breakpoints")
    ssub = s.add_subparsers(dest="break_action", required=True)
    sa = ssub.add_parser("add"); sa.add_argument("addr"); sa.add_argument("--size", type=int, default=2)
    sa = ssub.add_parser("clear"); sa.add_argument("addr")
    sa = ssub.add_parser("clear-all")
    sa = ssub.add_parser("list")
    s.set_defaults(fn=cmd_break)

    s = sub.add_parser("watch", help="Manage memory watchpoints (read/write)")
    ssub = s.add_subparsers(dest="watch_action", required=True)
    sa = ssub.add_parser("add")
    sa.add_argument("addr")
    sa.add_argument("--mode", choices=["read", "write"], required=True)
    sa.add_argument("--size", type=int, default=1)
    sa = ssub.add_parser("clear")
    sa.add_argument("addr")
    sa.add_argument("--mode", choices=["read", "write"], required=True)
    sa = ssub.add_parser("clear-all")
    sa = ssub.add_parser("list")
    s.set_defaults(fn=cmd_watch)

    s = sub.add_parser("hits", help="Drain (or peek) pending breakpoint/watchpoint hits")
    s.add_argument("--peek", action="store_true", help="Don't clear the queue after reading")
    s.set_defaults(fn=cmd_hits)

    s = sub.add_parser("backup", help="Import/export battery save (.sav/.dsv)")
    ssub = s.add_subparsers(dest="backup_action", required=True)
    sa = ssub.add_parser("import"); sa.add_argument("path"); sa.add_argument("--force-size", type=int, default=0, dest="force_size")
    sa = ssub.add_parser("export"); sa.add_argument("path")
    s.set_defaults(fn=cmd_backup)

    s = sub.add_parser("movie", help="Record / play DeSmuME TAS movies (.dsm)")
    ssub = s.add_subparsers(dest="movie_action", required=True)
    sa = ssub.add_parser("record"); sa.add_argument("path"); sa.add_argument("--author", default="agent-desmume")
    sa = ssub.add_parser("play"); sa.add_argument("path")
    sa = ssub.add_parser("stop")
    sa = ssub.add_parser("status")
    s.set_defaults(fn=cmd_movie)

    s = sub.add_parser("gpu", help="GPU layer control")
    ssub = s.add_subparsers(dest="gpu_action", required=True)
    sa = ssub.add_parser("layer", help="Enable/disable a BG/OBJ layer")
    sa.add_argument("screen", choices=["main", "sub"], help="Top screen = main; bottom = sub")
    sa.add_argument("index", type=int, help="0..3 = BG0-BG3, 4 = OBJ")
    sa.add_argument("state", choices=["on", "off"])
    s.set_defaults(fn=cmd_gpu)

    s = sub.add_parser("batch"); s.add_argument("file", help="JSON file (or '-' for stdin)"); s.set_defaults(fn=cmd_batch)

    return p


def main():
    ns = build_parser().parse_args()
    try:
        resp = ns.fn(ns.session, ns)
    except Exception as e:
        if ns.json:
            json.dump({"ok": False, "error": f"{type(e).__name__}: {e}"}, sys.stdout)
            sys.stdout.write("\n")
        else:
            print(f"error: {e}", file=sys.stderr)
        sys.exit(2)
    sys.exit(emit(resp, ns.json))


if __name__ == "__main__":
    main()
