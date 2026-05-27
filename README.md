# agent-desmume

**Headless DeSmuME for agents.** A persistent-daemon CLI that drives a Nintendo DS emulator over a Unix socket: boot a ROM, step frames, take screenshots, press buttons, touch the screen, save/load states, read and poke main RAM, set breakpoints and watchpoints. Built on top of a [fork of py-desmume](https://github.com/pakoito/py-desmume/tree/0.9.13-agent-desmume) pinned to DeSmuME 0.9.13 with compressed savestates enabled.

Designed for testing fan translations, automating menu navigation, and giving LLM agents a programmatic handle on a DS game without a window manager in sight.

## Install

`pip install` will git-clone the py-desmume fork and its DeSmuME submodule, then run `meson + ninja` to build `libdesmume.so` from source. **First install takes ~9 min** on a single core; subsequent installs reuse the wheel cache.

```bash
# Build deps for DeSmuME (Debian / Ubuntu):
sudo apt install -y meson ninja-build pkg-config libsdl2-dev libpcap-dev \
  libglib2.0-dev zlib1g-dev libopenal-dev libsoundtouch-dev libagg-dev \
  python3-dev libgl1

git clone https://github.com/pakoito/agent-desmume.git
cd agent-desmume
python3 -m venv .venv
.venv/bin/pip install -e .
```

Put the daemon on `$PATH` (typically `~/.local/bin` is already there):

```bash
ln -s "$(pwd)/.venv/bin/agent-desmume-daemon" ~/.local/bin/agent-desmume-daemon
```

### CLI: build the Rust client

The CLI is a thin client over the daemon's Unix-socket JSON protocol. The Python entry point (`.venv/bin/agent-desmume`) works but costs ~450 ms per invocation on Python startup + argparse — painful for agents that fire dozens of verbs. The Rust client at `cli-rs/` does the same job in 2–3 ms warm (~50× faster):

```bash
cargo build --release --manifest-path cli-rs/Cargo.toml
ln -s "$(pwd)/cli-rs/target/release/agent-desmume" ~/.local/bin/agent-desmume
```

The Rust binary speaks the exact same wire protocol and verb surface as the Python CLI, and auto-spawns `agent-desmume-daemon` from `$PATH` just like the Python one does. Set `AGENT_DESMUME_DAEMON_CMD` to override the daemon command if you need to (e.g. a custom wrapper).

If you'd rather stay all-Python, just symlink `.venv/bin/agent-desmume` into `~/.local/bin/` instead.

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
agent-desmume screenshot title.png --overlay   # … with touch-coord rulers on all 4 sides
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
agent-desmume CLI ─── Unix socket / NDJSON ──> agent-desmume-daemon (Python)
                                                       │
                                                       └── py-desmume (Cython)        ← pakoito/py-desmume fork
                                                              └── libdesmume.so       ← built locally from pakoito/desmume
```

The dependency chain is two patched forks pinned by `pyproject.toml`:

- [`pakoito/py-desmume@0.9.13-agent-desmume`](https://github.com/pakoito/py-desmume/tree/0.9.13-agent-desmume) — SkyTemple's bindings with the desmume submodule bumped to upstream `release_0_9_13` plus the three patches below.
- [`pakoito/desmume@0.9.13-agent-desmume`](https://github.com/pakoito/desmume/tree/0.9.13-agent-desmume) — upstream DeSmuME 0.9.13 with:
  - `-D_GNU_SOURCE` in the interface meson build (fixes `strdup`/`realpath` against modern glibc).
  - `-DHAVE_LIBZ` in the interface meson build (turns on the zlib-compressed savestate read/write paths — without it, every `.dst` produced by the standalone 0.9.13 GUI fails to load).
  - Three extra C exports cherry-picked from SkyTemple's downstream: `desmume_close`, `desmume_backup_import_file`, `desmume_backup_export_file` (needed by py-desmume's Python wrapper).

The published py-desmume 0.0.9 wheel on PyPI ships DeSmuME 0.9.12 without `HAVE_LIBZ`, so this project does **not** install cleanly from PyPI alone.

## License

GPL-3.0-or-later. Inherited from py-desmume (GPLv3+) and DeSmuME (GPLv2+).

See `LICENSE`.

## Credits

- [DeSmuME](https://github.com/TASEmulators/desmume) — the emulator itself.
- [py-desmume](https://github.com/SkyTemple/py-desmume) — the Python bindings that make this possible, maintained by the SkyTemple project. agent-desmume pulls from a [local fork](https://github.com/pakoito/py-desmume) with the 0.9.13 submodule bump.
