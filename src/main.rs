//! Shellshare - Live terminal broadcasting
//!
//! A Rust implementation of shellshare with both server and client functionality.
//!
//! Usage:
//! - `shellshare server` - Run the broadcasting server
//! - `shellshare` - Run the client (share your terminal)

mod cli;
mod server;

use clap::{Parser, Subcommand};
use tracing::Level;
use tracing_subscriber::FmtSubscriber;

/// Shellshare - Live terminal broadcasting
#[derive(Parser)]
#[command(name = "shellshare")]
#[command(author, version, about = "Live terminal broadcasting")]
#[command(long_about = "Share your terminal session in real-time.\n\n\
    Run without arguments to share your terminal.\n\
    Run with 'server' subcommand to start the broadcasting server.")]
#[command(disable_version_flag = true)]
struct Cli {
    /// Print version
    #[arg(short = 'v', short_alias = 'V', long, action = clap::ArgAction::Version)]
    version: (),
    #[command(subcommand)]
    command: Option<Commands>,

    /// Server URL to connect to
    #[arg(short, long, default_value = "https://shellshare.net", global = true)]
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
    },
}

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let cli = Cli::parse();

    match cli.command {
        Some(Commands::Server { host, port }) => {
            // Initialize logging for server
            let subscriber = FmtSubscriber::builder()
                .with_max_level(Level::INFO)
                .finish();
            tracing::subscriber::set_global_default(subscriber)?;

            // Create runtime only for server mode
            let runtime = tokio::runtime::Runtime::new()?;
            runtime.block_on(server::run(&host, port))?;
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
