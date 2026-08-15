//! WebSocket transport for the broadcasting client.
//!
//! Terminal output travels as binary frames of raw bytes; control
//! messages (`size`, `reset`) travel as JSON text frames. A session ends
//! by flushing and closing, not by deleting: the room and its history
//! stay on the server until it goes idle, so a short command still
//! leaves a working link. (The server still honors the `delete` frame
//! this client no longer sends - older binaries are still out there.)
//!
//! Reliability model: after the initial connect succeeds, `send` and
//! `tick` never fail on network errors. Output is held in a bounded
//! replay buffer until the server acknowledges it (`{"ack": n}`, a
//! cumulative per-connection byte count) - a TCP write succeeding proves
//! nothing once the connection dies. On any failure the connection is
//! re-established with backoff and everything unacknowledged replays,
//! giving at-least-once delivery. The only fatal error is authorization:
//! the room now belongs to another password.

use crate::cli::crypto::Encryptor;
use crate::cli::ThemeChoice;
use crate::cli::screen::Keyframer;
use crate::protocol::TermSize;
use serde_json::json;
use std::collections::VecDeque;
use std::io::ErrorKind;
use std::net::{SocketAddr, TcpStream, ToSocketAddrs};
use std::time::{Duration, Instant};
use tungstenite::client::{uri_mode, IntoClientRequest};
use tungstenite::handshake::HandshakeError;
use tungstenite::http::{header, HeaderValue, StatusCode, Uri};
use tungstenite::stream::{MaybeTlsStream, Mode};
use tungstenite::{Bytes, Error as WsError, Message, WebSocket};

/// Cap on data held for (re)delivery. When exceeded, the oldest chunks
/// are dropped: viewers may miss a window of output (just like a late
/// joiner does), but the broadcast survives the outage.
const MAX_BUFFERED_BYTES: usize = 1024 * 1024;
const INITIAL_BACKOFF: Duration = Duration::from_millis(250);
const MAX_BACKOFF: Duration = Duration::from_secs(5);
/// How long shutdown may wait for the final acknowledgments.
const SHUTDOWN_DRAIN: Duration = Duration::from_secs(1);
/// Total budget for the TCP dial, shared across every address the
/// server's hostname resolves to (see `dial`). Ten seconds is the same
/// bound as the write timeout below: long enough for a slow link or a
/// cold serverless backend, short enough that a black-holed address
/// becomes a retry rather than a wait.
const DIAL_TIMEOUT: Duration = Duration::from_secs(10);
/// Budget for the whole WebSocket upgrade once connected - TLS
/// handshake, request write and `101 Switching Protocols` response.
/// Also ten seconds: a server that has accepted the connection but
/// cannot finish the upgrade within it is not one worth waiting on, and
/// the reconnect backoff will try again anyway.
const HANDSHAKE_TIMEOUT: Duration = Duration::from_secs(10);
/// Ceiling on how often a stalled upgrade is retried while it waits.
const HANDSHAKE_POLL: Duration = Duration::from_millis(5);
/// Floor on what a single address gets out of `DIAL_TIMEOUT`, so a name
/// with many addresses cannot leave the first one - usually the one
/// that will answer - too little time to complete a TCP handshake over
/// a high-latency link.
const MIN_DIAL_ATTEMPT: Duration = Duration::from_secs(3);
/// Keepalive cadence. Pings refresh the room's activity server-side (so
/// an idle-but-attached broadcast isn't TTL-evicted), keep NAT mappings
/// alive, and turn a dead idle connection into a prompt reconnect.
const PING_INTERVAL: Duration = Duration::from_secs(30);

/// Errors a caller must act on. Network problems are not among them -
/// the transport absorbs those by buffering and reconnecting.
#[derive(Debug)]
pub enum TransportError {
    /// The room is claimed by another password.
    Unauthorized,
    /// The initial connection could not be established.
    Connect(String),
}

impl std::fmt::Display for TransportError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::Unauthorized => {
                write!(f, "You're not authorized to share on this room.")
            }
            Self::Connect(msg) => {
                write!(f, "There was an error connecting to the server: {msg}")
            }
        }
    }
}

