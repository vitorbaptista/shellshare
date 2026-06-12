//! Shellshare server - Live terminal broadcasting server
//!
//! This module contains the server implementation using axum + socketioxide.

mod analytics;
mod binaries;
mod pages;
mod rooms;

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
use binaries::BinaryDownloadQuery;
use rooms::{RoomId, Rooms};
use socketioxide::{
    extract::{Data, SocketRef, State as SioState},
    SocketIo,
};
use std::collections::HashMap;
use std::future::IntoFuture;
use std::sync::{Arc, OnceLock};
use std::time::Duration;
use tokio::sync::RwLock;
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
    /// Socket.IO instance - set once at startup, lock-free to read on the
    /// broadcast hot path
    io: Arc<OnceLock<SocketIo>>,
    /// Queue feeding the fan-out task - set once at startup, like `io`
    fanout: Arc<OnceLock<tokio::sync::mpsc::UnboundedSender<FanoutItem>>>,
    /// Rooms whose user count changed and needs re-broadcasting
    usercount: Arc<OnceLock<tokio::sync::mpsc::UnboundedSender<String>>>,
    /// Track which rooms each socket is in (for disconnect handling)
    socket_rooms: Arc<RwLock<HashMap<String, Vec<String>>>>,
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

    // Socket.IO setup. WebSocket only, no HTTP long-polling: the
    // engine.io polling encoder corrupts binary events when an emit
    // lands on a parked long-poll (the announce and its attachment are
    // concatenated without the packet separator - engineioxide bug,
    // still present in 0.17.3), which intermittently garbled history
    // replay and live output for viewers. Every consumer is ours (the
    // viewer page below connects websocket-only), so there is nothing
    // that needs the fallback.
    let (sio_layer, io) = SocketIo::builder()
        .transports([socketioxide::TransportType::Websocket])
        // Per-viewer send queue, in packets (default 128). Queued
        // packets are refcounted clones of one broadcast payload, so
        // depth is nearly free - but overflow silently drops frames for
        // that viewer, losing content with no signal to anyone. Deep
        // queue + fan-out coalescing makes a viewer have to fall
        // ~128MB behind before that can happen.
        .max_buffer_size(2048)
        .with_state(app_state.clone())
        .build_layer();

    // Setup Socket.IO event handlers
    setup_socket_handlers(&io);

    // Store io in shared state (handlers will see this via Arc)
    let _ = app_state.io.set(io);

    // The viewer fan-out task: ingest stores and acks, this emits.
    // Both tasks get only the io cell, not AppState: AppState holds
    // their senders, and a task holding its own sender would keep its
    // channel (and itself) alive forever
    let (fanout_tx, fanout_rx) = tokio::sync::mpsc::unbounded_channel();
    let _ = app_state.fanout.set(fanout_tx);
    tokio::spawn(fanout_loop(fanout_rx, app_state.io.clone()));

    // The user-count broadcast task: joins and disconnects queue the
    // room here instead of broadcasting inline
    let (usercount_tx, usercount_rx) = tokio::sync::mpsc::unbounded_channel();
    let _ = app_state.usercount.set(usercount_tx);
    tokio::spawn(usercount_loop(usercount_rx, app_state.io.clone()));

    // Spawn background cleanup task for abandoned rooms
    spawn_cleanup_task(app_state.clone());

    // Build router
    let app = Router::new()
        // API routes
        .route("/", get(pages::index_handler))
        .route("/r/{*room}", get(pages::room_page_handler))
        .route("/r/{*room}", post(broadcast_handler))
        .route("/r/{*room}", delete(delete_room_handler))
        // WebSocket ingest - the fast path for broadcasting clients
        .route("/ws/r/{*room}", get(ws_ingest_handler))
        // Binary download (serves embedded binaries or self)
        .route("/bin/shellshare", get(serve_binary))
        // Static files - fallback
        .fallback(pages::serve_static)
        // State and middleware
        .with_state(app_state)
        .layer(sio_layer)
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

