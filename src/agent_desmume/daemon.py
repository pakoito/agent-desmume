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


class RichError(RuntimeError):
    """RuntimeError carrying structured diagnostic data for the JSON response.

    py-desmume bubbles up generic strings like "Unable to load savesate." while
    the real DeSmuME error goes to a no-op `msgBoxFake` callback. Handlers
    raise RichError when they can attach pre-call inspection and a hint; the
    transport adds `hint` and `inspection` fields to the JSON error response.
    """
    def __init__(self, msg: str, *, hint: str = "", inspection: dict | None = None):
        super().__init__(msg)
        self.hint = hint
        self.inspection = inspection or {}


# The vendored libdesmume's savestate magic is the DeSmuME version encoded as
# MAJOR*10000 + MINOR*100 + PATCH, e.g. 91200 for 0.9.12, 91300 for 0.9.13.
# Bumped to 91300 after locally rebuilding py-desmume against upstream's
# release_0_9_13 tag (with HAVE_LIBZ enabled for compressed savestates).
DAEMON_DESMUME_MAGIC = 91300
SAVESTATE_FORMAT_VERSION = 12
SAVESTATE_SIG = b"DeSmuME SState\x00\x00"


def _decode_desmume_magic(m: int) -> str:
    # major*10000 + minor*100 + patch
    major = m // 10000
    minor = (m // 100) % 100
    patch = m % 100
    return f"{major}.{minor}.{patch} (raw={m})"


def _inspect_savestate_file(path: str) -> dict:
    """Read the .dst header (and zlib payload if present) for diagnostics.

    Returns a dict with at minimum {"exists": bool}. When the file looks like a
    DeSmuME savestate, also includes signature/version/magic/sizes/compressed,
    and for compressed files a `zlib_ok` flag from a trial inflate.
    """
    info: dict[str, Any] = {"path": os.path.abspath(path)}
    try:
        st = os.stat(path)
    except FileNotFoundError:
        info["exists"] = False
        return info
    except OSError as e:
        info["exists"] = False
        info["error"] = f"{type(e).__name__}: {e}"
        return info
    info["exists"] = True
    info["size"] = st.st_size
    if st.st_size < 32:
        info["error"] = "file shorter than 32-byte savestate header"
        return info
    try:
        with open(path, "rb") as f:
            head = f.read(32)
            body_head = f.read(64)
    except OSError as e:
        info["error"] = f"{type(e).__name__}: {e}"
        return info
    info["signature_ok"] = head[:16] == SAVESTATE_SIG
    info["signature_raw"] = head[:16].hex()
    if not info["signature_ok"]:
        return info
    fmt_version = int.from_bytes(head[16:20], "little")
    creator_magic = int.from_bytes(head[20:24], "little")
    uncompressed_size = int.from_bytes(head[24:28], "little")
    compressed_field = int.from_bytes(head[28:32], "little")
    info["format_version"] = fmt_version
    info["creator_desmume"] = _decode_desmume_magic(creator_magic)
    info["uncompressed_size"] = uncompressed_size
    info["compressed"] = compressed_field != 0xFFFFFFFF
    info["compressed_size"] = None if not info["compressed"] else compressed_field
    info["payload_head_hex"] = body_head[:8].hex()
    if info["compressed"]:
        # Spot-check: is the payload a valid zlib stream we can inflate to the
        # advertised size? If yes, the file itself is fine but the loader may
        # not have zlib support compiled in (older py-desmume builds don't).
        try:
            import zlib
            with open(path, "rb") as f:
                f.seek(32)
                payload = f.read(compressed_field)
            inflated = zlib.decompress(payload)
            info["zlib_ok"] = True
            info["inflated_size"] = len(inflated)
        except Exception as e:
            info["zlib_ok"] = False
            info["zlib_error"] = f"{type(e).__name__}: {e}"
    return info


def _libdesmume_has_zlib_savestates() -> bool:
    """Best-effort check: does the vendored libdesmume import zlib symbols
    needed for compressed savestates? Inspect the .so once and cache."""
    global _ZLIB_CHECK_RESULT
    try:
        return _ZLIB_CHECK_RESULT  # type: ignore[name-defined]
    except NameError:
        pass
    import subprocess
    from desmume import emulator as _de
    try:
        so = os.path.join(os.path.dirname(_de.__file__), "libdesmume.so")
        out = subprocess.run(
            ["readelf", "--dyn-syms", so], capture_output=True, text=True, timeout=5
        ).stdout
        _ZLIB_CHECK_RESULT = any(s in out for s in (" inflate", " deflate", " uncompress"))
    except Exception:
        _ZLIB_CHECK_RESULT = True  # be permissive if we can't tell
    return _ZLIB_CHECK_RESULT


def _explain_savestate_failure(diag: dict) -> str:
    if not diag.get("exists"):
        return "file does not exist"
    if not diag.get("signature_ok", False):
        return ("file is not a DeSmuME savestate "
                f"(first 16 bytes: {diag.get('signature_raw')})")
    fv = diag.get("format_version")
    if fv != SAVESTATE_FORMAT_VERSION:
        return (f"unsupported savestate format version {fv}; "
                f"daemon's DeSmuME 0.9.12 only handles version {SAVESTATE_FORMAT_VERSION}")
    creator = diag.get("creator_desmume", "?")
    daemon_v = _decode_desmume_magic(DAEMON_DESMUME_MAGIC)
    if diag.get("compressed"):
        if not diag.get("zlib_ok", True):
            return f"compressed payload won't inflate: {diag.get('zlib_error')}"
        if not _libdesmume_has_zlib_savestates():
            return ("savestate is zlib-compressed but the vendored libdesmume.so was "
                    "built without zlib decompression — load fails before reading any chunks")
    if creator != daemon_v:
        return (f"savestate was created by DeSmuME {creator} but the daemon is "
                f"running DeSmuME {daemon_v}; chunk format changed between minor "
                "versions, so loading across versions usually fails. Re-save the "
                "state from the same DeSmuME the daemon uses, or downgrade the GUI emulator.")
    return ("looks valid; loader probably hit an unrecognized chunk. Stderr in the "
            "daemon log may have more detail.")


def _inspect_rom_file(path: str) -> dict:
    """Read the first 512 bytes of an .nds file and pull out the standard
    Nintendo DS cart header so we can explain why ``emu.open`` rejected it.

    Header layout: https://problemkaputt.de/gbatek.htm#dscartridgeheader
    """
    info: dict[str, Any] = {"path": os.path.abspath(path)}
    try:
        st = os.stat(path)
    except FileNotFoundError:
        info["exists"] = False
        return info
    except OSError as e:
        info["exists"] = False
        info["error"] = f"{type(e).__name__}: {e}"
        return info
    info["exists"] = True
    info["size"] = st.st_size
    info["readable"] = os.access(path, os.R_OK)
    if st.st_size < 0x200:
        info["error"] = "file shorter than NDS header (512 bytes)"
        return info
    try:
        with open(path, "rb") as f:
            hdr = f.read(0x200)
    except OSError as e:
        info["error"] = f"{type(e).__name__}: {e}"
        return info
    try:
        info["game_title"] = hdr[0:12].split(b"\x00", 1)[0].decode("ascii", "replace").strip()
        info["game_code"] = hdr[0x0c:0x10].decode("ascii", "replace")
        info["maker_code"] = hdr[0x10:0x12].decode("ascii", "replace")
        info["unit_code"] = hdr[0x12]
        cap = hdr[0x14]
        info["device_capacity_log"] = cap
        info["device_capacity_bytes"] = (128 * 1024) << cap if cap < 32 else None
        info["rom_version"] = hdr[0x1e]
        info["arm9_rom_offset"] = int.from_bytes(hdr[0x20:0x24], "little")
        info["arm9_entry_address"] = int.from_bytes(hdr[0x24:0x28], "little")
    except Exception as e:
        info["parse_error"] = f"{type(e).__name__}: {e}"
    return info


def _explain_rom_failure(diag: dict) -> str:
    if not diag.get("exists"):
        return "file does not exist"
    if not diag.get("readable", True):
        return "file exists but is not readable (permissions)"
    if "error" in diag:
        return diag["error"]
    sz = diag.get("size", 0)
    if sz < 0x200:
        return "file too short for an NDS header"
    code = diag.get("game_code", "")
    if not code or not all(ch.isprintable() for ch in code):
        return (f"NDS header looks corrupt or this is not a DS ROM "
                f"(gamecode={code!r}, size={sz})")
    return (f"emulator refused to open the ROM (size={sz}, gamecode={code!r}, "
            f"title={diag.get('game_title')!r}); possibly encrypted, "
            "compressed (.zip/.7z aren't supported headlessly), or wrong format")


def _inspect_battery_file(path: str) -> dict:
    """Inspect a .sav (raw) / .dsv (DeSmuME) backup save file."""
    info: dict[str, Any] = {"path": os.path.abspath(path)}
    try:
        st = os.stat(path)
    except FileNotFoundError:
        info["exists"] = False
        return info
    except OSError as e:
        info["exists"] = False
        info["error"] = f"{type(e).__name__}: {e}"
        return info
    info["exists"] = True
    info["size"] = st.st_size
    info["extension"] = os.path.splitext(path)[1].lower()
    # Powers-of-2 from 512 B (4 Kbit EEPROM) to 8 MB (NAND).
    canonical = {512, 8192, 32768, 65536, 131072, 262144, 524288, 1048576, 8388608}
    info["size_is_canonical_raw"] = st.st_size in canonical
    if info["extension"] == ".dsv":
        # DeSmuME signs .dsv files with a trailer; the magic literal is
        # `|-DESMUME SAVE-|` and sits at the very end of the file.
        try:
            with open(path, "rb") as f:
                f.seek(max(0, st.st_size - 64))
                tail = f.read()
            info["dsv_footer_present"] = b"|-DESMUME SAVE-|" in tail
            info["dsv_tail_hex"] = tail[-32:].hex()
        except OSError as e:
            info["error"] = f"{type(e).__name__}: {e}"
    return info


def _explain_backup_failure(diag: dict) -> str:
    if not diag.get("exists"):
        return "file does not exist"
    sz = diag.get("size", 0)
    if sz == 0:
        return "file is empty"
    ext = diag.get("extension")
    if ext == ".dsv" and diag.get("dsv_footer_present") is False:
        return ("file has .dsv extension but is missing the "
                "`|-DESMUME SAVE-|` trailer DeSmuME writes; it may be a raw "
                ".sav misnamed, or corrupt")
    if ext in {".sav", ""} and not diag.get("size_is_canonical_raw", True):
        return (f"raw .sav size {sz} is not a recognised DS backup size; "
                "try --force-size N (e.g. 65536, 524288)")
    return "backup import returned false; format may not match cartridge"


def _inspect_movie_file(path: str) -> dict:
    """Inspect a .dsm TAS movie file. DeSmuME movies start with an ASCII
    header (`version 1\\nemuVersion 12\\nrerecordCount …`)."""
    info: dict[str, Any] = {"path": os.path.abspath(path)}
    try:
        st = os.stat(path)
    except FileNotFoundError:
        info["exists"] = False
        return info
    except OSError as e:
        info["exists"] = False
        info["error"] = f"{type(e).__name__}: {e}"
        return info
    info["exists"] = True
    info["size"] = st.st_size
    try:
        with open(path, "rb") as f:
            head = f.read(256)
        info["text_format"] = head.startswith(b"version ")
        info["head_preview"] = head[:96].decode("latin-1", "replace")
    except OSError as e:
        info["error"] = f"{type(e).__name__}: {e}"
    return info


def _inspect_output_path(path: str) -> dict:
    """Check writability of a path we're about to create."""
    apath = os.path.abspath(path)
    parent = os.path.dirname(apath) or "."
    info: dict[str, Any] = {"path": apath, "parent": parent}
    info["parent_exists"] = os.path.isdir(parent)
    info["parent_writable"] = info["parent_exists"] and os.access(parent, os.W_OK)
    info["target_exists"] = os.path.exists(apath)
    if info["target_exists"]:
        info["target_writable"] = os.access(apath, os.W_OK)
    return info


def _explain_output_failure(diag: dict) -> str:
    if not diag.get("parent_exists"):
        return f"parent directory does not exist: {diag.get('parent')}"
    if not diag.get("parent_writable"):
        return f"parent directory not writable: {diag.get('parent')}"
    if diag.get("target_exists") and not diag.get("target_writable", True):
        return "target file exists but is not writable"
    return "write failed for an unknown reason (check disk space)"


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
        # Mute alone leaves the SDL audio thread and the SPU mixer running,
        # which still costs a measurable chunk of frame time. Swap the SPU's
        # output backend to the no-op "dummy" core (coreid 0 in DeSmuME's
        # SNDCoreList). py-desmume doesn't expose this — call the mangled
        # libdesmume symbol directly. Best-effort: skip silently on builds
        # that didn't export it.
        try:
            import ctypes
            _spu_change = self.emu.lib._Z19SPU_ChangeSoundCoreii
            _spu_change.argtypes = [ctypes.c_int, ctypes.c_int]
            _spu_change.restype = ctypes.c_int
            _spu_change(0, 0)  # SNDCORE_DUMMY, buffersize=0
        except (AttributeError, OSError):
            pass
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
        diag = _inspect_rom_file(rom)
        if not diag.get("exists"):
            raise RichError(f"ROM not found: {rom}",
                            hint=_explain_rom_failure(diag), inspection=diag)
        # Drop any stale hooks from the previous session before swapping ROMs.
        self._clear_all_hooks()
        self._hits = []
        self._touch_pos = None
        try:
            self.emu.open(rom, auto_resume=True)
        except RuntimeError as e:
            raise RichError(str(e),
                            hint=_explain_rom_failure(diag), inspection=diag) from e
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
        slot = int(args["slot"])
        if not 0 <= slot <= 9:
            raise RichError(f"invalid savestate slot {slot}",
                            inspection={"slot": slot, "valid_range": [0, 9]})
        self.emu.savestate.save(slot)
        return {"slot": slot}

    async def _state_load(self, args):
        self._require_rom()
        slot = int(args["slot"])
        if not 0 <= slot <= 9:
            raise RichError(f"invalid savestate slot {slot}",
                            inspection={"slot": slot, "valid_range": [0, 9]})
        # scan() refreshes the in-memory bookkeeping the existence check uses.
        try:
            self.emu.savestate.scan()
            exists = bool(self.emu.savestate.exists(slot))
        except Exception:
            exists = None
        if exists is False:
            raise RichError(f"savestate slot {slot} is empty",
                            hint="no state saved here yet; call 'state save' first",
                            inspection={"slot": slot, "exists": False})
        self.emu.savestate.load(slot)
        return {"slot": slot}

    async def _state_save_file(self, args):
        self._require_rom()
        path = args["path"]
        out = _inspect_output_path(path)
        try:
            self.emu.savestate.save_file(path)
        except RuntimeError as e:
            raise RichError(str(e),
                            hint=_explain_output_failure(out),
                            inspection=out) from e
        return {"path": os.path.abspath(path)}

    async def _state_load_file(self, args):
        self._require_rom()
        path = args["path"]
        # py-desmume's wrapper only surfaces "Unable to load savesate." and the
        # real DeSmuME error string goes to a no-op `msgBoxFake` callback, so
        # we pre-inspect the file and attach diagnostics on failure.
        diag = _inspect_savestate_file(path)
        try:
            self.emu.savestate.load_file(path)
        except RuntimeError as e:
            raise RichError(str(e),
                            hint=_explain_savestate_failure(diag),
                            inspection=diag) from e
        return {"path": os.path.abspath(path)}

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
        diag = _inspect_battery_file(path)
        if not diag.get("exists"):
            raise RichError(f"backup file not found: {path}",
                            hint=_explain_backup_failure(diag), inspection=diag)
        try:
            ok = self.emu.backup.import_file(path, force_size=force_size)
        except FileNotFoundError as e:
            raise RichError(str(e),
                            hint=_explain_backup_failure(diag), inspection=diag) from e
        if not ok:
            raise RichError("backup import failed",
                            hint=_explain_backup_failure(diag), inspection=diag)
        # import_file auto-resets the emulator on success.
        self.frame_count = 0
        return {"path": os.path.abspath(path), "imported": True,
                "force_size": force_size}

    async def _backup_export(self, args):
        self._require_rom()
        path = args["path"]
        out = _inspect_output_path(path)
        ok = self.emu.backup.export_file(path)
        if not ok and not out.get("parent_writable", True):
            raise RichError("backup export failed",
                            hint=_explain_output_failure(out), inspection=out)
        # Documented behaviour: returns False if the game hasn't initialised
        # save data yet. Leave that as a soft signal rather than an error.
        return {"path": os.path.abspath(path), "exported": bool(ok)}

    # ── movie (TAS .dsm) ──────────────────────────────────────────────────

    async def _movie_record(self, args):
        self._require_rom()
        path = args["path"]
        author = args.get("author", "agent-desmume")
        out = _inspect_output_path(path)
        if not out.get("parent_exists"):
            raise RichError(f"cannot record to {path}",
                            hint=_explain_output_failure(out), inspection=out)
        self.emu.movie.record(path, author)
        return {"path": os.path.abspath(path), "author": author, "recording": True}

    async def _movie_play(self, args):
        self._require_rom()
        path = args["path"]
        # py-desmume.movie.play already returns DeSmuME's own error string when
        # playback fails — wrap it so the structured `inspection` still helps.
        diag = _inspect_movie_file(path)
        if not diag.get("exists"):
            raise RichError(f"movie file not found: {path}", inspection=diag)
        try:
            self.emu.movie.play(path)
        except RuntimeError as e:
            raise RichError(str(e), inspection=diag) from e
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
                    if isinstance(e, RichError):
                        if e.hint:
                            resp["hint"] = e.hint
                        if e.inspection:
                            resp["inspection"] = e.inspection
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
