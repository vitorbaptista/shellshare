//! Script mode for shellshare CLI
//!
//! Spawns a PTY and streams output to the server, similar to the `script` command.
//! Output is displayed locally AND sent to the server for remote viewing.

use crate::cli::encoding;
use crate::cli::http;
use crate::cli::terminal::{self, TerminalSize};

use portable_pty::{native_pty_system, CommandBuilder, PtySize};
use std::io::{Read, Write};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::thread;
use std::time::Duration;

/// RAII guard to enable/restore terminal raw mode.
/// This is essential for interactive apps like vim to work properly.
///
/// The key insight: `portable_pty` handles the PTY (slave side), but we also need
/// to configure the **user's terminal** (master side) to be in raw mode for proper
/// character-by-character input and escape sequence handling.
#[cfg(unix)]
struct RawModeGuard {
    original: Option<libc::termios>,
}

#[cfg(unix)]
impl RawModeGuard {
    fn new() -> Self {
        unsafe {
            // Check if stdin is a TTY
            if libc::isatty(libc::STDIN_FILENO) == 0 {
                return Self { original: None };
            }

            // Save original terminal settings
            let mut original: libc::termios = std::mem::zeroed();
            if libc::tcgetattr(libc::STDIN_FILENO, &mut original) != 0 {
                return Self { original: None };
            }

            // Create raw mode settings
            let mut raw = original;
            libc::cfmakeraw(&mut raw);

            // Apply raw mode
            if libc::tcsetattr(libc::STDIN_FILENO, libc::TCSANOW, &raw) != 0 {
                return Self { original: None };
            }

            Self { original: Some(original) }
        }
    }
}

#[cfg(unix)]
impl Drop for RawModeGuard {
    fn drop(&mut self) {
        if let Some(ref original) = self.original {
            unsafe {
                libc::tcsetattr(libc::STDIN_FILENO, libc::TCSANOW, original);
            }
        }
    }
}

// Windows: ConPTY handles raw mode automatically
#[cfg(windows)]
struct RawModeGuard;

#[cfg(windows)]
impl RawModeGuard {
    fn new() -> Self { Self }
}

/// Run script mode - spawn a shell in a PTY and stream output to server
pub fn run_script_mode(
    client: &http::Client,
    running: &Arc<AtomicBool>,
) -> Result<(), Box<dyn std::error::Error>> {
    // Enable raw mode BEFORE spawning shell
    // This allows character-by-character input and proper escape sequence handling
    // for interactive TUI apps like vim, less, htop, etc.
    let _raw_guard = RawModeGuard::new();

    // Get the user's shell
    let shell = std::env::var("SHELL").unwrap_or_else(|_| "/bin/sh".to_string());

    // Get terminal size
    let size = terminal::get_terminal_size();
    let pty_size = PtySize {
        rows: size.rows,
        cols: size.cols,
        pixel_width: 0,
        pixel_height: 0,
    };

    // Create PTY system
    let pty_system = native_pty_system();

    // Open a PTY pair
    let pair = pty_system.openpty(pty_size)?;

    // Build command to spawn the shell
    let cmd = CommandBuilder::new(&shell);

    // Spawn the shell in the PTY
    let mut child = pair.slave.spawn_command(cmd)?;

    // Get a reader for the PTY master
    let mut reader = pair.master.try_clone_reader()?;

    // Get a writer for the PTY master (for forwarding stdin)
    let mut writer = pair.master.take_writer()?;

    // Clone client for the streaming thread
    let client_clone = client.clone();
    let running_clone = running.clone();

    // Spawn a thread to read from PTY, display locally, and stream to server
    let stream_thread = thread::spawn(move || {
        let mut buffer = [0u8; 4096];
        let mut stdout = std::io::stdout();

        loop {
            if !running_clone.load(Ordering::SeqCst) {
                break;
            }

            match reader.read(&mut buffer) {
                Ok(0) => {
                    // EOF
                    break;
                }
                Ok(n) => {
                    let data = &buffer[..n];

                    // Write to local stdout so user sees their terminal
                    if stdout.write_all(data).is_err() {
                        break;
                    }
                    let _ = stdout.flush();

                    // Encode and send to server
                    let encoded = encoding::encode_message(data);
                    let size = TerminalSize {
                        cols: pty_size.cols,
                        rows: pty_size.rows,
                    };

                    if let Err(e) = client_clone.post_message(&encoded, size) {
                        eprintln!("\r\nERROR: {}", e);
                        eprintln!("\rERROR: Exit shellshare and try again later.");
                        break;
                    }
                }
                Err(e) => {
                    // Check if it's a would-block error (non-blocking IO)
                    if e.kind() == std::io::ErrorKind::WouldBlock {
                        thread::sleep(Duration::from_millis(10));
                        continue;
                    }
                    // Other errors - likely the PTY closed
                    break;
                }
            }
        }
    });

    // Clone running flag for stdin thread
    let running_stdin = running.clone();

    // Spawn a thread to forward stdin to the PTY
    let _stdin_thread = thread::spawn(move || {
        let stdin = std::io::stdin();
        let mut stdin_lock = stdin.lock();
        let mut buffer = [0u8; 1024];

        loop {
            if !running_stdin.load(Ordering::SeqCst) {
                break;
            }

            // Use read with a small buffer to be responsive
            match stdin_lock.read(&mut buffer) {
                Ok(0) => break, // EOF
                Ok(n) => {
                    if writer.write_all(&buffer[..n]).is_err() {
                        break;
                    }
                    // Flush to ensure data reaches the PTY immediately
                    if writer.flush().is_err() {
                        break;
                    }
                }
                Err(e) => {
                    if e.kind() == std::io::ErrorKind::WouldBlock {
                        thread::sleep(Duration::from_millis(10));
                        continue;
                    }
                    break;
                }
            }
        }
    });

    // Poll child status instead of blocking, so we can respond to Ctrl+C
    loop {
        // Check if Ctrl+C was pressed
        if !running.load(Ordering::SeqCst) {
            // Kill the child process
            let _ = child.kill();
            break;
        }

        // Check if child has exited (non-blocking)
        match child.try_wait() {
            Ok(Some(_status)) => {
                // Child exited
                break;
            }
            Ok(None) => {
                // Child still running, sleep briefly and check again
                thread::sleep(Duration::from_millis(50));
            }
            Err(_) => {
                // Error checking status, assume child died
                break;
            }
        }
    }

    // Signal threads to stop
    running.store(false, Ordering::SeqCst);

    // Wait for stream thread
    let _ = stream_thread.join();

    // Note: On Windows, we need to keep pair.slave alive until after child exits
    // (see portable-pty issue #4206). This is handled automatically since we
    // don't drop `pair` until here.
    drop(pair);

    Ok(())
}