impl std::error::Error for TransportError {}

type WsSocket = WebSocket<MaybeTlsStream<TcpStream>>;

/// A broadcasting connection to one room.
pub struct Transport {
    url: String,
    password: String,
    socket: Option<WsSocket>,
    /// Chunks not yet written on the current connection, oldest first
    pending: VecDeque<Bytes>,
    /// Chunks written on the current connection, awaiting their ack
    unacked: VecDeque<Bytes>,
    /// pending + unacked, for the buffer cap
    buffered_bytes: usize,
    /// Cumulative bytes the server confirmed on the current connection
    conn_acked: u64,
    /// Bytes dropped from the front of `unacked` by the buffer cap.
    /// Their acks still arrive and must be consumed BEFORE the queue
    /// walk, or `apply_ack` would release chunks that were never acked.
    dropped_unacked: u64,
    /// Terminal size the caller last reported
    size: TermSize,
    /// Size already delivered on the CURRENT connection, if any
    sent_size: Option<TermSize>,
    /// Viewer color theme, carried inside every size message (the
    /// server stores and forwards the size verbatim, so late joiners
    /// get the theme with no server-side knowledge of it)
    theme: ThemeChoice,
    /// End-to-end encryption, unless `--disable-encryption`. Chunks are
    /// sealed as they enter the replay buffer, so acks, the buffer cap,
    /// and reconnect replay all operate on ciphertext - a replayed
    /// record is byte-identical, never re-encrypted (no nonce reuse)
    cipher: Option<Encryptor>,
    /// Headless terminal emulator over the broadcast stream; periodically
    /// injects a full-screen redraw so late joiners reconstruct the whole
    /// screen of a long-running TUI (issue #164).
    keyframer: Keyframer,
    backoff: Duration,
    /// Do not attempt to reconnect before this instant
    next_attempt: Option<Instant>,
    /// When the last keepalive ping went out
    last_ping: Instant,
}

impl Transport {
    /// Connect to `{server_url}/ws/r/{room}`, claiming the room (or
    /// verifying its password) at the handshake. Fails fast: a broadcast
    /// that can never work should be reported before the shell spawns.
    pub fn connect(
        server_url: &str,
        room_path: &str,
        password: &str,
        size: TermSize,
        theme: ThemeChoice,
        cipher: Option<Encryptor>,
    ) -> Result<Self, TransportError> {
        let ws_base = if let Some(rest) = server_url.strip_prefix("https://") {
            format!("wss://{rest}")
        } else if let Some(rest) = server_url.strip_prefix("http://") {
            format!("ws://{rest}")
        } else {
            return Err(TransportError::Connect(format!(
                "unsupported server URL: {server_url}"
            )));
        };

        let mut transport = Self {
            url: format!("{ws_base}/ws/{room_path}"),
            password: password.to_string(),
            socket: None,
            pending: VecDeque::new(),
            unacked: VecDeque::new(),
            buffered_bytes: 0,
            conn_acked: 0,
            dropped_unacked: 0,
            size,
            sent_size: None,
            theme,
            cipher,
            keyframer: Keyframer::new(size),
            backoff: INITIAL_BACKOFF,
            next_attempt: None,
            last_ping: Instant::now(),
        };
        transport.socket = Some(transport.handshake()?);
        // A room outlives its broadcaster, and the default password is
        // this machine's id, so rerunning a named room re-claims one
        // that still holds the previous session. Clear it here - on the
        // FIRST connection only, never in `handshake`'s reconnect path,
        // where wiping history would destroy exactly what replay is
        // rebuilding. Best effort: a failed write means the connection
        // died, and the reconnect carries on without the reset.
        let _ = transport.write(Message::text(json!({"reset": true}).to_string()));
        Ok(transport)
    }

    /// Buffer terminal output and deliver everything pending. Network
    /// failures keep data buffered and schedule a reconnect; only
    /// authorization failures surface.
    pub fn send(&mut self, data: &[u8], size: TermSize) -> Result<(), TransportError> {
        // Feed the emulator the exact bytes being broadcast, then buffer the
        // data, then (when due) a keyframe AFTER it, so the redraw reflects the
        // screen as of this frame. The keyframe is an ordinary sealed record:
        // it rides the same acks/cap/replay path with no protocol change.
        self.keyframer.feed(data, size);
        if !data.is_empty() {
            self.buffer_plaintext(data);
        }
        if let Some(keyframe) = self.keyframer.maybe_keyframe() {
            self.buffer_plaintext(&keyframe);
        }
        self.size = size;
        self.flush()
    }

