//! Shellshare server - Live terminal broadcasting server
//!
//! This module contains the server implementation using axum + socketioxide.

mod binaries;
mod pages;
mod rooms;

use axum::{
    body::Body,
    extract::{DefaultBodyLimit, Path, Query, State},
    http::{header, HeaderMap, StatusCode},
    response::{IntoResponse, Response},
    routing::{delete, get, post},
    Json, Router,
};
use crate::protocol;
use binaries::BinaryDownloadQuery;
use rooms::{RoomId, Rooms};
use socketioxide::{
    extract::{Data, SocketRef, State as SioState},
    SocketIo,
};
use std::collections::HashMap;
use std::sync::Arc;
use std::time::Duration;
use tokio::sync::RwLock;
use tower_http::trace::TraceLayer;
use tracing::{info, warn};

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
    /// Socket.IO instance - wrapped in Arc<RwLock> so handlers see updates
    io: Arc<RwLock<Option<SocketIo>>>,
    /// Track which rooms each socket is in (for disconnect handling)
    socket_rooms: Arc<RwLock<HashMap<String, Vec<String>>>>,
    /// Cleanup configuration for abandoned rooms
    cleanup_config: CleanupConfig,
}

/// Request body for POST /r/:room - using Value to preserve null vs missing distinction
type BroadcastRequest = serde_json::Value;

/// Run the shellshare server
pub async fn run(
    host: &str,
    port: u16,
    cleanup_interval_secs: u64,
    room_ttl_secs: u64,
) -> Result<(), Box<dyn std::error::Error>> {
    info!("Starting shellshare server on {}:{}", host, port);
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
    {
        let mut io_lock = app_state.io.write().await;
        *io_lock = Some(io);
    }

    // Spawn background cleanup task for abandoned rooms
    spawn_cleanup_task(app_state.clone());

    // Build router
    let app = Router::new()
        // API routes
        .route("/", get(pages::index_handler))
        .route("/r/{*room}", get(pages::room_page_handler))
        .route("/r/{*room}", post(broadcast_handler))
        .route("/r/{*room}", delete(delete_room_handler))
        // Binary download (serves embedded binaries or self)
        .route("/bin/shellshare", get(serve_binary))
        // Static files - fallback
        .fallback(pages::serve_static)
        // State and middleware
        .with_state(app_state)
        .layer(sio_layer)
        .layer(DefaultBodyLimit::max(300 * 1024)) // 300KB limit
        .layer(TraceLayer::new_for_http());

    // Run server
    let listener = tokio::net::TcpListener::bind(format!("{host}:{port}")).await?;
    info!("Listening on {}:{}", host, port);
    axum::serve(listener, app).await?;

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
                if let Some(snapshot) = state.rooms.snapshot(&room_id).await {
                    // Send size FIRST - terminal must be sized before receiving content
                    if let Some(ref size) = snapshot.size {
                        if let Err(e) = socket.emit("size", size) {
                            warn!("Failed to emit size: {:?}", e);
                        }
                    }

                    // Then send accumulated message history
                    if let Some(msg) = snapshot.history {
                        if let Err(e) = socket.emit("message", msg.as_str()) {
                            warn!("Failed to emit message: {:?}", e);
                        }
                    }
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
            let io_guard = state.io.read().await;
            if let Some(ref io) = *io_guard {
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

/// POST /r/:room - Broadcast message to room
async fn broadcast_handler(
    Path(room_path): Path<String>,
    headers: HeaderMap,
    State(state): State<AppState>,
    Json(body): Json<BroadcastRequest>,
) -> impl IntoResponse {
    let room_id = RoomId::parse(&room_path);
    let secret = auth_secret(&headers);

    info!(
        "Broadcast to room: {:?}, auth present: {}",
        room_id,
        !secret.is_empty()
    );

    // Borrow the broadcastable parts of the body: a size carrying
    // dimensions, and a wire-format message string (both optional)
    let body_obj = body.as_object();
    let size = body_obj
        .and_then(|o| o.get("size"))
        .filter(|s| protocol::size_has_dimensions(s));
    let message = body_obj
        .and_then(|o| o.get("message"))
        .and_then(|m| m.as_str());

    // Claim/verify the room and store - atomically, and BEFORE emitting,
    // so a viewer joining mid-broadcast can't miss the message entirely
    if state
        .rooms
        .append(&room_id, secret, size, message)
        .await
        .is_err()
    {
        return plain_response(StatusCode::UNAUTHORIZED, "Unauthorized");
    }

    // Forward to viewers: size FIRST, so the terminal is resized before
    // content arrives
    {
        let io_guard = state.io.read().await;
        if let Some(io) = io_guard.as_ref() {
            let room_name = room_id.as_str().to_string();
            if let Some(size) = size {
                if let Some(ns) = io.of("/") {
                    let _ = ns.within(room_name.clone()).emit("size", size);
                }
            }
            // Emit ONLY the new message; accumulated history is sent on join
            if let Some(message) = message {
                if let Some(ns) = io.of("/") {
                    let _ = ns.within(room_name).emit("message", message);
                }
            }
        }
    }

    plain_response(StatusCode::OK, "OK")
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
