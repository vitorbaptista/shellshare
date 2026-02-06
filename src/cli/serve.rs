//! Serve mode - combined server and client in a single command
//!
//! Starts a local shellshare server and immediately connects to it,
//! providing a single command to share your terminal via a local URL.

use super::{http, script, terminal};

use std::net::TcpStream;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::time::Duration;

/// Configuration for the serve command
pub struct ServeArgs {
    pub host: String,
    pub port: u16,
    pub stdin: bool,
}

/// Fixed room name for serve mode (only one room needed)
const ROOM_NAME: &str = "terminal";

/// Run shellshare in serve mode: start a local server and stream to it
pub fn run(args: &ServeArgs) -> Result<(), Box<dyn std::error::Error>> {
    // Start a Tokio runtime for the async server
    let runtime = tokio::runtime::Runtime::new()?;

    // Spawn the server in the background
    let server_host = args.host.clone();
    let server_port = args.port;
    runtime.spawn(async move {
        if let Err(e) = crate::server::run(&server_host, server_port, 3600, 21600).await {
            eprintln!("Server error: {e}");
        }
    });

    // Wait for the server to accept connections
    let connect_host = connectable_host(&args.host);
    wait_for_server(connect_host, args.port)?;

    // Build the display URL
    let display_host = display_host(&args.host);
    let base_url = format!("http://{display_host}:{}", args.port);

    // Create HTTP client pointing to the local server
    let server_url = format!("http://{connect_host}:{}", args.port);
    let room_path = format!("r/{ROOM_NAME}");
    let password = generate_internal_password();
    let client = http::Client::new(&server_url, &room_path, &password)?;

    // Setup Ctrl+C handler for cleanup
    let running = Arc::new(AtomicBool::new(true));
    let running_clone = running.clone();
    let client_for_cleanup = client.clone();

    ctrlc::set_handler(move || {
        running_clone.store(false, Ordering::SeqCst);
        let _ = client_for_cleanup.delete_room();
    })?;

    if args.stdin {
        // Stdin mode - print to stderr (same as regular client stdin mode)
        eprintln!("Sharing terminal in {base_url}/r/{ROOM_NAME}");

        super::stream_stdin(&client, &running)?;

        let _ = client.delete_room();
        eprintln!("End of transmission.");
    } else {
        // Script mode (PTY) - print to stdout
        let size = terminal::get_terminal_size();
        if size.rows > 30 || size.cols > 160 {
            println!("Current terminal size is {}x{}.", size.rows, size.cols);
            println!("It's too big to be viewed on smaller screens.");
            println!("You can resize it anytime.");
        }

        println!("Sharing terminal in {base_url}/r/{ROOM_NAME}");

        script::run_script_mode(&client, &running)?;

        let _ = client.delete_room();
        println!("End of transmission.");
    }

    // Shut down the runtime without blocking
    runtime.shutdown_background();

    Ok(())
}

/// Generate a random password for internal client-server auth.
/// Uses a random value since the server may be exposed on the network.
fn generate_internal_password() -> String {
    use rand::Rng;
    rand::thread_rng().gen::<u64>().to_string()
}

/// Get a connectable host address from the bind address.
/// `0.0.0.0` isn't connectable on all platforms, so map it to `127.0.0.1`.
fn connectable_host(host: &str) -> &str {
    if host == "0.0.0.0" {
        "127.0.0.1"
    } else {
        host
    }
}

/// Get a human-friendly display host for URLs.
fn display_host(host: &str) -> &str {
    if host == "0.0.0.0" || host == "127.0.0.1" {
        "localhost"
    } else {
        host
    }
}

/// Wait for the server to be ready by attempting TCP connections.
fn wait_for_server(host: &str, port: u16) -> Result<(), Box<dyn std::error::Error>> {
    let addr = format!("{host}:{port}");

    for _ in 0..50 {
        if TcpStream::connect(&addr).is_ok() {
            return Ok(());
        }
        std::thread::sleep(Duration::from_millis(100));
    }

    Err(format!("Server failed to start (could not connect to {addr})").into())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_connectable_host_maps_wildcard() {
        assert_eq!(connectable_host("0.0.0.0"), "127.0.0.1");
    }

    #[test]
    fn test_connectable_host_preserves_specific() {
        assert_eq!(connectable_host("127.0.0.1"), "127.0.0.1");
        assert_eq!(connectable_host("192.168.1.1"), "192.168.1.1");
    }

    #[test]
    fn test_display_host_localhost_variants() {
        assert_eq!(display_host("0.0.0.0"), "localhost");
        assert_eq!(display_host("127.0.0.1"), "localhost");
    }

    #[test]
    fn test_display_host_preserves_specific() {
        assert_eq!(display_host("192.168.1.1"), "192.168.1.1");
        assert_eq!(display_host("10.0.0.1"), "10.0.0.1");
    }

    #[test]
    fn test_generate_internal_password_is_nonempty() {
        let pw = generate_internal_password();
        assert!(!pw.is_empty());
    }

    #[test]
    fn test_generate_internal_password_is_numeric() {
        let pw = generate_internal_password();
        assert!(pw.chars().all(|c| c.is_ascii_digit()));
    }

    #[test]
    fn test_wait_for_server_fails_on_closed_port() {
        // Port 1 is almost certainly not listening
        let result = wait_for_server("127.0.0.1", 1);
        assert!(result.is_err());
    }
}
