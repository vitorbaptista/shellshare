//! Wire protocol shared by the CLI client, the server, and the browser viewer.
//!
//! Terminal output travels as **raw bytes** end to end: the CLI sends
//! binary WebSocket frames, the server stores raw bytes, and viewers
//! receive binary Socket.IO attachments which the browser decodes with a
//! streaming `TextDecoder` (`templates/room.html`). The Python e2e
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
pub struct MessageHistory {
    chunks: VecDeque<Bytes>,
    max_messages: usize,
}

impl MessageHistory {
    /// An empty history holding at most `max_messages` messages.
    pub const fn new(max_messages: usize) -> Self {
        Self {
            chunks: VecDeque::new(),
            max_messages,
        }
    }

    /// Append a message, evicting the oldest when full. Empty messages
    /// are dropped rather than allowed to evict real history.
    pub fn push(&mut self, message: Bytes) {
        if message.is_empty() {
            return;
        }
        if self.chunks.len() == self.max_messages {
            self.chunks.pop_front();
        }
        self.chunks.push_back(message);
    }

    /// The whole history as one contiguous message, or `None` when
    /// there is no content to replay.
    pub fn accumulated(&self) -> Option<Bytes> {
        if self.chunks.is_empty() {
            return None;
        }
        let total: usize = self.chunks.iter().map(Bytes::len).sum();
        let mut all = Vec::with_capacity(total);
        for chunk in &self.chunks {
            all.extend_from_slice(chunk);
        }
        Some(all.into())
    }
}

/// Terminal dimensions, as carried in the `size` control message of
/// broadcast transports and the `size` Socket.IO event the viewer
/// listens for.
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
