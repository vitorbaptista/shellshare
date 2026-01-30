//! Shellshare server - Live terminal broadcasting server
//!
//! This module contains the server implementation using axum + socketioxide.

mod binaries;

use axum::{
    body::Body,
    extract::{DefaultBodyLimit, Path, Query, State},
    http::{header, HeaderMap, Method, Request, StatusCode},
    response::{IntoResponse, Response},
    routing::{delete, get, post},
    Json, Router,
};
use binaries::BinaryDownloadQuery;
use base64::{engine::general_purpose::STANDARD as BASE64, Engine as _};
use percent_encoding::percent_decode_str;
use rust_embed::Embed;
use socketioxide::{
    extract::{Data, SocketRef, State as SioState},
    SocketIo,
};
use std::collections::HashMap;
use std::sync::Arc;
use std::time::{Duration, Instant};
use tokio::sync::RwLock;
use tower_http::trace::TraceLayer;
use tracing::{info, warn};

/// Maximum number of messages to store per room for late joiners.
/// This prevents unbounded memory growth while keeping enough history
/// for a good late-joiner experience.
const MAX_HISTORY_MESSAGES: usize = 100;

/// Embedded static files from the public directory
#[derive(Embed, Clone)]
#[folder = "public/"]
struct StaticAssets;

/// Embedded view templates
#[derive(Embed, Clone)]
#[folder = "templates/"]
struct Templates;

/// Room data stored in memory
#[derive(Clone, Debug)]
struct RoomData {
    /// Accumulated messages (each is base64 encoded)
    messages: Vec<String>,
    /// Terminal size
    size: Option<serde_json::Value>,
    /// Last activity timestamp (broadcast or viewer join)
    last_activity: Instant,
}

impl Default for RoomData {
    fn default() -> Self {
        Self {
            messages: Vec::new(),
            size: None,
            last_activity: Instant::now(),
        }
    }
}

impl RoomData {
    /// Get accumulated message: decode all base64 messages, join, re-encode
    fn get_accumulated_message(&self) -> Option<String> {
        if self.messages.is_empty() {
            return None;
        }

        // Decode all messages and concatenate
        let mut accumulated = Vec::new();
        for msg in &self.messages {
            if let Ok(decoded) = BASE64.decode(msg) {
                accumulated.extend(decoded);
            }
        }

        if accumulated.is_empty() {
            None
        } else {
            Some(BASE64.encode(&accumulated))
        }
    }
}

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
    /// Authorization cache: room -> secret
    auth_cache: Arc<RwLock<HashMap<String, String>>>,
    /// Room data cache: room -> data
    rooms: Arc<RwLock<HashMap<String, RoomData>>>,
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
        .route("/", get(index_handler))
        .route("/r/{*room}", get(room_page_handler))
        .route("/r/{*room}", post(broadcast_handler))
        .route("/r/{*room}", delete(delete_room_handler))
        // Binary download (serves embedded binaries or self)
        .route("/bin/shellshare", get(serve_binary))
        // Static files - fallback
        .fallback(serve_static)
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
                // Normalize room name - strip /r/ prefix if present
                let room_name = normalize_room_name(&room);

                info!(
                    "Client {} joining room: {} (normalized: {})",
                    socket.id, room, room_name
                );

                // Join the socket to the room
                if let Err(e) = socket.join(room_name.clone()) {
                    warn!("Failed to join room {}: {:?}", room_name, e);
                }

                // Track this socket's rooms for disconnect handling
                {
                    let mut socket_rooms = state.socket_rooms.write().await;
                    socket_rooms
                        .entry(socket.id.to_string())
                        .or_default()
                        .push(room_name.clone());
                }

                // Get room data and update activity timestamp
                let room_data = {
                    let mut rooms = state.rooms.write().await;
                    if let Some(room_data) = rooms.get_mut(&room_name) {
                        room_data.last_activity = Instant::now();
                        Some(room_data.clone())
                    } else {
                        None
                    }
                };

                // Send existing room data if any
                if let Some(data) = room_data {
                    // Send size FIRST - terminal must be sized before receiving content
                    if let Some(ref size) = data.size {
                        if let Err(e) = socket.emit("size", size) {
                            warn!("Failed to emit size: {:?}", e);
                        }
                    }

                    // Then send accumulated message history
                    if let Some(msg) = data.get_accumulated_message() {
                        if let Err(e) = socket.emit("message", &msg) {
                            warn!("Failed to emit message: {:?}", e);
                        }
                    }
                }

                // Get fresh user count right before emissions
                let user_count = socket
                    .within(room_name.clone())
                    .sockets()
                    .map(|s| s.len())
                    .unwrap_or(0);

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
                        ns.within(room.clone())
                            .sockets()
                            .map(|s| s.len())
                            .unwrap_or(0)
                    });
                    info!("Room {room} now has {user_count} users");
                    // Emit with fresh ns reference
                    if let Some(ns) = io.of("/") {
                        let _ = ns.within(room).emit("usersCount", &user_count);
                    }
                }
            }
        });
    });
}

