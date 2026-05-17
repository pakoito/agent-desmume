#!/usr/bin/env python3
"""
agent-desmume-daemon: a headless DeSmuME wrapper exposing a Unix-socket JSON
protocol for agent-driven Nintendo DS ROM testing.

Protocol: newline-delimited JSON over AF_UNIX.
Request:  {"id": <int>, "verb": "<name>", "args": {...}}
Response: {"id": <int>, "ok": true|false, "result": ..., "error": "..."}
"""

import argparse
import asyncio
import json
import os
import signal
import sys
import time
from pathlib import Path
from typing import Any, Callable

from desmume.controls import Keys, keymask
from desmume.emulator import DeSmuME, SCREEN_WIDTH, SCREEN_HEIGHT, SCREEN_HEIGHT_BOTH

KEY_NAMES = {
    "A": Keys.KEY_A, "B": Keys.KEY_B, "X": Keys.KEY_X, "Y": Keys.KEY_Y,
    "L": Keys.KEY_L, "R": Keys.KEY_R,
    "START": Keys.KEY_START, "SELECT": Keys.KEY_SELECT,
    "UP": Keys.KEY_UP, "DOWN": Keys.KEY_DOWN,
    "LEFT": Keys.KEY_LEFT, "RIGHT": Keys.KEY_RIGHT,
    "LID": Keys.KEY_LID,
}


def resolve_key(name: str) -> int:
    k = name.strip().upper().removeprefix("KEY_")
    if k not in KEY_NAMES:
        raise ValueError(f"unknown key: {name!r} (valid: {sorted(KEY_NAMES)})")
    return keymask(KEY_NAMES[k])


def clamp01(v: float) -> float:
    return max(0.0, min(1.0, float(v)))


REG_NAMES = ["r0", "r1", "r2", "r3", "r4", "r5", "r6", "r7",
             "r8", "r9", "r10", "r11", "r12", "r13", "r14", "r15"]
REG_ALIASES = {"sp": "r13", "lr": "r14", "pc": "r15"}


