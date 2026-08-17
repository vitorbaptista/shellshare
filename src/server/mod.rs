//! Shellshare server - Live terminal broadcasting server

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
use binaries::BinaryDownloadQuery;
use bytes::Bytes;
use rooms::{RoomId, Rooms};
use std::future::IntoFuture;
use std::time::Duration;
use tower_http::trace::TraceLayer;
use tracing::{debug, info, warn};

#[derive(Clone, Debug)]
struct CleanupConfig {
    interval: Duration,
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

#[derive(Clone)]
struct AppState {
    rooms: Rooms,
    viewer_delivery: viewers::ViewerDelivery,
    cleanup_config: CleanupConfig,
    /// A no-op unless the operator opted in
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
    // Render the pages before taking a port: `serve` reports itself ready
    // as soon as it has bound one, so a template that panics after that
    // point would kill the server thread while the client goes on to
    // print a share link for a listener that is already gone.
    pages::warm();

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
    // Public and taking listeners, so a caller can reach this without
    // coming through `bind`. Warming twice is free - it is a `OnceLock`
    pages::warm();

    for listener in &listeners {
        info!("Starting shellshare server on {}", listener.local_addr()?);
    }
    info!(
        "Room cleanup: interval={}s, TTL={}s",
        cleanup_interval_secs, room_ttl_secs
    );

    // Constructing viewer delivery here removes the invalid "server is
    // running but its fan-out queues were never installed" state.
    let rooms = Rooms::default();
    let analytics = analytics::Analytics::new(analytics_config);
    let viewer_delivery = viewers::ViewerDelivery::start(rooms.clone(), analytics.clone());
    let app_state = AppState {
        rooms,
        viewer_delivery,
        cleanup_config: CleanupConfig {
            interval: Duration::from_secs(cleanup_interval_secs),
            inactive_ttl: Duration::from_secs(room_ttl_secs),
        },
        analytics,
    };

    spawn_cleanup_task(app_state.clone());

