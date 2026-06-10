//! Shellshare server - Live terminal broadcasting server
//!
//! This module contains the server implementation using axum + socketioxide.

mod binaries;
mod pages;
mod rooms;

use axum::{
    body::Body,
    extract::{
        ws::{Message as WsMessage, WebSocket, WebSocketUpgrade},
        DefaultBodyLimit, Path, Query, State,
    },
    http::{header, HeaderMap, StatusCode},
    response::{IntoResponse, Response},
    routing::{delete, get, post},
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
    /// Track which rooms each socket is in (for disconnect handling)
    socket_rooms: Arc<RwLock<HashMap<String, Vec<String>>>>,
    /// Cleanup configuration for abandoned rooms
    cleanup_config: CleanupConfig,
}

/// What `POST /r/:room` answers since the WebSocket transport replaced
/// it. Old clients land here when they try to broadcast; the body is the
/// closest thing to an upgrade prompt we can give them (some print it,
/// the rest at least fail loudly with the 410).
const LEGACY_CLIENT_MESSAGE: &str = "This shellshare server no longer supports broadcasting \
    over HTTP POST. Your client is too old: download the latest from this server's home page \
    (it serves the binary at /bin/shellshare) and share again.";

/// Run the shellshare server
pub async fn run(
    host: &str,
    port: u16,
    cleanup_interval_secs: u64,
    room_ttl_secs: u64,
) -> Result<(), Box<dyn std::error::Error>> {
    let listeners = bind(host, port).await?;
    serve_on(listeners, cleanup_interval_secs, room_ttl_secs).await
}

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
) -> Result<(), Box<dyn std::error::Error>> {
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
        ..Default::default()
    };

    // Socket.IO setup
    let (sio_layer, io) = SocketIo::builder()
        .with_state(app_state.clone())
        .build_layer();

    // Setup Socket.IO event handlers
    setup_socket_handlers(&io);

    // Store io in shared state (handlers will see this via Arc)
    let _ = app_state.io.set(io);

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
        servers.push(tokio::spawn(axum::serve(listener, app.clone()).into_future()));
    }
    for server in servers {
        server.await??;
    }

    Ok(())
}

/// Setup Socket.IO event handlers
fn setup_socket_handlers(io: &SocketIo) {
    io.ns("/", |socket: SocketRef, _state: SioState<AppState>| async move {
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
                if !tracked.contains(&room_name) {
                    tracked.push(room_name.clone());
                }
                // Release before the room snapshot below takes its own lock
                drop(socket_rooms);

                // Catch the viewer up if the room is live
                let snapshot = state.rooms.snapshot(&room_id).await;
                let broadcasting = snapshot.as_ref().is_some_and(|s| s.broadcasting);
                if let Some(snapshot) = snapshot {
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

                // Send user count directly to this client (guaranteed delivery)
                if let Err(e) = socket.emit("usersCount", &user_count) {
                    warn!("Failed to emit usersCount: {:?}", e);
                }

                // Also broadcast to notify other clients in the room
                let _ = socket.within(room_name).emit("usersCount", &user_count);
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

            // Update user counts for each room
            if let Some(io) = state.io.get() {
                for room in rooms {
                    // Count remaining users in room (this socket is already removed)
                    let user_count = io.of("/").map_or(0, |ns| {
                        ns.within(room.clone()).sockets().map_or(0, |s| s.len())
                    });
                    info!("Room {room:?} now has {user_count} users");
                    // Emit with fresh ns reference
                    if let Some(ns) = io.of("/") {
                        let _ = ns.within(room).emit("usersCount", &user_count);
                    }
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
/// The room is claimed/verified and mutated atomically, BEFORE emitting,
/// so a viewer joining mid-broadcast can't miss the message entirely.
/// Size is forwarded FIRST, so the terminal is resized before content
/// arrives.
async fn ingest(
    state: &AppState,
    room_id: &RoomId,
    secret: &str,
    size: Option<&serde_json::Value>,
    message: Option<&Bytes>,
) -> Result<(), rooms::Unauthorized> {
    state.rooms.append(room_id, secret, size, message).await?;

    if let Some(io) = state.io.get() {
        let room_name = room_id.as_str().to_string();
        if let Some(size) = size {
            if let Some(ns) = io.of("/") {
                let _ = ns.within(room_name.clone()).emit("size", size);
            }
        }
        // Emit ONLY the new message - as a binary attachment - and let
        // joins deliver the accumulated history
        if let Some(message) = message {
            if let Some(ns) = io.of("/") {
                let _ = ns.within(room_name).emit("message", message);
            }
        }
    }

    Ok(())
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

    if state
        .rooms
        .append(&room_id, &secret, None, None)
        .await
        .is_err()
    {
        return plain_response(StatusCode::UNAUTHORIZED, "Unauthorized");
    }

    info!("WS ingest connected for room {:?}", room_id);
    ws.on_upgrade(move |socket| ws_ingest_loop(socket, state, room_id, secret))
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
async fn ws_ingest_loop(mut socket: WebSocket, state: AppState, room_id: RoomId, secret: String) {
    // The connection itself is the aliveness signal: viewers show the
    // room as live while at least one ingest connection is attached.
    // A count of 0 means the room vanished between handshake and
    // upgrade - the loop below then ends on its first failed append.
    if state.rooms.broadcaster_connected(&room_id).await > 0 {
        emit_broadcasting(&state, &room_id, true);
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
                    let _ = state.rooms.delete(&room_id, &secret).await;
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
                (state.rooms.append(&room_id, &secret, None, None).await, false)
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

    if state.rooms.broadcaster_disconnected(&room_id).await == 0 {
        emit_broadcasting(&state, &room_id, false);
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

    if state.rooms.delete(&room_id, secret).await.is_err() {
        return plain_response(StatusCode::UNAUTHORIZED, "Unauthorized");
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