/// Normalize room name by stripping /r/ prefix and URL-decoding
fn normalize_room_name(room: &str) -> String {
    let room = room.trim_start_matches('/');
    let room = room.strip_prefix("r/").unwrap_or(room);
    // URL-decode to handle encoded characters from Socket.IO
    // (Axum auto-decodes HTTP paths, but Socket.IO sends raw strings)
    // Use strict UTF-8 decoding to avoid conflating invalid UTF-8 with
    // valid inputs that contain the replacement character.
    percent_decode_str(room)
        .decode_utf8()
        .map_or_else(|_| room.to_string(), std::borrow::Cow::into_owned)
}

/// Detect OS from User-Agent header
fn detect_os_from_user_agent(user_agent: &str) -> &'static str {
    let ua = user_agent.to_lowercase();
    if ua.contains("windows") {
        "windows"
    } else if ua.contains("mac") {
        "macos"
    } else {
        "linux" // default
    }
}

/// GET / - Home page
async fn index_handler(headers: HeaderMap) -> impl IntoResponse {
    match Templates::get("index.html") {
        Some(content) => {
            let user_agent = headers
                .get(header::USER_AGENT)
                .and_then(|v| v.to_str().ok())
                .unwrap_or("");

            let os = detect_os_from_user_agent(user_agent);
            let body_class = format!("instructions-{os}");

            let html = String::from_utf8_lossy(&content.data);
            let html = html
                .replace("{{BODY_CLASS}}", &body_class)
                .replace(
                    "{{LINUX_CHECKED}}",
                    if os == "linux" { " checked" } else { "" },
                )
                .replace(
                    "{{MACOS_CHECKED}}",
                    if os == "macos" { " checked" } else { "" },
                )
                .replace(
                    "{{WINDOWS_CHECKED}}",
                    if os == "windows" { " checked" } else { "" },
                );

            Response::builder()
                .status(StatusCode::OK)
                .header(header::CONTENT_TYPE, "text/html; charset=utf-8")
                .body(Body::from(html))
                .unwrap()
        }
        None => Response::builder()
            .status(StatusCode::INTERNAL_SERVER_ERROR)
            .header(header::CONTENT_TYPE, "text/plain; charset=utf-8")
            .body(Body::from("Template not found"))
            .unwrap(),
    }
}

/// GET /r/:room - Room page
async fn room_page_handler(Path(_room): Path<String>) -> impl IntoResponse {
    match Templates::get("room.html") {
        Some(content) => Response::builder()
            .status(StatusCode::OK)
            .header(header::CONTENT_TYPE, "text/html; charset=utf-8")
            .body(Body::from(content.data.into_owned()))
            .unwrap(),
        None => Response::builder()
            .status(StatusCode::INTERNAL_SERVER_ERROR)
            .header(header::CONTENT_TYPE, "text/plain; charset=utf-8")
            .body(Body::from("Template not found"))
            .unwrap(),
    }
}

/// POST /r/:room - Broadcast message to room
#[allow(clippy::significant_drop_tightening)] // Lock must span room_data mutations
async fn broadcast_handler(
    Path(room_path): Path<String>,
    headers: HeaderMap,
    State(state): State<AppState>,
    Json(body): Json<BroadcastRequest>,
) -> impl IntoResponse {
    let room_name = normalize_room_name(&room_path);

    let auth_header = headers
        .get(header::AUTHORIZATION)
        .and_then(|v| v.to_str().ok())
        .unwrap_or("");

    info!(
        "Broadcast to room: {}, auth present: {}",
        room_name,
        !auth_header.is_empty()
    );

    // Check authorization
    if !check_authorization(&state, &room_name, auth_header).await {
        return Response::builder()
            .status(StatusCode::UNAUTHORIZED)
            .header(header::CONTENT_TYPE, "text/plain; charset=utf-8")
            .body(Body::from("Unauthorized"))
            .unwrap();
    }

    // Store message and emit to Socket.IO clients
    {
        let mut rooms = state.rooms.write().await;
        let room_data = rooms.entry(room_name.clone()).or_default();

        // Update activity timestamp
        room_data.last_activity = Instant::now();

        // Extract message and size from body (preserving null vs missing)
        let body_obj = body.as_object();

        // Handle size FIRST - only if it has valid cols/rows fields
        // This must come before message so the terminal is resized before content arrives
        if let Some(obj) = body_obj {
            if let Some(size) = obj.get("size") {
                // Only store and emit size if it has the expected cols/rows fields
                // (client ignores size without these fields anyway)
                if let Some(size_obj) = size.as_object() {
                    if size_obj.contains_key("cols") && size_obj.contains_key("rows") {
                        room_data.size = Some(size.clone());

                        // Emit size to all clients in room
                        let io_guard = state.io.read().await;
                        if let Some(ref io) = *io_guard {
                            if let Some(ns) = io.of("/") {
                                let _ = ns.within(room_name.clone()).emit("size", size);
                            }
                        }
                    }
                }
            }
        }

        // Handle message (only if it's a non-null string)
        if let Some(message) = body_obj.and_then(|o| o.get("message")) {
            if let Some(msg_str) = message.as_str() {
                room_data.messages.push(msg_str.to_string());

                // Limit history size to prevent unbounded memory growth
                while room_data.messages.len() > MAX_HISTORY_MESSAGES {
                    room_data.messages.remove(0); // Remove oldest
                }

                // Emit ONLY the new message to all clients in room
                // (accumulated message is only sent when a new client joins)
                let io_guard = state.io.read().await;
                if let Some(ref io) = *io_guard {
                    if let Some(ns) = io.of("/") {
                        let _ = ns.within(room_name.clone()).emit("message", msg_str);
                    }
                }
            }
        }
    }

    Response::builder()
        .status(StatusCode::OK)
        .header(header::CONTENT_TYPE, "text/plain; charset=utf-8")
        .body(Body::from("OK"))
        .unwrap()
}

