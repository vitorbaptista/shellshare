//! Raw-WebSocket viewer delivery: connection lifecycle, catch-up replay,
//! live fan-out, and membership convergence.
//!
//! Each viewer connection owns a bounded queue of outgoing messages;
//! the fan-out tasks `try_send` into it and never block. A viewer that
//! falls so far behind that its queue fills is DISCONNECTED, not
//! silently skipped: a dropped frame would lose content with no signal
//! to anyone (and garble what renders after it), while a disconnect
//! makes the page reconnect and resync cleanly from the room history.
//!
//! Queued payloads are refcounted [`Bytes`] clones of one broadcast
//! buffer, so queue depth costs almost nothing until a viewer actually
//! falls behind.

use super::analytics::Analytics;
use super::rooms::{RoomId, Rooms};
use axum::extract::ws::{Message as WsMessage, WebSocket};
use bytes::Bytes;
use dashmap::DashMap;
use futures_util::{SinkExt, StreamExt};
use std::collections::{HashMap, HashSet};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Arc;
use std::time::Duration;
use tokio::sync::mpsc;
use tracing::info;

/// Outgoing queue depth per viewer, in messages. Fan-out coalesces
/// bursts into frames of up to 64KB, so this is up to ~128MB of
/// (shared, refcounted) backlog - a viewer must stall outright to
/// hit it.
const VIEWER_QUEUE: usize = 2048;

/// One message to one viewer: terminal bytes, or a pre-serialized JSON
/// control event (`{"size":...}`, `{"usersCount":n}`,
/// `{"broadcasting":bool}`).
enum ViewerMsg {
    Bytes(Bytes),
    Control(String),
}

/// All connected viewers, by room. Cheap to clone; clones share state.
#[derive(Clone, Default)]
struct Viewers {
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
    fn join(&self, room: &str) -> (u64, mpsc::Receiver<ViewerMsg>) {
        let id = self.next_id.fetch_add(1, Ordering::Relaxed);
        let (tx, rx) = mpsc::channel(VIEWER_QUEUE);
        self.rooms
            .entry(room.to_string())
            .or_default()
            .insert(id, tx);
        (id, rx)
    }

    fn leave(&self, room: &str, id: u64) {
        if let Some(mut entry) = self.rooms.get_mut(room) {
            entry.remove(&id);
            if entry.is_empty() {
                drop(entry);
                // remove_if re-checks under the entry lock, so a viewer
                // joining concurrently is not swept away with the map entry
                self.rooms.remove_if(room, |_, viewers| viewers.is_empty());
            }
        }
    }

    fn count(&self, room: &str) -> usize {
        self.rooms.get(room).map_or(0, |viewers| viewers.len())
    }

    /// Queue terminal bytes to every viewer in the room. A viewer whose
    /// queue is full (or whose task is gone) is dropped from the room;
    /// its connection task sees the closed channel and disconnects it.
    fn send_bytes(&self, room: &str, payload: &Bytes) {
        self.send(room, || ViewerMsg::Bytes(payload.clone()));
    }

    fn send_control(&self, room: &str, json: &str) {
        self.send(room, || ViewerMsg::Control(json.to_string()));
    }

    fn send(&self, room: &str, make: impl Fn() -> ViewerMsg) {
        if let Some(mut entry) = self.rooms.get_mut(room) {
            entry.retain(|_, tx| tx.try_send(make()).is_ok());
        }
    }
}

/// Viewer-visible controls that bypass the ingest fan-out queue.
#[derive(Clone, Copy)]
pub enum ViewerControl {
    /// Clear the terminal for a returning broadcaster's fresh session.
    Reset,
    /// Whether at least one broadcaster is attached to the room.
    Broadcasting(bool),
}

/// Complete raw-WebSocket viewer delivery for every room.
///
/// Cheap to clone. Clones share the room snapshots, viewer registry, and
/// worker senders. The worker tasks capture only the registry, never this
/// handle, so dropping the final handle closes their channels.
#[derive(Clone)]
pub struct ViewerDelivery {
    rooms: Rooms,
    analytics: Analytics,
    viewers: Viewers,
    fanout: Arc<Vec<mpsc::UnboundedSender<FanoutItem>>>,
    usercount: mpsc::UnboundedSender<String>,
}

