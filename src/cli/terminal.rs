//! Terminal utilities for shellshare CLI
//!
//! Provides terminal size detection matching the Python CLI's behavior.

use std::process::Command;

/// Terminal size in columns and rows
#[derive(Debug, Clone, Copy)]
pub struct TerminalSize {
    pub cols: u16,
    pub rows: u16,
}

impl Default for TerminalSize {
    fn default() -> Self {
        Self {
            cols: 80,
            rows: 24,
        }
    }
}

/// Get the current terminal size using tput (matching Python CLI behavior)
pub fn get_terminal_size() -> TerminalSize {
    let default = TerminalSize::default();

    // Try to get cols using tput
    let cols = Command::new("tput")
        .arg("cols")
        .output()
        .ok()
        .and_then(|output| {
            if output.status.success() {
                String::from_utf8_lossy(&output.stdout)
                    .trim()
                    .parse::<u16>()
                    .ok()
            } else {
                None
            }
        })
        .unwrap_or(default.cols);

    // Try to get rows using tput
    let rows = Command::new("tput")
        .arg("lines")
        .output()
        .ok()
        .and_then(|output| {
            if output.status.success() {
                String::from_utf8_lossy(&output.stdout)
                    .trim()
                    .parse::<u16>()
                    .ok()
            } else {
                None
            }
        })
        .unwrap_or(default.rows);

    TerminalSize { cols, rows }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_default_size() {
        let size = TerminalSize::default();
        assert_eq!(size.cols, 80);
        assert_eq!(size.rows, 24);
    }

    #[test]
    fn test_get_terminal_size_returns_valid() {
        let size = get_terminal_size();
        // Should return some positive values
        assert!(size.cols > 0);
        assert!(size.rows > 0);
    }
}
