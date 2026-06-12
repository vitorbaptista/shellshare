//! Raw-WebSocket viewer registry: which viewers are watching which
//! room, and how broadcast data reaches them.
//!
//! Each viewer connection owns a bounded queue of outgoing messages;
//! the fan-out tasks `try_send` into it and never block. A viewer that
//! falls so far behind that its queue fills is DISCONNECTED, not
//! silently skipped: a dropped frame would lose content with no signal
//! to anyone (and garble what renders after it), while a disconnect
//! makes the page reconnect and resync cleanly from the room history.
//! The browser client reconnects automatically, so the worst case for
//! a hopelessly slow viewer is a fresh-history reset loop instead of a
//! corrupt terminal.
//!
//! Queued payloads are refcounted [`Bytes`] clones of one broadcast
//! buffer, so queue depth costs almost nothing until a viewer actually
//! falls behind.

use bytes::Bytes;
use dashmap::DashMap;
use std::collections::HashMap;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Arc;
use tokio::sync::mpsc;

/// Outgoing queue depth per viewer, in messages. Fan-out coalesces
/// bursts into 64KB frames, so this is ~64MB of (shared) backlog - a
/// viewer must stall outright to hit it.
const VIEWER_QUEUE: usize = 2048;

/// One message to one viewer: terminal bytes, or a pre-serialized JSON
/// control event (`{"size":...}`, `{"usersCount":n}`,
/// `{"broadcasting":bool}`).
pub enum ViewerMsg {
    Bytes(Bytes),
    Control(String),
}

/// All connected viewers, by room. Cheap to clone; clones share state.
#[derive(Clone, Default)]
pub struct Viewers {
    rooms: Arc<DashMap<String, HashMap<u64, mpsc::Sender<ViewerMsg>>>>,
    next_id: Arc<AtomicU64>,
}

impl Viewers {
    /// Register a viewer; the connection task drains the returned
    /// receiver. Registration happens BEFORE the caller snapshots the
    /// room history, so no frame can be missed - a frame broadcast
    /// between the two arrives in the queue AND may sit in the
    /// snapshot, the same at-least-once duplicate already accepted
    /// around client reconnect replay.
    pub fn join(&self, room: &str) -> (u64, mpsc::Receiver<ViewerMsg>) {
        let id = self.next_id.fetch_add(1, Ordering::Relaxed);
        let (tx, rx) = mpsc::channel(VIEWER_QUEUE);
        self.rooms.entry(room.to_string()).or_default().insert(id, tx);
        (id, rx)
    }

    /// Remove a viewer (connection closed, or dropped for stalling).
    pub fn leave(&self, room: &str, id: u64) {
        if let Some(mut entry) = self.rooms.get_mut(room) {
            entry.remove(&id);
            if entry.is_empty() {
                drop(entry);
                // Last viewer gone: drop the room's (empty) map entry.
                // remove_if re-checks under the entry lock, so a viewer
                // joining concurrently is not swept away with it
                self.rooms.remove_if(room, |_, viewers| viewers.is_empty());
            }
        }
    }

    /// How many viewers are watching the room right now.
    pub fn count(&self, room: &str) -> usize {
        self.rooms.get(room).map_or(0, |viewers| viewers.len())
    }

    /// Queue terminal bytes to every viewer in the room. A viewer whose
    /// queue is full (or whose task is gone) is dropped from the room;
    /// its connection task sees the closed channel and disconnects it.
    pub fn send_bytes(&self, room: &str, payload: &Bytes) {
        self.send(room, || ViewerMsg::Bytes(payload.clone()));
    }

    /// Queue a control event (pre-serialized JSON) to every viewer.
    pub fn send_control(&self, room: &str, json: &str) {
        self.send(room, || ViewerMsg::Control(json.to_string()));
    }

    fn send(&self, room: &str, make: impl Fn() -> ViewerMsg) {
        if let Some(mut entry) = self.rooms.get_mut(room) {
            entry.retain(|_, tx| tx.try_send(make()).is_ok());
        }
    }
}