    let app = Router::new()
        .route("/", get(pages::index_handler))
        // Rendered, not static: it inlines templates/agent.mjs
        .route("/llms.txt", get(pages::llms_handler))
        .route("/r/{*room}", get(room_get_handler))
        .route("/r/{*room}", post(broadcast_handler))
        .route("/r/{*room}", delete(delete_room_handler))
        .route("/ws/r/{*room}", get(ws_ingest_handler))
        .route("/ws/v/r/{*room}", get(ws_view_handler))
        .route("/bin/shellshare", get(serve_binary))
        .fallback(pages::serve_static)
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

/// POST /r/:room - The retired HTTP broadcast endpoint. Always answers
/// 410 Gone with an upgrade prompt, whatever the body.
///
/// The body is extracted (and thereby drained) even though it's unused:
/// answering with unread request bytes in flight makes Windows abort
/// the connection (WSAECONNABORTED) before the client reads the 410.
async fn broadcast_handler(Path(room_path): Path<String>, _body: Bytes) -> impl IntoResponse {
    debug!("Legacy POST broadcast to room {:?} rejected", RoomId::parse(&room_path));
    plain_response(StatusCode::GONE, LEGACY_CLIENT_MESSAGE)
}

/// Store a broadcast and forward it to viewers.
///
/// The room is claimed/verified and mutated atomically BEFORE the fan-out
/// is queued, so a viewer joining mid-broadcast can't miss the message. Size is
/// queued FIRST, so the terminal is resized before content arrives.
///
/// Forwarding goes through [`viewers::ViewerDelivery::publish_ingest`]
/// instead of emitting inline: emitting to N viewer sockets costs O(N),
/// and paying it here would stall this broadcaster's ack turnaround -
/// measurably collapsing ingest throughput as the audience grows.
fn ingest(
    state: &AppState,
    room_id: &RoomId,
    secret: &str,
    size: Option<&serde_json::Value>,
    message: Option<&Bytes>,
) -> Result<(), rooms::Unauthorized> {
    let _ = state.rooms.append(room_id, secret, size, message)?;
    state.viewer_delivery.publish_ingest(room_id, size, message);

    Ok(())
}

/// GET /ws/v/r/:room - raw-WebSocket viewer endpoint.
///
/// The room is the URL: no join handshake exists to lose. On connect the
/// server pushes, in order: the current `size` (a JSON text frame, so the
/// terminal is sized before content), the accumulated history (one binary
/// frame), the `broadcasting` state, and the current `usersCount`. After
/// that, binary frames are live terminal bytes and text frames are JSON
/// control events - the exact mirror of the ingest protocol.
async fn ws_view_handler(
    Path(room_path): Path<String>,
    State(state): State<AppState>,
    ws: WebSocketUpgrade,
) -> Response {
    let room_id = RoomId::parse(&room_path);
    let delivery = state.viewer_delivery;
    ws.on_upgrade(move |socket| async move { delivery.join(room_id, socket).await })
}

/// GET /ws/r/:room - WebSocket ingest for broadcasting clients.
///
/// Binary frames carry raw terminal bytes; text frames carry JSON control
/// messages (`{"size": {...}}` to resize, `{"reset": true}` to clear a
/// reused room's history at the start of a new session, `{"delete": true}`
/// to delete the room - the retired exit path older clients still send).
/// The room is claimed - or the password verified - at upgrade time, so an
/// unauthorized client is rejected with 401 before the connection is
/// established.
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
    // The history's byte budget can still be overshot by one frame - the
    // newest chunk always survives, whatever its size - so the frame size
    // is what actually closes the ceiling. Without this a single frame
    // could be tungstenite's 64MiB default and sit in the room for the
    // whole TTL.
    ws.max_message_size(MAX_INGEST_FRAME)
        .on_upgrade(move |socket| ws_ingest_loop(socket, state, room_id, secret, claimed))
}

/// Largest ingest frame accepted. Must stay >= the client's
/// `MAX_BUFFERED_BYTES` (`src/cli/ws.rs`), which is the largest chunk it
/// will ever hold: a chunk the client keeps but the server refuses sits
/// at the head of its replay buffer and is rewritten on every reconnect,
/// so nothing behind it is ever delivered. They are equal today; lower
/// this, or raise that, and the two must move together.
const MAX_INGEST_FRAME: usize = 1024 * 1024;

/// Largest control (text) frame accepted. Control messages are a few
/// hundred bytes; the cap exists because the `size` value is stored verbatim for the room's whole life and re-sent
/// to every viewer on connect - outside the history budget. Without a
/// bound, one bloated `size` (the protocol only requires that `cols` and
/// `rows` be present) pins megabytes per room, since a parsed
/// `serde_json::Value` costs several times its wire size.
const MAX_CONTROL_FRAME: usize = 4 * 1024;

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
    // A count of 0 means the room vanished between handshake and upgrade
    // - the loop below then ends on its first failed append.
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
        // Size frames add no bytes, so their ack repeats the current
        // count - a no-op for the client's buffer, but it lets a sender
        // await durability of a resize
        let (result, ack) = match msg {
            WsMessage::Binary(bytes) => {
                let frame_len = bytes.len() as u64;
                let result = ingest(&state, &room_id, &secret, None, Some(&bytes));
                if result.is_ok() {
                    received_bytes += frame_len;
                }
                (result, true)
            }
            WsMessage::Text(text) if text.len() > MAX_CONTROL_FRAME => {
                warn!(
                    "WS ingest for room {:?}: {} byte control frame over the {} cap; ignoring",
                    room_id,
                    text.len(),
                    MAX_CONTROL_FRAME
                );
                // Still proof the peer is alive, so the room stays fresh
                (state.rooms.append(&room_id, &secret, None, None).map(|_| ()), false)
            }
            WsMessage::Text(text) => {
                let Ok(body) = serde_json::from_str::<serde_json::Value>(&text) else {
                    // Unparseable, but it still proves the peer is alive:
                    // refresh the room, or a client sending only these
                    // would hold the connection open while its room aged
                    // out underneath it
                    let result = state.rooms.append(&room_id, &secret, None, None);
                    if result.is_err() {
                        break;
                    }
                    continue;
                };
                if body.get("delete").and_then(serde_json::Value::as_bool) == Some(true) {
                    // Deleting removes the room, so this is the last chance
                    // to know the live segment's length (the loop's own
                    // detach below finds nothing)
                    if let Ok(Some(duration)) = state.rooms.delete(&room_id, &secret) {
                        state
                            .analytics
                            .broadcast_ended(&secret, room_id.as_str(), duration);
                    }
                    break;
                }
                if body.get("reset").and_then(serde_json::Value::as_bool) == Some(true) {
                    // Tabs already open are mid-render of the old session,
                    // so they get the same clear the reconnect path runs
                    if state.rooms.reset(&room_id, &secret).is_err() {
                        break;
                    }
                    state
                        .viewer_delivery
                        .publish_control(&room_id, viewers::ViewerControl::Reset);
                    continue;
                }
                let size = body
                    .get("size")
                    .filter(|s| protocol::size_has_dimensions(s));
                size.map_or_else(
                    // Some other control message, or a size with no
                    // dimensions: nothing to store, but the peer is alive
                    || (state.rooms.append(&room_id, &secret, None, None).map(|_| ()), false),
                    |size| (ingest(&state, &room_id, &secret, Some(size), None), true),
                )
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
/// silent past [`INGEST_IDLE_TIMEOUT`].
async fn recv_with_timeout(
    socket: &mut WebSocket,
) -> Option<Result<WsMessage, axum::Error>> {
    tokio::time::timeout(INGEST_IDLE_TIMEOUT, socket.recv())
        .await
        .ok()
        .flatten()
}

fn emit_broadcasting(state: &AppState, room_id: &RoomId, live: bool) {
    state
        .viewer_delivery
        .publish_control(room_id, viewers::ViewerControl::Broadcasting(live));
}

/// GET /r/:room - the viewer page, or - with a `.bin` suffix - the
/// room's accumulated history as raw bytes.
///
/// `/r/<room>.bin` is the agent-friendly consumer door: the body is
/// exactly what a WebSocket viewer receives as its history frame -
/// opaque, self-delimiting ciphertext records when the broadcast is
/// encrypted, or plaintext terminal bytes otherwise. A non-browser
/// consumer decrypts client-side with the key from the share link's
/// #fragment, so the server stays an opaque relay. (A room whose name
/// literally ends in `.bin` is shadowed by this suffix; auto-generated
/// room ids are alphanumeric, so only an explicit `--room ...bin`
/// collides.)
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
            let (noindex, value) = pages::noindex_header();
            Response::builder()
                .status(StatusCode::OK)
                .header(header::CONTENT_TYPE, "application/octet-stream")
                // Live, per-room, mutating bytes - never storable. The
                // header has to be explicit because a CDN in front of us
                // reads the `.bin` suffix as a static asset: with no
                // origin `Cache-Control`, Cloudflare's default extension
                // list gave this endpoint a month-long TTL and served
                // agents a frozen snapshot with no way to tell. A short
                // `max-age` would only shrink that window; the body is
                // never reusable, so `no-store` is the honest answer.
                .header(header::CACHE_CONTROL, "no-store")
                // Not HTML, so it cannot carry the page's `noindex`
                // meta tag - and it is the response that would actually
                // hold a broadcast's bytes
                .header(noindex, value)
                .body(Body::from(body))
                .unwrap()
        }
        None => plain_response(StatusCode::NOT_FOUND, "Room not found"),
    }
}

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

fn auth_secret(headers: &HeaderMap) -> &str {
    headers
        .get(header::AUTHORIZATION)
        .and_then(|v| v.to_str().ok())
        .unwrap_or("")
}

fn plain_response(status: StatusCode, body: &'static str) -> Response {
    Response::builder()
        .status(status)
        .header(header::CONTENT_TYPE, "text/plain; charset=utf-8")
        .body(Body::from(body))
        .unwrap()
}

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

/// Serve platform-specific binary or fallback to self. Supports `?os=`
/// and User-Agent detection; see [`binaries::serve_binary`].
async fn serve_binary(
    query: Query<BinaryDownloadQuery>,
    headers: HeaderMap,
) -> impl IntoResponse {
    binaries::serve_binary(query, headers).await
}
