# agent-desmume

**Headless DeSmuME for agents.** A persistent-daemon CLI that drives a Nintendo DS emulator over a Unix socket: boot a ROM, step frames, take screenshots, press buttons, touch the screen, save/load states, read and poke main RAM, set breakpoints and watchpoints. Built on top of [py-desmume](https://github.com/SkyTemple/py-desmume).

Designed for testing fan translations, automating menu navigation, and giving LLM agents a programmatic handle on a DS game without a window manager in sight.

## Install

```bash
git clone https://github.com/pakoito/agent-desmume.git
cd agent-desmume
python3 -m venv .venv
.venv/bin/pip install -e .

# One system dependency on Debian / Ubuntu (skipped if already installed):
sudo apt install -y libgl1
```

Put the binaries on `$PATH` (typically `~/.local/bin` is already there):

```bash
ln -s "$(pwd)/.venv/bin/agent-desmume"        ~/.local/bin/agent-desmume
ln -s "$(pwd)/.venv/bin/agent-desmume-daemon" ~/.local/bin/agent-desmume-daemon
```

Register the skill with Claude Code (optional, lets agents discover the tool automatically):

```bash
mkdir -p ~/.claude/skills/agent-desmume
ln -s "$(pwd)/SKILL.md" ~/.claude/skills/agent-desmume/SKILL.md
```

## Usage

```bash
agent-desmume boot /path/to/game.nds
agent-desmume step 600                  # advance ~10 seconds
agent-desmume screenshot title.png      # both screens, 256×384 PNG
agent-desmume screenshot title.png --overlay   # … with bottom+left touch-coord rulers
agent-desmume tap START                 # press + step + release
agent-desmume touch 0.5 0.7             # touch the bottom screen at 50%, 70%
agent-desmume touch 128 96 --pixels     # … or by integer pixel (clamped to 0..255, 0..191)
agent-desmume state save 1              # checkpoint into slot 1
agent-desmume read-string 0x021A8C40 --codec shift_jis
agent-desmume stop
```

Everything responds in JSON with `--json`. The daemon auto-spawns on first call and stays alive across invocations — boot once, then drive it.

See [`SKILL.md`](SKILL.md) for the complete verb reference, NDS memory map, common workflows (find-a-string, watchpoint-the-writer, code breakpoints, TAS movies, GPU layer isolation), and limitations.

## What it can do

- Frame-perfect stepping (deterministic; no realtime).
- Top / bottom / both-screen PNG screenshots, optional touch-coord ruler overlay (`--overlay`).
- Buttons, touch (normalized 0–1 or integer pixel coords with `--pixels`), lid open/close.
- Savestate slots and file-backed savestates.
- Battery save (.sav/.dsv/.duc/.dss) import and export — auto-discovered next to the ROM if named `<rom-basename>.dsv`.
- Read / write any DS memory address; read NUL-terminated strings with arbitrary codecs.
- ARM9 + ARM7 register read/write.
- Code breakpoints (`break add ADDR`) and memory watchpoints (`watch add ADDR --mode read|write`) with synchronous early-halt on hit.
- DeSmuME movie record/play for reproducible test runs.
- GPU layer toggle per screen, useful for isolating text in screenshots.
- Multiple parallel sessions via `--session NAME`.

## What it can't do (yet)

- **Microphone input.** Mic-required scenes can't be passed. The underlying `libdesmume.so` C ABI doesn't export the mic functions; adding them would require a patch to `py-desmume`'s vendored DeSmuME and a wheel rebuild.
- **Realtime playback.** Each call to `step` is synchronous frame stepping — fine for agents and CI, not for humans watching live.
- **Jump-to-PC.** Writing the PC register is a no-op while DeSmuME's JIT is on (it is, by default).

See `SKILL.md` for the full list.

## Architecture

```
agent-desmume CLI  ──── Unix socket / newline-delimited JSON ───>  agent-desmume-daemon (Python)
                                                                          │
                                                                          └── py-desmume  (Cython)
                                                                                 └── libdesmume.so  (bundled in py-desmume wheel)
```

No vendoring, no submodules. The daemon imports `py-desmume`; the wheel ships its own `libdesmume.so`.

## License

GPL-3.0-or-later. Inherited from py-desmume (GPLv3+) and DeSmuME (GPLv2+).

See `LICENSE`.

## Credits

- [DeSmuME](https://github.com/TASEmulators/desmume) — the emulator itself.
- [py-desmume](https://github.com/SkyTemple/py-desmume) — the Python bindings that make this possible. Maintained by the SkyTemple project.
