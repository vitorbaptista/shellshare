//! Script mode for shellshare CLI
//!
//! Spawns a PTY and streams output to the server, similar to the `script` command.
//! Output is displayed locally AND sent to the server for remote viewing.

#![allow(unsafe_code)] // PTY handling requires unsafe for terminal control

use crate::cli::http;
use crate::cli::get_terminal_size;
use crate::protocol::{EncodedMessage, TermSize};

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

/// Outcome of one attempt to pull data from the PTY-output channel,
/// unifying `try_recv` and `recv_timeout` results for the sender loop.
enum Recv {
    Data(Vec<u8>),
    Empty,
    Closed,
}

/// Run script mode - spawn a shell in a PTY and stream output to server
#[allow(clippy::too_many_lines)] // Complex PTY setup with multiple threads
pub fn run_script_mode(
    client: &http::Client,
    running: &Arc<AtomicBool>,
) -> Result<(), Box<dyn std::error::Error>> {
    // Enable raw mode BEFORE spawning shell
    // This allows character-by-character input and proper escape sequence handling
    // for interactive TUI apps like vim, less, htop, etc.
    let _raw_guard = RawModeGuard::new();

    // Get the user's shell (platform-specific)
    #[cfg(unix)]
    let shell = std::env::var("SHELL").unwrap_or_else(|_| "/bin/sh".to_string());

    #[cfg(windows)]
    let shell = std::env::var("COMSPEC").unwrap_or_else(|_| "cmd.exe".to_string());

    // Get terminal size
    let size = get_terminal_size();
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

    // Build command to spawn the shell, preserving current working directory
    let mut cmd = CommandBuilder::new(&shell);
    if let Ok(cwd) = std::env::current_dir() {
        cmd.cwd(cwd);
    }

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
            if let Ok(mut signals) = Signals::new([SIGWINCH]) {
                for _ in signals.forever() {
                    if !running_sigwinch.load(Ordering::SeqCst) {
                        break;
                    }
                    // Get new terminal size
                    let new_size = get_terminal_size();
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

    // Channel for sending PTY output to HTTP sender thread (non-blocking)
    let (tx, rx) = mpsc::channel::<Vec<u8>>();

    // Clone for threads
    let client_clone = client.clone();
    let running_clone = running.clone();
    let running_http = running.clone();

    // Move atomics to HTTP thread (last use of these Arcs)
    let http_cols = current_cols;
    let http_rows = current_rows;

    // Spawn HTTP sender thread - handles all network I/O separately
    // This ensures network latency never blocks terminal display
    let http_thread = thread::spawn(move || {
        // Latency-first sending: output is flushed as soon as it appears,
        // with MIN_SEND_INTERVAL pacing the request rate. A chatty PTY
        // batches naturally - whatever accumulates while pacing (or while
        // the previous request is in flight) goes out as one request.
        const MIN_SEND_INTERVAL: Duration = Duration::from_millis(15);
        // Per-request payload cap. Worst-case wire expansion is 4x (3x
        // percent-encoding, then 4/3 Base64), so 64KB raw stays under the
        // server's 300KB body limit no matter what the bytes are.
        const MAX_BUFFER_SIZE: usize = 64 * 1024;

        let mut send_buffer: Vec<u8> = Vec::with_capacity(8192);
        let mut last_send: Option<Instant> = None;
        let mut disconnected = false;

        // Encode and POST the first MAX_BUFFER_SIZE bytes of the buffer,
        // keeping any remainder for the next request. The cap lives here,
        // not in the receive loop, so no burst of channel chunks can push
        // a request over the server's body limit.
        let send_chunk = |send_buffer: &mut Vec<u8>| -> Result<(), http::HttpError> {
            let send_len = send_buffer.len().min(MAX_BUFFER_SIZE);
            let encoded = EncodedMessage::encode(&send_buffer[..send_len]);
            let size = TermSize {
                cols: http_cols.load(Ordering::SeqCst),
                rows: http_rows.load(Ordering::SeqCst),
            };
            client_clone.post_message(&encoded, size)?;
            send_buffer.drain(..send_len);
            Ok(())
        };

        while !disconnected && running_http.load(Ordering::SeqCst) {
            // Wait for the first chunk
            if send_buffer.is_empty() {
                match rx.recv_timeout(Duration::from_millis(50)) {
                    Ok(data) => send_buffer.extend(data),
                    Err(mpsc::RecvTimeoutError::Timeout) => continue,
                    Err(mpsc::RecvTimeoutError::Disconnected) => {
                        disconnected = true;
                        break;
                    }
                }
            }

            // Honor pacing, batching anything that arrives while waiting;
            // then drain whatever else is already queued. A full buffer
            // sends immediately - the backlog must not grow unbounded.
            while send_buffer.len() < MAX_BUFFER_SIZE {
                let wait = last_send
                    .map_or(Duration::ZERO, |t| MIN_SEND_INTERVAL.saturating_sub(t.elapsed()));
                let received = if wait.is_zero() {
                    match rx.try_recv() {
                        Ok(data) => Recv::Data(data),
                        Err(mpsc::TryRecvError::Empty) => Recv::Empty,
                        Err(mpsc::TryRecvError::Disconnected) => Recv::Closed,
                    }
                } else {
                    match rx.recv_timeout(wait) {
                        Ok(data) => Recv::Data(data),
                        Err(mpsc::RecvTimeoutError::Timeout) => Recv::Empty,
                        Err(mpsc::RecvTimeoutError::Disconnected) => Recv::Closed,
                    }
                };
                match received {
                    Recv::Data(data) => send_buffer.extend(data),
                    // Pacing satisfied and nothing queued: send now
                    Recv::Empty if wait.is_zero() => break,
                    // Pacing wait elapsed: loop once more to drain the queue
                    Recv::Empty => {}
                    Recv::Closed => {
                        disconnected = true;
                        break;
                    }
                }
            }

            if let Err(e) = send_chunk(&mut send_buffer) {
                eprintln!("\r\nERROR: {e}");
                eprintln!("\rERROR: Exit shellshare and try again later.");
                running_http.store(false, Ordering::SeqCst);
                return;
            }
            last_send = Some(Instant::now());
        }

        // Channel closed: flush whatever is left
        if disconnected {
            while !send_buffer.is_empty() && send_chunk(&mut send_buffer).is_ok() {}
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
        const CTRLC_BYTE: u8 = 0x03;
        const DOUBLE_CTRLC_WINDOW: Duration = Duration::from_millis(500);

        let stdin = std::io::stdin();
        let mut stdin_lock = stdin.lock();
        let mut buffer = [0u8; 1024];
        let mut last_ctrlc: Option<Instant> = None;

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
            // Kill the child process and reap it (bounded), so the slave
            // is only dropped after the child is actually gone
            let _ = child.kill();
            for _ in 0..40 {
                match child.try_wait() {
                    Ok(None) => thread::sleep(Duration::from_millis(50)),
                    Ok(Some(_)) | Err(_) => break,
                }
            }
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

    // Close our copy of the slave BEFORE joining the reader thread.
    // The child has exited (or been killed and reaped, bounded) in every
    // path that reaches here, so this is safe on Windows too (portable-pty
    // only requires the slave to outlive the child). Without this, the
    // master never sees EOF and the PTY reader thread can block in read()
    // forever, hanging the join below.
    drop(pair.slave);

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

    Ok(())
}