    /// Seal a chunk (when encrypting) and queue it for delivery, evicting the
    /// oldest buffered output if the replay cap is exceeded.
    ///
    /// Sealed here, before buffering: everything downstream (acks, the cap,
    /// replay) counts ciphertext bytes, exactly what the server sees and
    /// acknowledges. A replayed record is byte-identical, never re-encrypted
    /// (no nonce reuse).
    fn buffer_plaintext(&mut self, data: &[u8]) {
        let sealed: Bytes = self.cipher.as_ref().map_or_else(
            || Bytes::copy_from_slice(data),
            |cipher| cipher.seal(data).into(),
        );
        self.buffered_bytes += sealed.len();
        self.pending.push_back(sealed);
        // Drop the oldest buffered output (acked data is already
        // gone; the oldest is the front of `unacked`, then `pending`)
        while self.buffered_bytes > MAX_BUFFERED_BYTES {
            if let Some(dropped) = self.unacked.pop_front() {
                // Already written: its ack may still arrive and must
                // not be credited to a later chunk
                self.dropped_unacked += dropped.len() as u64;
                self.buffered_bytes -= dropped.len();
            } else if let Some(dropped) = self.pending.pop_front() {
                self.buffered_bytes -= dropped.len();
            } else {
                break;
            }
        }
    }

    /// Idle housekeeping: process acknowledgments, retry delivery of
    /// anything pending (including a reconnect, when its backoff has
    /// elapsed), propagate size changes, and keep the connection alive.
    pub fn tick(&mut self, size: TermSize) -> Result<(), TransportError> {
        self.size = size;
        let result = self.flush();
        if self.socket.is_some() && self.last_ping.elapsed() >= PING_INTERVAL {
            self.last_ping = Instant::now();
            let _ = self.write(Message::Ping(Bytes::new()));
        }
        result
    }

    /// Flush and close at the end of a session, leaving the room behind.
    ///
    /// Pending output is written and its acknowledgments awaited,
    /// briefly, because nothing will retransmit it: the room outlives
    /// this process (until the server's inactivity TTL evicts it), so
    /// the tail of the output has to land before the socket closes.
    pub fn shutdown(&mut self) {
        let _ = self.flush();
        let deadline = Instant::now() + SHUTDOWN_DRAIN;
        while self.socket.is_some() && !self.unacked.is_empty() && Instant::now() < deadline {
            std::thread::sleep(Duration::from_millis(20));
            self.drain_acks();
        }
        if let Some(socket) = self.socket.as_mut() {
            let _ = socket.close(None);
        }
        self.socket = None;
    }

    /// Deliver the pending size and chunks on the current connection,
    /// reconnecting first if necessary.
    fn flush(&mut self) -> Result<(), TransportError> {
        self.drain_acks();

        if self.socket.is_none() {
            if self.next_attempt.is_some_and(|at| Instant::now() < at) {
                return Ok(()); // still backing off
            }
            match self.handshake() {
                Ok(socket) => self.socket = Some(socket),
                Err(TransportError::Unauthorized) => return Err(TransportError::Unauthorized),
                Err(_) => return Ok(()), // retry after backoff
            }
        }

        // Size first, so viewers resize before the content that follows
        if self.sent_size != Some(self.size) {
            let mut size = json!(self.size);
            // A named theme the viewer looks up, or this terminal's own
            // colors by value; `colors` wins in the viewer, so a client
            // never sends both.
            //
            // Both ride in cleartext even under e2ee, as the theme name
            // always has. A name is one of nine fixed values, but a
            // detected palette is a fingerprint of the broadcaster's
            // terminal setup, linkable across rooms by the server.
            // `--theme <name>` avoids emitting it; see crypto.rs's
            // threat model on visible metadata.
            match &self.theme {
                ThemeChoice::Named(name) => size["theme"] = json!(name),
                ThemeChoice::Detected(colors) => size["colors"] = json!(colors),
            }
            // Rides verbatim to viewers like the theme: tells a viewer
            // whether to expect encrypted records, so a plaintext room
            // renders and a keyless link to an encrypted room can say so
            if self.cipher.is_some() {
                size["encrypted"] = json!(true);
            }
            let frame = Message::text(json!({"size": size}).to_string());
            if self.write(frame).is_err() {
                return Ok(());
            }
            self.sent_size = Some(self.size);
        }

        // `Bytes` clones share the buffer; a written chunk moves to
        // `unacked` and is only released by `drain_acks`
        while let Some(chunk) = self.pending.front().cloned() {
            if self.write(Message::Binary(chunk.clone())).is_err() {
                return Ok(()); // stays pending for the reconnect
            }
            self.pending.pop_front();
            self.unacked.push_back(chunk);
        }

        Ok(())
    }

