//! Script mode for shellshare CLI
//!
//! Spawns a PTY and streams output to the server, similar to the `script` command.
//! Output is displayed locally AND sent to the server for remote viewing.

use crate::cli::encoding;
use crate::cli::http;
use crate::cli::terminal::{self, TerminalSize};

use portable_pty::{native_pty_system, CommandBuilder, PtySize};
use std::io::{Read, Write};
use std::sync::atomic::{AtomicBool, AtomicU16, Ordering};
use std::sync::mpsc;
use std::sync::Arc;
use std::thread;
use std::time::{Duration, Instant};

#[cfg(unix)]
use signal_hook::consts::SIGWINCH;
#[cfg(unix)]
use signal_hook::iterator::Signals;

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

    // Keep master reference for resize operations
    let master = pair.master;

    // Explicitly resize PTY after spawning to ensure correct dimensions
    master.resize(pty_size)?;

    // Get a reader for the PTY master
    let mut reader = master.try_clone_reader()?;

    // Get a writer for the PTY master (for forwarding stdin)
    let mut writer = master.take_writer()?;

    // Shared terminal size for HTTP thread (updated by SIGWINCH handler)
    let current_cols = Arc::new(AtomicU16::new(size.cols));
    let current_rows = Arc::new(AtomicU16::new(size.rows));

    // Channel for resize requests from SIGWINCH handler to main loop
    #[cfg(unix)]
    let (resize_tx, resize_rx) = mpsc::channel::<PtySize>();

    // Spawn SIGWINCH handler thread for dynamic terminal resize
    #[cfg(unix)]
    let sigwinch_thread = {
        let running_sigwinch = running.clone();
        let cols = current_cols.clone();
        let rows = current_rows.clone();

        let handle = thread::spawn(move || {
            if let Ok(mut signals) = Signals::new(&[SIGWINCH]) {
                for _ in signals.forever() {
                    if !running_sigwinch.load(Ordering::SeqCst) {
                        break;
                    }
                    // Get new terminal size
                    let new_size = terminal::get_terminal_size();
                    // Update shared size atomics
                    cols.store(new_size.cols, Ordering::SeqCst);
                    rows.store(new_size.rows, Ordering::SeqCst);
                    // Send resize request to main loop (which owns the master)
                    let _ = resize_tx.send(PtySize {
                        rows: new_size.rows,
                        cols: new_size.cols,
                        pixel_width: 0,
                        pixel_height: 0,
                    });
                }
            }
        });
        Some(handle)
    };

    #[cfg(not(unix))]
    let sigwinch_thread: Option<thread::JoinHandle<()>> = None;

    // Channel for sending PTY output to HTTP sender thread (non-blocking)
    let (tx, rx) = mpsc::channel::<Vec<u8>>();

    // Clone for threads
    let client_clone = client.clone();
    let running_clone = running.clone();
    let running_http = running.clone();

    // Clone atomics for HTTP thread
    let http_cols = current_cols.clone();
    let http_rows = current_rows.clone();

    // Spawn HTTP sender thread - handles all network I/O separately
    // This ensures network latency never blocks terminal display
    let http_thread = thread::spawn(move || {
        let mut send_buffer: Vec<u8> = Vec::with_capacity(8192);
        let mut last_send = Instant::now();
        const SEND_INTERVAL: Duration = Duration::from_millis(100);
        const MAX_BUFFER_SIZE: usize = 4096;

        loop {
            if !running_http.load(Ordering::SeqCst) {
                break;
            }

            // Try to receive data with timeout
            match rx.recv_timeout(Duration::from_millis(50)) {
                Ok(data) => {
                    send_buffer.extend(data);
                }
                Err(mpsc::RecvTimeoutError::Timeout) => {
                    // No data received, check if we should flush buffer
                }
                Err(mpsc::RecvTimeoutError::Disconnected) => {
                    // Channel closed, send remaining data and exit
                    if !send_buffer.is_empty() {
                        let encoded = encoding::encode_message(&send_buffer);
                        let size = TerminalSize {
                            cols: http_cols.load(Ordering::SeqCst),
                            rows: http_rows.load(Ordering::SeqCst),
                        };
                        let _ = client_clone.post_message(&encoded, size);
                    }
                    break;
                }
            }

            // Send if buffer is large enough OR enough time has passed
            let should_send = send_buffer.len() >= MAX_BUFFER_SIZE
                || (last_send.elapsed() >= SEND_INTERVAL && !send_buffer.is_empty());

            if should_send {
                let encoded = encoding::encode_message(&send_buffer);
                let size = TerminalSize {
                    cols: http_cols.load(Ordering::SeqCst),
                    rows: http_rows.load(Ordering::SeqCst),
                };

                if let Err(e) = client_clone.post_message(&encoded, size) {
                    eprintln!("\r\nERROR: {}", e);
                    eprintln!("\rERROR: Exit shellshare and try again later.");
                    running_http.store(false, Ordering::SeqCst);
                    break;
                }
                send_buffer.clear();
                last_send = Instant::now();
            }
        }
    });

    // Spawn PTY reader thread - reads from PTY, displays locally, sends to channel
    // This thread NEVER blocks on network I/O
    let stream_thread = thread::spawn(move || {
        let mut read_buffer = [0u8; 4096];
        let mut stdout = std::io::stdout();

        loop {
            if !running_clone.load(Ordering::SeqCst) {
                break;
            }

            match reader.read(&mut read_buffer) {
                Ok(0) => {
                    // EOF
                    break;
                }
                Ok(n) => {
                    let data = &read_buffer[..n];

                    // Write to local stdout IMMEDIATELY - never blocks on network
                    if stdout.write_all(data).is_err() {
                        break;
                    }
                    let _ = stdout.flush();

                    // Send to HTTP thread via channel (non-blocking)
                    // If channel is full/closed, we just drop the data for server
                    // (local display already happened)
                    let _ = tx.send(data.to_vec());
                }
                Err(e) => {
                    if e.kind() == std::io::ErrorKind::WouldBlock {
                        thread::sleep(Duration::from_millis(10));
                        continue;
                    }
                    // Other errors - likely the PTY closed
                    break;
                }
            }
        }

        // Drop sender to signal HTTP thread to flush and exit
        drop(tx);
    });

    // Clone running flag for stdin thread
    let running_stdin = running.clone();

    // Spawn a thread to forward stdin to the PTY
    // Also detects double Ctrl+C for force-quit (useful when shell is unresponsive)
    let _stdin_thread = thread::spawn(move || {
        let stdin = std::io::stdin();
        let mut stdin_lock = stdin.lock();
        let mut buffer = [0u8; 1024];
        let mut last_ctrlc: Option<Instant> = None;
        const CTRLC_BYTE: u8 = 0x03;
        const DOUBLE_CTRLC_WINDOW: Duration = Duration::from_millis(500);

        loop {
            if !running_stdin.load(Ordering::SeqCst) {
                break;
            }

            // Use read with a small buffer to be responsive
            match stdin_lock.read(&mut buffer) {
                Ok(0) => break, // EOF
                Ok(n) => {
                    let data = &buffer[..n];

                    // Check for Ctrl+C (0x03) for double-tap force quit
                    if data.contains(&CTRLC_BYTE) {
                        if let Some(last) = last_ctrlc {
                            if last.elapsed() < DOUBLE_CTRLC_WINDOW {
                                // Double Ctrl+C detected - force quit
                                running_stdin.store(false, Ordering::SeqCst);
                                break;
                            }
                        }
                        last_ctrlc = Some(Instant::now());
                    }

                    // Forward input to PTY (including single Ctrl+C for normal use)
                    if writer.write_all(data).is_err() {
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

    // Poll child status instead of blocking, so we can respond to Ctrl+C and resize requests
    loop {
        // Check if Ctrl+C was pressed
        if !running.load(Ordering::SeqCst) {
            // Kill the child process
            let _ = child.kill();
            break;
        }

        // Handle any pending resize requests (Unix only)
        #[cfg(unix)]
        while let Ok(new_size) = resize_rx.try_recv() {
            let _ = master.resize(new_size);
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

    // Wait for threads to finish
    let _ = stream_thread.join();
    let _ = http_thread.join();

    // Join SIGWINCH handler thread (Unix only)
    #[cfg(unix)]
    if let Some(handle) = sigwinch_thread {
        // Send SIGWINCH to ourselves to unblock the signal iterator
        unsafe {
            libc::raise(SIGWINCH);
        }
        let _ = handle.join();
    }

    // Note: On Windows, we need to keep pair.slave alive until after child exits
    // (see portable-pty issue #4206). This is handled automatically since we
    // don't drop `pair.slave` until here.
    drop(pair.slave);

    Ok(())
}
