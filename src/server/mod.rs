//! Shellshare server - Live terminal broadcasting server
//!
//! This module contains the server implementation using axum.

mod analytics;
mod binaries;
mod pages;
mod rooms;
mod viewers;

pub use analytics::{Config as AnalyticsConfig, DEFAULT_POSTHOG_HOST};

use axum::{
    body::Body,
    extract::{
        ws::{Message as WsMessage, WebSocket, WebSocketUpgrade},
        DefaultBodyLimit, Path, Query, State,
    },
    http::{header, HeaderMap, StatusCode},
    response::{IntoResponse, Response},
    routing::{delete, get, post},
    serve::ListenerExt,
    Router,
};
use crate::protocol;
use bytes::Bytes;
use futures_util::{SinkExt, StreamExt};
use binaries::BinaryDownloadQuery;
use rooms::{RoomId, Rooms};
use std::collections::HashMap;
use std::future::IntoFuture;
use std::sync::{Arc, OnceLock};
use std::time::Duration;
use tower_http::trace::TraceLayer;
use tracing::{debug, info, warn};

/// Room cleanup configuration
#[derive(Clone, Debug)]
struct CleanupConfig {
    /// How often to run cleanup
    interval: Duration,
    /// TTL for inactive rooms
    inactive_ttl: Duration,
}

impl Default for CleanupConfig {
    fn default() -> Self {
        Self {
            interval: Duration::from_secs(60 * 60),         // 1 hour
            inactive_ttl: Duration::from_secs(24 * 60 * 60), // 24 hours
        }
    }
}

/// Shared application state
#[derive(Default, Clone)]
struct AppState {
    /// All live rooms (state, passwords, history, eviction)
    rooms: Rooms,
    /// Queues feeding the fan-out tasks, one per shard - set once at
    /// startup. A room always hashes to the same shard, so
    /// per-room ordering holds; spreading rooms across shards keeps one
    /// busy room from delaying every other room's viewers
    fanout: Arc<OnceLock<Vec<tokio::sync::mpsc::UnboundedSender<FanoutItem>>>>,
    /// Rooms whose user count changed and needs re-broadcasting
    usercount: Arc<OnceLock<tokio::sync::mpsc::UnboundedSender<String>>>,
    /// Raw-WebSocket viewers, by room
    viewers: viewers::Viewers,
    /// Cleanup configuration for abandoned rooms
    cleanup_config: CleanupConfig,
    /// Optional usage analytics; a no-op unless the operator opted in
    analytics: analytics::Analytics,
}

/// What `POST /r/:room` answers since the WebSocket transport replaced
/// it. Old clients land here when they try to broadcast; the body is the
/// closest thing to an upgrade prompt we can give them (some print it,
/// the rest at least fail loudly with the 410).
const LEGACY_CLIENT_MESSAGE: &str = "This shellshare server no longer supports broadcasting \
    over HTTP POST. Your client is too old: download the latest from this server's home page \
    (it serves the binary at /bin/shellshare) and share again.";

/// Bind the server's TCP listeners.
///
/// Separated from [`serve_on`] so callers (e.g. `shellshare serve`) can
/// report bind failures before handing the terminal over to the client.
///
/// A hostname like `localhost` can resolve to several addresses (127.0.0.1
/// and `::1`). `TcpListener::bind` would silently settle for whichever one is
/// free, leaving another service answering on the same name and port, so we
/// bind every resolved address and fail if any of them is taken. With
/// `--port 0` the OS-picked port from the first bind is reused for the rest
/// so all listeners share one port.
pub async fn bind(host: &str, port: u16) -> std::io::Result<Vec<tokio::net::TcpListener>> {
    // A literal IP (possibly bracketed, like `[::1]`) needs no resolver
    let bare_host = host
        .strip_prefix('[')
        .and_then(|h| h.strip_suffix(']'))
        .unwrap_or(host);
    if let Ok(ip) = bare_host.parse::<std::net::IpAddr>() {
        return Ok(vec![tokio::net::TcpListener::bind((ip, port)).await?]);
    }

    let mut addrs: Vec<_> = tokio::net::lookup_host((host, port)).await?.collect();
    addrs.dedup();
    let mut listeners = Vec::with_capacity(addrs.len());
    let mut bound_port = port;
    for mut addr in addrs {
        addr.set_port(bound_port);
        let listener = tokio::net::TcpListener::bind(addr).await?;
        bound_port = listener.local_addr()?.port();
        listeners.push(listener);
    }
    if listeners.is_empty() {
        return Err(std::io::Error::new(
            std::io::ErrorKind::AddrNotAvailable,
            format!("{host} did not resolve to any address"),
        ));
    }
    Ok(listeners)
}

