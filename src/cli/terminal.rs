//! Terminal utilities for shellshare CLI
//!
//! Provides terminal size detection using direct ioctl calls via term_size crate.

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

/// Get the current terminal size using ioctl (TIOCGWINSZ) via term_size crate.
/// This is more reliable than spawning tput commands, especially during PTY setup.
pub fn get_terminal_size() -> TerminalSize {
    term_size::dimensions()
        .map(|(cols, rows)| TerminalSize {
            cols: cols as u16,
            rows: rows as u16,
        })
        .unwrap_or_default()
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
