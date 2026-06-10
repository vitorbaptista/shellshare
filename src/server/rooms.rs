//! Room lifecycle: claiming, history, activity tracking, and eviction.
//!
//! Every room invariant lives behind this module's interface:
//!
//! - **First-caller-wins claiming**: the first broadcast to a room name
//!   claims it with that request's password; later requests must match.
//! - **Bounded history**: at most [`MAX_HISTORY_MESSAGES`] messages are
//!   kept per room for late joiners.
//! - **Canonical names**: a [`RoomId`] is normalized at construction, so
//!   no caller can accidentally address a phantom room.
//! - **Atomicity**: password, history, size, and activity live in one map
//!   behind one lock, so authorization and mutation cannot race - claims,
//!   appends, deletions, and eviction are each a single critical section.

use crate::protocol::MessageHistory;
use bytes::Bytes;
use percent_encoding::percent_decode_str;
use std::collections::HashMap;
use std::sync::Arc;
use std::time::{Duration, Instant};
use tokio::sync::RwLock;

/// Maximum number of messages to store per room for late joiners.
/// This prevents unbounded memory growth while keeping enough history
/// for a good late-joiner experience.
const MAX_HISTORY_MESSAGES: usize = 100;

/// A canonical room identifier.
///
/// Construction is the only place normalization happens: the `/r/` route
/// prefix is stripped and percent-encoding is decoded (Axum auto-decodes
/// HTTP paths, but Socket.IO sends raw strings).
#[derive(Clone, Debug, PartialEq, Eq, Hash)]
pub struct RoomId(String);

impl RoomId {
    pub fn parse(raw: &str) -> Self {
        let name = raw.trim_start_matches('/');
        let name = name.strip_prefix("r/").unwrap_or(name);
        // Strict UTF-8 decoding avoids conflating invalid UTF-8 with valid
        // inputs that contain the replacement character.
        let name = percent_decode_str(name)
            .decode_utf8()
            .map_or_else(|_| name.to_string(), std::borrow::Cow::into_owned);
        Self(name)
    }

    pub fn as_str(&self) -> &str {
        &self.0
    }
}

impl std::fmt::Display for RoomId {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.0)
    }
}

/// The broadcast was rejected: the room is claimed by another password.
#[derive(Debug)]
pub struct Unauthorized;

/// Everything a late joiner needs to catch up with a live room.
pub struct RoomSnapshot {
    /// Current terminal size; must be applied before the history
    pub size: Option<serde_json::Value>,
    /// Accumulated message history as one contiguous byte payload
    pub history: Option<Bytes>,
    /// Whether a broadcaster's ingest connection is currently attached
    pub broadcasting: bool,
}

/// One broadcasting session's state
struct Room {
    /// The password that claimed this room
    password: String,
    /// Message history, capped at [`MAX_HISTORY_MESSAGES`]
    messages: MessageHistory,
    /// Terminal size, forwarded verbatim to viewers
    size: Option<serde_json::Value>,
    /// Last activity (broadcast or viewer join), drives eviction
    last_activity: Instant,
    /// Ingest connections currently attached. More than one is possible
    /// around a client reconnect (the stale loop briefly overlaps the
    /// new one) or with two broadcasters sharing a password.
    broadcasters: usize,
}

impl Room {
    fn new(password: &str) -> Self {
        Self {
            password: password.to_string(),
            messages: MessageHistory::new(MAX_HISTORY_MESSAGES),
            size: None,
            last_activity: Instant::now(),
            broadcasters: 0,
        }
    }
}

/// All live rooms. Cheap to clone; clones share state.
#[derive(Clone, Default)]
pub struct Rooms {
    inner: Arc<RwLock<HashMap<RoomId, Room>>>,
}

impl Rooms {
    /// Record a broadcast: claim or verify the room, then store the size
    /// and/or message and refresh activity - atomically.
    ///
    /// A first broadcast claims the room with `secret`, even when it
    /// carries neither size nor message. `size` must already be validated
    /// by the caller (see `protocol::size_has_dimensions`); `message` is
    /// raw terminal bytes (`Bytes` clones share the buffer, so the caller
    /// keeps emitting from the same payload without copying).
    #[allow(clippy::significant_drop_tightening)] // the lock spanning verify+mutate IS the invariant
    pub async fn append(
        &self,
        room: &RoomId,
        secret: &str,
        size: Option<&serde_json::Value>,
        message: Option<&Bytes>,
    ) -> Result<(), Unauthorized> {
        let mut rooms = self.inner.write().await;
        let entry = rooms
            .entry(room.clone())
            .or_insert_with(|| Room::new(secret));

        if entry.password != secret {
            return Err(Unauthorized);
        }

        entry.last_activity = Instant::now();

        if let Some(size) = size {
            entry.size = Some(size.clone());
        }

        if let Some(message) = message {
            entry.messages.push(message.clone());
        }

        Ok(())
    }

    /// Catch-up data for a joining viewer; refreshes the room's activity.
    /// Returns `None` (and creates nothing) when the room does not exist.
    #[allow(clippy::significant_drop_tightening)] // touch + read must be one atomic step
    pub async fn snapshot(&self, room: &RoomId) -> Option<RoomSnapshot> {
        let mut rooms = self.inner.write().await;
        let entry = rooms.get_mut(room)?;
        entry.last_activity = Instant::now();
        Some(RoomSnapshot {
            size: entry.size.clone(),
            history: entry.messages.accumulated(),
            broadcasting: entry.broadcasters > 0,
        })
    }

    /// Record that a broadcaster's ingest connection attached to the
    /// room. Returns the new connection count (0 when the room vanished
    /// between the handshake and the upgrade).
    pub async fn broadcaster_connected(&self, room: &RoomId) -> usize {
        let mut rooms = self.inner.write().await;
        rooms.get_mut(room).map_or(0, |entry| {
            entry.broadcasters += 1;
            entry.broadcasters
        })
    }

    /// Record that a broadcaster's ingest connection detached. Returns
    /// the remaining count; 0 also covers a room already deleted (the
    /// `{"delete": true}` path removes the room before the loop ends).
    pub async fn broadcaster_disconnected(&self, room: &RoomId) -> usize {
        let mut rooms = self.inner.write().await;
        rooms.get_mut(room).map_or(0, |entry| {
            entry.broadcasters = entry.broadcasters.saturating_sub(1);
            entry.broadcasters
        })
    }

    /// Delete a room and release its password.
    ///
    /// Deleting a room that does not exist succeeds (the caller's goal -
    /// "this room is gone" - already holds).
    #[allow(clippy::significant_drop_tightening)] // verify + remove must be one atomic step
    pub async fn delete(&self, room: &RoomId, secret: &str) -> Result<(), Unauthorized> {
        let mut rooms = self.inner.write().await;
        match rooms.get(room) {
            Some(entry) if entry.password != secret => Err(Unauthorized),
            Some(_) => {
                rooms.remove(room);
                Ok(())
            }
            None => Ok(()),
        }
    }

    /// Remove rooms that have been inactive for longer than `ttl`.
    /// Returns how many rooms were evicted.
    pub async fn evict_stale(&self, ttl: Duration) -> usize {
        let now = Instant::now();
        let mut rooms = self.inner.write().await;
        let before = rooms.len();
        rooms.retain(|_, room| now.duration_since(room.last_activity) <= ttl);
        before - rooms.len()
    }
}
