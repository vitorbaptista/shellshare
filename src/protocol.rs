//! Wire protocol shared by the CLI client, the server, and the browser viewer.
//!
//! Terminal output travels as **raw bytes** end to end: the CLI sends
//! binary WebSocket frames, the server stores raw bytes, and viewers
//! receive binary Socket.IO attachments which the browser decodes with a
//! streaming `TextDecoder` (`templates/room.html`). The Python e2e
//! helpers (`e2e/conftest.py`) must stay in lockstep.
//!
//! One legacy door remains: `POST /r/:room` accepts messages in the
//! original wire format (URL-encoded like Python's
//! `urllib.parse.quote(data, safe="")`, then Base64). [`decode_wire`]
//! converts those to raw bytes at the edge; everything past ingest is
//! raw.

use base64::{engine::general_purpose::STANDARD as BASE64, Engine as _};
use bytes::Bytes;
use percent_encoding::percent_decode;
use std::collections::VecDeque;

/// Decode a legacy wire-format message (URL-encoded then Base64) into
/// raw bytes. Returns `None` when the Base64 layer is invalid; stray
/// `%` sequences pass through verbatim, like Python's `unquote`.
pub fn decode_wire(wire: &str) -> Option<Bytes> {
    let url_encoded = BASE64.decode(wire).ok()?;
    Some(percent_decode(&url_encoded).collect::<Vec<u8>>().into())
}

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
