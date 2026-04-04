//! Serve mode - combined server and client in a single command
//!
//! Starts a local shellshare server and immediately connects to it,
//! providing a single command to share your terminal via a local URL.

use super::http;

use std::net::TcpStream;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::time::Duration;

use rand::Rng;

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
    let runtime = tokio::runtime::Runtime::new()?;

    // Spawn the server in the background
    let server_config = crate::server::ServerConfig {
        host: args.host.clone(),
        port: args.port,
        cleanup_interval_secs: 3600,
        room_ttl_secs: 21600,
        serve_room: Some(ROOM_NAME.to_string()),
    };
    runtime.spawn(async move {
        if let Err(e) = crate::server::run(&server_config).await {
            eprintln!("Server error: {e}");
        }
    });

    // Wait for the server to accept connections
    let connect_host = connectable_host(&args.host);
    wait_for_server(connect_host, args.port)?;

    // Build URLs
    let display_host = display_host(&args.host);
    let base_url = format!("http://{display_host}:{}", args.port);
    let server_url = format!("http://{connect_host}:{}", args.port);

    // Create HTTP client pointing to the local server
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

    // Stream (reuses the shared stdin/script logic from cli module)
    let result = super::stream_and_cleanup(&client, &running, args.stdin, &base_url);

    // Always shut down the runtime, even on error
    runtime.shutdown_background();

    result
}

/// Generate a random password for internal client-server auth.
fn generate_internal_password() -> String {
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
    fn test_wait_for_server_fails_on_unroutable_addr() {
        // Use a non-routable address with a short timeout to reliably test failure
        let result = wait_for_server("192.0.2.1", 1);
        assert!(result.is_err());
    }
}
