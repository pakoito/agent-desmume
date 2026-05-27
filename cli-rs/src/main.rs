//! agent-desmume CLI (Rust): thin client over the Python daemon's
//! newline-delimited JSON Unix-socket protocol. One CLI invocation
//! == one request (or a batch). Drop-in replacement for
//! `src/agent_desmume/cli.py`.

mod addr;
mod args;
mod daemon;
mod format;
mod wire;

use anyhow::{anyhow, bail, Context, Result};
use clap::Parser;
use serde_json::{json, Value};
use std::path::Path;
use std::process::ExitCode;

use crate::args::{
    BackupAction, BreakAction, Cli, Cmd, GpuAction, MovieAction, RegsAction, StateAction,
    WatchAction,
};

fn abs_path(p: &Path) -> Result<String> {
    let absp = std::path::absolute(p).with_context(|| format!("absolutizing {}", p.display()))?;
    Ok(absp.to_string_lossy().into_owned())
}

/// Translate a parsed `Cmd` into a `(verb, args)` request, or a synthetic
/// "already done locally" response for `start`/`stop`.
fn build_request(cli: &Cli) -> Result<Dispatch> {
    use Dispatch::{Local, Send};
    let r = match &cli.cmd {
        Cmd::Ping => Send("ping", json!({})),
        Cmd::Info => Send("info", json!({})),
        Cmd::Status => Send("is_running", json!({})),

        Cmd::Boot { rom } => Send("boot", json!({"rom": abs_path(rom)?})),
        Cmd::Close => Send("close", json!({})),
        Cmd::Reset => Send("reset", json!({})),

        Cmd::Step { n } => Send("step", json!({"n": n})),

        Cmd::Screenshot {
            path,
            screen,
            overlay,
        } => Send(
            "screenshot",
            json!({
                "path": abs_path(path)?,
                "screen": screen.as_str(),
                "overlay": overlay,
            }),
        ),

        Cmd::Press { key } => Send("press", json!({"key": key})),
        Cmd::Release { key } => Send("release", json!({"key": key})),
        Cmd::Keys { keys } => Send("keys", json!({"pressed": keys})),

        Cmd::Tap { key, frames } => Send(
            "batch",
            json!({
                "cmds": [
                    {"verb": "press",   "args": {"key": key}},
                    {"verb": "step",    "args": {"n": frames}},
                    {"verb": "release", "args": {"key": key}},
                    {"verb": "step",    "args": {"n": 1}},
                ]
            }),
        ),

        Cmd::Touch { x, y, pixels } => {
            let mut args = serde_json::Map::new();
            args.insert("x".into(), json!(x));
            args.insert("y".into(), json!(y));
            if *pixels {
                args.insert("mode".into(), json!("pixels"));
            }
            Send("touch", Value::Object(args))
        }
        Cmd::Untouch => Send("touch_release", json!({})),

        Cmd::Mic { state } => Send("mic_blow", json!({"on": state.is_on()})),

        Cmd::State { action } => match action {
            StateAction::Save { slot } => Send("state_save", json!({"slot": slot})),
            StateAction::Load { slot } => Send("state_load", json!({"slot": slot})),
            StateAction::SaveFile { path } => {
                Send("state_save_file", json!({"path": abs_path(path)?}))
            }
            StateAction::LoadFile { path } => {
                Send("state_load_file", json!({"path": abs_path(path)?}))
            }
        },

        Cmd::ReadMem { addr, length } => Send(
            "read_mem",
            json!({"addr": addr::parse_addr(addr)?, "len": length}),
        ),
        Cmd::WriteMem { addr, hex } => Send(
            "write_mem",
            json!({"addr": addr::parse_addr(addr)?, "hex": hex}),
        ),
        Cmd::ReadString { addr, codec, max } => Send(
            "read_string",
            json!({
                "addr": addr::parse_addr(addr)?,
                "codec": codec,
                "max": max,
            }),
        ),

        Cmd::Regs { action } => match action {
            RegsAction::Read { cpu } => Send("regs_read", json!({"cpu": cpu.as_str()})),
            RegsAction::Write { cpu, assignments } => {
                let mut updates = serde_json::Map::new();
                for a in assignments {
                    let (name, val) = a
                        .split_once('=')
                        .ok_or_else(|| anyhow!("expected NAME=VALUE, got {a:?}"))?;
                    let n = addr::parse_addr(val.trim())?;
                    updates.insert(name.trim().to_string(), json!(n));
                }
                Send(
                    "regs_write",
                    json!({"cpu": cpu.as_str(), "updates": Value::Object(updates)}),
                )
            }
        },

        Cmd::Break { action } => match action {
            BreakAction::Add { addr, size } => Send(
                "break_add",
                json!({"addr": addr::parse_addr(addr)?, "size": size}),
            ),
            BreakAction::Clear { addr } => {
                Send("break_clear", json!({"addr": addr::parse_addr(addr)?}))
            }
            BreakAction::ClearAll => Send("break_clear_all", json!({})),
            BreakAction::List => Send("break_list", json!({})),
        },

        Cmd::Watch { action } => match action {
            WatchAction::Add { addr, mode, size } => Send(
                "watch_add",
                json!({
                    "addr": addr::parse_addr(addr)?,
                    "mode": mode.as_str(),
                    "size": size,
                }),
            ),
            WatchAction::Clear { addr, mode } => Send(
                "watch_clear",
                json!({
                    "addr": addr::parse_addr(addr)?,
                    "mode": mode.as_str(),
                }),
            ),
            WatchAction::ClearAll => Send("watch_clear_all", json!({})),
            WatchAction::List => Send("watch_list", json!({})),
        },

        Cmd::Hits { peek } => Send("hits", json!({"drain": !peek})),

        Cmd::Backup { action } => match action {
            BackupAction::Import { path, force_size } => Send(
                "backup_import",
                json!({"path": abs_path(path)?, "force_size": force_size}),
            ),
            BackupAction::Export { path } => {
                Send("backup_export", json!({"path": abs_path(path)?}))
            }
        },

        Cmd::Movie { action } => match action {
            MovieAction::Record { path, author } => Send(
                "movie_record",
                json!({"path": abs_path(path)?, "author": author}),
            ),
            MovieAction::Play { path } => Send("movie_play", json!({"path": abs_path(path)?})),
            MovieAction::Stop => Send("movie_stop", json!({})),
            MovieAction::Status => Send("movie_status", json!({})),
        },

        Cmd::Gpu { action } => match action {
            GpuAction::Layer {
                screen,
                index,
                state,
            } => Send(
                "gpu_layer",
                json!({"screen": screen.as_str(), "index": index, "on": state.is_on()}),
            ),
        },

        Cmd::Batch { file } => {
            let raw: Value = if file == "-" {
                let s = std::io::read_to_string(std::io::stdin()).context("reading stdin")?;
                serde_json::from_str(&s).context("parsing batch payload from stdin")?
            } else {
                let s = std::fs::read_to_string(file)
                    .with_context(|| format!("reading {file}"))?;
                serde_json::from_str(&s).with_context(|| format!("parsing {file}"))?
            };
            let cmds = match raw {
                Value::Object(mut m) => m
                    .remove("cmds")
                    .ok_or_else(|| anyhow!("batch payload object missing \"cmds\" key"))?,
                Value::Array(_) => raw,
                _ => bail!("batch payload must be a list or {{\"cmds\":[...]}}"),
            };
            Send("batch", json!({"cmds": cmds}))
        }

        Cmd::Start { rom } => Local(LocalDispatch::Start { rom: rom.clone() }),
        Cmd::Stop => Local(LocalDispatch::Stop),
    };
    Ok(r)
}