/// Serve the shellshare app on already-bound listeners
pub async fn serve_on(
    listeners: Vec<tokio::net::TcpListener>,
    cleanup_interval_secs: u64,
    room_ttl_secs: u64,
    analytics_config: Option<AnalyticsConfig>,
) -> Result<(), Box<dyn std::error::Error>> {
    // Render the pages up front: a broken template or stylesheet
    // reference fails the boot instead of the first request
    pages::warm();

    for listener in &listeners {
        info!("Starting shellshare server on {}", listener.local_addr()?);
    }
    info!(
        "Room cleanup: interval={}s, TTL={}s",
        cleanup_interval_secs, room_ttl_secs
    );

    // Shared state with cleanup configuration
    let app_state = AppState {
        cleanup_config: CleanupConfig {
            interval: Duration::from_secs(cleanup_interval_secs),
            inactive_ttl: Duration::from_secs(room_ttl_secs),
        },
        analytics: analytics::Analytics::new(analytics_config),
        ..Default::default()
    };

    // The viewer fan-out tasks: ingest stores and acks, these emit.
    // The tasks get only the viewer registry, not AppState: AppState
    // holds their senders, and a task holding its own sender would keep
    // its channel (and itself) alive forever. One task per shard, sized
    // to the machine, so concurrent rooms emit in parallel
    let shards = std::thread::available_parallelism().map_or(4, std::num::NonZeroUsize::get);
    let mut fanout_txs = Vec::with_capacity(shards);
    for _ in 0..shards {
        let (tx, rx) = tokio::sync::mpsc::unbounded_channel();
        fanout_txs.push(tx);
        tokio::spawn(fanout_loop(rx, app_state.viewers.clone()));
    }
    let _ = app_state.fanout.set(fanout_txs);

    // The user-count broadcast task: joins and disconnects queue the
    // room here instead of broadcasting inline
    let (usercount_tx, usercount_rx) = tokio::sync::mpsc::unbounded_channel();
    let _ = app_state.usercount.set(usercount_tx);
    tokio::spawn(usercount_loop(usercount_rx, app_state.viewers.clone()));

    // Spawn background cleanup task for abandoned rooms
    spawn_cleanup_task(app_state.clone());

    // Build router
    let app = Router::new()
        // API routes
        .route("/", get(pages::index_handler))
        // GET serves the viewer page, or the raw history bytes with a
        // `.bin` suffix (the agent-friendly consumer door)
        .route("/r/{*room}", get(room_get_handler))
        .route("/r/{*room}", post(broadcast_handler))
        .route("/r/{*room}", delete(delete_room_handler))
        // WebSocket ingest - the fast path for broadcasting clients
        .route("/ws/r/{*room}", get(ws_ingest_handler))
        // WebSocket viewers - binary frames are terminal bytes,
        // JSON text frames are control events
        .route("/ws/v/r/{*room}", get(ws_view_handler))
        // Binary download (serves embedded binaries or self)
        .route("/bin/shellshare", get(serve_binary))
        // Static files - fallback
        .fallback(pages::serve_static)
        // State and middleware
        .with_state(app_state)
        .layer(DefaultBodyLimit::max(300 * 1024)) // 300KB limit
        .layer(TraceLayer::new_for_http());

    // Run a server per listener; the first to fail brings the whole thing down
    let mut servers = Vec::with_capacity(listeners.len());
    for listener in listeners {
        info!("Listening on {}", listener.local_addr()?);
        // Terminal frames are small and frequent - exactly the case
        // Nagle's algorithm stalls (up to ~40ms against delayed ACKs).
        // The client side already sets nodelay on its socket
        let listener = listener.tap_io(|tcp| {
            let _ = tcp.set_nodelay(true);
        });
        servers.push(tokio::spawn(axum::serve(listener, app.clone()).into_future()));
    }
    for server in servers {
        server.await??;
    }

    Ok(())
}

/// POST /r/:room - The retired HTTP broadcast endpoint.
///
/// Always answers 410 Gone with an upgrade prompt, whatever the body:
/// only pre-WebSocket clients still POST here, and a clear rejection
/// beats silently dropping their output.
///
/// The body is extracted (and thereby drained) even though it's unused:
/// answering with unread request bytes in flight makes Windows abort
/// the connection (WSAECONNABORTED) before the client reads the 410.
async fn broadcast_handler(Path(room_path): Path<String>, _body: Bytes) -> impl IntoResponse {
    debug!("Legacy POST broadcast to room {:?} rejected", RoomId::parse(&room_path));
    plain_response(StatusCode::GONE, LEGACY_CLIENT_MESSAGE)
}

