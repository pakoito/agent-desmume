//! parse_addr: parity with Python's `int(s, 0)`.
//!
//! Accepts `0x`/`0X` (hex), `0o`/`0O` (octal), `0b`/`0B` (binary),
//! otherwise base-10. Underscores allowed (Python permits them since
//! 3.6). Leading/trailing whitespace tolerated; Python's `int()`
//! strips it.

use anyhow::{anyhow, Result};

pub fn parse_addr(s: &str) -> Result<u64> {
    let trimmed = s.trim();
    if trimmed.is_empty() {
        return Err(anyhow!("empty address"));
    }
    let (sign, rest) = match trimmed.as_bytes()[0] {
        b'-' => (-1i128, &trimmed[1..]),
        b'+' => (1, &trimmed[1..]),
        _ => (1, trimmed),
    };
    let (radix, digits) = if rest.len() >= 2 {
        match &rest.as_bytes()[..2] {
            b"0x" | b"0X" => (16, &rest[2..]),
            b"0o" | b"0O" => (8, &rest[2..]),
            b"0b" | b"0B" => (2, &rest[2..]),
            _ => (10, rest),
        }
    } else {
        (10, rest)
    };
    let cleaned: String = digits.chars().filter(|&c| c != '_').collect();
    if cleaned.is_empty() {
        return Err(anyhow!("no digits in address: {s:?}"));
    }
    let n = i128::from_str_radix(&cleaned, radix)
        .map_err(|e| anyhow!("invalid address {s:?}: {e}"))?;
    let signed = sign * n;
    // The Python daemon expects u64-ish values; allow the full range
    // including negative interpretations cast through two's complement
    // for `regs write pc=-1` style inputs.
    Ok(signed as u64)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn hex_lower() {
        assert_eq!(parse_addr("0x21a8c40").unwrap(), 0x021a_8c40);
    }
    #[test]
    fn hex_upper() {
        assert_eq!(parse_addr("0X1F").unwrap(), 31);
    }
    #[test]
    fn decimal() {
        assert_eq!(parse_addr("42").unwrap(), 42);
    }
    #[test]
    fn binary() {
        assert_eq!(parse_addr("0b1011").unwrap(), 0b1011);
    }
    #[test]
    fn octal() {
        assert_eq!(parse_addr("0o17").unwrap(), 0o17);
    }
    #[test]
    fn underscored() {
        assert_eq!(parse_addr("0x02_1a_8c40").unwrap(), 0x021a_8c40);
    }
    #[test]
    fn whitespace() {
        assert_eq!(parse_addr("  0x10 ").unwrap(), 16);
    }
    #[test]
    fn empty_rejected() {
        assert!(parse_addr("").is_err());
        assert!(parse_addr("   ").is_err());
        assert!(parse_addr("0x").is_err());
    }
    #[test]
    fn negative_wraps() {
        // Python's int("-1", 0) == -1; we cast through two's complement.
        assert_eq!(parse_addr("-1").unwrap(), u64::MAX);
    }
}
