//! Shellshare CLI client - Share your terminal session
//!
//! This module provides the client functionality for streaming terminal
//! output to a shellshare server.

mod script;
mod ws;

use std::io::{self, Read};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;

use crate::protocol::TermSize;
use rand::Rng;

/// Get the current terminal size using ioctl (TIOCGWINSZ) via `term_size`.
/// Falls back to 80x24 when not attached to a terminal.
#[allow(clippy::cast_possible_truncation)] // Terminal dimensions always fit in u16
fn get_terminal_size() -> TermSize {
    term_size::dimensions()
        .map(|(cols, rows)| TermSize {
            cols: cols as u16,
            rows: rows as u16,
        })
        .unwrap_or_default()
}

/// Client configuration arguments
pub struct ClientArgs {
    pub server: String,
    pub room: Option<String>,
    pub password: Option<String>,
    pub stdin: bool,
}

/// Generate a random 18-character alphanumeric room ID
fn generate_room_id() -> String {
    const CHARSET: &[u8] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789";
    let mut rng = rand::thread_rng();

    (0..18)
        .map(|_| {
            let idx = rng.gen_range(0..CHARSET.len());
            CHARSET[idx] as char
        })
        .collect()
}

/// Get the default password (MAC address as integer, like Python's `uuid.getnode()`)
fn get_default_password() -> String {
    if let Ok(Some(mac)) = mac_address::get_mac_address() {
        // Convert MAC address bytes to an integer (like Python's uuid.getnode())
        let bytes = mac.bytes();
        let mut value: u64 = 0;
        for &byte in &bytes {
            value = (value << 8) | u64::from(byte);
        }
        value.to_string()
    } else {
        // Fallback to a random value if MAC address is unavailable
        let mut rng = rand::thread_rng();
        rng.gen::<u64>().to_string()
    }
}

/// Normalize server URL (ensure it has a scheme, strip trailing slashes)
fn normalize_server_url(server: &str) -> String {
    let server = if server.contains("://") {
        server.to_string()
    } else {
        format!("http://{server}")
    };
    server.trim_end_matches('/').to_string()
}

/// Run the shellshare client
pub fn run(args: ClientArgs) -> Result<(), Box<dyn std::error::Error>> {
    let server = normalize_server_url(&args.server);
    let room = args.room.unwrap_or_else(generate_room_id);
    let password = args.password.unwrap_or_else(get_default_password);

    // Build room path (r/{room})
    let room_path = format!("r/{room}");

    // Connecting claims the room (or fails on a password mismatch), so
    // a broadcast that can never work is reported before the terminal
    // is handed over to the shell
    let size = get_terminal_size();
    let transport = match ws::Transport::connect(&server, &room_path, &password, size) {
        Ok(transport) => transport,
        Err(e) => {
            eprintln!("ERROR: {e}");
            std::process::exit(1);
        }
    };

    // Ctrl+C only flips the flag; the sending thread owns the transport
    // and performs cleanup (flush, room deletion) when it stops
    let running = Arc::new(AtomicBool::new(true));
    let running_clone = running.clone();
    ctrlc::set_handler(move || {
        running_clone.store(false, Ordering::SeqCst);
    })?;

    if args.stdin {
        // Stdin mode - print to stderr
        eprintln!("Sharing terminal in {server}/{room_path}");

        // Read from stdin and stream to server
        stream_stdin(transport, &running)?;

        eprintln!("End of transmission.");
    } else {
        // Script mode - print to stdout
        if size.rows > 30 || size.cols > 160 {
            println!("Current terminal size is {}x{}.", size.rows, size.cols);
            println!("It's too big to be viewed on smaller screens.");
            println!("You can resize it anytime.");
        }

        println!("Sharing terminal in {server}/{room_path}");

        // Run script mode with PTY
        script::run_script_mode(transport, &running)?;

        println!("End of transmission.");
    }

    Ok(())
}

/// Stream stdin to the server (for testing)
fn stream_stdin(
    mut transport: ws::Transport,
    running: &Arc<AtomicBool>,
) -> Result<(), Box<dyn std::error::Error>> {
    let mut buffer = [0u8; 4096];

    loop {
        if !running.load(Ordering::SeqCst) {
            break;
        }

        let bytes_read = io::stdin().read(&mut buffer)?;
        if bytes_read == 0 {
            // EOF
            break;
        }

        let data = buffer[..bytes_read].to_vec();
        let size = get_terminal_size();

        if let Err(e) = transport.send(data, size) {
            eprintln!("\r\nERROR: {e}");
            eprintln!("\rERROR: Exit shellshare and try again later.");
            // The room belongs to someone else now; it is not ours to
            // delete, so skip the shutdown cleanup
            return Ok(());
        }
    }

    transport.shutdown_and_delete();
    Ok(())
}