    /// Process whatever the server has sent without blocking: acks
    /// release chunks from the replay buffer; a close or read error
    /// drops the connection so the next flush reconnects.
    fn drain_acks(&mut self) {
        // Take the socket while draining; it goes back unless it died
        let Some(mut socket) = self.socket.take() else {
            return;
        };
        if set_nonblocking(socket.get_ref(), true).is_err() {
            self.socket = Some(socket);
            return;
        }
        let mut dead = false;
        loop {
            match socket.read() {
                Ok(Message::Text(text)) => {
                    let acked = serde_json::from_str::<serde_json::Value>(&text)
                        .ok()
                        .and_then(|v| v.get("ack").and_then(serde_json::Value::as_u64));
                    if let Some(acked) = acked {
                        self.apply_ack(acked);
                    }
                }
                Ok(Message::Close(_)) => {
                    dead = true;
                    break;
                }
                Ok(_) => {}
                Err(WsError::Io(e)) if e.kind() == ErrorKind::WouldBlock => break,
                Err(_) => {
                    dead = true;
                    break;
                }
            }
        }
        if dead || set_nonblocking(socket.get_ref(), false).is_err() {
            self.disconnect();
        } else {
            self.socket = Some(socket);
        }
    }

    /// Release chunks covered by a cumulative per-connection ack. The
    /// server acks whole frames, so ack values land on chunk boundaries.
    fn apply_ack(&mut self, acked: u64) {
        let mut newly = acked.saturating_sub(self.conn_acked);
        self.conn_acked = self.conn_acked.max(acked);
        // Acks for chunks the buffer cap discarded come first - those
        // bytes precede everything still queued
        let discarded = newly.min(self.dropped_unacked);
        self.dropped_unacked -= discarded;
        newly -= discarded;
        while newly > 0 {
            let Some(front) = self.unacked.front() else {
                break;
            };
            let len = front.len() as u64;
            if len > newly {
                break; // partial ack: keep the chunk, replay is harmless
            }
            newly -= len;
            self.buffered_bytes -= front.len();
            self.unacked.pop_front();
        }
    }

    /// Drop the connection and arm the reconnect backoff. Everything
    /// unacknowledged goes back to pending, for replay after reconnect.
    fn disconnect(&mut self) {
        self.socket = None;
        self.arm_backoff();
        while let Some(chunk) = self.unacked.pop_back() {
            self.pending.push_front(chunk);
        }
    }

    /// Send one frame; on failure drop the connection and arm the
    /// reconnect backoff.
    fn write(&mut self, frame: Message) -> Result<(), ()> {
        let Some(socket) = self.socket.as_mut() else {
            return Err(());
        };
        socket.send(frame).map_err(|_| self.disconnect())
    }

