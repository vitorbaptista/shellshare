//! Shellshare - Live terminal broadcasting
//!
//! A Rust implementation of shellshare with both server and client functionality.
//!
//! Usage:
//! - `shellshare server` - Run the broadcasting server
//! - `shellshare serve` - Share your terminal through a local server
//! - `shellshare` - Run the client (share your terminal)

mod cli;
mod protocol;
mod server;

use clap::{Parser, Subcommand};
use tracing::Level;
use tracing_subscriber::FmtSubscriber;

/// Default public server the client broadcasts to
const DEFAULT_SERVER_URL: &str = "https://shellshare.net";
/// How often the server sweeps for abandoned rooms
const DEFAULT_CLEANUP_INTERVAL_SECS: u64 = 3600;
/// How long a room may stay inactive before being removed
const DEFAULT_ROOM_TTL_SECS: u64 = 21600;

/// Shellshare - Live terminal broadcasting
#[derive(Parser)]
#[command(name = "shellshare")]
#[command(author, version, about = "Live terminal broadcasting")]
#[command(long_about = "Share your terminal session in real-time.\n\n\
    Run without arguments to share your terminal.\n\
    Run with 'serve' subcommand to share through a local server (no external server needed).\n\
    Run with 'server' subcommand to start the broadcasting server.")]
#[command(disable_version_flag = true)]
struct Cli {
    /// Print version
    #[arg(short = 'v', short_alias = 'V', long, action = clap::ArgAction::Version)]
    version: (),
    #[command(subcommand)]
    command: Option<Commands>,

    /// Server URL to connect to
    #[arg(short, long, default_value = DEFAULT_SERVER_URL, global = true)]
    server: String,

    /// Room name (default: random 18-char alphanumeric)
    #[arg(short, long, global = true)]
    room: Option<String>,

    /// Password for room authentication (default: MAC address as integer)
    #[arg(short = 'W', long, global = true)]
    password: Option<String>,

    /// Read from stdin instead of spawning a shell
    #[arg(long, global = true)]
    stdin: bool,
}

#[derive(Subcommand)]
enum Commands {
    /// Start the shellshare server
    Server {
        /// Host to bind to
        #[arg(short = 'H', long, default_value = "0.0.0.0")]
        host: String,

        /// Port to listen on
        #[arg(short, long, default_value = "3000", env = "PORT")]
        port: u16,

        /// Room cleanup interval in seconds (default: 3600 = 1 hour)
        #[arg(long, default_value_t = DEFAULT_CLEANUP_INTERVAL_SECS)]
        cleanup_interval: u64,

        /// Room TTL in seconds - rooms inactive for this long are removed (default: 21600 = 6 hours)
        #[arg(long, default_value_t = DEFAULT_ROOM_TTL_SECS)]
        room_ttl: u64,
    },
    /// Share your terminal through a local server (no external server needed)
    Serve {
        /// Host to bind the local server to
        #[arg(short = 'H', long, default_value = "localhost")]
        host: String,

        /// Port for the local server
        #[arg(short, long, default_value = "8000")]
        port: u16,
    },
}

/// Start the embedded server on a background thread and wait until it is
/// bound, so `shellshare serve` can report errors (e.g. port already in
/// use) before handing the terminal over to the client.
///
/// Returns the address actually bound: with `--port 0` the OS picks a free
/// port, and the client must broadcast to the real one.
fn start_local_server(host: &str, port: u16) -> Result<std::net::SocketAddr, String> {
    let (ready_tx, ready_rx) = std::sync::mpsc::channel();
    let bind_host = host.to_string();

    std::thread::spawn(move || {
        let runtime = match tokio::runtime::Runtime::new() {
            Ok(rt) => rt,
            Err(e) => {
                let _ = ready_tx.send(Err(e.to_string()));
                return;
            }
        };
        runtime.block_on(async {
            let listener = match server::bind(&bind_host, port).await {
                Ok(listener) => listener,
                Err(e) => {
                    let _ = ready_tx.send(Err(e.to_string()));
                    return;
                }
            };
            match listener.local_addr() {
                Ok(addr) => {
                    let _ = ready_tx.send(Ok(addr));
                }
                Err(e) => {
                    let _ = ready_tx.send(Err(e.to_string()));
                    return;
                }
            }
            if let Err(e) = server::serve_on(
                listener,
                DEFAULT_CLEANUP_INTERVAL_SECS,
                DEFAULT_ROOM_TTL_SECS,
            )
            .await
            {
                // The client likely holds the terminal in raw mode by now,
                // so carriage returns keep the message readable
                eprint!("\r\nERROR: local server stopped: {e}\r\n");
            }
        });
    });

    ready_rx
        .recv()
        .unwrap_or_else(|_| Err("server thread exited unexpectedly".to_string()))
        .map_err(|e| format!("could not start local server on {host}:{port}: {e}"))
}

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let cli = Cli::parse();

    match cli.command {
        Some(Commands::Server {
            host,
            port,
            cleanup_interval,
            room_ttl,
        }) => {
            // Initialize logging for server
            let subscriber = FmtSubscriber::builder()
                .with_max_level(Level::INFO)
                .finish();
            tracing::subscriber::set_global_default(subscriber)?;

            // Create runtime only for server mode
            let runtime = tokio::runtime::Runtime::new()?;
            runtime.block_on(server::run(&host, port, cleanup_interval, room_ttl))?;
        }
        Some(Commands::Serve { host, port }) => {
            if cli.server != DEFAULT_SERVER_URL {
                eprintln!("WARNING: --server is ignored by 'serve'; broadcasting to the local server");
            }
            if !matches!(host.as_str(), "localhost" | "127.0.0.1" | "::1") {
                eprintln!(
                    "WARNING: binding a non-loopback address; anyone who can reach this machine can view this terminal"
                );
            }

            let addr = match start_local_server(&host, port) {
                Ok(addr) => addr,
                Err(e) => {
                    eprintln!("ERROR: {e}");
                    std::process::exit(1);
                }
            };

            // Browsers can't reach wildcard addresses; point the client
            // (and the printed share link) at localhost instead. IPv6
            // hosts need brackets in URLs.
            let url_host = match host.as_str() {
                "0.0.0.0" | "::" => "localhost".to_string(),
                h if h.contains(':') => format!("[{h}]"),
                h => h.to_string(),
            };
            let args = cli::ClientArgs {
                server: format!("http://{url_host}:{}", addr.port()),
                room: cli.room,
                password: cli.password,
                stdin: cli.stdin,
            };
            cli::run(args)?;
        }
        None => {
            // Run client mode
            let args = cli::ClientArgs {
                server: cli.server,
                room: cli.room,
                password: cli.password,
                stdin: cli.stdin,
            };
            cli::run(args)?;
        }
    }

    Ok(())
}
