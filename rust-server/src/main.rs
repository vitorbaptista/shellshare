//! Shellshare - Live terminal broadcasting
//!
//! A Rust implementation of the shellshare server using actix-web.

use actix_web::{web, App, HttpRequest, HttpResponse, HttpServer, middleware};
use clap::{Parser, Subcommand};
use rust_embed::Embed;
use serde::{Deserialize, Serialize};
use std::collections::HashMap;
use std::sync::Arc;
use tokio::sync::RwLock;
use tracing::{info, Level};
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

        /// MongoDB connection URI (not used yet - using in-memory storage)
        #[arg(long, env = "MONGODB_URI", default_value = "mongodb://localhost:27017/shellshare")]
        mongodb_uri: String,
    },
}

/// Embedded static files from the public directory
#[derive(Embed)]
#[folder = "../public/"]
struct StaticAssets;

/// Embedded view templates
#[derive(Embed)]
#[folder = "templates/"]
struct Templates;

/// Application state
struct AppState {
    /// Authorization cache: room -> password
    auth_cache: RwLock<HashMap<String, String>>,
    /// Room data cache: room -> messages
    rooms_cache: RwLock<HashMap<String, RoomData>>,
}

#[derive(Default, Clone)]
struct RoomData {
    messages: Vec<String>,
    size: Option<serde_json::Value>,
}

impl AppState {
    fn new() -> Self {
        Self {
            auth_cache: RwLock::new(HashMap::new()),
            rooms_cache: RwLock::new(HashMap::new()),
        }
    }
}

/// Request body for POST /r/:room
#[derive(Debug, Deserialize)]
struct BroadcastRequest {
    message: Option<serde_json::Value>,
    size: Option<serde_json::Value>,
}

#[actix_web::main]
async fn main() -> std::io::Result<()> {
    // Initialize logging
    let subscriber = FmtSubscriber::builder()
        .with_max_level(Level::INFO)
        .finish();
    tracing::subscriber::set_global_default(subscriber)
        .expect("Failed to set tracing subscriber");

    let cli = Cli::parse();

    match cli.command {
        Commands::Server { host, port, mongodb_uri } => {
            run_server(&host, port, &mongodb_uri).await
        }
    }
}

async fn run_server(host: &str, port: u16, mongodb_uri: &str) -> std::io::Result<()> {
    info!("Starting shellshare server on {}:{}", host, port);
    info!("MongoDB URI: {} (not connected - using in-memory storage)", mongodb_uri);

    let app_state = web::Data::new(AppState::new());

    HttpServer::new(move || {
        App::new()
            .app_data(app_state.clone())
            // API routes
            .route("/", web::get().to(index_handler))
            .route("/r/{room}", web::get().to(room_page_handler))
            .route("/r/{room}", web::post().to(broadcast_handler))
            .route("/r/{room}", web::delete().to(delete_room_handler))
            // Static files (embedded) - must be last
            .route("/{filename:.*}", web::get().to(serve_static))
            .wrap(middleware::Logger::default())
    })
    .bind((host, port))?
    .run()
    .await
}

/// GET / - Home page
async fn index_handler(req: HttpRequest) -> HttpResponse {
    let user_agent = req
        .headers()
        .get("user-agent")
        .and_then(|v| v.to_str().ok())
        .unwrap_or("");
    
    let is_linux = user_agent.to_lowercase().contains("linux");
    
    match Templates::get("index.html") {
        Some(content) => {
            let html = String::from_utf8_lossy(&content.data);
            // Simple template replacement for isLinux
            let html = if is_linux {
                html.replace("class=\"instructions-macos\"", "class=\"instructions-linux\"")
                    .replace("id=\"os-macos\" name=\"os\" value=\"macos\" checked", 
                             "id=\"os-macos\" name=\"os\" value=\"macos\"")
                    .replace("id=\"os-linux\" name=\"os\" value=\"linux\"",
                             "id=\"os-linux\" name=\"os\" value=\"linux\" checked")
            } else {
                html.to_string()
            };
            
            HttpResponse::Ok()
                .content_type("text/html; charset=utf-8")
                .body(html)
        }
        None => {
            HttpResponse::InternalServerError().body("Template not found")
        }
    }
}

/// GET /r/:room - Room page
async fn room_page_handler(_path: web::Path<String>) -> HttpResponse {
    match Templates::get("room.html") {
        Some(content) => {
            HttpResponse::Ok()
                .content_type("text/html; charset=utf-8")
                .body(content.data.into_owned())
        }
        None => {
            HttpResponse::InternalServerError().body("Template not found")
        }
    }
}

/// POST /r/:room - Broadcast message to room
async fn broadcast_handler(
    path: web::Path<String>,
    req: HttpRequest,
    body: web::Json<BroadcastRequest>,
    state: web::Data<AppState>,
) -> HttpResponse {
    let room = format!("/{}", path.into_inner());
    let auth_header = req
        .headers()
        .get("Authorization")
        .and_then(|v| v.to_str().ok())
        .unwrap_or("");
    
    info!("Broadcast to room: {}, auth present: {}", room, !auth_header.is_empty());
    
    // Check authorization
    if !check_authorization(&state, &room, auth_header).await {
        return HttpResponse::Unauthorized().body("Unauthorized");
    }
    
    // Store message
    {
        let mut rooms = state.rooms_cache.write().await;
        let room_data = rooms.entry(room.clone()).or_default();
        
        if let Some(msg) = &body.message {
            if let Some(msg_str) = msg.as_str() {
                room_data.messages.push(msg_str.to_string());
            } else {
                // Store JSON value as string
                room_data.messages.push(msg.to_string());
            }
        }
        if body.size.is_some() {
            room_data.size = body.size.clone();
        }
    }
    
    // TODO: Emit to Socket.IO clients
    
    HttpResponse::Ok().body("OK")
}

/// DELETE /r/:room - Delete room
async fn delete_room_handler(
    path: web::Path<String>,
    req: HttpRequest,
    state: web::Data<AppState>,
) -> HttpResponse {
    let room = format!("/{}", path.into_inner());
    let auth_header = req
        .headers()
        .get("Authorization")
        .and_then(|v| v.to_str().ok())
        .unwrap_or("");
    
    info!("Delete room: {}, auth present: {}", room, !auth_header.is_empty());
    
    // Check authorization
    if !check_authorization(&state, &room, auth_header).await {
        return HttpResponse::Unauthorized().body("Unauthorized");
    }
    
    // Remove room data
    {
        let mut rooms = state.rooms_cache.write().await;
        rooms.remove(&room);
    }
    {
        let mut auth = state.auth_cache.write().await;
        auth.remove(&room);
    }
    
    HttpResponse::Accepted().body("Accepted")
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
async fn serve_static(path: web::Path<String>) -> HttpResponse {
    let path = path.into_inner();
    
    match StaticAssets::get(&path) {
        Some(content) => {
            let mime = mime_guess::from_path(&path).first_or_octet_stream();
            HttpResponse::Ok()
                .content_type(mime.as_ref())
                .insert_header(("Cache-Control", "public, max-age=2678400"))
                .body(content.data.into_owned())
        }
        None => HttpResponse::NotFound().body("Not Found"),
    }
}