    /// One connection attempt. A definitive 401 is fatal; anything else
    /// arms the backoff and lets the caller try again later. On success
    /// the per-connection state (backoff, acks, size) is reset.
    ///
    /// The dial is done here rather than by `tungstenite::connect`,
    /// which offers no way to bound any part of it: it resolves,
    /// connects, writes the upgrade request and then blocks reading the
    /// response on a socket with no timeouts at all. A peer that accepts
    /// the connection and then says nothing parked this thread in
    /// `recvfrom` forever, and since every termination signal only sets
    /// a flag that a blocked thread never reads, the process needed
    /// SIGKILL (issue #173). Now the dial is bounded by `DIAL_TIMEOUT`
    /// and the upgrade by `HANDSHAKE_TIMEOUT`, so an unresponsive peer
    /// is just another `Connect` error feeding the reconnect backoff.
    ///
    /// One thing `tungstenite::connect` did that this does not: follow
    /// redirects. Nothing needs it - the `Location` would have to name a
    /// `ws://` or `wss://` URL to be usable at all, and an https
    /// redirect (what a real server in front of shellshare sends) failed
    /// on the old path too.
    fn handshake(&mut self) -> Result<WsSocket, TransportError> {
        let result = (|| {
            let mut request = self
                .url
                .as_str()
                .into_client_request()
                .map_err(|e| TransportError::Connect(e.to_string()))?;
            let auth = HeaderValue::from_str(&self.password)
                .map_err(|e| TransportError::Connect(format!("invalid password: {e}")))?;
            request.headers_mut().insert(header::AUTHORIZATION, auth);

            let stream = dial(request.uri())?;
            // Non-blocking for the upgrade, so ONE deadline covers the
            // whole exchange rather than one deadline per syscall. A
            // socket read timeout would bound each read separately, and
            // every read that returns a byte restarts it: a peer
            // trickling header bytes just under that timeout would stay
            // inside tungstenite's parse loop for as long as its
            // anti-DoS limits allow (512 packets, i.e. over an hour).
            // It also covers the write side, and the TLS handshake for
            // wss, which happen through the same stream.
            stream
                .set_nonblocking(true)
                .map_err(|e| TransportError::Connect(e.to_string()))?;
            let deadline = Instant::now() + HANDSHAKE_TIMEOUT;

            // `client_tls_with_config` handles both modes: with no
            // connector given it wraps the stream in rustls for wss and
            // passes it through for ws, building the connector from the
            // same webpki root store `tungstenite::connect` used.
            let mut attempt = tungstenite::client_tls_with_config(request, stream, None, None);
            // Long enough that a stalled peer is not polled hot, short
            // enough that it adds nothing measurable to a local connect
            let mut idle = Duration::from_micros(200);
            let socket = loop {
                match attempt {
                    Ok((socket, _response)) => break socket,
                    Err(HandshakeError::Failure(WsError::Http(response)))
                        if response.status() == StatusCode::UNAUTHORIZED =>
                    {
                        return Err(TransportError::Unauthorized)
                    }
                    Err(HandshakeError::Failure(e)) => {
                        return Err(TransportError::Connect(e.to_string()))
                    }
                    // Nothing to read or write yet. Every platform
                    // reports this the same way (WSAEWOULDBLOCK
                    // included), and the mid-handshake state owns the
                    // socket, so giving up here closes it.
                    Err(HandshakeError::Interrupted(mid)) => {
                        if Instant::now() >= deadline {
                            return Err(TransportError::Connect(format!(
                                "no handshake response after {}s",
                                HANDSHAKE_TIMEOUT.as_secs()
                            )));
                        }
                        std::thread::sleep(idle);
                        idle = (idle * 2).min(HANDSHAKE_POLL);
                        attempt = mid.handshake();
                    }
                }
            };

            // Back to blocking: `drain_acks` owns the non-blocking reads
            // from here on, and `write` relies on blocking sends
            set_nonblocking(socket.get_ref(), false)
                .map_err(|()| TransportError::Connect("cannot restore socket".into()))?;
            Ok(socket)
        })();

        match result {
            Ok(socket) => {
                configure_tcp(socket.get_ref());
                self.backoff = INITIAL_BACKOFF;
                self.next_attempt = None;
                self.sent_size = None;
                self.conn_acked = 0;
                self.dropped_unacked = 0;
                self.last_ping = Instant::now();
                Ok(socket)
            }
            Err(e) => {
                self.arm_backoff();
                Err(e)
            }
        }
    }