enum Dispatch {
    /// Forward to daemon with `(verb, args)`.
    Send(&'static str, Value),
    /// Handle locally (start/stop manage the daemon lifecycle directly).
    Local(LocalDispatch),
}

enum LocalDispatch {
    Start { rom: Option<std::path::PathBuf> },
    Stop,
}

fn run(cli: Cli) -> Result<Value> {
    let dispatch = build_request(&cli)?;
    match dispatch {
        Dispatch::Send(verb, args) => {
            daemon::ensure_daemon(&cli.session)?;
            let sock = daemon::socket_path(&cli.session)?;
            wire::send_one(&sock, verb, &args)
        }
        Dispatch::Local(LocalDispatch::Start { rom }) => {
            daemon::ensure_daemon(&cli.session)?;
            if let Some(p) = rom {
                let sock = daemon::socket_path(&cli.session)?;
                return wire::send_one(&sock, "boot", &json!({"rom": abs_path(&p)?}));
            }
            let sock = daemon::socket_path(&cli.session)?;
            let pid = daemon::read_pid(&cli.session)?;
            Ok(json!({
                "ok": true,
                "result": {
                    "socket": sock.to_string_lossy(),
                    "pid": pid,
                }
            }))
        }
        Dispatch::Local(LocalDispatch::Stop) => {
            let stopped = daemon::stop_daemon(&cli.session)?;
            Ok(json!({"ok": true, "result": stopped}))
        }
    }
}

fn main() -> ExitCode {
    let cli = Cli::parse();
    let as_json = cli.json;
    match run(cli) {
        Ok(resp) => ExitCode::from(format::emit(&resp, as_json) as u8),
        Err(e) => {
            let msg = format!("{e:#}");
            ExitCode::from(format::emit_fatal("Error", &msg, as_json) as u8)
        }
    }
}