/// Store a broadcast and forward it to viewers - the single ingest path
/// shared by the HTTP and WebSocket transports.
///
/// The room is claimed/verified and mutated atomically, BEFORE the
/// fan-out is queued, so a viewer joining mid-broadcast can't miss the
/// message entirely. Size is queued FIRST, so the terminal is resized
/// before content arrives.
///
/// Forwarding goes through [`fanout_loop`] instead of emitting inline:
/// emitting to N viewer sockets costs O(N), and paying it here would
/// stall this broadcaster's ack turnaround - measurably collapsing
/// ingest throughput as the audience grows.
fn ingest(
    state: &AppState,
    room_id: &RoomId,
    secret: &str,
    size: Option<&serde_json::Value>,
    message: Option<&Bytes>,
) -> Result<(), rooms::Unauthorized> {
    let _ = state.rooms.append(room_id, secret, size, message)?;

    if let Some(txs) = state.fanout.get() {
        // The channels are unbounded; depth is bounded in practice by
        // the aggregate ingest rate across broadcasters, and queued
        // payloads are refcounted slices of already-stored history. A
        // room always lands on the same shard (hash below), which is
        // what preserves its ordering; rooms sharing a shard can still
        // delay each other, but the blast radius is 1/shards
        let shard = room_shard(room_id.as_str(), txs.len());
        let _ = txs[shard].send(FanoutItem {
            room: room_id.as_str().to_string(),
            size: size.cloned(),
            payload: message.cloned(),
        });
    }

    Ok(())
}

/// Stable room -> fan-out shard assignment.
fn room_shard(room: &str, shards: usize) -> usize {
    use std::hash::{Hash, Hasher};
    let mut hasher = std::collections::hash_map::DefaultHasher::new();
    room.hash(&mut hasher);
    usize::try_from(hasher.finish() % shards.max(1) as u64).unwrap_or(0)
}

/// One unit of viewer fan-out work: a `size` control event and/or a
/// binary terminal payload for a room, in broadcast order.
struct FanoutItem {
    room: String,
    size: Option<serde_json::Value>,
    payload: Option<Bytes>,
}

/// Max bytes coalesced into a single viewer frame. Matches the client's
/// own send batching (`MAX_BATCH` in `cli/script.rs`), so viewers never
/// see a frame shape the client couldn't already have produced.
const FANOUT_MAX_BATCH: usize = 64 * 1024;

/// The viewer fan-out task: drains the queue and emits to viewers.
///
/// Whatever queued up while the previous emits ran is coalesced - each
/// room's payloads are concatenated (up to [`FANOUT_MAX_BATCH`] per
/// emit), exactly like the client's sender thread coalesces PTY output.
/// Under burst load this collapses thousands of tiny per-socket sends
/// into a few large ones, which is what keeps slow viewers' buffers
/// from overflowing into silent content loss. Terminal output is a raw
/// byte stream of whole frames, so concatenation is invisible to
/// viewers (room history already concatenates the same way).
///
/// A single task consumes the queue, so per-room ordering is exactly
/// the ingest order (cross-room ordering carries no meaning). The
/// store-then-queue gap means a viewer joining mid-burst may see a
/// queued frame around its history replay - duplicated, or even before
/// the replay containing it. That race pre-exists this task (emits were
/// always concurrent with the join handler); the queue widens it from
/// microseconds to the drain latency. Same class as the duplicate
/// render already accepted around client reconnect replay: delivery is
/// at-least-once end to end.
async fn fanout_loop(
    mut rx: tokio::sync::mpsc::UnboundedReceiver<FanoutItem>,
    viewers: viewers::Viewers,
) {
    /// Payloads accumulated for one room, flushed as one emit.
    #[derive(Default)]
    struct Pending {
        chunks: Vec<Bytes>,
        len: usize,
    }

    fn flush(viewers: &viewers::Viewers, room: &str, p: Pending) {
        if p.chunks.is_empty() {
            return;
        }
        let payload = if p.chunks.len() == 1 {
            p.chunks.into_iter().next().unwrap_or_default()
        } else {
            let mut buf = bytes::BytesMut::with_capacity(p.len);
            for chunk in p.chunks {
                buf.extend_from_slice(&chunk);
            }
            buf.freeze()
        };
        viewers.send_bytes(room, &payload);
    }

    while let Some(first) = rx.recv().await {
        let mut batch = vec![first];
        while let Ok(item) = rx.try_recv() {
            batch.push(item);
        }
        let mut pending: HashMap<String, Pending> = HashMap::new();
        for item in batch {
            if let Some(size) = item.size {
                // A size event is an ordering barrier within its room:
                // anything queued before it must reach viewers first
                if let Some(p) = pending.remove(&item.room) {
                    flush(&viewers, &item.room, p);
                }
                let control = serde_json::json!({ "size": size }).to_string();
                viewers.send_control(&item.room, &control);
            }
            if let Some(payload) = item.payload {
                match pending.entry(item.room) {
                    std::collections::hash_map::Entry::Occupied(mut e) => {
                        if e.get().len + payload.len() > FANOUT_MAX_BATCH {
                            let full = std::mem::take(e.get_mut());
                            flush(&viewers, e.key(), full);
                        }
                        let p = e.get_mut();
                        p.len += payload.len();
                        p.chunks.push(payload);
                    }
                    std::collections::hash_map::Entry::Vacant(v) => {
                        v.insert(Pending {
                            len: payload.len(),
                            chunks: vec![payload],
                        });
                    }
                }
            }
        }
        for (room, p) in pending {
            flush(&viewers, &room, p);
        }
    }
}

