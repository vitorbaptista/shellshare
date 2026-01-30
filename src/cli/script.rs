//! Script mode for shellshare CLI
//!
//! Spawns a PTY and streams output to the server, similar to the `script` command.

use crate::cli::encoding;
use crate::cli::http;
use crate::cli::terminal::{self, TerminalSize};

use portable_pty::{native_pty_system, CommandBuilder, PtySize};
use std::io::{Read, Write};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::thread;
use std::time::Duration;

/// Run script mode - spawn a shell in a PTY and stream output to server
pub fn run_script_mode(
    client: &http::Client,
    running: &Arc<AtomicBool>,
) -> Result<(), Box<dyn std::error::Error>> {
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
    let running_reader = running.clone();

    // Spawn a thread to read from PTY and stream to server
    let stream_thread = thread::spawn(move || {
        let mut buffer = [0u8; 4096];

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
    let stdin_thread = thread::spawn(move || {
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

    // Wait for the child process to exit
    let _exit_status = child.wait()?;

    // Signal threads to stop
    running.store(false, Ordering::SeqCst);
    running_reader.store(false, Ordering::SeqCst);

    // Wait for stream thread (with timeout via join)
    let _ = stream_thread.join();

    // stdin_thread may block on read, so we can't reliably wait for it
    // Just let it be when the process exits
    drop(stdin_thread);

    // Note: On Windows, we need to keep pair.slave alive until after child exits
    // (see portable-pty issue #4206). This is handled automatically since we
    // don't drop `pair` until here.
    drop(pair);

    Ok(())
}