    fn arm_backoff(&mut self) {
        self.next_attempt = Some(Instant::now() + self.backoff);
        self.backoff = (self.backoff * 2).min(MAX_BACKOFF);
    }
}

/// Open the TCP connection for a `ws`/`wss` URL within `DIAL_TIMEOUT`.
///
/// A hostname routinely resolves to several addresses - shellshare.net
/// alone answers with two IPv4 and two IPv6 - and `connect_timeout`
/// takes exactly one, so every candidate is tried in the resolver's
/// order (which already encodes the OS's source-address preference).
/// The budget is *shared*: each attempt gets an equal slice of what is
/// left, so a black-holed IPv6 address cannot spend the time its IPv4
/// sibling needs, and the whole dial still ends within `DIAL_TIMEOUT`.
///
/// Name resolution itself stays blocking - `getaddrinfo` has no timeout
/// to give it - but it is bounded by the resolver's own retry limits,
/// unlike the silent-peer read this function exists to bound.
fn dial(uri: &Uri) -> Result<TcpStream, TransportError> {
    let mode = uri_mode(uri).map_err(|e| TransportError::Connect(e.to_string()))?;
    let host = uri
        .host()
        .ok_or_else(|| TransportError::Connect(format!("no host in {uri}")))?;
    // A literal IPv6 host is bracketed in a URL; the resolver wants it bare
    let host = host
        .strip_prefix('[')
        .and_then(|h| h.strip_suffix(']'))
        .unwrap_or(host);
    let port = uri.port_u16().unwrap_or(match mode {
        Mode::Plain => 80,
        Mode::Tls => 443,
    });

    let addrs: Vec<SocketAddr> = (host, port)
        .to_socket_addrs()
        .map_err(|e| TransportError::Connect(format!("cannot resolve {host}: {e}")))?
        .collect();
    let deadline = Instant::now() + DIAL_TIMEOUT;
    let mut left = addrs.len();
    let mut last_error = None;
    for addr in addrs {
        let remaining = deadline.saturating_duration_since(Instant::now());
        let slice = (remaining / u32::try_from(left).unwrap_or(u32::MAX))
            .max(MIN_DIAL_ATTEMPT)
            .min(remaining);
        left -= 1;
        if slice.is_zero() {
            break; // out of budget (and connect_timeout rejects zero)
        }
        match TcpStream::connect_timeout(&addr, slice) {
            Ok(stream) => {
                // Broadcasts are many small frames; Nagle would only add
                // delay, and the handshake is the first thing to profit
                let _ = stream.set_nodelay(true);
                return Ok(stream);
            }
            Err(e) => last_error = Some(e),
        }
    }
    Err(TransportError::Connect(last_error.map_or_else(
        || format!("could not connect to {host}:{port}"),
        |e| format!("could not connect to {host}:{port}: {e}"),
    )))
}

/// Low-latency, bounded-blocking socket options. Broadcasts are many
/// small frames, so Nagle's algorithm would only add delay; the write
/// timeout turns a dead connection into a reconnect instead of a hang.
/// Nothing to undo from the handshake: it bounds itself by polling a
/// non-blocking socket rather than by arming a socket timeout.
fn configure_tcp(stream: &MaybeTlsStream<TcpStream>) {
    if let Some(tcp) = tcp_stream(stream) {
        let _ = tcp.set_nodelay(true);
        let _ = tcp.set_write_timeout(Some(Duration::from_secs(10)));
    }
}

fn set_nonblocking(stream: &MaybeTlsStream<TcpStream>, nonblocking: bool) -> Result<(), ()> {
    tcp_stream(stream)
        .ok_or(())?
        .set_nonblocking(nonblocking)
        .map_err(|_| ())
}

fn tcp_stream(stream: &MaybeTlsStream<TcpStream>) -> Option<&TcpStream> {
    match stream {
        MaybeTlsStream::Plain(tcp) => Some(tcp),
        MaybeTlsStream::Rustls(tls) => Some(tls.get_ref()),
        // Unreachable with the current feature set (plain + rustls).
        // If another TLS backend is ever enabled, `drain_acks` would
        // stop processing acks and the replay buffer would fill - so
        // make sure new variants are handled here.
        _ => None,
    }
}