/// The user-count broadcast task: re-announces a room's viewer count
/// to the room whenever its membership changed.
///
/// Joins and disconnects queue the room name here instead of
/// broadcasting inline: an audience of N joining produces N broadcasts
/// to up to N members - O(N^2) emits in a connect storm. Draining and
/// deduplicating turns that into at most one broadcast per room per
/// pass, each carrying the count current at emit time, so every viewer
/// still converges on the exact final number (intermediate values may
/// be skipped, exactly as if the joins had raced the same broadcast).
async fn usercount_loop(
    mut rx: tokio::sync::mpsc::UnboundedReceiver<String>,
    viewers: viewers::Viewers,
) {
    let mut rooms = std::collections::HashSet::new();
    while let Some(first) = rx.recv().await {
        rooms.insert(first);
        while let Ok(room) = rx.try_recv() {
            rooms.insert(room);
        }
        for room in rooms.drain() {
            let count = viewers.count(&room);
            viewers.send_control(&room, &format!("{{\"usersCount\":{count}}}"));
        }
    }
}

/// GET /ws/v/r/:room - raw-WebSocket viewer endpoint.
///
/// The room is the URL: no join handshake exists to lose. On connect
/// the server pushes, in order: the current `size` (a JSON text frame,
/// so the terminal is sized before content), the accumulated history
/// (one binary frame), the `broadcasting` state, and the current
/// `usersCount`. After that, binary frames are live terminal bytes and
/// text frames are JSON control events - the exact mirror of the
/// ingest protocol on `/ws/r/:room`.
async fn ws_view_handler(
    Path(room_path): Path<String>,
    State(state): State<AppState>,
    ws: WebSocketUpgrade,
) -> Response {
    let room_id = RoomId::parse(&room_path);
    ws.on_upgrade(move |socket| ws_view_loop(socket, state, room_id))
}

/// How long a viewer may go without sending anything (pong frames
/// answer our pings automatically in every browser) before the
/// connection is presumed dead. Pings go out every 25s, so a healthy
/// peer is never close to this. The same bound caps every write: a
/// peer that stops reading (frozen tab, zero TCP window) would
/// otherwise block `sink.send` forever, pinning the task and its
/// backlog - the select loop can't reach its idle check while a send
/// is in flight.
const VIEWER_IDLE_TIMEOUT: Duration = Duration::from_secs(75);

/// Send with [`VIEWER_IDLE_TIMEOUT`] as the stall bound.
async fn timed_send<S>(sink: &mut S, msg: WsMessage) -> Result<(), axum::Error>
where
    S: futures_util::Sink<WsMessage, Error = axum::Error> + Unpin,
{
    match tokio::time::timeout(VIEWER_IDLE_TIMEOUT, sink.send(msg)).await {
        Ok(result) => result,
        Err(elapsed) => Err(axum::Error::new(elapsed)),
    }
}
/// Viewer ping cadence; keeps NATs open and detects dead peers.
const VIEWER_PING_INTERVAL: Duration = Duration::from_secs(25);

