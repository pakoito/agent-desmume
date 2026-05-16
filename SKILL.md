---
name: agent-desmume
description: Drive a headless Nintendo DS emulator (DeSmuME) from the shell — boot ROMs, step frames, screenshot, press buttons, touch the screen at normalized 0-1 coordinates, save/load states, read/poke main RAM, read NUL-terminated strings with arbitrary codecs, inspect/write CPU registers, set code breakpoints and memory watchpoints (step halts early on hit), import/export battery saves, record/play TAS movies, toggle individual GPU layers. Use when testing or validating NDS ROMs (especially fan translations) without the GUI emulator. The tool is `agent-desmume`; an auto-spawned daemon persists emulator state across invocations.
---

# agent-desmume — headless Nintendo DS emulator control

A persistent-daemon CLI that wraps DeSmuME (via `py-desmume`) so an agent can drive a Nintendo DS ROM without a window manager. Each CLI call is a one-shot request; the daemon process owns the live emulator state between calls.

## When to use

- Manually testing a fan translation ROM is too slow; you want an agent to navigate menus and capture screenshots.
- You need to dump or scan main RAM during gameplay (e.g. find where a Japanese string sits so you can re-poke a translated version).
- You want reproducible regression tests: savestate at a known scene, replay inputs, diff screenshots.
- You're hunting for crashes during specific input sequences.

**Don't** use it for: realtime gameplay, audio testing (audio is muted), microphone-required scenes (not yet supported — see Limitations).

## Installation

```bash
git clone https://github.com/pakoito/agent-desmume.git
cd agent-desmume
python3 -m venv .venv
.venv/bin/pip install -e .
# One system dep (Debian/Ubuntu); harmless if already installed:
sudo apt install -y libgl1
```

The two binaries land in `.venv/bin/`. Put them on PATH once:

```bash
ln -s "$(pwd)/.venv/bin/agent-desmume"        ~/.local/bin/agent-desmume
ln -s "$(pwd)/.venv/bin/agent-desmume-daemon" ~/.local/bin/agent-desmume-daemon
```

Optional: register the agent skill with Claude Code so agents discover it automatically.
```bash
mkdir -p ~/.claude/skills/agent-desmume
ln -s "$(pwd)/SKILL.md" ~/.claude/skills/agent-desmume/SKILL.md
```

The rest of this skill writes `agent-desmume` assuming it's on `$PATH`; substitute the full path otherwise.

## Quick start

```bash
agent-desmume boot /path/to/game.nds       # load ROM (auto-spawns daemon)
agent-desmume step 180                     # advance ~3 seconds of frames
agent-desmume screenshot shot.png          # PNG of both screens stacked (256x384)
agent-desmume screenshot top.png  --screen top      # top only  (256x192)
agent-desmume screenshot bot.png  --screen bottom   # bottom (touchable) only
agent-desmume tap START                    # press + step a few frames + release
agent-desmume touch 0.5 0.7                # touch bottom screen at (50%, 70%)
agent-desmume untouch
agent-desmume stop                         # shut down the daemon
```

Add `--json` to any command to get the machine-readable response.

## Verbs (full reference)

All verbs accept `--session NAME` (default `default`, overridable via `$AGENT_DESMUME_SESSION`) for parallel emulator instances, and `--json` for structured output.

### Daemon lifecycle
- `agent-desmume start [ROM]` — explicit daemon start; optionally boot a ROM in the same call.
- `agent-desmume stop` — gracefully shut down this session's daemon.
- `agent-desmume ping` — round-trip check; returns `{pong, frame, rom}`.
- `agent-desmume info` — daemon capabilities (key list, verb list, screen geometry).
- `agent-desmume status` — `{running, frame, rom}`.

### ROM control
- `agent-desmume boot ROM_PATH` — load a `.nds` ROM (auto-spawns daemon if needed).
- `agent-desmume close` — close the ROM (daemon stays up).
- `agent-desmume reset` — soft-reset the current ROM.