def _add_ruler_overlay(img, screen: str, touch_pos: tuple[int, int] | None = None):
    """Pad the screenshot with rulers on all 4 sides, labelled in TOUCH coords.

    The Nintendo DS touch screen is the bottom screen only, with origin (0,0)
    at its top-left, going right (+x → 255) and down (+y → 191).

    Labels reflect the touchable region:
    - `bottom`: rulers map directly to touch coords (px 0..255 / 0..191, % 0-100).
    - `top`:    rulers show top-screen pixel coords in grey; no percent. The
                top screen is not touchable.
    - `both`:   x-axis is shared (touch x). y-axis is touch-relative for the
                bottom half (image y 192..383 → touch y 0..191); the top half
                shows tick marks only, with a "TOP — not touchable" caption.
                A red line marks the screen boundary at image y=192.

    A uniform PAD-pixel margin is added on every side. "pad=Npx" labels in
    all four corners advertise the margin size so the agent can subtract it
    when mapping canvas pixels back to image pixels.
    """
    from PIL import Image, ImageDraw, ImageFont

    PAD = 52
    STEP = 32
    BLACK = (0, 0, 0)
    GREY = (110, 110, 110)
    BOUNDARY = (220, 50, 50)
    MAGENTA = (255, 0, 255)
    WHITE = (255, 255, 255)
    DOT_R = 2

    iw, ih = img.size
    cw, ch = iw + 2 * PAD, ih + 2 * PAD
    canvas = Image.new("RGB", (cw, ch), (255, 255, 255))
    canvas.paste(img, (PAD, PAD))
    draw = ImageDraw.Draw(canvas)

    try:
        font = ImageFont.load_default(size=10)
    except TypeError:  # older Pillow
        font = ImageFont.load_default()

    def text_w(s):
        try:
            return draw.textlength(s, font=font)
        except AttributeError:
            return len(s) * 6

    img_top, img_bot = PAD, PAD + ih
    img_left, img_right = PAD, PAD + iw

    # ---- X-axis rulers (top + bottom). Same dimension across all screens. ----
    x_is_touch = screen in ("bottom", "both")
    px_color = BLACK if x_is_touch else GREY
    for x in range(0, iw, STEP):
        xc = PAD + x
        px_t = str(x)
        pct_t = f"{round(x / (SCREEN_WIDTH - 1) * 100)}%" if x_is_touch else None

        # Bottom ruler: ticks extend down from the image edge; labels below.
        draw.line([(xc, img_bot), (xc, img_bot + 4)], fill=BLACK, width=1)
        draw.text((xc - text_w(px_t) / 2, img_bot + 6), px_t, fill=px_color, font=font)
        if pct_t is not None:
            draw.text((xc - text_w(pct_t) / 2, img_bot + 22), pct_t, fill=GREY, font=font)

        # Top ruler: ticks extend up from the image edge; labels above (mirror).
        draw.line([(xc, img_top), (xc, img_top - 4)], fill=BLACK, width=1)
        draw.text((xc - text_w(px_t) / 2, img_top - 16), px_t, fill=px_color, font=font)
        if pct_t is not None:
            draw.text((xc - text_w(pct_t) / 2, img_top - 32), pct_t, fill=GREY, font=font)

    # ---- Y-axis rulers (left + right). Layout depends on screen mode. ----
    # Label columns sized for max widths "192" (~18 px) and "100%" (~25 px) in
    # the default font. With PAD=52, tick at PAD-4..PAD leaves ~48 px for two
    # columns side-by-side: px nearer x=2, pct nearer x=22.
    def draw_y_row(yc, px_label, pct_label, px_col, tick_col=BLACK, text_dy=-5):
        # Left tick + labels.
        draw.line([(img_left - 4, yc), (img_left, yc)], fill=tick_col, width=1)
        y_text = yc + text_dy
        draw.text((2, y_text), px_label, fill=px_col, font=font)
        if pct_label is not None:
            draw.text((22, y_text), pct_label, fill=GREY, font=font)
        # Right tick + labels (mirror: pct nearer the image, px further out).
        draw.line([(img_right, yc), (img_right + 4, yc)], fill=tick_col, width=1)
        if pct_label is not None:
            draw.text((img_right + 4, y_text), pct_label, fill=GREY, font=font)
            draw.text((img_right + 32, y_text), px_label, fill=px_col, font=font)
        else:
            draw.text((img_right + 6, y_text), px_label, fill=px_col, font=font)

    def _text_dy(y, ih_):
        return -5 if y < ih_ - 1 else -10

    if screen == "bottom":
        for y in list(range(0, ih, STEP)) + [ih - 1]:
            pct = round(y / (SCREEN_HEIGHT - 1) * 100)
            draw_y_row(img_top + y, str(y), f"{pct}%", BLACK, text_dy=_text_dy(y, ih))
    elif screen == "top":
        for y in list(range(0, ih, STEP)) + [ih - 1]:
            draw_y_row(img_top + y, str(y), None, GREY, text_dy=_text_dy(y, ih))
    elif screen == "both":
        # Top half: tick marks only (not touchable).
        for y in range(0, SCREEN_HEIGHT, STEP):
            yc = img_top + y
            draw.line([(img_left - 4, yc), (img_left, yc)], fill=GREY, width=1)
            draw.line([(img_right, yc), (img_right + 4, yc)], fill=GREY, width=1)
        # Bottom half: labels show touch_y = image_y - 192.
        for y in list(range(SCREEN_HEIGHT, ih, STEP)) + [ih - 1]:
            touch_y = y - SCREEN_HEIGHT
            pct = round(touch_y / (SCREEN_HEIGHT - 1) * 100)
            draw_y_row(img_top + y, str(touch_y), f"{pct}%", BLACK,
                       text_dy=_text_dy(y, ih))
        # Red boundary line at top of touch screen.
        draw.line([(img_left, img_top + SCREEN_HEIGHT),
                   (img_right - 1, img_top + SCREEN_HEIGHT)],
                  fill=BOUNDARY, width=1)
        # "TOP (no touch)" caption stacked against the non-touchable top half.
        cap_y = img_top + 60
        draw.text((4, cap_y), "TOP", fill=GREY, font=font)
        draw.text((4, cap_y + 12), "(no", fill=GREY, font=font)
        draw.text((4, cap_y + 24), "touch)", fill=GREY, font=font)

    # ---- Padding-size label in each corner (so the agent knows the margin). ----
    pad_t = f"pad={PAD}px"
    pw = text_w(pad_t)
    draw.text((4, 4), pad_t, fill=GREY, font=font)
    draw.text((cw - pw - 4, 4), pad_t, fill=GREY, font=font)
    draw.text((4, ch - 14), pad_t, fill=GREY, font=font)
    draw.text((cw - pw - 4, ch - 14), pad_t, fill=GREY, font=font)

    # ---- Active-touch indicator: magenta dot at the live touch position. ----
    if touch_pos is not None and screen in ("bottom", "both"):
        tx, ty = touch_pos
        cx = PAD + tx
        cy = PAD + ty + (SCREEN_HEIGHT if screen == "both" else 0)
        draw.ellipse([cx - DOT_R - 1, cy - DOT_R - 1, cx + DOT_R + 1, cy + DOT_R + 1],
                     outline=WHITE, width=1)
        draw.ellipse([cx - DOT_R, cy - DOT_R, cx + DOT_R, cy + DOT_R],
                     fill=MAGENTA, outline=WHITE, width=1)

    return canvas