/// One viewer connection: replay the catch-up snapshot, then relay the
/// registry queue until the peer leaves or stalls out.
async fn ws_view_loop(mut socket: WebSocket, state: AppState, room_id: RoomId) {
    let room = room_id.as_str().to_string();
    // Register BEFORE the snapshot: a frame broadcast in between shows
    // up in the queue (and possibly also in the snapshot - the
    // accepted at-least-once duplicate), but can never be missed
    let (viewer_id, mut rx) = state.viewers.join(&room);
    info!("WS viewer {viewer_id} joined room {room_id:?}");

    let snapshot = state.rooms.snapshot(&room_id);
    let broadcasting = snapshot.as_ref().is_some_and(|s| s.broadcasting);
    let room_exists = snapshot.is_some();
    let catch_up = async {
        if let Some(snap) = snapshot {
            if let Some(size) = snap.size {
                let frame = serde_json::json!({ "size": size }).to_string();
                timed_send(&mut socket, WsMessage::Text(frame.into())).await?;
            }
            if let Some(history) = snap.history {
                timed_send(&mut socket, WsMessage::Binary(history)).await?;
            }
        }
        let frame = format!("{{\"broadcasting\":{broadcasting}}}");
        timed_send(&mut socket, WsMessage::Text(frame.into())).await?;
        let count = total_viewers(&state, &room);
        timed_send(
            &mut socket,
            WsMessage::Text(format!("{{\"usersCount\":{count}}}").into()),
        )
        .await
    };
    if catch_up.await.is_err() {
        state.viewers.leave(&room, viewer_id);
        // The room may have seen this viewer in a count broadcast
        // during its brief membership; converge the others
        if let Some(tx) = state.usercount.get() {
            let _ = tx.send(room);
        }
        return;
    }
    // The rest of the room learns the new count via the coalescing task
    if let Some(tx) = state.usercount.get() {
        let _ = tx.send(room.clone());
    }
    // Joins to nonexistent rooms (dead links) are not an audience
    if room_exists {
        state
            .analytics
            .viewer_joined(room_id.as_str(), total_viewers(&state, &room), broadcasting);
    }

    // Split the socket: the writer must never poll the read half. A
    // read poll per delivered message costs a wasted read syscall and
    // tungstenite read/write interplay - measured as ~4x the delivery
    // latency at thousands of viewers. The reader task only proves the
    // peer is alive (pongs and anything else it sends) and reports
    // when the connection dies.
    let (mut sink, mut stream) = socket.split();
    let started = tokio::time::Instant::now();
    let last_seen_ms = Arc::new(std::sync::atomic::AtomicU64::new(0));
    let seen = last_seen_ms.clone();
    let mut reader = tokio::spawn(async move {
        while let Some(Ok(_)) = stream.next().await {
            let elapsed_ms = started.elapsed().as_millis().min(u128::from(u64::MAX));
            #[allow(clippy::cast_possible_truncation)] // bounded by the min above
            seen.store(elapsed_ms as u64, std::sync::atomic::Ordering::Relaxed);
        }
    });
    let mut ping = tokio::time::interval(VIEWER_PING_INTERVAL);
    ping.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Delay);
    loop {
        tokio::select! {
            queued = rx.recv() => {
                // Dropped by the registry for stalling: close, so the
                // page reconnects and resyncs from history
                let Some(first) = queued else { break };
                if relay(&mut sink, &mut rx, first).await.is_err() {
                    break;
                }
            }
            // The peer hung up (or errored): stop delivering
            _ = &mut reader => break,
            _ = ping.tick() => {
                let last_seen = Duration::from_millis(
                    last_seen_ms.load(std::sync::atomic::Ordering::Relaxed),
                );
                if started.elapsed().saturating_sub(last_seen) > VIEWER_IDLE_TIMEOUT {
                    info!("WS viewer {viewer_id} in {room_id:?} timed out");
                    break;
                }
                if timed_send(&mut sink, WsMessage::Ping(Vec::new().into()))
                    .await
                    .is_err()
                {
                    break;
                }
            }
        }
    }
    reader.abort();
    // Best effort: a Close frame lets the page distinguish a server
    // close from a network drop (it reconnects either way)
    let _ = tokio::time::timeout(
        Duration::from_secs(1),
        sink.send(WsMessage::Close(None)),
    )
    .await;
    state.viewers.leave(&room, viewer_id);
    info!("WS viewer {viewer_id} left room {room_id:?}");
    if let Some(tx) = state.usercount.get() {
        let _ = tx.send(room);
    }
}

