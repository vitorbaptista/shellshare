//! Terminal utilities for shellshare CLI
//!
//! Provides terminal size detection using direct ioctl calls via `term_size` crate.

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

/// Get the current terminal size using ioctl (TIOCGWINSZ) via `term_size` crate.
/// This is more reliable than spawning tput commands, especially during PTY setup.
#[allow(clippy::cast_possible_truncation)] // Terminal dimensions always fit in u16
pub fn get_terminal_size() -> TerminalSize {
    term_size::dimensions()
        .map(|(cols, rows)| TerminalSize {
            cols: cols as u16,
            rows: rows as u16,
        })
        .unwrap_or_default()
}