/// DELETE /r/:room - Delete room
async fn delete_room_handler(
    Path(room_path): Path<String>,
    headers: HeaderMap,
    State(state): State<AppState>,
) -> impl IntoResponse {
    let room_name = normalize_room_name(&room_path);

    let auth_header = headers
        .get(header::AUTHORIZATION)
        .and_then(|v| v.to_str().ok())
        .unwrap_or("");

    info!(
        "Delete room: {}, auth present: {}",
        room_name,
        !auth_header.is_empty()
    );

    // Check authorization
    if !check_authorization(&state, &room_name, auth_header).await {
        return Response::builder()
            .status(StatusCode::UNAUTHORIZED)
            .header(header::CONTENT_TYPE, "text/plain; charset=utf-8")
            .body(Body::from("Unauthorized"))
            .unwrap();
    }

    // Remove room data
    {
        let mut rooms = state.rooms.write().await;
        rooms.remove(&room_name);
    }
    {
        let mut auth = state.auth_cache.write().await;
        auth.remove(&room_name);
    }

    Response::builder()
        .status(StatusCode::ACCEPTED)
        .header(header::CONTENT_TYPE, "text/plain; charset=utf-8")
        .body(Body::from("Accepted"))
        .unwrap()
}

/// Check if the given authorization is valid for the room
async fn check_authorization(state: &AppState, room: &str, secret: &str) -> bool {
    let auth_cache = state.auth_cache.read().await;

    if let Some(existing_secret) = auth_cache.get(room) {
        // Room exists, check if secret matches
        existing_secret == secret
    } else {
        // Room doesn't exist, claim it with this secret
        drop(auth_cache);
        let mut auth_cache = state.auth_cache.write().await;
        // Double-check in case another request claimed it
        if let Some(existing_secret) = auth_cache.get(room) {
            return existing_secret == secret;
        }
        auth_cache.insert(room.to_string(), secret.to_string());
        true
    }
}

/// Spawn background task to clean up abandoned rooms
fn spawn_cleanup_task(state: AppState) {
    tokio::spawn(async move {
        let mut interval = tokio::time::interval(state.cleanup_config.interval);
        loop {
            interval.tick().await;
            cleanup_abandoned_rooms(&state).await;
        }
    });
}

/// Remove rooms that have been inactive for longer than the TTL
async fn cleanup_abandoned_rooms(state: &AppState) {
    let now = Instant::now();
    let ttl = state.cleanup_config.inactive_ttl;

    // Collect rooms to delete (inactive for > TTL)
    let rooms_to_delete: Vec<String> = {
        let rooms = state.rooms.read().await;
        rooms
            .iter()
            .filter(|(_, data)| now.duration_since(data.last_activity) > ttl)
            .map(|(name, _)| name.clone())
            .collect()
    };

    // Delete rooms
    if !rooms_to_delete.is_empty() {
        info!("Cleaning up {} abandoned rooms", rooms_to_delete.len());
        {
            let mut rooms = state.rooms.write().await;
            for room in &rooms_to_delete {
                rooms.remove(room);
            }
        }
        {
            let mut auth = state.auth_cache.write().await;
            for room in &rooms_to_delete {
                auth.remove(room);
            }
        }
    }
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

/// Serve embedded static files
async fn serve_static(req: Request<Body>) -> impl IntoResponse {
    let path = req.uri().path().trim_start_matches('/');
    let method = req.method();

    match StaticAssets::get(path) {
        Some(content) => {
            // Only allow GET and HEAD for existing static files
            if method != Method::GET && method != Method::HEAD {
                return Response::builder()
                    .status(StatusCode::METHOD_NOT_ALLOWED)
                    .header(header::ALLOW, "GET, HEAD")
                    .header(header::CONTENT_TYPE, "text/plain; charset=utf-8")
                    .body(Body::from("Method Not Allowed"))
                    .unwrap();
            }
            let mime = mime_guess::from_path(path).first_or_octet_stream();
            Response::builder()
                .status(StatusCode::OK)
                .header(header::CONTENT_TYPE, mime.as_ref())
                .header(header::CACHE_CONTROL, "public, max-age=2678400")
                .body(Body::from(content.data.into_owned()))
                .unwrap()
        }
        None => Response::builder()
            .status(StatusCode::NOT_FOUND)
            .header(header::CONTENT_TYPE, "text/plain; charset=utf-8")
            .body(Body::from("Not Found"))
            .unwrap(),
    }
}