/// Relay one wake-up's worth of queued messages to the viewer.
///
/// Everything already queued is drained and consecutive binary
/// payloads are merged into one WebSocket frame (terminal output is a
/// raw byte stream, so the merge is invisible - the client batches the
/// same way before sending). Every message otherwise costs its own
/// write+flush, and at thousands of viewers x dozens of frames/s those
/// per-frame flushes dominate the server. Control events keep their
/// own text frames, and ordering is preserved throughout.
async fn relay(
    sink: &mut futures_util::stream::SplitSink<WebSocket, WsMessage>,
    rx: &mut tokio::sync::mpsc::Receiver<viewers::ViewerMsg>,
    first: viewers::ViewerMsg,
) -> Result<(), axum::Error> {
    /// Bound on a merged frame; beyond it the backlog flushes in parts.
    const MAX_MERGED: usize = 1024 * 1024;
    /// One frame from the accumulated chunks - without copying when a
    /// single chunk stands alone (the common case at normal pace).
    fn take_merged(chunks: &mut Vec<Bytes>, len: &mut usize) -> Bytes {
        *len = 0;
        if chunks.len() == 1 {
            return chunks.pop().unwrap_or_default();
        }
        let mut buf = bytes::BytesMut::with_capacity(chunks.iter().map(Bytes::len).sum());
        for chunk in chunks.drain(..) {
            buf.extend_from_slice(&chunk);
        }
        buf.freeze()
    }
    let mut chunks: Vec<Bytes> = Vec::new();
    let mut pending_len = 0;
    let mut item = Some(first);
    loop {
        match item {
            Some(viewers::ViewerMsg::Bytes(payload)) => {
                if pending_len + payload.len() > MAX_MERGED && !chunks.is_empty() {
                    let merged = take_merged(&mut chunks, &mut pending_len);
                    timed_send(sink, WsMessage::Binary(merged)).await?;
                }
                pending_len += payload.len();
                chunks.push(payload);
            }
            Some(viewers::ViewerMsg::Control(json)) => {
                if !chunks.is_empty() {
                    let merged = take_merged(&mut chunks, &mut pending_len);
                    timed_send(sink, WsMessage::Binary(merged)).await?;
                }
                timed_send(sink, WsMessage::Text(json.into())).await?;
            }
            None => break,
        }
        item = match rx.try_recv() {
            Ok(next) => Some(next),
            Err(_) => break,
        };
    }
    if !chunks.is_empty() {
        let merged = take_merged(&mut chunks, &mut pending_len);
        timed_send(sink, WsMessage::Binary(merged)).await?;
    }
    Ok(())
}

/// How many viewers are watching the room right now.
fn total_viewers(state: &AppState, room: &str) -> usize {
    state.viewers.count(room)
}

/// GET /ws/r/:room - WebSocket ingest for broadcasting clients.
///
/// The fast path: binary frames carry raw terminal bytes; text frames
/// carry JSON control messages (`{"size": {...}}` to resize,
/// `{"delete": true}` to delete the room on exit). The room is claimed -
/// or the password verified - at upgrade time, so an unauthorized client
/// is rejected with 401 before the connection is established.
async fn ws_ingest_handler(
    Path(room_path): Path<String>,
    headers: HeaderMap,
    State(state): State<AppState>,
    ws: WebSocketUpgrade,
) -> Response {
    let room_id = RoomId::parse(&room_path);
    let secret = auth_secret(&headers).to_string();

    let claimed = match state.rooms.append(&room_id, &secret, None, None) {
        Ok(rooms::Appended::Claimed) => true,
        Ok(rooms::Appended::Verified) => false,
        Err(rooms::Unauthorized) => {
            return plain_response(StatusCode::UNAUTHORIZED, "Unauthorized");
        }
    };

    info!("WS ingest connected for room {:?}", room_id);
    ws.on_upgrade(move |socket| ws_ingest_loop(socket, state, room_id, secret, claimed))
}

/// How long the ingest loop waits for ANY frame before declaring the
/// broadcaster dead. The client pings every 30s even when idle, so only
/// a vanished peer (crashed machine, dropped NAT mapping) goes silent
/// this long - and without a bound, a half-open TCP connection would
/// keep the room's "live" indicator on for the kernel's timeout.
const INGEST_IDLE_TIMEOUT: Duration = Duration::from_secs(90);

