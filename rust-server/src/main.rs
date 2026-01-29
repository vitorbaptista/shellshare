//! Shellshare - Live terminal broadcasting
//!
//! A Rust implementation of the shellshare server using axum + socketioxide.

use axum::{
    body::Body,
    extract::{Path, State, DefaultBodyLimit},
    http::{header, HeaderMap, Request, StatusCode, Method},
    response::{Html, IntoResponse, Response},
    routing::{get, post, delete},
    Json, Router,
};
use base64::{engine::general_purpose::STANDARD as BASE64, Engine as _};
use percent_encoding::percent_decode_str;
use clap::{Parser, Subcommand};
use rust_embed::Embed;
use serde::Deserialize;
use socketioxide::{
    extract::{Data, SocketRef, State as SioState},
    SocketIo,
};
use std::collections::HashMap;
use std::sync::Arc;
use tokio::sync::RwLock;
use tower_http::trace::TraceLayer;
use tracing::{info, warn, Level};
use tracing_subscriber::FmtSubscriber;

/// Shellshare - Live terminal broadcasting
#[derive(Parser)]
#[command(name = "shellshare")]
#[command(author, version, about, long_about = None)]
struct Cli {
    #[command(subcommand)]
    command: Commands,
}

#[derive(Subcommand)]
enum Commands {
    /// Start the shellshare server
    Server {
        /// Host to bind to
        #[arg(short = 'H', long, default_value = "0.0.0.0")]
        host: String,

        /// Port to listen on
        #[arg(short, long, default_value = "3000")]
        port: u16,
    },
}

/// Embedded static files from the public directory
#[derive(Embed, Clone)]
#[folder = "../public/"]
struct StaticAssets;

/// Embedded view templates
#[derive(Embed, Clone)]
#[folder = "templates/"]
struct Templates;

/// Room data stored in memory
#[derive(Default, Clone, Debug)]
struct RoomData {
    /// Accumulated messages (each is base64 encoded)
    messages: Vec<String>,
    /// Terminal size
    size: Option<serde_json::Value>,
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
}

/// Request body for POST /r/:room - using Value to preserve null vs missing distinction
type BroadcastRequest = serde_json::Value;

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    // Initialize logging
    let subscriber = FmtSubscriber::builder()
        .with_max_level(Level::INFO)
        .finish();
    tracing::subscriber::set_global_default(subscriber)?;

    let cli = Cli::parse();

    match cli.command {
        Commands::Server { host, port } => {
            run_server(&host, port).await?;
        }
    }

    Ok(())
}

async fn run_server(host: &str, port: u16) -> Result<(), Box<dyn std::error::Error>> {
    info!("Starting shellshare server on {}:{}", host, port);

    // Shared state
    let mut app_state = AppState::default();

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

    // Build router
    let app = Router::new()
        // API routes
        .route("/", get(index_handler))
        .route("/r/{*room}", get(room_page_handler))
        .route("/r/{*room}", post(broadcast_handler))
        .route("/r/{*room}", delete(delete_room_handler))
        // Static files - fallback
        .fallback(serve_static)
        // State and middleware
        .with_state(app_state)
        .layer(sio_layer)
        .layer(DefaultBodyLimit::max(300 * 1024)) // 300KB limit
        .layer(TraceLayer::new_for_http());

    // Run server
    let listener = tokio::net::TcpListener::bind(format!("{}:{}", host, port)).await?;
    info!("Listening on {}:{}", host, port);
    axum::serve(listener, app).await?;

    Ok(())
}

/// Setup Socket.IO event handlers
fn setup_socket_handlers(io: &SocketIo) {
    io.ns("/", |socket: SocketRef, state: SioState<AppState>| async move {
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

                // Get room data
                let rooms = state.rooms.read().await;
                let room_data = rooms.get(&room_name).cloned();
                drop(rooms);

                // Count users in room
                let user_count = socket
                    .within(room_name.clone())
                    .sockets()
                    .map(|s| s.len())
                    .unwrap_or(0);

                // Send user count to this client
                if let Err(e) = socket.emit("usersCount", &user_count) {
                    warn!("Failed to emit usersCount: {:?}", e);
                }

                // Send existing room data if any
                if let Some(data) = room_data {
                    // Send accumulated message
                    if let Some(msg) = data.get_accumulated_message() {
                        if let Err(e) = socket.emit("message", &msg) {
                            warn!("Failed to emit message: {:?}", e);
                        }
                    }

                    // Send size if set
                    if let Some(ref size) = data.size {
                        if let Err(e) = socket.emit("size", size) {
                            warn!("Failed to emit size: {:?}", e);
                        }
                    }
                }

                // Broadcast updated user count to all in room (including this client)
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
                    let user_count = if let Some(ns) = io.of("/") {
                        ns.within(room.clone()).sockets().map(|s| s.len()).unwrap_or(0)
                    } else {
                        0
                    };
                    info!("Room {} now has {} users", room, user_count);
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
    let room = if room.starts_with("r/") {
        &room[2..]
    } else {
        room
    };
    // URL-decode to handle encoded characters from Socket.IO
    // (Axum auto-decodes HTTP paths, but Socket.IO sends raw strings)
    // Use strict UTF-8 decoding to avoid conflating invalid UTF-8 with
    // valid inputs that contain the replacement character.
    match percent_decode_str(room).decode_utf8() {
        Ok(decoded) => decoded.into_owned(),
        // On invalid UTF-8, fall back to the original (prefix-stripped)
        // room name to avoid silent collisions.
        Err(_) => room.to_string(),
    }
}

/// GET / - Home page
async fn index_handler(headers: HeaderMap) -> impl IntoResponse {
    let user_agent = headers
        .get(header::USER_AGENT)
        .and_then(|v| v.to_str().ok())
        .unwrap_or("");

    let is_linux = user_agent.to_lowercase().contains("linux");

    match Templates::get("index.html") {
        Some(content) => {
            let html = String::from_utf8_lossy(&content.data);
            // Simple template replacement for isLinux
            let html = if is_linux {
                html.replace(
                    "class=\"instructions-macos\"",
                    "class=\"instructions-linux\"",
                )
                .replace(
                    "id=\"os-macos\" name=\"os\" value=\"macos\" checked",
                    "id=\"os-macos\" name=\"os\" value=\"macos\"",
                )
                .replace(
                    "id=\"os-linux\" name=\"os\" value=\"linux\"",
                    "id=\"os-linux\" name=\"os\" value=\"linux\" checked",
                )
            } else {
                html.to_string()
            };

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
        Some(content) => {
            Response::builder()
                .status(StatusCode::OK)
                .header(header::CONTENT_TYPE, "text/html; charset=utf-8")
                .body(Body::from(content.data.into_owned()))
                .unwrap()
        }
        None => Response::builder()
            .status(StatusCode::INTERNAL_SERVER_ERROR)
            .header(header::CONTENT_TYPE, "text/plain; charset=utf-8")
            .body(Body::from("Template not found"))
            .unwrap(),
    }
}

/// POST /r/:room - Broadcast message to room
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

                // Emit accumulated message to all clients in room
                if let Some(accumulated) = room_data.get_accumulated_message() {
                    let io_guard = state.io.read().await;
                    if let Some(ref io) = *io_guard {
                        if let Some(ns) = io.of("/") {
                            let _ = ns.within(room_name.clone()).emit("message", &accumulated);
                        }
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
