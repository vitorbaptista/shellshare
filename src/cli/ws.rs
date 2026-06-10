//! WebSocket transport for the broadcasting client.
//!
//! Terminal output travels as binary frames of raw bytes; control
//! messages (`size`, `delete`) travel as JSON text frames.
//!
//! Reliability model: after the initial connect succeeds, `send` and
//! `tick` never fail on network errors. Output is held in a bounded
//! replay buffer until the server acknowledges it (`{"ack": n}`, a
//! cumulative per-connection byte count) - a TCP write succeeding proves
//! nothing once the connection dies. On any failure the connection is
//! re-established with backoff and everything unacknowledged replays,
//! giving at-least-once delivery. The only fatal error is authorization:
//! the room now belongs to another password.

use crate::protocol::TermSize;
use serde_json::json;
use std::collections::VecDeque;
use std::io::ErrorKind;
use std::net::TcpStream;
use std::time::{Duration, Instant};
use tungstenite::client::IntoClientRequest;
use tungstenite::http::{header, HeaderValue, StatusCode};
use tungstenite::stream::MaybeTlsStream;
use tungstenite::{Bytes, Error as WsError, Message, WebSocket};

/// Cap on data held for (re)delivery. When exceeded, the oldest chunks
/// are dropped: viewers may miss a window of output (just like a late
/// joiner does), but the broadcast survives the outage.
const MAX_BUFFERED_BYTES: usize = 1024 * 1024;
const INITIAL_BACKOFF: Duration = Duration::from_millis(250);
const MAX_BACKOFF: Duration = Duration::from_secs(5);
/// How long shutdown may wait for the final acknowledgments.
const SHUTDOWN_DRAIN: Duration = Duration::from_secs(1);
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
            backoff: INITIAL_BACKOFF,
            next_attempt: None,
            last_ping: Instant::now(),
        };
        transport.socket = Some(transport.handshake()?);
        Ok(transport)
    }

    /// Buffer terminal output and deliver everything pending. Network
    /// failures keep data buffered and schedule a reconnect; only
    /// authorization failures surface.
    pub fn send(&mut self, data: Vec<u8>, size: TermSize) -> Result<(), TransportError> {
        if !data.is_empty() {
            self.buffered_bytes += data.len();
            self.pending.push_back(data.into());
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
        self.size = size;
        self.flush()
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

    /// Best-effort room deletion and close, for the end of a session.
    /// Pending output is flushed - and its acknowledgments awaited,
    /// briefly - so viewers see the final screen.
    pub fn shutdown_and_delete(&mut self) {
        let _ = self.flush();
        let deadline = Instant::now() + SHUTDOWN_DRAIN;
        while self.socket.is_some() && !self.unacked.is_empty() && Instant::now() < deadline {
            std::thread::sleep(Duration::from_millis(20));
            self.drain_acks();
        }
        if let Some(socket) = self.socket.as_mut() {
            let _ = socket.send(Message::text(json!({"delete": true}).to_string()));
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
                Err(TransportError::Unauthorized) => {
                    return Err(TransportError::Unauthorized)
                }
                Err(_) => return Ok(()), // retry after backoff
            }
        }

        // Size first, so viewers resize before the content that follows
        if self.sent_size != Some(self.size) {
            let frame = Message::text(json!({"size": self.size}).to_string());
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

            match tungstenite::connect(request) {
                Ok((socket, _response)) => Ok(socket),
                Err(WsError::Http(response))
                    if response.status() == StatusCode::UNAUTHORIZED =>
                {
                    Err(TransportError::Unauthorized)
                }
                Err(e) => Err(TransportError::Connect(e.to_string())),
            }
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

/// Low-latency, bounded-blocking socket options. Broadcasts are many
/// small frames, so Nagle's algorithm would only add delay; the write
/// timeout turns a dead connection into a reconnect instead of a hang.
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
