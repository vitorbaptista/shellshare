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
//! - **Rooms outlive their broadcaster**: a session ends by closing, not
//!   by deleting, so a shared link keeps working after the command that
//!   made it exits. TTL eviction is the only teardown left.
//! - **Only the broadcaster postpones eviction**: [`Rooms::append`]
//!   (frames, sizes, keepalive pings) refreshes a room's activity;
//!   reads never do, so no amount of watching or polling can pin a
//!   finished broadcast alive.
//! - **Atomicity**: password, history, size, and activity live in one
//!   entry behind one per-room lock (a sharded [`DashMap`]), so
//!   authorization and mutation cannot race - claims, appends, and
//!   deletions are each a single critical section on their room. Locking
//!   per room instead of per map keeps concurrent broadcasters from
//!   serializing each other's ingest on a shared lock.

use crate::protocol::MessageHistory;
use bytes::Bytes;
use dashmap::DashMap;
use percent_encoding::percent_decode_str;
use std::sync::Arc;
use std::time::{Duration, Instant};

/// Maximum number of messages to store per room for late joiners.
/// This prevents unbounded memory growth while keeping enough history
/// for a good late-joiner experience.
const MAX_HISTORY_MESSAGES: usize = 200;

/// A canonical room identifier.
///
/// Construction is the only place normalization happens: the `/r/` route
/// prefix is stripped and percent-encoding is decoded (Axum auto-decodes
/// HTTP paths, but other callers may pass raw strings).
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

/// What an authorized [`Rooms::append`] did to the room.
#[derive(Debug)]
pub enum Appended {
    /// The room did not exist; this call claimed it.
    Claimed,
    /// The room existed and the password matched.
    Verified,
}

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
    /// When the current live segment started (broadcaster count went
    /// 0 -> 1). Broadcast durations are measured per segment, so a
    /// reconnect yields two events whose durations sum to the real
    /// broadcast time instead of overlapping spans measured from the
    /// claim. `None` while no broadcaster is attached.
    live_since: Option<Instant>,
    /// Last activity, which drives eviction. Only the broadcaster
    /// refreshes it (its frames and keepalive pings, via
    /// [`Rooms::append`]) - reads deliberately do not, so a room cannot
    /// be pinned alive by being watched or polled
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
            live_since: None,
            last_activity: Instant::now(),
            broadcasters: 0,
        }
    }
}

/// All live rooms. Cheap to clone; clones share state.
#[derive(Clone, Default)]
pub struct Rooms {
    inner: Arc<DashMap<RoomId, Room>>,
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
    ///
    /// Reports whether this call [`Appended::Claimed`] the room, so the
    /// caller can observe room creation without a separate lookup.
    #[allow(clippy::significant_drop_tightening)] // the guard spanning verify+mutate IS the invariant
    pub fn append(
        &self,
        room: &RoomId,
        secret: &str,
        size: Option<&serde_json::Value>,
        message: Option<&Bytes>,
    ) -> Result<Appended, Unauthorized> {
        let mut appended = Appended::Verified;
        // The entry guard is the room's lock: verify + mutate is one
        // critical section on this room and no others
        let mut entry = self.inner.entry(room.clone()).or_insert_with(|| {
            appended = Appended::Claimed;
            Room::new(secret)
        });

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

        Ok(appended)
    }

    /// Catch-up data for a joining viewer or a one-shot `/r/:room.bin`
    /// read. Returns `None` (and creates nothing) when the room does not
    /// exist.
    ///
    /// Reading deliberately does NOT refresh the room's activity. A room
    /// outlives its broadcaster, so TTL eviction is the only teardown
    /// left, and only the broadcaster may postpone it: otherwise a
    /// forgotten browser tab reconnecting, or an agent polling the
    /// snapshot, would pin a finished broadcast forever. A room whose
    /// broadcast is still live is kept alive by that broadcaster's own
    /// frames and 30s keepalive pings.
    pub fn snapshot(&self, room: &RoomId) -> Option<RoomSnapshot> {
        self.inner.get(room).map(|entry| RoomSnapshot {
            size: entry.size.clone(),
            history: entry.messages.accumulated(),
            broadcasting: entry.broadcasters > 0,
        })
    }

