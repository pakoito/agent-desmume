//! Daemon lifecycle: state dir, pid file, spawn, graceful stop.
//!
//! Wire-compatible with `agent_desmume.cli`'s `ensure_daemon` /
//! `stop_daemon`. The daemon binary itself is still Python; we just
//! exec it (`agent-desmume-daemon` on PATH, or `AGENT_DESMUME_DAEMON_CMD`).

use anyhow::{anyhow, bail, Context, Result};
use std::ffi::OsString;
use std::fs::{self, File, OpenOptions};
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::thread::sleep;
use std::time::{Duration, Instant};

use crate::wire;

pub fn state_dir(session: &str) -> Result<PathBuf> {
    let base: PathBuf = match std::env::var_os("XDG_RUNTIME_DIR") {
        Some(v) if !v.is_empty() => PathBuf::from(v),
        _ => {
            let home = std::env::var_os("HOME")
                .ok_or_else(|| anyhow!("HOME unset and XDG_RUNTIME_DIR unset"))?;
            PathBuf::from(home).join(".cache")
        }
    };
    let d = base.join("agent-desmume").join(session);
    fs::create_dir_all(&d).with_context(|| format!("creating state dir {}", d.display()))?;
    Ok(d)
}

pub fn socket_path(session: &str) -> Result<PathBuf> {
    Ok(state_dir(session)?.join("sock"))
}

pub fn pid_file(session: &str) -> Result<PathBuf> {
    Ok(state_dir(session)?.join("pid"))
}

pub fn log_file(session: &str) -> Result<PathBuf> {
    Ok(state_dir(session)?.join("daemon.log"))
}

pub fn alive(pid: i32) -> bool {
    // SAFETY: kill(pid, 0) is a no-op signal probe; never modifies state.
    let r = unsafe { libc::kill(pid, 0) };
    if r == 0 {
        return true;
    }
    // ESRCH = no such process; EPERM = exists but we can't signal it.
    let err = std::io::Error::last_os_error().raw_os_error();
    matches!(err, Some(libc::EPERM))
}

pub fn read_pid(session: &str) -> Result<Option<i32>> {
    let pf = pid_file(session)?;
    if !pf.exists() {
        return Ok(None);
    }
    let s = match fs::read_to_string(&pf) {
        Ok(s) => s,
        Err(_) => return Ok(None),
    };
    let pid: i32 = match s.trim().parse() {
        Ok(n) => n,
        Err(_) => return Ok(None),
    };
    Ok(if alive(pid) { Some(pid) } else { None })
}

fn resolve_daemon_argv() -> Result<Vec<OsString>> {
    if let Some(cmd) = std::env::var_os("AGENT_DESMUME_DAEMON_CMD") {
        let s = cmd.to_string_lossy().into_owned();
        let parts = shell_words::split(&s)
            .with_context(|| format!("parsing AGENT_DESMUME_DAEMON_CMD={s:?}"))?;
        if parts.is_empty() {
            bail!("AGENT_DESMUME_DAEMON_CMD is empty");
        }
        return Ok(parts.into_iter().map(OsString::from).collect());
    }
    Ok(vec![OsString::from("agent-desmume-daemon")])
}

pub fn ensure_daemon(session: &str) -> Result<()> {
    if read_pid(session)?.is_some() {
        return Ok(());
    }
    let sock = socket_path(session)?;
    if sock.exists() {
        let _ = fs::remove_file(&sock);
    }
    let log = log_file(session)?;
    let log_handle: File = OpenOptions::new()
        .create(true)
        .append(true)
        .open(&log)
        .with_context(|| format!("opening daemon log {}", log.display()))?;
    let log_err = log_handle.try_clone()?;

    let mut argv = resolve_daemon_argv()?;
    argv.push(OsString::from("--socket"));
    argv.push(sock.as_os_str().to_os_string());

    let prog = argv.remove(0);
    let mut cmd = Command::new(&prog);
    cmd.args(&argv)
        .stdin(Stdio::null())
        .stdout(Stdio::from(log_handle))
        .stderr(Stdio::from(log_err));
    // Detach into a new session so the daemon outlives this process.
    use std::os::unix::process::CommandExt;
    unsafe {
        cmd.pre_exec(|| {
            if libc::setsid() == -1 {
                return Err(std::io::Error::last_os_error());
            }
            Ok(())
        });
    }

    let child = cmd
        .spawn()
        .with_context(|| daemon_spawn_hint(&prog, &log))?;
    let pid = child.id() as i32;
    fs::write(pid_file(session)?, pid.to_string())
        .with_context(|| "writing pid file".to_string())?;

    let deadline = Instant::now() + Duration::from_secs(8);
    while Instant::now() < deadline {
        if sock.exists() && alive(pid) {
            return Ok(());
        }
        if !alive(pid) {
            bail!(
                "daemon exited before socket appeared; see {}",
                log.display()
            );
        }
        sleep(Duration::from_millis(50));
    }
    bail!(
        "daemon failed to listen on {} within 8s; see {}",
        sock.display(),
        log.display()
    )
}

fn daemon_spawn_hint(prog: &std::ffi::OsStr, log: &Path) -> String {
    format!(
        "failed to spawn daemon {prog:?}. \
         Ensure `agent-desmume-daemon` is on PATH \
         (e.g. `ln -s $(pwd)/.venv/bin/agent-desmume-daemon ~/.local/bin/`), \
         or set AGENT_DESMUME_DAEMON_CMD. log: {}",
        log.display()
    )
}

/// Graceful shutdown: send the `shutdown` verb, wait up to 3s, then SIGTERM.
pub fn stop_daemon(session: &str) -> Result<serde_json::Value> {
    let Some(pid) = read_pid(session)? else {
        return Ok(serde_json::json!({"running": false}));
    };
    let sock = socket_path(session)?;
    // Best-effort shutdown verb; ignore errors (socket may be torn down mid-call).
    let _ = wire::send_one(&sock, "shutdown", &serde_json::json!({}));

    let deadline = Instant::now() + Duration::from_secs(3);
    while Instant::now() < deadline && alive(pid) {
        sleep(Duration::from_millis(50));
    }
    if alive(pid) {
        // SAFETY: SIGTERM to a known-alive pid we just spawned.
        unsafe {
            libc::kill(pid, libc::SIGTERM);
        }
        sleep(Duration::from_millis(200));
    }
    let _ = fs::remove_file(pid_file(session)?);
    let _ = fs::remove_file(socket_path(session)?);
    Ok(serde_json::json!({"stopped": pid}))
}
