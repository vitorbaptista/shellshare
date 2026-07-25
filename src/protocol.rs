//! Wire protocol shared by the CLI client, the server, and the browser viewer.
//!
//! Terminal output travels as **raw bytes** end to end: the CLI sends
//! binary WebSocket frames, the server stores raw bytes, and viewers
//! receive binary WebSocket frames which xterm.js decodes with its
//! streaming UTF-8 decoder (`templates/room.html`). The Python e2e
//! helpers (`e2e/conftest.py`) must stay in lockstep.
//!
//! End-to-end encryption changes none of this: every broadcast's bytes
//! are sealed records (`src/cli/crypto.rs`) the server still stores,
//! acks, and relays opaquely; only the viewer page decrypts them, with
//! a key from the share link's URL fragment.

use bytes::Bytes;
use std::collections::VecDeque;

/// Bounded message history for replaying to late joiners.
///
/// Chunks are raw terminal bytes, so a joiner's catch-up message is a
/// single concatenation. Eviction is O(1). `Bytes` chunks share their
/// buffers with the live broadcast path instead of copying.
///
/// Two independent bounds, because they guard different things. The byte
/// budget is the memory bound: a chunk is a coalesced frame of up to
/// 64KB (plus periodic full-screen keyframes), so counting frames alone
/// left a room's ceiling at frames x 64KB. The frame count bounds
/// per-chunk overhead instead - every chunk costs a `Bytes` in the deque
/// and a step in `accumulated`'s walk, so without it a producer writing
/// a byte at a time could sit inside the byte budget while holding
/// millions of slivers. Which bound binds depends on frame size: below
/// the budget-over-count average the frame cap wins (interactive typing
/// keeps its full 200 frames), while any fast output - a build log, a
/// `cat` - coalesces into large frames and is bounded by bytes instead.
pub struct MessageHistory {
    chunks: VecDeque<Bytes>,
    max_messages: usize,
    max_bytes: usize,
    /// Sum of `chunks`' lengths, maintained on every push and eviction
    bytes: usize,
}

impl MessageHistory {
    /// An empty history holding at most `max_messages` messages and
    /// `max_bytes` bytes, whichever binds first.
    pub const fn new(max_messages: usize, max_bytes: usize) -> Self {
        Self {
            chunks: VecDeque::new(),
            max_messages,
            max_bytes,
            bytes: 0,
        }
    }

    /// Append a message, evicting the oldest until both bounds hold.
    /// Empty messages are dropped rather than allowed to evict real
    /// history.
    ///
    /// The newest chunk always survives, even when it alone exceeds the
    /// byte budget: dropping it would silently discard the very output
    /// being broadcast, and a viewer showing one oversized frame beats a
    /// viewer showing nothing.
    pub fn push(&mut self, message: Bytes) {
        if message.is_empty() {
            return;
        }
        self.bytes += message.len();
        self.chunks.push_back(message);
        while self.chunks.len() > self.max_messages
            || (self.bytes > self.max_bytes && self.chunks.len() > 1)
        {
            let Some(dropped) = self.chunks.pop_front() else {
                break;
            };
            self.bytes -= dropped.len();
        }
    }

    /// The whole history as one contiguous message, or `None` when
    /// there is no content to replay.
    pub fn accumulated(&self) -> Option<Bytes> {
        if self.chunks.is_empty() {
            return None;
        }
        let mut all = Vec::with_capacity(self.bytes);
        for chunk in &self.chunks {
            all.extend_from_slice(chunk);
        }
        Some(all.into())
    }
}

/// Terminal dimensions, as carried in the `size` control message on
/// both the ingest and viewer `WebSockets`.
///
/// The client may attach extra fields next to `cols`/`rows` - today a
/// `theme` name (see `themes.rs`) - which ride along verbatim to the
/// viewer without the server knowing about them.
#[derive(Debug, Clone, Copy, PartialEq, Eq, serde::Serialize)]
pub struct TermSize {
    pub cols: u16,
    pub rows: u16,
}

impl Default for TermSize {
    fn default() -> Self {
        Self { cols: 80, rows: 24 }
    }
}

/// Whether an incoming `size` JSON value carries terminal dimensions.
///
/// The protocol is deliberately lenient: the server forwards the value
/// verbatim (extra fields and all) as long as `cols` and `rows` are
/// present, because the viewer ignores a size without them anyway.
pub fn size_has_dimensions(size: &serde_json::Value) -> bool {
    size.as_object()
        .is_some_and(|obj| obj.contains_key("cols") && obj.contains_key("rows"))
}