impl ViewerDelivery {
    /// Must be called from within a Tokio runtime. One fan-out task is started
    /// per available CPU, so concurrent rooms emit in parallel while one room
    /// always stays on one shard and preserves its accepted publish order.
    pub fn start(rooms: Rooms, analytics: Analytics) -> Self {
        let viewers = Viewers::default();
        let shards = std::thread::available_parallelism().map_or(4, std::num::NonZeroUsize::get);
        let mut fanout = Vec::with_capacity(shards);
        for _ in 0..shards {
            let (tx, rx) = mpsc::unbounded_channel();
            fanout.push(tx);
            tokio::spawn(fanout_loop(rx, viewers.clone()));
        }

        let (usercount, usercount_rx) = mpsc::unbounded_channel();
        tokio::spawn(usercount_loop(usercount_rx, viewers.clone()));

        Self {
            rooms,
            analytics,
            viewers,
            fanout: Arc::new(fanout),
            usercount,
        }
    }

    /// Queue a successfully stored ingest item for live viewers.
    ///
    /// The caller must invoke this only after [`Rooms::append`] succeeds.
    /// Size and payload deliberately share one queue item: size is an ordering
    /// barrier and must reach viewers before content from the same ingest.
    /// Delivery stays best-effort because viewer work must never fail an ingest
    /// acknowledgment after the room has stored the bytes.
    pub fn publish_ingest(
        &self,
        room: &RoomId,
        size: Option<&serde_json::Value>,
        payload: Option<&Bytes>,
    ) {
        let shard = room_shard(room.as_str(), self.fanout.len());
        let _ = self.fanout[shard].send(FanoutItem {
            room: room.as_str().to_string(),
            size: size.cloned(),
            payload: payload.cloned(),
        });
    }

    /// Publish a viewer control immediately, outside the ingest queue.
    ///
    /// This preserves the existing ordering: reset and broadcaster-status
    /// transitions may overtake bytes still waiting in a fan-out shard. Moving
    /// them into that queue would be an observable protocol change.
    pub fn publish_control(&self, room: &RoomId, control: ViewerControl) {
        let json = match control {
            ViewerControl::Reset => "{\"reset\":true}".to_string(),
            ViewerControl::Broadcasting(live) => format!("{{\"broadcasting\":{live}}}"),
        };
        self.viewers.send_control(room.as_str(), &json);
    }

    /// Run one raw-WebSocket viewer connection from catch-up through cleanup.
    ///
    /// Registration happens before the snapshot, so a concurrent frame can be
    /// delivered twice (history plus live queue) but can never be missed.
    pub async fn join(&self, room_id: RoomId, mut socket: WebSocket) {
        let room = room_id.as_str().to_string();
        let (viewer_id, mut rx) = self.viewers.join(&room);
        info!("WS viewer {viewer_id} joined room {room_id:?}");

        let snapshot = self.rooms.snapshot(&room_id);
        let broadcasting = snapshot.as_ref().is_some_and(|s| s.broadcasting);
        let room_exists = snapshot.is_some();
        let catch_up = async {
            if let Some(snap) = snapshot {
                if let Some(size) = snap.size {
                    let frame = serde_json::json!({ "size": size }).to_string();
                    timed_send(&mut socket, WsMessage::Text(frame.into())).await?;
                }
                if let Some(history) = snap.history {
                    timed_send(&mut socket, WsMessage::Binary(history)).await?;
                }
            }
            let frame = format!("{{\"broadcasting\":{broadcasting}}}");
            timed_send(&mut socket, WsMessage::Text(frame.into())).await?;
            let count = self.viewers.count(&room);
            timed_send(
                &mut socket,
                WsMessage::Text(format!("{{\"usersCount\":{count}}}").into()),
            )
            .await
        };
        if catch_up.await.is_err() {
            self.viewers.leave(&room, viewer_id);
            // The room may have seen this viewer in a count broadcast
            // during its brief membership; converge the others
            let _ = self.usercount.send(room);
            return;
        }
        // The rest of the room learns the new count via the coalescing task
        let _ = self.usercount.send(room.clone());
        // Joins to nonexistent rooms (dead links) are not an audience
        if room_exists {
            self.analytics
                .viewer_joined(room_id.as_str(), self.viewers.count(&room), broadcasting);
        }

        // Split the socket: the writer must never poll the read half. A
        // read poll per delivered message costs a wasted read syscall and
        // tungstenite read/write interplay - measured as ~4x the delivery
        // latency at thousands of viewers. The reader task only proves the
        // peer is alive (pongs and anything else it sends) and reports
        // when the connection dies.
        let (mut sink, mut stream) = socket.split();
        let started = tokio::time::Instant::now();
        let last_seen_ms = Arc::new(AtomicU64::new(0));
        let seen = last_seen_ms.clone();
        let mut reader = tokio::spawn(async move {
            while let Some(Ok(_)) = stream.next().await {
                let elapsed_ms = started.elapsed().as_millis().min(u128::from(u64::MAX));
                #[allow(clippy::cast_possible_truncation)] // bounded by the min above
                seen.store(elapsed_ms as u64, Ordering::Relaxed);
            }
        });
        let mut ping = tokio::time::interval(VIEWER_PING_INTERVAL);
        ping.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Delay);
        loop {
            tokio::select! {
                queued = rx.recv() => {
                    // Dropped by the registry for stalling: close, so the
                    // page reconnects and resyncs from history
                    let Some(first) = queued else { break };
                    if relay(&mut sink, &mut rx, first).await.is_err() {
                        break;
                    }
                }
                // The peer hung up (or errored): stop delivering
                _ = &mut reader => break,
                _ = ping.tick() => {
                    let last_seen = Duration::from_millis(
                        last_seen_ms.load(Ordering::Relaxed),
                    );
                    if started.elapsed().saturating_sub(last_seen) > VIEWER_IDLE_TIMEOUT {
                        info!("WS viewer {viewer_id} in {room_id:?} timed out");
                        break;
                    }
                    if timed_send(&mut sink, WsMessage::Ping(Vec::new().into()))
                        .await
                        .is_err()
                    {
                        break;
                    }
                }
            }
        }
        reader.abort();
        // Best effort: a Close frame lets the page distinguish a server
        // close from a network drop (it reconnects either way)
        let _ =
            tokio::time::timeout(Duration::from_secs(1), sink.send(WsMessage::Close(None))).await;
        self.viewers.leave(&room, viewer_id);
        info!("WS viewer {viewer_id} left room {room_id:?}");
        let _ = self.usercount.send(room);
    }
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

