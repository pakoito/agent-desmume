//! clap derive surface — mirrors `src/agent_desmume/cli.py`'s argparse tree.
//!
//! Verb naming convention: clap derive kebab-cases CamelCase variants
//! automatically, which matches the Python CLI's `read-mem`, `write-mem`,
//! `read-string`, `save-file`, `load-file`, `clear-all` style.

use clap::{Parser, Subcommand, ValueEnum};
use std::path::PathBuf;

#[derive(Parser, Debug)]
#[command(
    name = "agent-desmume",
    version,
    about = "Headless DeSmuME daemon client [Rust]"
)]
pub struct Cli {
    /// Session name (state-dir bucket). Default "default".
    #[arg(long, env = "AGENT_DESMUME_SESSION", default_value = "default", global = true)]
    pub session: String,

    /// Emit raw JSON response on stdout instead of key:value lines.
    #[arg(long, global = true)]
    pub json: bool,

    #[command(subcommand)]
    pub cmd: Cmd,
}

#[derive(Subcommand, Debug)]
pub enum Cmd {
    /// Ensure daemon is running, optionally boot a ROM.
    Start {
        rom: Option<PathBuf>,
    },
    /// Shut down the daemon (graceful, falls back to SIGTERM).
    Stop,
    /// Ping the daemon.
    Ping,
    /// Daemon info (frame, rom, etc.).
    Info,
    /// Whether the emulator is running a ROM.
    Status,

    /// Load a ROM and start emulation.
    Boot { rom: PathBuf },
    /// Unload the current ROM.
    Close,
    /// Reset the emulator.
    Reset,

    /// Advance N frames (default 1).
    Step {
        #[arg(default_value_t = 1)]
        n: u32,
    },

    /// Capture a PNG screenshot.
    Screenshot {
        path: PathBuf,
        #[arg(long, value_enum, default_value_t = ScreenChoice::Both)]
        screen: ScreenChoice,
        /// Pad image with rulers (pixel + percent labels) for touch-coord debugging.
        #[arg(long)]
        overlay: bool,
    },

    /// Press a key (held until released).
    Press { key: String },
    /// Release a previously-pressed key.
    Release { key: String },
    /// Set the set of pressed keys (zero or more).
    Keys { keys: Vec<String> },

    /// press + step N frames + release + step 1 (default frames=2).
    Tap {
        key: String,
        #[arg(long, default_value_t = 2)]
        frames: u32,
    },

    /// Touch the bottom screen. Default normalized 0.0-1.0; --pixels for ints.
    Touch {
        x: f64,
        y: f64,
        /// Interpret x,y as integer pixel coords (clamped to 0..255, 0..191).
        #[arg(long)]
        pixels: bool,
    },
    /// Release the touch screen.
    Untouch,

    /// Toggle microphone-blow input.
    Mic { state: OnOff },

    /// Save / load emulator state (slots 0-9 or arbitrary file paths).
    State {
        #[command(subcommand)]
        action: StateAction,
    },

    /// Read raw bytes from main RAM (hex output).
    #[command(name = "read-mem")]
    ReadMem {
        /// Address (Python int(s, 0) syntax: 0x.., 0o.., 0b.., or decimal).
        addr: String,
        length: u32,
    },
    /// Write raw bytes (hex string) to main RAM.
    #[command(name = "write-mem")]
    WriteMem { addr: String, hex: String },
    /// Read a NUL-terminated string from RAM.
    #[command(name = "read-string")]
    ReadString {
        addr: String,
        #[arg(long, default_value = "shift_jis")]
        codec: String,
        #[arg(long, default_value_t = 256)]
        max: u32,
    },

    /// Read/write CPU registers (ARM9 or ARM7).
    Regs {
        #[command(subcommand)]
        action: RegsAction,
    },

    /// Manage code (exec) breakpoints.
    Break {
        #[command(subcommand)]
        action: BreakAction,
    },

    /// Manage memory watchpoints (read/write).
    Watch {
        #[command(subcommand)]
        action: WatchAction,
    },

    /// Drain (or peek) pending breakpoint/watchpoint hits.
    Hits {
        /// Don't clear the queue after reading.
        #[arg(long)]
        peek: bool,
    },

    /// Import/export battery save (.sav/.dsv).
    Backup {
        #[command(subcommand)]
        action: BackupAction,
    },

    /// Record / play DeSmuME TAS movies (.dsm).
    Movie {
        #[command(subcommand)]
        action: MovieAction,
    },