/// Setup Socket.IO event handlers
///
/// The connect handler MUST stay synchronous: socketioxide sends the
/// connect ack to the client and only then runs this handler - an async
/// handler is `tokio::spawn`'ed, so under load a client's first `join`
/// (emitted as soon as it sees the ack) could arrive before
/// `socket.on("join")` is registered and be silently dropped, leaving a
/// viewer stuck on an empty terminal. A sync handler registers
/// everything inline before the connect task yields.
fn setup_socket_handlers(io: &SocketIo) {
    io.ns("/", |socket: SocketRef, _state: SioState<AppState>| {
        info!("Client connected: {}", socket.id);

        // Handle join event
        socket.on(
            "join",
            |socket: SocketRef, Data::<String>(room), state: SioState<AppState>| async move {
                let room_id = RoomId::parse(&room);

                // Debug-format the room ids: they are client-controlled and
                // could otherwise inject control characters into the log
                info!(
                    "Client {} joining room: {:?} (normalized: {:?})",
                    socket.id, room, room_id
                );

                // Join the socket to the room
                let room_name = room_id.as_str().to_string();
                if let Err(e) = socket.join(room_name.clone()) {
                    warn!("Failed to join room {:?}: {:?}", room_id, e);
                }

                // Track this socket's rooms for disconnect handling.
                // Joins are idempotent (clients may re-emit until
                // confirmed), so deduplicate to keep disconnect from
                // emitting usersCount more than once per room.
                let mut socket_rooms = state.socket_rooms.write().await;
                let tracked = socket_rooms.entry(socket.id.to_string()).or_default();
                // Re-emitted joins are not new viewers; remember which
                // this was so analytics counts each socket once per room
                let newly_joined = !tracked.contains(&room_name);
                if newly_joined {
                    tracked.push(room_name.clone());
                }
                // Release before the room snapshot below takes its own lock
                drop(socket_rooms);

                // Catch the viewer up if the room is live - but only on
                // the socket's FIRST join to this room: clients re-emit
                // `join` until they see the usersCount confirmation, and
                // replaying history for a retry that merely overtook a
                // slow confirmation would duplicate terminal content
                let snapshot = state.rooms.snapshot(&room_id).await;
                let room_exists = snapshot.is_some();
                let broadcasting = snapshot.as_ref().is_some_and(|s| s.broadcasting);
                if let Some(snapshot) = snapshot.filter(|_| newly_joined) {
                    // Send size FIRST - terminal must be sized before receiving content
                    if let Some(ref size) = snapshot.size {
                        if let Err(e) = socket.emit("size", size) {
                            warn!("Failed to emit size: {:?}", e);
                        }
                    }

                    // Then send accumulated message history as one
                    // binary attachment
                    if let Some(history) = snapshot.history {
                        if let Err(e) = socket.emit("message", &history) {
                            warn!("Failed to emit message: {:?}", e);
                        }
                    }
                }

                // Tell the viewer whether a broadcaster is attached
                // right now; transitions arrive as room broadcasts
                if let Err(e) = socket.emit("broadcasting", &broadcasting) {
                    warn!("Failed to emit broadcasting: {:?}", e);
                }

                // Get fresh user count right before emissions
                let user_count = socket
                    .within(room_name.clone())
                    .sockets()
                    .map_or(0, |s| s.len());

                // Send user count directly to this client (guaranteed
                // delivery - clients treat it as the join confirmation)
                if let Err(e) = socket.emit("usersCount", &user_count) {
                    warn!("Failed to emit usersCount: {:?}", e);
                }

                // Notify the rest of the room via the coalescing task:
                // broadcasting from every join is O(N^2) in a connect storm
                if let Some(tx) = state.usercount.get() {
                    let _ = tx.send(room_name);
                }

                // Joins to nonexistent rooms (dead links) are not an
                // audience and would only inflate the numbers; replay
                // viewers count, flagged by `broadcasting`
                if newly_joined && room_exists {
                    state
                        .analytics
                        .viewer_joined(room_id.as_str(), user_count, broadcasting);
                }
            },
        );

        // Handle disconnect - need state to access io for proper broadcast
        socket.on_disconnect(|socket: SocketRef, state: SioState<AppState>| async move {
            let socket_id = socket.id.to_string();
            info!("Client disconnected: {}", socket_id);

            // Get rooms from our tracking (socket.rooms() may be empty at disconnect time)
            let rooms = {
                let mut socket_rooms = state.socket_rooms.write().await;
                socket_rooms.remove(&socket_id).unwrap_or_default()
            };

            info!("Socket {} was in rooms: {:?}", socket_id, rooms);

            // Update user counts for each room (the coalescing task
            // computes the count after this socket's removal)
            if let Some(tx) = state.usercount.get() {
                for room in rooms {
                    let _ = tx.send(room);
                }
            }
        });
    });
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
async fn ingest(
    state: &AppState,
    room_id: &RoomId,
    secret: &str,
    size: Option<&serde_json::Value>,
    message: Option<&Bytes>,
) -> Result<(), rooms::Unauthorized> {
    let _ = state.rooms.append(room_id, secret, size, message).await?;

    if let Some(tx) = state.fanout.get() {
        // The channel is unbounded; depth is bounded in practice by the
        // aggregate ingest rate across broadcasters, and queued payloads
        // are refcounted slices of already-stored history. All rooms
        // share one consumer, so a very large audience in one room can
        // delay another room's emits - acceptable until multi-room load
        // says otherwise (per-room ordering is the only hard requirement)
        let _ = tx.send(FanoutItem {
            room: room_id.as_str().to_string(),
            size: size.cloned(),
            payload: message.cloned(),
        });
    }

    Ok(())
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

/// The viewer fan-out task: drains the queue and emits to Socket.IO.
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
    io: Arc<OnceLock<SocketIo>>,
) {
    /// Payloads accumulated for one room, flushed as one emit.
    #[derive(Default)]
    struct Pending {
        chunks: Vec<Bytes>,
        len: usize,
    }

    fn flush(io: &SocketIo, room: &str, p: Pending) {
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
        if let Some(ns) = io.of("/") {
            let _ = ns.within(room.to_string()).emit("message", &payload);
        }
    }

    while let Some(first) = rx.recv().await {
        let mut batch = vec![first];
        while let Ok(item) = rx.try_recv() {
            batch.push(item);
        }
        // `io` is set before the channel exists; an unset value here
        // would silently drop the whole batch, so keep that invariant
        let Some(io) = io.get() else { continue };
        let mut pending: HashMap<String, Pending> = HashMap::new();
        for item in batch {
            if let Some(size) = item.size {
                // A size event is an ordering barrier within its room:
                // anything queued before it must reach viewers first
                if let Some(p) = pending.remove(&item.room) {
                    flush(io, &item.room, p);
                }
                if let Some(ns) = io.of("/") {
                    let _ = ns.within(item.room.clone()).emit("size", &size);
                }
            }
            if let Some(payload) = item.payload {
                match pending.entry(item.room) {
                    std::collections::hash_map::Entry::Occupied(mut e) => {
                        if e.get().len + payload.len() > FANOUT_MAX_BATCH {
                            let full = std::mem::take(e.get_mut());
                            flush(io, e.key(), full);
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
            flush(io, &room, p);
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
    io: Arc<OnceLock<SocketIo>>,
) {
    let mut rooms = std::collections::HashSet::new();
    while let Some(first) = rx.recv().await {
        rooms.insert(first);
        while let Ok(room) = rx.try_recv() {
            rooms.insert(room);
        }
        // Set before the channel exists, like in fanout_loop
        let Some(io) = io.get() else {
            rooms.clear();
            continue;
        };
        for room in rooms.drain() {
            let count = io
                .of("/")
                .and_then(|ns| ns.within(room.clone()).sockets().ok())
                .map_or(0, |sockets| sockets.len());
            if let Some(ns) = io.of("/") {
                let _ = ns.within(room).emit("usersCount", &count);
            }
        }
    }
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

    let claimed = match state.rooms.append(&room_id, &secret, None, None).await {
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
    let connections = state.rooms.broadcaster_connected(&room_id, &secret).await;
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
                let result = ingest(&state, &room_id, &secret, None, Some(&bytes)).await;
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
                    if let Ok(Some(duration)) = state.rooms.delete(&room_id, &secret).await {
                        state
                            .analytics
                            .broadcast_ended(&secret, room_id.as_str(), duration);
                    }
                    break;
                }
                let size = body
                    .get("size")
                    .filter(|s| protocol::size_has_dimensions(s));
                match size {
                    Some(size) => (ingest(&state, &room_id, &secret, Some(size), None).await, true),
                    None => (Ok(()), false),
                }
            }
            // axum answers pings itself; a ping still refreshes the room
            // so an idle-but-connected broadcast isn't evicted
            WsMessage::Ping(_) | WsMessage::Pong(_) => {
                let result = state.rooms.append(&room_id, &secret, None, None).await;
                (result.map(|_| ()), false)
            }
            WsMessage::Close(_) => break,
        };
        if result.is_err() {
            warn!("WS ingest for room {:?} lost the room; closing", room_id);
            break;
        }
        if ack {
            let frame = format!("{{\"ack\":{received_bytes}}}");
            if socket.send(WsMessage::Text(frame.into())).await.is_err() {
                break;
            }
        }
    }
    info!("WS ingest disconnected for room {:?}", room_id);

    let (remaining, duration) = state.rooms.broadcaster_disconnected(&room_id, &secret).await;
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
    if let Some(ns) = state.io.get().and_then(|io| io.of("/")) {
        let _ = ns
            .within(room_id.as_str().to_string())
            .emit("broadcasting", &live);
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

    match state.rooms.delete(&room_id, secret).await {
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
            let evicted = state
                .rooms
                .evict_stale(state.cleanup_config.inactive_ttl)
                .await;
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