/// The viewer fan-out task: drains the queue and emits to viewers.
///
/// Whatever queued up while the previous emits ran is coalesced. Under
/// burst load this collapses thousands of tiny per-socket sends into a
/// few large ones, which is what keeps slow viewers' buffers from
/// overflowing into silent content loss. Terminal output is a raw byte
/// stream of whole frames, so concatenation is invisible to viewers.
///
/// A single task consumes the queue, so per-room ordering is exactly the
/// ingest order (cross-room ordering carries no meaning). The
/// store-then-queue gap means a viewer joining mid-burst may see a queued
/// frame around its history replay - duplicated, or even before the
/// replay containing it. Same class as the duplicate render already
/// accepted around client reconnect replay: delivery is at-least-once
/// end to end.
async fn fanout_loop(mut rx: mpsc::UnboundedReceiver<FanoutItem>, viewers: Viewers) {
    /// Payloads accumulated for one room, flushed as one emit.
    #[derive(Default)]
    struct Pending {
        chunks: Vec<Bytes>,
        len: usize,
    }

    fn flush(viewers: &Viewers, room: &str, p: Pending) {
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
        viewers.send_bytes(room, &payload);
    }

    while let Some(first) = rx.recv().await {
        let mut batch = vec![first];
        while let Ok(item) = rx.try_recv() {
            batch.push(item);
        }
        let mut pending: HashMap<String, Pending> = HashMap::new();
        for item in batch {
            if let Some(size) = item.size {
                // A size event is an ordering barrier within its room:
                // anything queued before it must reach viewers first
                if let Some(p) = pending.remove(&item.room) {
                    flush(&viewers, &item.room, p);
                }
                let control = serde_json::json!({ "size": size }).to_string();
                viewers.send_control(&item.room, &control);
            }
            if let Some(payload) = item.payload {
                match pending.entry(item.room) {
                    std::collections::hash_map::Entry::Occupied(mut e) => {
                        if e.get().len + payload.len() > FANOUT_MAX_BATCH {
                            let full = std::mem::take(e.get_mut());
                            flush(&viewers, e.key(), full);
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
            flush(&viewers, &room, p);
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
/// still converges on the exact final number.
async fn usercount_loop(mut rx: mpsc::UnboundedReceiver<String>, viewers: Viewers) {
    let mut rooms = HashSet::new();
    while let Some(first) = rx.recv().await {
        rooms.insert(first);
        while let Ok(room) = rx.try_recv() {
            rooms.insert(room);
        }
        for room in rooms.drain() {
            let count = viewers.count(&room);
            viewers.send_control(&room, &format!("{{\"usersCount\":{count}}}"));
        }
    }
}

/// Stable room -> fan-out shard assignment.
fn room_shard(room: &str, shards: usize) -> usize {
    use std::hash::{Hash, Hasher};
    let mut hasher = std::collections::hash_map::DefaultHasher::new();
    room.hash(&mut hasher);
    usize::try_from(hasher.finish() % shards.max(1) as u64).unwrap_or(0)
}

/// How long a viewer may go without sending anything (pong frames
/// answer our pings automatically in every browser) before the
/// connection is presumed dead. The same bound caps every write: a
/// peer that stops reading (frozen tab, zero TCP window) would
/// otherwise block `sink.send` forever, pinning the task and its
/// backlog - the select loop can't reach its idle check while a send
/// is in flight.
const VIEWER_IDLE_TIMEOUT: Duration = Duration::from_secs(75);

/// Send with [`VIEWER_IDLE_TIMEOUT`] as the stall bound.
async fn timed_send<S>(sink: &mut S, msg: WsMessage) -> Result<(), axum::Error>
where
    S: futures_util::Sink<WsMessage, Error = axum::Error> + Unpin,
{
    match tokio::time::timeout(VIEWER_IDLE_TIMEOUT, sink.send(msg)).await {
        Ok(result) => result,
        Err(elapsed) => Err(axum::Error::new(elapsed)),
    }
}

/// Viewer ping cadence; keeps NATs open and detects dead peers. Must stay
/// well under [`VIEWER_IDLE_TIMEOUT`], or healthy peers are disconnected.
const VIEWER_PING_INTERVAL: Duration = Duration::from_secs(25);

/// Relay one wake-up's worth of queued messages to the viewer.
///
/// Everything already queued is drained and consecutive binary payloads
/// are merged into one WebSocket frame. Every message otherwise costs
/// its own write+flush, and at thousands of viewers x dozens of frames/s
/// those per-frame flushes dominate the server. Control events keep
/// their own text frames, and ordering is preserved throughout.
async fn relay(
    sink: &mut futures_util::stream::SplitSink<WebSocket, WsMessage>,
    rx: &mut mpsc::Receiver<ViewerMsg>,
    first: ViewerMsg,
) -> Result<(), axum::Error> {
    /// Bound on a merged frame; beyond it the backlog flushes in parts.
    const MAX_MERGED: usize = 1024 * 1024;
    /// One frame from the accumulated chunks - without copying when a
    /// single chunk stands alone (the common case at normal pace).
    fn take_merged(chunks: &mut Vec<Bytes>, len: &mut usize) -> Bytes {
        *len = 0;
        if chunks.len() == 1 {
            return chunks.pop().unwrap_or_default();
        }
        let mut buf = bytes::BytesMut::with_capacity(chunks.iter().map(Bytes::len).sum());
        for chunk in chunks.drain(..) {
            buf.extend_from_slice(&chunk);
        }
        buf.freeze()
    }
    let mut chunks: Vec<Bytes> = Vec::new();
    let mut pending_len = 0;
    let mut item = Some(first);
    loop {
        match item {
            Some(ViewerMsg::Bytes(payload)) => {
                if pending_len + payload.len() > MAX_MERGED && !chunks.is_empty() {
                    let merged = take_merged(&mut chunks, &mut pending_len);
                    timed_send(sink, WsMessage::Binary(merged)).await?;
                }
                pending_len += payload.len();
                chunks.push(payload);
            }
            Some(ViewerMsg::Control(json)) => {
                if !chunks.is_empty() {
                    let merged = take_merged(&mut chunks, &mut pending_len);
                    timed_send(sink, WsMessage::Binary(merged)).await?;
                }
                timed_send(sink, WsMessage::Text(json.into())).await?;
            }
            None => break,
        }
        item = match rx.try_recv() {
            Ok(next) => Some(next),
            Err(_) => break,
        };
    }
    if !chunks.is_empty() {
        let merged = take_merged(&mut chunks, &mut pending_len);
        timed_send(sink, WsMessage::Binary(merged)).await?;
    }
    Ok(())
}