    /// GPU layer control.
    Gpu {
        #[command(subcommand)]
        action: GpuAction,
    },

    /// Execute a JSON list of {verb, args} atomically.
    Batch {
        /// JSON file path, or "-" for stdin.
        file: String,
    },
}

#[derive(Subcommand, Debug)]
pub enum StateAction {
    /// Save to slot 0-9.
    Save { slot: u32 },
    /// Load from slot 0-9.
    Load { slot: u32 },
    /// Save to a file path.
    #[command(name = "save-file")]
    SaveFile { path: PathBuf },
    /// Load from a file path.
    #[command(name = "load-file")]
    LoadFile { path: PathBuf },
}

#[derive(Subcommand, Debug)]
pub enum RegsAction {
    /// Read all CPU registers (arm9 by default).
    Read {
        #[arg(value_enum, default_value_t = Cpu::Arm9)]
        cpu: Cpu,
    },
    /// Write registers: NAME=VALUE pairs.
    Write {
        #[arg(value_enum)]
        cpu: Cpu,
        /// e.g. `pc=0x022f8818 r0=42`
        #[arg(num_args = 1..)]
        assignments: Vec<String>,
    },
}

#[derive(Subcommand, Debug)]
pub enum BreakAction {
    Add {
        addr: String,
        #[arg(long, default_value_t = 2)]
        size: u32,
    },
    Clear {
        addr: String,
    },
    #[command(name = "clear-all")]
    ClearAll,
    List,
}

#[derive(Subcommand, Debug)]
pub enum WatchAction {
    Add {
        addr: String,
        #[arg(long, value_enum)]
        mode: WatchMode,
        #[arg(long, default_value_t = 1)]
        size: u32,
    },
    Clear {
        addr: String,
        #[arg(long, value_enum)]
        mode: WatchMode,
    },
    #[command(name = "clear-all")]
    ClearAll,
    List,
}

#[derive(Subcommand, Debug)]
pub enum BackupAction {
    Import {
        path: PathBuf,
        #[arg(long = "force-size", default_value_t = 0)]
        force_size: u32,
    },
    Export {
        path: PathBuf,
    },
}

#[derive(Subcommand, Debug)]
pub enum MovieAction {
    Record {
        path: PathBuf,
        #[arg(long, default_value = "agent-desmume")]
        author: String,
    },
    Play {
        path: PathBuf,
    },
    Stop,
    Status,
}

#[derive(Subcommand, Debug)]
pub enum GpuAction {
    /// Enable/disable a BG/OBJ layer.
    Layer {
        /// Top screen = main; bottom = sub.
        #[arg(value_enum)]
        screen: GpuScreen,
        /// 0..3 = BG0-BG3, 4 = OBJ.
        index: u32,
        #[arg(value_enum)]
        state: OnOff,
    },
}

#[derive(Copy, Clone, Debug, ValueEnum)]
pub enum Cpu {
    Arm9,
    Arm7,
}
impl Cpu {
    pub fn as_str(self) -> &'static str {
        match self {
            Cpu::Arm9 => "arm9",
            Cpu::Arm7 => "arm7",
        }
    }
}

#[derive(Copy, Clone, Debug, ValueEnum)]
pub enum OnOff {
    On,
    Off,
}
impl OnOff {
    pub fn is_on(self) -> bool {
        matches!(self, OnOff::On)
    }
}

#[derive(Copy, Clone, Debug, ValueEnum)]
pub enum WatchMode {
    Read,
    Write,
}
impl WatchMode {
    pub fn as_str(self) -> &'static str {
        match self {
            WatchMode::Read => "read",
            WatchMode::Write => "write",
        }
    }
}

#[derive(Copy, Clone, Debug, ValueEnum)]
pub enum ScreenChoice {
    Top,
    Bottom,
    Both,
}
impl ScreenChoice {
    pub fn as_str(self) -> &'static str {
        match self {
            ScreenChoice::Top => "top",
            ScreenChoice::Bottom => "bottom",
            ScreenChoice::Both => "both",
        }
    }
}

#[derive(Copy, Clone, Debug, ValueEnum)]
pub enum GpuScreen {
    Main,
    Sub,
}
impl GpuScreen {
    pub fn as_str(self) -> &'static str {
        match self {
            GpuScreen::Main => "main",
            GpuScreen::Sub => "sub",
        }
    }
}
