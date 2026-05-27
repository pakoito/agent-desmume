//! Unix-socket JSON transport: one request, one response, close.

use anyhow::{anyhow, Context, Result};
use serde_json::Value;
use std::io::{BufRead, BufReader, Write};
use std::os::unix::net::UnixStream;
use std::path::Path;

/// Send `{"id":1, "verb": verb, "args": args}` as one JSON line and read one line back.
pub fn send_one(sock: &Path, verb: &str, args: &Value) -> Result<Value> {
    let req = serde_json::json!({"id": 1, "verb": verb, "args": args});
    let mut s = UnixStream::connect(sock)
        .with_context(|| format!("connect {}", sock.display()))?;
    let mut line = serde_json::to_vec(&req)?;
    line.push(b'\n');
    s.write_all(&line).context("write request")?;
    // Daemon is single-response per request; close write side to be polite.
    let _ = s.shutdown(std::net::Shutdown::Write);
    let mut reader = BufReader::new(s);
    let mut buf = String::new();
    let n = reader.read_line(&mut buf).context("read response")?;
    if n == 0 {
        return Err(anyhow!("daemon closed connection without responding"));
    }
    let resp: Value = serde_json::from_str(buf.trim_end_matches('\n'))
        .with_context(|| format!("parsing daemon response: {buf:?}"))?;
    Ok(resp)
}
