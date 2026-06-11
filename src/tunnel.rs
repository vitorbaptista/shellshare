//! Public URLs for the local server via Cloudflare quick tunnels.
//!
//! `--tunnel` spawns the user's `cloudflared` against the local server and
//! waits for the `https://*.trycloudflare.com` URL it prints. cloudflared
//! is a hard requirement on purpose: downloading or bundling it ourselves
//! would mean running a binary the user never chose to install.

#![allow(unsafe_code)] // pre_exec is the only way to set PR_SET_PDEATHSIG on the child

use std::io::{BufRead, BufReader, ErrorKind};
use std::process::{Child, Command, Stdio};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{mpsc, Arc};
use std::time::Duration;

/// How long cloudflared gets to print the tunnel URL before giving up
const URL_TIMEOUT: Duration = Duration::from_secs(30);

const NOT_INSTALLED: &str = "--tunnel requires cloudflared, which was not found in PATH.\n\
    Install it (macOS: brew install cloudflared) from:\n\
    https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/";

/// A quick tunnel owned by this process: dropping it kills cloudflared,
/// which closes the public URL with the session.
pub struct Tunnel {
    child: Child,
    /// Tells the stderr reader this exit is ours, so it stays quiet
    /// instead of warning that the tunnel died
    closing: Arc<AtomicBool>,
    /// Public base URL, e.g. `https://lamp-grew-firms.trycloudflare.com`
    pub url: String,
}

impl Drop for Tunnel {
    fn drop(&mut self) {
        self.closing.store(true, Ordering::SeqCst);
        let _ = self.child.kill();
        let _ = self.child.wait();
    }
}

/// Open a quick tunnel to the bound local address and return once the
/// public URL is known. Every failure is a `String` ready to show the
/// user: cloudflared missing, exiting early (its output is included), or
/// never printing a URL.
pub fn start(local_addr: std::net::SocketAddr) -> Result<Tunnel, String> {
    // A wildcard bind can't be dialed; point cloudflared at the
    // loopback of the same family instead
    let target = if local_addr.ip().is_unspecified() {
        let loopback: std::net::IpAddr = if local_addr.is_ipv4() {
            std::net::Ipv4Addr::LOCALHOST.into()
        } else {
            std::net::Ipv6Addr::LOCALHOST.into()
        };
        std::net::SocketAddr::new(loopback, local_addr.port())
    } else {
        local_addr
    };

    let mut command = Command::new("cloudflared");
    command
        .args([
            "tunnel",
            "--url",
            // SocketAddr's Display brackets IPv6 addresses as URLs need
            &format!("http://{target}"),
            "--no-autoupdate",
        ])
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::piped());

    // Drop's kill only runs on orderly exits; a SIGTERM/SIGKILL would
    // orphan cloudflared with the public URL still up. On Linux the
    // kernel can deliver the kill for us when the parent dies (macOS
    // has no pdeathsig equivalent; there `server --tunnel` relies on
    // orderly shutdown alone).
    // SAFETY: prctl with PR_SET_PDEATHSIG is async-signal-safe and the
    // closure touches nothing else, as pre_exec requires post-fork
    #[cfg(target_os = "linux")]
    unsafe {
        use std::os::unix::process::CommandExt;
        command.pre_exec(|| {
            libc::prctl(libc::PR_SET_PDEATHSIG, libc::SIGTERM);
            Ok(())
        });
    }

    let mut child = match command.spawn() {
        Ok(child) => child,
        Err(e) if e.kind() == ErrorKind::NotFound => return Err(NOT_INSTALLED.to_string()),
        Err(e) => return Err(format!("could not start cloudflared: {e}")),
    };

    let Some(stderr) = child.stderr.take() else {
        kill(&mut child);
        return Err("could not capture cloudflared's output".to_string());
    };

    // The reader thread outlives this function: after the URL is found it
    // keeps draining stderr so cloudflared never blocks on a full pipe
    let closing = Arc::new(AtomicBool::new(false));
    let closing_reader = closing.clone();
    let (tx, rx) = mpsc::channel();
    std::thread::spawn(move || {
        let mut startup_log = Vec::new();
        let mut found = false;
        for line in BufReader::new(stderr).lines() {
            let Ok(line) = line else { break };
            if found {
                continue;
            }
            if let Some(url) = extract_tunnel_url(&line) {
                found = true;
                let _ = tx.send(Ok(url));
            } else {
                startup_log.push(line);
            }
        }
        // Stderr EOF after startup means cloudflared died mid-session;
        // the terminal may be in raw mode, hence the carriage returns
        if found && !closing_reader.load(Ordering::SeqCst) {
            eprint!("\r\nWARNING: cloudflared exited; the public link no longer works\r\n");
        }
        if !found {
            let _ = tx.send(Err(startup_log.join("\n")));
        }
    });

    match rx.recv_timeout(URL_TIMEOUT) {
        Ok(Ok(url)) => Ok(Tunnel {
            child,
            closing,
            url,
        }),
        Ok(Err(output)) => {
            kill(&mut child);
            Err(format!(
                "cloudflared exited before reporting a tunnel URL. Its output:\n{output}"
            ))
        }
        Err(_) => {
            kill(&mut child);
            Err(format!(
                "cloudflared did not report a tunnel URL within {}s",
                URL_TIMEOUT.as_secs()
            ))
        }
    }
}

/// Pick the quick-tunnel URL out of cloudflared's startup banner, e.g.
/// `INF |  https://lamp-grew-firms.trycloudflare.com  |`
fn extract_tunnel_url(line: &str) -> Option<String> {
    line.split_whitespace()
        .find(|token| {
            token.starts_with("https://")
                && token.ends_with(".trycloudflare.com")
                // cloudflared logs its own API endpoint too; that one is
                // never the tunnel
                && !token.starts_with("https://api.")
        })
        .map(str::to_string)
}

fn kill(child: &mut Child) {
    let _ = child.kill();
    let _ = child.wait();
}