/// Receive loop for one broadcasting WebSocket connection.
///
/// Every stored binary frame is acknowledged with the cumulative byte
/// count received on this connection (`{"ack": n}`), so the client can
/// release its replay buffer only for data that actually arrived - a
/// TCP write succeeding proves nothing once the connection dies.
///
/// Delivery is at-least-once: around a reconnect, a frame whose ack was
/// lost in flight is replayed, and a stale loop for the previous
/// connection may briefly overlap with the new one. Viewers can see a
/// short duplicate render in that window; the alternative (fencing old
/// connections) would make two broadcasters sharing a password kick
/// each other in an endless reconnect fight.
///
/// Ends on close, error, or when the room's password no longer matches
/// (possible if the room was evicted for inactivity and re-claimed by
/// someone else); the client reconnects and is then rejected at upgrade.
async fn ws_ingest_loop(
    mut socket: WebSocket,
    state: AppState,
    room_id: RoomId,
    secret: String,
    claimed: bool,
) {
    // The connection itself is the aliveness signal: viewers show the
    // room as live while at least one ingest connection is attached.
    // A count of 0 means the room vanished between handshake and
    // upgrade - the loop below then ends on its first failed append.
    let connections = state.rooms.broadcaster_connected(&room_id, &secret);
    if connections > 0 {
        emit_broadcasting(&state, &room_id, true);
    }
    // Count 1 means this attach took the room live (0 -> 1): a new
    // segment. An overlapping reconnect (1 -> 2) continues the segment
    if connections == 1 {
        state
            .analytics
            .broadcast_started(&secret, room_id.as_str(), claimed);
    }

    let mut received_bytes: u64 = 0;
    while let Some(Ok(msg)) = recv_with_timeout(&mut socket).await {
        // Every stored frame is acked; size frames add no bytes, so
        // their ack repeats the current count (a no-op for the client's
        // buffer, but it lets a sender await durability of a resize)
        let (result, ack) = match msg {
            WsMessage::Binary(bytes) => {
                let frame_len = bytes.len() as u64;
                let result = ingest(&state, &room_id, &secret, None, Some(&bytes));
                if result.is_ok() {
                    received_bytes += frame_len;
                }
                (result, true)
            }
            WsMessage::Text(text) => {
                let Ok(body) = serde_json::from_str::<serde_json::Value>(&text) else {
                    continue;
                };
                if body.get("delete").and_then(serde_json::Value::as_bool) == Some(true) {
                    // The clean-exit path: deleting removes the room, so
                    // this is the last chance to know the live segment's
                    // length (the loop's own detach below finds nothing)
                    if let Ok(Some(duration)) = state.rooms.delete(&room_id, &secret) {
                        state
                            .analytics
                            .broadcast_ended(&secret, room_id.as_str(), duration);
                    }
                    break;
                }
                let size = body
                    .get("size")
                    .filter(|s| protocol::size_has_dimensions(s));
                size.map_or((Ok(()), false), |size| {
                    (ingest(&state, &room_id, &secret, Some(size), None), true)
                })
            }
            // axum answers pings itself; a ping still refreshes the room
            // so an idle-but-connected broadcast isn't evicted
            WsMessage::Ping(_) | WsMessage::Pong(_) => {
                let result = state.rooms.append(&room_id, &secret, None, None);
                (result.map(|_| ()), false)
            }
            WsMessage::Close(_) => break,
        };
        if result.is_err() {
            warn!("WS ingest for room {:?} lost the room; closing", room_id);
            break;
        }
        if ack {
            // Bounded like the viewer writes: a broadcaster that stops
            // reading would otherwise block this send forever, pinning
            // the task past INGEST_IDLE_TIMEOUT and keeping the room
            // "live" with a dead connection
            let frame = format!("{{\"ack\":{received_bytes}}}");
            let sent = tokio::time::timeout(
                INGEST_IDLE_TIMEOUT,
                socket.send(WsMessage::Text(frame.into())),
            )
            .await;
            if !matches!(sent, Ok(Ok(()))) {
                break;
            }
        }
    }
    info!("WS ingest disconnected for room {:?}", room_id);

    let (remaining, duration) = state.rooms.broadcaster_disconnected(&room_id, &secret);
    if remaining == 0 {
        emit_broadcasting(&state, &room_id, false);
    }
    // `duration` is Some only when this detach ended a still-live
    // room's segment; the delete paths already reported theirs
    if let Some(duration) = duration {
        state
            .analytics
            .broadcast_ended(&secret, room_id.as_str(), duration);
    }
}