**Battery save auto-discovery on boot.** When you `boot game.nds`, DeSmuME looks **in the same directory as the ROM** for a save file with the matching basename: `game.dsv` (DeSmuME's format) is tried first, then `game.sav` (raw). If found, it's loaded automatically. Saves the game writes during play are persisted back to `game.dsv` next to the ROM, so booting from a Windows path like `/mnt/c/games/foo.nds` will write `foo.dsv` straight onto the Windows drive. If your save lives elsewhere or has a different name, boot the ROM then call `backup import PATH` (see Battery save section).

### Time
- `agent-desmume step [N]` — cycle N frames (default 1). Each frame is ~16.7 ms of in-game time. Stepping is deterministic.

### Capture
- `agent-desmume screenshot PATH [--screen top|bottom|both]` — write PNG.
  - `both` = 256×384, top above bottom (default).
  - `top` = 256×192. `bottom` = 256×192 (this is the touch screen).

### Input
- `agent-desmume press KEY` / `agent-desmume release KEY` — press/release one button.
- `agent-desmume tap KEY [--frames N]` — press, step N frames (default 2), release, step 1 more frame. Use for menu navigation.
- `agent-desmume keys [KEY ...]` — replace the entire pressed-key set (everything not listed is released).
- `agent-desmume touch X Y` — touch the bottom screen at normalized coords (0.0–1.0 on each axis). `0.5 0.5` is dead center.
- `agent-desmume untouch` — release the stylus.

**Valid KEY values** (case-insensitive, `KEY_` prefix optional): `A B X Y L R START SELECT UP DOWN LEFT RIGHT LID`.

### Memory
- `agent-desmume read-mem ADDR LEN` — read LEN bytes from ADDR. ADDR accepts decimal or `0x...` hex. Returns `{addr, len, hex}` where `hex` is the bytes as lowercase hex.
- `agent-desmume write-mem ADDR HEX` — write the bytes encoded as a hex string to ADDR.
- `agent-desmume read-string ADDR [--codec NAME] [--max N]` — read up to N bytes from ADDR, stop at first NUL, decode with the given codec. Default codec is `shift_jis` (Japan ROMs), default max 256. Use `--codec utf-16-le` for UTF-16, `--codec ascii` / `--codec latin-1` for English, `--codec cp932` for extended Shift-JIS.

Example: dump 64 bytes of main RAM near a known string pointer:
```bash
agent-desmume --json read-mem 0x02000100 64
# {"id":1,"ok":true,"result":{"addr":33554688,"len":64,"hex":"4e696e74656e646f..."}}

agent-desmume --json read-string 0x02000100 --codec shift_jis
# {"id":1,"ok":true,"result":{"addr":33554688,"codec":"shift_jis","bytes":N,"hex":"...","text":"スタート","terminated":true}}
```

### CPU registers
- `agent-desmume regs read [arm9|arm7]` — dump all 16 GP regs (r0–r15) plus `sp` (r13), `lr` (r14), `pc` (r15) aliases. Defaults to ARM9.
- `agent-desmume regs write arm9|arm7 r0=42 pc=0x022f8818 …` — set one or more registers. Hex (`0x…`) and decimal values both accepted.

Caveat: `pc =` (jump-to) is a no-op when DeSmuME's JIT is enabled. JIT is on by default; there is no daemon flag for this yet. Reading PC always works.

### Breakpoints and watchpoints

The emulator core fires hooks **synchronously** during a frame; the daemon catches them, queues hit records, and makes the running `step` halt early. After a halt, the response includes a `hits` array. The queue is capped (32 entries) to handle tight loops — when full, `hit_cap_reached: true` is set; subsequent hits in the same frame are dropped.

- `agent-desmume break add ADDR [--size N]` — break when the CPU executes the instruction at ADDR. Default size 2 (sufficient for thumb/arm instructions; bump for wider regions).
- `agent-desmume break clear ADDR` — remove one breakpoint.
- `agent-desmume break clear-all` — wipe all breakpoints.
- `agent-desmume break list` — show active breakpoints.

- `agent-desmume watch add ADDR --mode read|write [--size N]` — break when ADDR (or any of size N bytes from it) is read or written.
- `agent-desmume watch clear ADDR --mode read|write` — remove one watchpoint.
- `agent-desmume watch clear-all` — wipe all watchpoints.
- `agent-desmume watch list` — show active watchpoints.

- `agent-desmume hits [--peek]` — drain (or peek) pending hits without stepping. Useful if you want to inspect what fired during the last step without re-stepping.

Each hit record: `{kind: "exec"|"read"|"write", watch_addr: <the addr you registered>, hit_addr: <the actual access addr>, size: N, frame: F}`. `hit_addr` may differ from `watch_addr` when `size > 1`.

Disable early-halt for a single step (run all N frames regardless of hits): pass `--json` and post a custom batch, or… (currently always halts on hit; if you need a no-halt mode tell me).

Example — find where the game writes to a known flag byte:
```bash
agent-desmume watch add 0x021A0C40 --mode write
agent-desmume step 600                    # runs until the write happens
agent-desmume --json hits                 # see what fired
agent-desmume --json regs read            # PC at that moment
agent-desmume watch clear-all
```

### State persistence
- `agent-desmume state save SLOT` / `agent-desmume state load SLOT` — 10 numbered slots (0–9). Ephemeral, in-memory.
- `agent-desmume state save-file PATH` / `agent-desmume state load-file PATH` — savestate to a real file. Persists across daemon restarts.

### Battery save (.sav / .dsv)
The on-cartridge backup memory the game actually writes to. If your save is `game.dsv` or `game.sav` sitting next to `game.nds`, **plain `boot` already picks it up** (see the ROM control section). The verbs below are for the cases where the save is somewhere else or has a different name.
- `agent-desmume backup import PATH [--force-size N]` — load `.sav` (raw), `.dsv` (DeSmuME), `.duc` (Action Replay), or `.dss` (DSOrganize). Auto-resets the emulator on success. `--force-size` is bytes (0 = auto-detect; common values 65536, 524288).
- `agent-desmume backup export PATH` — dump current backup memory to a `.dsv` file. Returns `exported: false` if the game hasn't yet initialized save data.

### Movie / TAS replay (.dsm)
Reproducible input traces, great for regression tests.
- `agent-desmume movie record PATH [--author NAME]` — start recording a fresh movie at the current frame.
- `agent-desmume movie play PATH` — play back a movie file.
- `agent-desmume movie stop` — stop the current record/play.
- `agent-desmume movie status` — `{active, recording, playing, finished, length, name, …}`.

### GPU layers
Toggle individual background / sprite layers per screen — handy for isolating text in screenshots (turn off all BG layers except the one the dialogue lives on).
- `agent-desmume gpu layer main|sub IDX on|off`
  - `main` = top screen, `sub` = bottom screen.
  - `IDX` 0–3 = BG0–BG3, 4 = OBJ (sprites).

### Batch
- `agent-desmume batch FILE` (or `-` for stdin) — execute a JSON array of `{verb, args}` objects atomically. Returns per-step results. Faster than N round trips for menu sequences.

Example batch input:
```json
[
  {"verb": "press",   "args": {"key": "A"}},
  {"verb": "step",    "args": {"n": 4}},
  {"verb": "release", "args": {"key": "A"}},
  {"verb": "step",    "args": {"n": 60}},
  {"verb": "screenshot", "args": {"path": "/tmp/after.png", "screen": "bottom"}}
]
```

## NDS memory map cheat sheet

| Region | Address | Size | Notes |
|---|---|---|---|
| Main RAM | `0x02000000`–`0x023FFFFF` | 4 MB | Where game state, dialogue buffers, NPCs live. Most translation-relevant data is here. |
| Shared WRAM | `0x03000000`–`0x03007FFF` | 32 KB | ARM7/ARM9 IPC scratch. |
| ARM9 I/O | `0x04000000`+ | — | Hardware registers (GPU, IPC, DMA, timers). |
| Palette RAM | `0x05000000`–`0x050007FF` | 2 KB | BG + OBJ palettes. |
| VRAM | `0x06000000`–`0x06FFFFFF` | up to 656 KB | Tile / framebuffer storage. |
| OAM | `0x07000000`–`0x070007FF` | 2 KB | Sprite attributes. |
| Cart ROM | `0x08000000`+ | — | Slot-2 (GBA) — usually unmapped for DS-only carts. |

For fan translations, scan **main RAM** (`0x02000000`–`0x023FFFFF`) for the text encoding the ROM uses (usually Shift-JIS, sometimes a custom table, occasionally UTF-16-LE).

## Common workflows

### "Get past the splash and capture the title screen"
```bash
agent-desmume boot game.nds
agent-desmume step 600           # ~10s; long enough for most splashes
agent-desmume screenshot title.png
```

### "Navigate a menu"
```bash
agent-desmume boot game.nds
agent-desmume step 600
agent-desmume tap START          # advance from title
agent-desmume step 120
agent-desmume tap DOWN; agent-desmume tap DOWN; agent-desmume tap A    # pick 3rd menu item
agent-desmume step 180
agent-desmume screenshot menu_result.png
```

### "Snapshot before a hard scene, replay variants"
```bash
agent-desmume state save-file checkpoint.dst   # save the moment before the boss
agent-desmume tap A; agent-desmume step 120; agent-desmume screenshot variant_a.png
agent-desmume state load-file checkpoint.dst   # rewind
agent-desmume tap B; agent-desmume step 120; agent-desmume screenshot variant_b.png
```

### "Find a string in main RAM"

There's no `scan` verb yet — fall back to dumping a range and grepping locally:
```bash
agent-desmume --json read-mem 0x02000000 0x100000 \
  | python3 -c "import sys,json; d=json.load(sys.stdin)['result']; \
                open('/tmp/wram.bin','wb').write(bytes.fromhex(d['hex']))"
# Then search the dump with whatever encoding the game uses
strings -e l /tmp/wram.bin | head     # UTF-16-LE
strings        /tmp/wram.bin | head   # ASCII
# For Shift-JIS:
iconv -f shift_jis -t utf-8 /tmp/wram.bin 2>/dev/null | strings | head
```

Once you have an address, read it directly:
```bash
agent-desmume --json read-string 0x021A8C40 --codec shift_jis --max 128
```

### "Find where the game writes a known field" (watchpoint workflow)

Say you discovered that the player's HP byte lives at `0x021A0C40`. To find the code that decrements it:
```bash
agent-desmume state save-file before_hit.dst         # baseline
agent-desmume watch add 0x021A0C40 --mode write
# do whatever causes HP to drop (touch the enemy, etc.)
agent-desmume step 600                                # halts as soon as the write fires
agent-desmume --json regs read                        # PC = the code that wrote it
agent-desmume watch clear-all
```

### "Set a code breakpoint at a known routine"
```bash
agent-desmume break add 0x022F8818                    # the routine you want to inspect
agent-desmume step 600
# Inspect state at the hit:
agent-desmume --json regs read
agent-desmume --json read-mem 0x0210A000 64
agent-desmume break clear 0x022F8818
```

### "Bring in a real save file"
```bash
agent-desmume boot game.nds
agent-desmume backup import my_save.dsv               # auto-resets after import
agent-desmume step 300                                # let it boot to the post-load state
agent-desmume screenshot loaded.png
```

### "Record a TAS for regression"
```bash
agent-desmume boot game.nds
agent-desmume movie record /tmp/run.dsm --author paco
# … drive the agent through the scene …
agent-desmume movie stop
# Later, replay deterministically:
agent-desmume boot game.nds
agent-desmume movie play /tmp/run.dsm
agent-desmume step 1800
agent-desmume screenshot result.png
```

### "Isolate the text layer for cleaner screenshots"
```bash
# Disable BG0..BG2 + OBJ on the top screen, leaving only BG3 (often where dialogue lives):
for i in 0 1 2 4; do agent-desmume gpu layer main $i off; done
agent-desmume step 1
agent-desmume screenshot text_only.png --screen top
# Restore:
for i in 0 1 2 4; do agent-desmume gpu layer main $i on; done
```

### "Spawn two sessions in parallel"
```bash
agent-desmume --session a boot game-jp.nds
agent-desmume --session b boot game-en.nds
agent-desmume --session a step 600; agent-desmume --session a screenshot a.png
agent-desmume --session b step 600; agent-desmume --session b screenshot b.png
```

## Limitations

- **Microphone**: not exposed. Mic-required scenes ("blow into the mic" puzzles) cannot be passed. Adding it requires patching `py-desmume`'s vendored DeSmuME and rebuilding the wheel.
- **Real-time / 60fps playback**: not built in. Each `step` is synchronous frame stepping — fine for agent control, not for a human watching. Build a separate viewer if you need that.
- **Jump-to-PC** (`regs write … pc=0x…`): writing PC is a no-op when DeSmuME's JIT is enabled. JIT is on by default; there is no daemon flag to disable it yet. Reading PC always works.
- **Hit cap**: breakpoint/watchpoint hits are capped at 32 records per drain to keep responses small in tight loops; the `hit_cap_reached` flag tells you when extras were dropped.
- **`backup export`** silently returns `exported: false` if the game hasn't yet initialized save data (you need to play through at least one save point first).

## Discovery

If you need the live, authoritative list of supported verbs and keys for the running daemon:
```bash
agent-desmume --json info
```

## Project layout (for maintenance)

- `src/agent_desmume/daemon.py` — Unix-socket JSON server wrapping `py-desmume`.
- `src/agent_desmume/cli.py` — CLI client; auto-spawns the daemon on first call.
- `pyproject.toml` — declares `agent-desmume` and `agent-desmume-daemon` entry points (installed via `pip install -e .` into `.venv`).
- `SKILL.md` — this file. Symlink it into `~/.claude/skills/agent-desmume/` to register with Claude Code.
- State per session lives under `$XDG_RUNTIME_DIR/agent-desmume/<session>/` (or `~/.cache/agent-desmume/<session>/`): socket + pid + daemon.log.
