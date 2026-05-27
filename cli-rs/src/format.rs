//! Output formatter matching the Python CLI's `emit()` shape.
//!
//! Key requirements (see plan, "Parity traps"):
//! - Default mode iterates `result` as a map and prints `key: value` lines.
//!   Booleans print as `True`/`False` (capital), `null` as `None`. Numbers
//!   and strings pass through. Nested objects/arrays serialize as JSON.
//! - `--json` mode dumps the raw response as one JSON line to stdout.
//! - Exit code 0 if `ok: true`, else 1.
//! - On exception/transport error, the dispatch layer prints to stderr and
//!   exits 2 (see `main.rs`).

use serde_json::Value;
use std::fmt::Write as _;
use std::io::{self, Write};

/// Custom serde_json formatter matching Python `json.dump`'s default
/// separators: `", "` between items, `": "` between key and value.
/// serde_json's built-in compact formatter writes `,` and `:` with no
/// spaces — agents that parse `--json` output may depend on Python's
/// shape, so we match it exactly.
#[derive(Default)]
struct PythonJsonFormatter;

impl serde_json::ser::Formatter for PythonJsonFormatter {
    fn begin_array_value<W: ?Sized + Write>(&mut self, w: &mut W, first: bool) -> io::Result<()> {
        if !first {
            w.write_all(b", ")?;
        }
        Ok(())
    }
    fn begin_object_key<W: ?Sized + Write>(&mut self, w: &mut W, first: bool) -> io::Result<()> {
        if !first {
            w.write_all(b", ")?;
        }
        Ok(())
    }
    fn begin_object_value<W: ?Sized + Write>(&mut self, w: &mut W) -> io::Result<()> {
        w.write_all(b": ")
    }
}

fn write_python_json<W: Write>(w: &mut W, v: &Value) -> io::Result<()> {
    let mut ser = serde_json::Serializer::with_formatter(w, PythonJsonFormatter);
    serde::Serialize::serialize(v, &mut ser).map_err(io::Error::other)
}

/// Format a value matching Python's `str()` / repr conventions for grep parity.
///
/// - Top-level strings (passed through `fmt_top_level`) render unquoted, as
///   Python's `print(f"{k}: {v}")` does for `str(v)`.
/// - Nested strings render single-quoted (Python repr).
/// - Bools → `True`/`False`; null → `None`.
/// - Lists / dicts → `[a, b]`, `{'k': v}` with the spacing Python uses.
fn fmt_top_level(v: &Value) -> String {
    match v {
        Value::String(s) => s.clone(),
        _ => fmt_nested(v),
    }
}

fn fmt_nested(v: &Value) -> String {
    let mut out = String::new();
    write_nested(&mut out, v);
    out
}

fn write_nested(out: &mut String, v: &Value) {
    match v {
        Value::Null => out.push_str("None"),
        Value::Bool(true) => out.push_str("True"),
        Value::Bool(false) => out.push_str("False"),
        Value::Number(n) => {
            let _ = write!(out, "{n}");
        }
        Value::String(s) => write_python_str(out, s),
        Value::Array(items) => {
            out.push('[');
            for (i, item) in items.iter().enumerate() {
                if i > 0 {
                    out.push_str(", ");
                }
                write_nested(out, item);
            }
            out.push(']');
        }
        Value::Object(map) => {
            out.push('{');
            for (i, (k, val)) in map.iter().enumerate() {
                if i > 0 {
                    out.push_str(", ");
                }
                write_python_str(out, k);
                out.push_str(": ");
                write_nested(out, val);
            }
            out.push('}');
        }
    }
}

/// Single-quote a string Python-style, escaping `\` and `'`.
fn write_python_str(out: &mut String, s: &str) {
    out.push('\'');
    for c in s.chars() {
        match c {
            '\\' => out.push_str("\\\\"),
            '\'' => out.push_str("\\'"),
            _ => out.push(c),
        }
    }
    out.push('\'');
}

/// Print `resp` to stdout. Returns the process exit code (0 ok, 1 daemon error).
pub fn emit(resp: &Value, as_json: bool) -> i32 {
    let stdout = std::io::stdout();
    let mut out = stdout.lock();
    if as_json {
        // Match Python `json.dump(resp, sys.stdout)` (default separators) + newline.
        let _ = write_python_json(&mut out, resp);
        let _ = out.write_all(b"\n");
    } else if resp.get("ok").and_then(Value::as_bool).unwrap_or(false) {
        match resp.get("result") {
            Some(Value::Object(map)) if !map.is_empty() => {
                for (k, v) in map {
                    let _ = writeln!(out, "{k}: {}", fmt_top_level(v));
                }
            }
            _ => {
                let _ = writeln!(out, "ok");
            }
        }
    } else {
        let err = resp
            .get("error")
            .and_then(Value::as_str)
            .map(str::to_string)
            .unwrap_or_else(|| "None".to_string());
        let _ = writeln!(std::io::stderr(), "error: {err}");
    }
    if resp.get("ok").and_then(Value::as_bool).unwrap_or(false) {
        0
    } else {
        1
    }
}

/// Print a hard failure (transport error, CLI parse error) in the requested format
/// and return exit code 2. Matches the Python `main()` exception branch.
pub fn emit_fatal(kind: &str, msg: &str, as_json: bool) -> i32 {
    if as_json {
        let payload = serde_json::json!({"ok": false, "error": format!("{kind}: {msg}")});
        let stdout = std::io::stdout();
        let mut out = stdout.lock();
        let _ = write_python_json(&mut out, &payload);
        let _ = out.write_all(b"\n");
    } else {
        eprintln!("error: {msg}");
    }
    2
}