class Daemon:
    def __init__(self, socket_path: str):
        self.socket_path = socket_path
        self.emu = DeSmuME()
        self.emu.volume_set(0)
        self.rom: str | None = None
        self.frame_count = 0
        self.lock = asyncio.Lock()  # serialize emulator access
        self.last_activity = time.monotonic()
        # Hook bookkeeping. Keys are (kind, addr). Values are the ctypes callback
        # objects so they don't get GC'd while libdesmume holds the function ptr.
        # _hits collects pending hits to return on the next step boundary.
        self._hooks: dict[tuple[str, int], Any] = {}
        self._hits: list[dict] = []
        # Last touch position (touch-screen coords) while the stylus is down,
        # so --overlay screenshots can draw a magenta dot at the contact point.
        self._touch_pos: tuple[int, int] | None = None
        self.handlers: dict[str, Callable] = {
            "ping": self._ping,
            "boot": self._boot,
            "close": self._close,
            "reset": self._reset,
            "step": self._step,
            "screenshot": self._screenshot,
            "press": self._press,
            "release": self._release,
            "keys": self._keys,
            "touch": self._touch,
            "touch_release": self._touch_release,
            "mic_blow": self._mic_blow,
            "state_save": self._state_save,
            "state_load": self._state_load,
            "state_save_file": self._state_save_file,
            "state_load_file": self._state_load_file,
            "read_mem": self._read_mem,
            "write_mem": self._write_mem,
            "read_string": self._read_string,
            "regs_read": self._regs_read,
            "regs_write": self._regs_write,
            "break_add": self._break_add,
            "break_clear": self._break_clear,
            "break_clear_all": self._break_clear_all,
            "break_list": self._break_list,
            "watch_add": self._watch_add,
            "watch_clear": self._watch_clear,
            "watch_clear_all": self._watch_clear_all,
            "watch_list": self._watch_list,
            "hits": self._hits_read,
            "backup_import": self._backup_import,
            "backup_export": self._backup_export,
            "movie_record": self._movie_record,
            "movie_play": self._movie_play,
            "movie_stop": self._movie_stop,
            "movie_status": self._movie_status,
            "gpu_layer": self._gpu_layer,
            "is_running": self._is_running,
            "info": self._info,
            "batch": self._batch,
            "shutdown": self._shutdown,
        }
        self._shutdown_event = asyncio.Event()

    # ── verbs ─────────────────────────────────────────────────────────────

    async def _ping(self, _):
        return {"pong": True, "frame": self.frame_count, "rom": self.rom}

    async def _boot(self, args):
        rom = args["rom"]
        if not os.path.isfile(rom):
            raise FileNotFoundError(rom)
        # Drop any stale hooks from the previous session before swapping ROMs.
        self._clear_all_hooks()
        self._hits = []
        self._touch_pos = None
        self.emu.open(rom, auto_resume=True)
        self.rom = rom
        self.frame_count = 0
        return {"rom": rom}

    async def _close(self, _):
        self._clear_all_hooks()
        self._hits = []
        self._touch_pos = None
        self.emu.close()
        self.rom = None
        self.frame_count = 0
        return {}

    def _clear_all_hooks(self):
        for (kind, addr) in list(self._hooks.keys()):
            self._clear_hook_unchecked(kind, addr)

    async def _reset(self, _):
        self._require_rom()
        self.emu.reset()
        self.frame_count = 0
        self._touch_pos = None
        return {}

    def _require_rom(self):
        if self.rom is None:
            raise RuntimeError("no ROM loaded; call 'boot' first")

    async def _step(self, args):
        self._require_rom()
        n = int(args.get("n", 1))
        if n < 1:
            raise ValueError("n must be >= 1")
        # If False, ignore breakpoints/watchpoints and run all n frames anyway.
        break_on_hit = bool(args.get("break_on_hit", True))
        stepped = 0
        halted = False
        for _ in range(n):
            self.emu.cycle(with_joystick=False)
            stepped += 1
            self.frame_count += 1
            if break_on_hit and self._hits:
                halted = True
                break
        out = {"frame": self.frame_count, "stepped": stepped, "halted": halted}
        if self._hits:
            # Drain whatever's accumulated; further hits will reaccumulate.
            out["hits"] = self._hits
            out["hit_cap_reached"] = len(self._hits) >= self.HIT_CAP
            self._hits = []
        return out

    # ── hooks (memory watchpoints + code breakpoints) ─────────────────────

    # Hot instructions fire the callback thousands of times per frame. Cap the
    # in-memory hit queue so we don't OOM and so JSON responses stay small.
    HIT_CAP = 32

    def _make_callback(self, kind: str, addr: int):
        from ctypes import CFUNCTYPE, c_uint, c_int

        # Matches MemoryCbFn / memory_cb_fnc in py-desmume / interface.h:
        # BOOL (*)(unsigned int address, int size)
        CB_T = CFUNCTYPE(c_int, c_uint, c_int)

        def trampoline(hit_addr, hit_size):
            if len(self._hits) >= self.HIT_CAP:
                return 1
            self._hits.append({
                "kind": kind,
                "watch_addr": addr,
                "hit_addr": int(hit_addr),
                "size": int(hit_size),
                "frame": self.frame_count,
            })
            return 1
        return CB_T(trampoline)

    def _add_hook(self, kind: str, addr: int, size: int):
        self._require_rom()
        key = (kind, addr)
        if key in self._hooks:
            self._clear_hook_unchecked(kind, addr)
        cb = self._make_callback(kind, addr)
        if kind == "exec":
            self.emu.memory.register_exec(addr, cb, size=size)
        elif kind == "read":
            self.emu.memory.register_read(addr, cb, size=size)
        elif kind == "write":
            self.emu.memory.register_write(addr, cb, size=size)
        else:
            raise ValueError(f"unknown hook kind: {kind!r}")
        self._hooks[key] = {"cb": cb, "size": size}
        return {"kind": kind, "addr": addr, "size": size}

    def _clear_hook_unchecked(self, kind: str, addr: int):
        # libdesmume removes a hook when called with a NULL callback. py-desmume
        # forwards None directly, which translates to a NULL function pointer.
        if kind == "exec":
            self.emu.memory.register_exec(addr, None, size=self._hooks.get((kind, addr), {}).get("size", 2))
        elif kind == "read":
            self.emu.memory.register_read(addr, None, size=self._hooks.get((kind, addr), {}).get("size", 1))
        elif kind == "write":
            self.emu.memory.register_write(addr, None, size=self._hooks.get((kind, addr), {}).get("size", 1))
        self._hooks.pop((kind, addr), None)

    async def _break_add(self, args):
        addr = int(args["addr"])
        size = int(args.get("size", 2))
        return self._add_hook("exec", addr, size)

    async def _break_clear(self, args):
        addr = int(args["addr"])
        existed = ("exec", addr) in self._hooks
        self._clear_hook_unchecked("exec", addr)
        return {"addr": addr, "existed": existed}

    async def _break_clear_all(self, _):
        n = 0
        for (kind, addr) in list(self._hooks.keys()):
            if kind == "exec":
                self._clear_hook_unchecked("exec", addr)
                n += 1
        return {"cleared": n}

    async def _break_list(self, _):
        return {"breakpoints": [
            {"addr": a, "size": meta["size"]}
            for (k, a), meta in self._hooks.items() if k == "exec"
        ]}

    async def _watch_add(self, args):
        addr = int(args["addr"])
        mode = args["mode"]
        size = int(args.get("size", 1))
        if mode not in ("read", "write"):
            raise ValueError("mode must be 'read' or 'write'")
        return self._add_hook(mode, addr, size)

    async def _watch_clear(self, args):
        addr = int(args["addr"])
        mode = args["mode"]
        if mode not in ("read", "write"):
            raise ValueError("mode must be 'read' or 'write'")
        existed = (mode, addr) in self._hooks
        self._clear_hook_unchecked(mode, addr)
        return {"addr": addr, "mode": mode, "existed": existed}

    async def _watch_clear_all(self, _):
        n = 0
        for (kind, addr) in list(self._hooks.keys()):
            if kind in ("read", "write"):
                self._clear_hook_unchecked(kind, addr)
                n += 1
        return {"cleared": n}

    async def _watch_list(self, _):
        return {"watchpoints": [
            {"addr": a, "mode": k, "size": meta["size"]}
            for (k, a), meta in self._hooks.items() if k in ("read", "write")
        ]}

    async def _hits_read(self, args):
        drain = bool(args.get("drain", True))
        hits = list(self._hits)
        if drain:
            self._hits = []
        return {"hits": hits}

    async def _screenshot(self, args):
        self._require_rom()
        path = args["path"]
        screen = args.get("screen", "both").lower()
        overlay = bool(args.get("overlay", False))
        img = self.emu.screenshot()  # PIL.Image 256x384 RGB
        if screen == "top":
            img = img.crop((0, 0, SCREEN_WIDTH, SCREEN_HEIGHT))
        elif screen == "bottom":
            img = img.crop((0, SCREEN_HEIGHT, SCREEN_WIDTH, SCREEN_HEIGHT_BOTH))
        elif screen != "both":
            raise ValueError(f"screen must be top|bottom|both, got {screen!r}")
        if overlay:
            img = _add_ruler_overlay(img, screen, self._touch_pos)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        img.save(path, "PNG")
        out = {"path": os.path.abspath(path), "w": img.width, "h": img.height,
               "screen": screen, "overlay": overlay, "frame": self.frame_count}
        if self._touch_pos is not None:
            out["touch"] = {"x": self._touch_pos[0], "y": self._touch_pos[1]}
        return out

    async def _press(self, args):
        self.emu.input.keypad_add_key(resolve_key(args["key"]))
        return {"keypad": self.emu.input.keypad_get()}

    async def _release(self, args):
        self.emu.input.keypad_rm_key(resolve_key(args["key"]))
        return {"keypad": self.emu.input.keypad_get()}

    async def _keys(self, args):
        # Replace the whole keypad with a list of pressed keys (everything else released).
        mask = 0
        for name in args.get("pressed", []):
            mask |= resolve_key(name)
        self.emu.input.keypad_update(mask)
        return {"keypad": mask}

    async def _touch(self, args):
        # Touch screen is the bottom DS screen: 256x192 pixels.
        mode = str(args.get("mode", "norm")).lower()
        if mode == "pixels":
            px = max(0, min(SCREEN_WIDTH - 1, int(args["x"])))
            py = max(0, min(SCREEN_HEIGHT - 1, int(args["y"])))
        elif mode == "norm":
            x_norm = clamp01(args["x"])
            y_norm = clamp01(args["y"])
            px = round(x_norm * (SCREEN_WIDTH - 1))
            py = round(y_norm * (SCREEN_HEIGHT - 1))
        else:
            raise ValueError(f"touch mode must be 'norm' or 'pixels', got {mode!r}")
        self.emu.input.touch_set_pos(px, py)
        self._touch_pos = (px, py)
        return {"pixel": {"x": px, "y": py}}

    async def _touch_release(self, _):
        self.emu.input.touch_release()
        self._touch_pos = None
        return {}

    async def _mic_blow(self, args):
        # STUB. The upstream interface.h does not export mic injection. To support
        # this we will need to fork py-desmume, patch desmume_src/.../interface.cpp
        # (FAKE_MIC + Mic_DoNoise + micButtonPressed) and rebuild. See Phase 2.
        state = bool(args.get("on", False))
        return {"unsupported": True,
                "reason": "mic injection not yet exposed by py-desmume / libdesmume",
                "requested_state": state}

    async def _state_save(self, args):
        self._require_rom()
        self.emu.savestate.save(int(args["slot"]))
        return {"slot": int(args["slot"])}

    async def _state_load(self, args):
        self._require_rom()
        self.emu.savestate.load(int(args["slot"]))
        return {"slot": int(args["slot"])}

    async def _state_save_file(self, args):
        self._require_rom()
        self.emu.savestate.save_file(args["path"])
        return {"path": os.path.abspath(args["path"])}

    async def _state_load_file(self, args):
        self._require_rom()
        self.emu.savestate.load_file(args["path"])
        return {"path": os.path.abspath(args["path"])}

    async def _read_mem(self, args):
        self._require_rom()
        addr = int(args["addr"])
        length = int(args["len"])
        if length <= 0 or length > 1 << 20:
            raise ValueError("len must be in (0, 1MB]")
        data = bytes(self.emu.memory.unsigned[addr:addr + length])
        return {"addr": addr, "len": length, "hex": data.hex()}

    async def _write_mem(self, args):
        self._require_rom()
        addr = int(args["addr"])
        data = bytes.fromhex(args["hex"])
        for i, b in enumerate(data):
            self.emu.memory.unsigned[addr + i] = b
        return {"addr": addr, "len": len(data)}

    async def _read_string(self, args):
        self._require_rom()
        addr = int(args["addr"])
        codec = args.get("codec", "shift_jis")
        max_len = int(args.get("max", 256))
        # py-desmume's read_string hardcodes a small max and slow byte-at-a-time
        # reads. Faster: pull a block, then split at the first NUL.
        block = bytes(self.emu.memory.unsigned[addr:addr + max_len])
        nul = block.find(b"\x00")
        raw = block if nul < 0 else block[:nul]
        try:
            decoded = raw.decode(codec, errors="replace")
        except LookupError:
            raise ValueError(f"unknown codec: {codec!r}")
        return {"addr": addr, "codec": codec, "bytes": len(raw),
                "hex": raw.hex(), "text": decoded, "terminated": nul >= 0}

    # ── CPU registers ─────────────────────────────────────────────────────

    def _resolve_reg(self, name: str) -> str:
        n = name.lower()
        n = REG_ALIASES.get(n, n)
        if n not in REG_NAMES:
            raise ValueError(f"unknown register: {name!r} (valid: r0..r15, sp, lr, pc)")
        return n

    def _reg_bank(self, cpu: str):
        cpu = cpu.lower()
        if cpu == "arm9":
            return self.emu.memory.register_arm9
        if cpu == "arm7":
            return self.emu.memory.register_arm7
        raise ValueError(f"cpu must be 'arm9' or 'arm7', got {cpu!r}")

    async def _regs_read(self, args):
        self._require_rom()
        cpu = args.get("cpu", "arm9")
        bank = self._reg_bank(cpu)
        regs = {name: int(getattr(bank, name)) for name in REG_NAMES}
        return {"cpu": cpu, "regs": regs,
                "sp": regs["r13"], "lr": regs["r14"], "pc": regs["r15"]}

    async def _regs_write(self, args):
        self._require_rom()
        cpu = args.get("cpu", "arm9")
        bank = self._reg_bank(cpu)
        updates = args["updates"]  # dict[name -> int]
        written = {}
        for name, value in updates.items():
            r = self._resolve_reg(name)
            setattr(bank, r, int(value))
            written[r] = int(value)
        return {"cpu": cpu, "written": written}

    # ── battery save (.dsv / .sav) ────────────────────────────────────────

    async def _backup_import(self, args):
        self._require_rom()
        path = args["path"]
        force_size = int(args.get("force_size", 0))
        ok = self.emu.backup.import_file(path, force_size=force_size)
        # import_file auto-resets the emulator on success.
        if ok:
            self.frame_count = 0
        return {"path": os.path.abspath(path), "imported": bool(ok),
                "force_size": force_size}

    async def _backup_export(self, args):
        self._require_rom()
        path = args["path"]
        ok = self.emu.backup.export_file(path)
        return {"path": os.path.abspath(path), "exported": bool(ok)}

    # ── movie (TAS .dsm) ──────────────────────────────────────────────────

    async def _movie_record(self, args):
        self._require_rom()
        path = args["path"]
        author = args.get("author", "agent-desmume")
        self.emu.movie.record(path, author)
        return {"path": os.path.abspath(path), "author": author, "recording": True}

    async def _movie_play(self, args):
        self._require_rom()
        path = args["path"]
        self.emu.movie.play(path)
        return {"path": os.path.abspath(path), "playing": True}

    async def _movie_stop(self, _):
        self.emu.movie.stop()
        return {}

    async def _movie_status(self, _):
        m = self.emu.movie
        active = bool(m.is_active())
        out = {"active": active,
               "recording": bool(m.is_recording()),
               "playing": bool(m.is_playing()),
               "finished": bool(m.is_finished())}
        if active:
            try:
                out["length"] = int(m.get_length())
                out["rerecord_count"] = int(m.get_rerecord_count())
                out["readonly"] = bool(m.get_readonly())
                name = m.get_name()
                out["name"] = name.decode("utf-8", errors="replace") if isinstance(name, bytes) else str(name)
            except Exception as e:
                out["info_error"] = f"{type(e).__name__}: {e}"
        return out

    # ── GPU layer toggle ──────────────────────────────────────────────────

    async def _gpu_layer(self, args):
        self._require_rom()
        screen = args["screen"].lower()
        idx = int(args["index"])
        state = bool(args["on"])
        if not 0 <= idx <= 4:
            # DS has BG0..BG3 (4 layers) plus an OBJ layer; libdesmume exposes them indexed 0..4.
            raise ValueError("layer index must be 0..4")
        if screen == "main":
            self.emu.gpu_set_layer_main_enable_state(idx, state)
            new = bool(self.emu.gpu_get_layer_main_enable_state(idx))
        elif screen == "sub":
            self.emu.gpu_set_layer_sub_enable_state(idx, state)
            new = bool(self.emu.gpu_get_layer_sub_enable_state(idx))
        else:
            raise ValueError("screen must be 'main' or 'sub'")
        return {"screen": screen, "index": idx, "on": new}

    async def _is_running(self, _):
        return {"running": self.emu.is_running(), "frame": self.frame_count, "rom": self.rom}

    async def _info(self, _):
        return {
            "rom": self.rom,
            "frame": self.frame_count,
            "running": self.emu.is_running(),
            "screen": {"w": SCREEN_WIDTH, "top_h": SCREEN_HEIGHT, "both_h": SCREEN_HEIGHT_BOTH},
            "keys": sorted(KEY_NAMES),
            "verbs": sorted(self.handlers),
        }

    async def _batch(self, args):
        results = []
        for cmd in args["cmds"]:
            verb = cmd["verb"]
            sub_args = cmd.get("args", {})
            h = self.handlers.get(verb)
            if h is None or verb == "batch":
                results.append({"ok": False, "error": f"bad verb in batch: {verb!r}"})
                continue
            try:
                r = await h(sub_args)
                results.append({"ok": True, "result": r})
            except Exception as e:
                results.append({"ok": False, "error": f"{type(e).__name__}: {e}"})
        return {"results": results}

    async def _shutdown(self, _):
        self._shutdown_event.set()
        return {"bye": True}

    # ── transport ─────────────────────────────────────────────────────────

    async def handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        peer = writer.get_extra_info("peername") or "?"
        try:
            while not reader.at_eof():
                line = await reader.readline()
                if not line:
                    break
                self.last_activity = time.monotonic()
                try:
                    req = json.loads(line)
                    rid = req.get("id")
                    verb = req["verb"]
                    handler = self.handlers.get(verb)
                    if handler is None:
                        resp = {"id": rid, "ok": False, "error": f"unknown verb: {verb!r}"}
                    else:
                        async with self.lock:
                            result = await handler(req.get("args", {}) or {})
                        resp = {"id": rid, "ok": True, "result": result}
                except Exception as e:
                    resp = {"id": req.get("id") if isinstance(req, dict) else None,
                            "ok": False, "error": f"{type(e).__name__}: {e}"}
                writer.write((json.dumps(resp) + "\n").encode("utf-8"))
                await writer.drain()
        except (ConnectionResetError, BrokenPipeError):
            pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass

    async def run(self):
        # Remove stale socket
        if os.path.exists(self.socket_path):
            os.unlink(self.socket_path)
        server = await asyncio.start_unix_server(self.handle_client, path=self.socket_path)
        os.chmod(self.socket_path, 0o600)
        print(f"agent-desmume-daemon listening on {self.socket_path} (pid {os.getpid()})", flush=True)

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, self._shutdown_event.set)

        async with server:
            shutdown_task = asyncio.create_task(self._shutdown_event.wait())
            serve_task = asyncio.create_task(server.serve_forever())
            done, pending = await asyncio.wait(
                {shutdown_task, serve_task}, return_when=asyncio.FIRST_COMPLETED
            )
            for t in pending:
                t.cancel()
        print("agent-desmume-daemon shutting down", flush=True)


def main():
    ap = argparse.ArgumentParser(prog="agent-desmume-daemon")
    ap.add_argument("--socket", required=True, help="Path to Unix socket to listen on")
    args = ap.parse_args()
    try:
        asyncio.run(Daemon(args.socket).run())
    except KeyboardInterrupt:
        pass
    finally:
        if os.path.exists(args.socket):
            try:
                os.unlink(args.socket)
            except OSError:
                pass


if __name__ == "__main__":
    main()
