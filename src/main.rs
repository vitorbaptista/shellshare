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
mod themes;
mod tunnel;

use clap::{Parser, Subcommand};
use tracing::Level;
use tracing_subscriber::FmtSubscriber;

/// Default public server the client broadcasts to
const DEFAULT_SERVER_URL: &str = "https://shellshare.net";
/// Default port the server listens on, shared by `server` and `serve`
const DEFAULT_PORT: &str = "3000";
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

    /// Color theme viewers see the broadcast in
    // Validated at parse time, so a typo fails before the room is
    // claimed and the shell spawns.
    #[arg(short, long, global = true, default_value = "tango", value_parser = clap::builder::PossibleValuesParser::new(themes::names()))]
    theme: Option<String>,
}

#[derive(Subcommand)]
enum Commands {
    /// Start the shellshare server
    Server {
        /// Host to bind to
        #[arg(short = 'H', long, default_value = "0.0.0.0")]
        host: String,

        /// Port to listen on
        #[arg(short, long, default_value = DEFAULT_PORT, env = "PORT")]
        port: u16,

        /// Room cleanup interval in seconds (default: 3600 = 1 hour)
        #[arg(long, default_value_t = DEFAULT_CLEANUP_INTERVAL_SECS)]
        cleanup_interval: u64,

        /// Room TTL in seconds - rooms inactive for this long are removed (default: 21600 = 6 hours)
        #[arg(long, default_value_t = DEFAULT_ROOM_TTL_SECS)]
        room_ttl: u64,

        /// `PostHog` project API key. Usage analytics are sent only when
        /// this AND --posthog-salt are set; otherwise nothing is collected
        #[arg(long, env = "SHELLSHARE_POSTHOG_KEY")]
        posthog_key: Option<String>,

        /// `PostHog` ingestion host to send analytics events to
        #[arg(long, env = "SHELLSHARE_POSTHOG_HOST", default_value = server::DEFAULT_POSTHOG_HOST)]
        posthog_host: String,

        /// Secret salt for pseudonymizing analytics identifiers. Keep it
        /// stable across restarts and servers so returning users stay
        /// recognizable; rotating it resets all identities
        #[arg(long, env = "SHELLSHARE_POSTHOG_SALT")]
        posthog_salt: Option<String>,

        /// Expose the server publicly through a Cloudflare quick tunnel (requires cloudflared)
        #[arg(long)]
        tunnel: bool,
    },
    /// Share your terminal through a local server (no external server needed)
    Serve {
        /// Host to bind the local server to
        #[arg(short = 'H', long, default_value = "localhost")]
        host: String,

        /// Port for the local server
        #[arg(short, long, default_value = DEFAULT_PORT)]
        port: u16,

        /// Share a public link through a Cloudflare quick tunnel (requires cloudflared)
        #[arg(long)]
        tunnel: bool,
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
            let listeners = match server::bind(&bind_host, port).await {
                Ok(listeners) => listeners,
                Err(e) => {
                    let _ = ready_tx.send(Err(e.to_string()));
                    return;
                }
            };
            match listeners[0].local_addr() {
                Ok(addr) => {
                    let _ = ready_tx.send(Ok(addr));
                }
                Err(e) => {
                    let _ = ready_tx.send(Err(e.to_string()));
                    return;
                }
            }
            // The embedded server never sends analytics: it serves the
            // machine's own broadcast, not an operator's deployment
            if let Err(e) = server::serve_on(
                listeners,
                DEFAULT_CLEANUP_INTERVAL_SECS,
                DEFAULT_ROOM_TTL_SECS,
                None,
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

/// Build the analytics config from the server's PostHog flags.
///
/// Both the key and the salt are required; refusing a half-configured
/// setup beats silently degrading it (a random fallback salt would
/// reset every identity on restart and corrupt the recurring-user
/// metric invisibly), so each half-configured case warns and disables.
fn analytics_config(
    key: Option<String>,
    host: String,
    salt: Option<String>,
) -> Option<server::AnalyticsConfig> {
    match (key, salt) {
        (Some(api_key), Some(salt)) => Some(server::AnalyticsConfig {
            api_key,
            host,
            salt,
        }),
        (Some(_), None) => {
            tracing::warn!("Analytics disabled: --posthog-key is set but --posthog-salt is not");
            None
        }
        (None, Some(_)) => {
            tracing::warn!("Analytics disabled: --posthog-salt is set but --posthog-key is not");
            None
        }
        (None, None) => None,
    }
}

/// Print a client error the way the CLI always has and exit non-zero.
/// Call sites must drop any [`tunnel::Tunnel`] first: exiting skips
/// destructors, so a live handle would leak cloudflared.
fn exit_on_error(result: Result<(), Box<dyn std::error::Error>>) {
    if let Err(e) = result {
        eprintln!("ERROR: {e}");
        std::process::exit(1);
    }
}

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let cli = Cli::parse();

    match cli.command {
        Some(Commands::Server {
            host,
            port,
            cleanup_interval,
            room_ttl,
            posthog_key,
            posthog_host,
            posthog_salt,
            tunnel,
        }) => {
            // Initialize logging for server
            let subscriber = FmtSubscriber::builder()
                .with_max_level(Level::INFO)
                .finish();
            tracing::subscriber::set_global_default(subscriber)?;

            let analytics_config = analytics_config(posthog_key, posthog_host, posthog_salt);

            // Create runtime only for server mode
            let runtime = tokio::runtime::Runtime::new()?;
            runtime.block_on(async {
                let listeners = server::bind(&host, port).await?;
                // Bind before tunneling so cloudflared forwards to the
                // real port (`--port 0` lets the OS pick), and hold the
                // handle for the server's lifetime: dropping it would
                // kill cloudflared and the public URL with it
                let _tunnel = if tunnel {
                    let t = tunnel::start(listeners[0].local_addr()?)?;
                    tracing::info!("Tunnel ready: rooms are public at {}/r/<room>", t.url);
                    tracing::warn!(
                        "anyone with that URL can view rooms and broadcast their own \
                         through this server"
                    );
                    Some(t)
                } else {
                    None
                };
                server::serve_on(listeners, cleanup_interval, room_ttl, analytics_config).await
            })?;
        }
        Some(Commands::Serve { host, port, tunnel }) => {
            // No tracing subscriber on purpose: the embedded server's
            // logs would garble the shared terminal
            if cli.server.trim_end_matches('/') != DEFAULT_SERVER_URL {
                eprintln!(
                    "WARNING: --server is ignored by 'serve'; broadcasting to the local server"
                );
            }
            serve(&host, port, tunnel, cli.room, cli.password, cli.stdin, cli.theme);
        }
        None => {
            // Run client mode
            let args = cli::ClientArgs {
                server: cli.server,
                display_server: None,
                room: cli.room,
                password: cli.password,
                stdin: cli.stdin,
                theme: cli.theme,
            };
            exit_on_error(cli::run(args));
        }
    }

    Ok(())
}

/// `shellshare serve`: boot the embedded server, optionally tunnel it,
/// and broadcast this terminal to it.
fn serve(
    host: &str,
    port: u16,
    tunnel: bool,
    room: Option<String>,
    password: Option<String>,
    stdin: bool,
    theme: Option<String>,
) {
    let addr = match start_local_server(host, port) {
        Ok(addr) => addr,
        Err(e) => {
            eprintln!("ERROR: {e}");
            std::process::exit(1);
        }
    };

    // bind() accepts both "::1" and "[::1]"; strip brackets so
    // the loopback check and the URL see the same host
    let bare_host = host
        .strip_prefix('[')
        .and_then(|h| h.strip_suffix(']'))
        .unwrap_or(host);
    let parsed_ip = bare_host.parse::<std::net::IpAddr>().ok();
    let is_loopback =
        bare_host.eq_ignore_ascii_case("localhost") || parsed_ip.is_some_and(|ip| ip.is_loopback());
    let is_wildcard = parsed_ip.is_some_and(|ip| ip.is_unspecified());
    if !is_loopback {
        eprintln!(
            "WARNING: binding a non-loopback address; anyone who can reach this machine \
             can view this terminal and broadcast their own rooms on this server"
        );
        if is_wildcard {
            eprintln!(
                "Viewers on other machines must replace 'localhost' in the link below \
                 with this machine's address"
            );
        }
    }

    // Browsers can't reach wildcard addresses; point the client
    // (and the printed share link) at localhost instead. IPv6
    // hosts need brackets in URLs.
    let url_host = if is_wildcard {
        "localhost".to_string()
    } else if bare_host.contains(':') {
        format!("[{bare_host}]")
    } else {
        bare_host.to_string()
    };
    // The broadcaster keeps talking to the local server directly;
    // only the share link (and so the viewers) goes through the
    // tunnel. The handle must outlive the client: dropping it
    // kills cloudflared and the public URL with it
    let tunnel_handle = if tunnel {
        match tunnel::start(addr) {
            Ok(t) => {
                eprintln!(
                    "WARNING: anyone with the link below can view this terminal \
                     and broadcast their own rooms through this server"
                );
                Some(t)
            }
            Err(e) => {
                eprintln!("ERROR: {e}");
                std::process::exit(1);
            }
        }
    } else {
        None
    };

    // Connecting the client claims the room immediately, so
    // nobody can take the name between server start and the
    // first broadcast
    let args = cli::ClientArgs {
        server: format!("http://{url_host}:{}", addr.port()),
        display_server: tunnel_handle.as_ref().map(|t| t.url.clone()),
        room,
        password,
        stdin,
        theme,
    };
    let result = cli::run(args);
    // Close the tunnel before a possible exit: process::exit
    // skips destructors, which would leave cloudflared running
    // with the public URL pointing at a dead server
    drop(tunnel_handle);
    exit_on_error(result);
}