    /// Clear a room's history while keeping its claim.
    ///
    /// Unlike [`Rooms::delete`], the room, its password, its size, and
    /// its live segment survive: the caller is a broadcaster starting a
    /// fresh session on a name it already owns. A room that does not
    /// exist is already clear, so that succeeds; a password mismatch is
    /// [`Unauthorized`].
    ///
    /// Only the room's SOLE broadcaster may clear it. The frame carries
    /// no ordering guarantee across connections, and both ways that can
    /// go wrong need a second one attached: a previous session's ingest
    /// task still draining its last frames would append them after the
    /// clear, and a stalled first connection's late reset would wipe
    /// what its own reconnect already stored. Skipping the clear
    /// degrades to appending; the alternative destroys history.
    ///
    /// The size is deliberately kept: the resetting client re-sends its
    /// own size on this fresh connection anyway, so clearing it would
    /// only open a window where a joining viewer gets none - and a
    /// viewer with no size never learns the stream is encrypted (the
    /// flag rides inside the size message), so it would render raw
    /// ciphertext instead of saying the key is missing.
    pub fn reset(&self, room: &RoomId, secret: &str) -> Result<(), Unauthorized> {
        let Some(mut entry) = self.inner.get_mut(room) else {
            return Ok(());
        };
        if entry.password != secret {
            return Err(Unauthorized);
        }
        if entry.broadcasters > 1 {
            return Ok(());
        }
        entry.messages = MessageHistory::new(MAX_HISTORY_MESSAGES);
        entry.last_activity = Instant::now();
        Ok(())
    }

    /// Record that a broadcaster's ingest connection attached to the
    /// room. Returns the new connection count (0 when the room vanished
    /// between the handshake and the upgrade, or was re-claimed by
    /// another password in that window - this connection must not touch
    /// the new owner's bookkeeping).
    pub fn broadcaster_connected(&self, room: &RoomId, secret: &str) -> usize {
        self.inner.get_mut(room).map_or(0, |mut entry| {
            if entry.password != secret {
                return 0;
            }
            if entry.broadcasters == 0 {
                entry.live_since = Some(Instant::now());
            }
            entry.broadcasters += 1;
            entry.broadcasters
        })
    }

    /// Record that a broadcaster's ingest connection detached. Returns
    /// the remaining count - 0 also covers a room already deleted (the
    /// `{"delete": true}` path removes the room before the loop ends) -
    /// and, when this detach ended the room's live segment, how long
    /// that segment ran.
    ///
    /// A stale loop whose room was meanwhile re-claimed by another
    /// password must not decrement the new owner's count or consume its
    /// segment; the mismatch leaves the room untouched.
    pub fn broadcaster_disconnected(
        &self,
        room: &RoomId,
        secret: &str,
    ) -> (usize, Option<Duration>) {
        self.inner.get_mut(room).map_or((0, None), |mut entry| {
            if entry.password != secret {
                return (entry.broadcasters, None);
            }
            entry.broadcasters = entry.broadcasters.saturating_sub(1);
            let duration = if entry.broadcasters == 0 {
                entry.live_since.take().map(|since| since.elapsed())
            } else {
                None
            };
            (entry.broadcasters, duration)
        })
    }

    /// Delete a room and release its password.
    ///
    /// Deleting a room that does not exist succeeds (the caller's goal -
    /// "this room is gone" - already holds). Deletion cutting short a
    /// live segment reports the segment's duration, since nobody can ask
    /// the room afterwards; `Ok(None)` means no broadcast was live.
    pub fn delete(&self, room: &RoomId, secret: &str) -> Result<Option<Duration>, Unauthorized> {
        // Entry, not get+remove: verify + remove must be one atomic
        // step on the room's lock
        match self.inner.entry(room.clone()) {
            dashmap::Entry::Occupied(entry) if entry.get().password != secret => {
                Err(Unauthorized)
            }
            dashmap::Entry::Occupied(entry) => {
                let duration = entry.get().live_since.map(|since| since.elapsed());
                entry.remove();
                Ok(duration)
            }
            dashmap::Entry::Vacant(_) => Ok(None),
        }
    }

    /// Remove rooms that have been inactive for longer than `ttl`.
    /// Returns how many rooms were evicted.
    pub fn evict_stale(&self, ttl: Duration) -> usize {
        let now = Instant::now();
        // Count inside the closure: a before/after len() difference is
        // not atomic across shards, so a room claimed mid-retain would
        // make it underflow
        let mut evicted = 0;
        self.inner.retain(|_, room| {
            // An attached broadcaster IS the room's liveness, so a live
            // broadcast is never evicted however quiet it goes. Relying
            // on activity alone would mean a producer that says nothing
            // for longer than the TTL (`journalctl -f` on an idle unit)
            // loses its room mid-broadcast - keepalive pings only refresh
            // it every 30s, which a shorter TTL outruns. A connection
            // that actually died is detached by the ingest idle timeout
            // first, and only then does the room start aging.
            let keep = room.broadcasters > 0
                || now.duration_since(room.last_activity) <= ttl;
            if !keep {
                evicted += 1;
            }
            keep
        });
        evicted
    }
}