/// Receive the next frame, or `None` when the broadcaster has been
/// silent past [`INGEST_IDLE_TIMEOUT`] - a live client pings every 30s,
/// so silence that long means the connection is dead on a half-open TCP
/// socket that would otherwise linger.
async fn recv_with_timeout(
    socket: &mut WebSocket,
) -> Option<Result<WsMessage, axum::Error>> {
    tokio::time::timeout(INGEST_IDLE_TIMEOUT, socket.recv())
        .await
        .ok()
        .flatten()
}

/// Tell every viewer in the room whether a broadcaster is attached
fn emit_broadcasting(state: &AppState, room_id: &RoomId, live: bool) {
    state
        .viewers
        .send_control(room_id.as_str(), &format!("{{\"broadcasting\":{live}}}"));
}

/// GET /r/:room - the viewer page, or - with a `.bin` suffix - the
/// room's accumulated history as raw bytes.
///
/// `/r/<room>.bin` is the agent-friendly consumer door: the body is
/// exactly what a WebSocket viewer receives as its history frame -
/// opaque, self-delimiting ciphertext records when the broadcast is
/// encrypted (the server never holds the key), or plaintext terminal
/// bytes otherwise. A non-browser consumer (curl, an AI agent) fetches
/// it once and decrypts client-side with the key from the share link's
/// #fragment, so the server stays an opaque relay - the same guarantee
/// the browser viewer relies on. Returns 404 when the room does not
/// exist. (A room whose name literally ends in `.bin` is shadowed by
/// this suffix; auto-generated room ids are alphanumeric, so only an
/// explicitly chosen `--room ...bin` collides.)
async fn room_get_handler(
    Path(room_path): Path<String>,
    headers: HeaderMap,
    State(state): State<AppState>,
) -> Response {
    let Some(room) = room_path.strip_suffix(".bin") else {
        return pages::room_page_response(&headers);
    };
    let room_id = RoomId::parse(room);
    match state.rooms.snapshot(&room_id) {
        Some(snapshot) => {
            let body = snapshot.history.unwrap_or_default();
            Response::builder()
                .status(StatusCode::OK)
                .header(header::CONTENT_TYPE, "application/octet-stream")
                .body(Body::from(body))
                .unwrap()
        }
        None => plain_response(StatusCode::NOT_FOUND, "Room not found"),
    }
}

/// DELETE /r/:room - Delete room
async fn delete_room_handler(
    Path(room_path): Path<String>,
    headers: HeaderMap,
    State(state): State<AppState>,
) -> impl IntoResponse {
    let room_id = RoomId::parse(&room_path);
    let secret = auth_secret(&headers);

    info!(
        "Delete room: {:?}, auth present: {}",
        room_id,
        !secret.is_empty()
    );

    match state.rooms.delete(&room_id, secret) {
        // Deleting out from under a live broadcaster ends its segment
        // here; its loop then finds the room gone and reports nothing
        Ok(Some(duration)) => state
            .analytics
            .broadcast_ended(secret, room_id.as_str(), duration),
        Ok(None) => {}
        Err(rooms::Unauthorized) => {
            return plain_response(StatusCode::UNAUTHORIZED, "Unauthorized");
        }
    }

    plain_response(StatusCode::ACCEPTED, "Accepted")
}

/// The room password carried in the Authorization header
fn auth_secret(headers: &HeaderMap) -> &str {
    headers
        .get(header::AUTHORIZATION)
        .and_then(|v| v.to_str().ok())
        .unwrap_or("")
}

/// Build a plain-text response
fn plain_response(status: StatusCode, body: &'static str) -> Response {
    Response::builder()
        .status(status)
        .header(header::CONTENT_TYPE, "text/plain; charset=utf-8")
        .body(Body::from(body))
        .unwrap()
}

/// Spawn background task to clean up abandoned rooms
fn spawn_cleanup_task(state: AppState) {
    tokio::spawn(async move {
        let mut interval = tokio::time::interval(state.cleanup_config.interval);
        loop {
            interval.tick().await;
            let evicted = state.rooms.evict_stale(state.cleanup_config.inactive_ttl);
            if evicted > 0 {
                info!("Cleaned up {} abandoned rooms", evicted);
            }
        }
    });
}

/// Serve platform-specific binary or fallback to self
///
/// Supports `?os=` query parameter and User-Agent detection.
/// See [`binaries::serve_binary`] for details.
async fn serve_binary(
    query: Query<BinaryDownloadQuery>,
    headers: HeaderMap,
) -> impl IntoResponse {
    binaries::serve_binary(query, headers).await
}
