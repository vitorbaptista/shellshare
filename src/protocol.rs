//! Wire protocol shared by the CLI client, the server, and the browser viewer.
//!
//! This is the single home for the project's defining compatibility
//! constraint: messages are encoded exactly like the original Python CLI,
//! and decoded by the JavaScript viewer (`public/javascript/room.js`) and
//! the Python e2e helpers (`e2e/conftest.py`). Any change here must stay
//! in lockstep with both.
//!
//! The encoding is two steps:
//! 1. URL-encode the bytes like Python's `urllib.parse.quote(data, safe="")`
//!    (letters, digits and `_.-~` stay unencoded; everything else becomes
//!    `%XX`). Note the empty `safe` set: unlike `quote()`'s default, `/` IS
//!    encoded.
//! 2. Base64-encode the result
//!
//! The viewer reverses it with `decodeURIComponent(atob(message))`.

use base64::{engine::general_purpose::STANDARD as BASE64, Engine as _};
use percent_encoding::{percent_encode, AsciiSet, NON_ALPHANUMERIC};
use std::collections::VecDeque;

/// Characters Python's `urllib.parse.quote(s, safe="")` leaves unencoded:
/// ASCII alphanumerics plus `_ . - ~`. Everything else - including `/`,
/// which `quote()`'s default `safe='/'` would preserve - is
/// percent-encoded.
const PYTHON_QUOTE: &AsciiSet = &NON_ALPHANUMERIC
    .remove(b'_')
    .remove(b'.')
    .remove(b'-')
    .remove(b'~');

/// A message in wire format (URL-encoded then Base64-encoded).
///
/// The only ways to obtain one are [`EncodedMessage::encode`] (from raw
/// bytes) and [`MessageHistory::accumulated`] (from stored history). This
/// keeps raw and encoded strings from being confused at compile time.
#[derive(Clone, Debug)]
pub struct EncodedMessage(String);

impl EncodedMessage {
    /// Encode raw bytes into wire format.
    ///
    /// Percent-encoding operates on raw bytes, so invalid UTF-8 needs no
    /// special casing: every byte is encoded exactly as Python's `quote()`
    /// would encode it.
    pub fn encode(data: &[u8]) -> Self {
        let url_encoded: String = percent_encode(data, PYTHON_QUOTE).collect();
        Self(BASE64.encode(url_encoded))
    }

    /// The wire-format string, as sent over HTTP and Socket.IO.
    pub fn as_str(&self) -> &str {
        &self.0
    }

}

/// Bounded message history for replaying to late joiners.
///
/// Messages are kept base64-decoded (i.e. as URL-encoded bytes): they
/// concatenate cleanly at that layer, so producing a joiner's catch-up
/// message is one concatenation and one Base64 encode, instead of
/// re-decoding the entire history on every join. Eviction is O(1).
pub struct MessageHistory {
    chunks: VecDeque<Vec<u8>>,
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

    /// Append a wire-format message, evicting the oldest when full.
    /// Messages that fail to decode (the viewer could not have decoded
    /// them either) or carry no content are dropped rather than allowed
    /// to evict real history.
    pub fn push(&mut self, wire: &str) {
        let Ok(decoded) = BASE64.decode(wire) else {
            return;
        };
        if decoded.is_empty() {
            return;
        }
        if self.chunks.len() == self.max_messages {
            self.chunks.pop_front();
        }
        self.chunks.push_back(decoded);
    }

    /// The whole history as a single wire message, or `None` when there
    /// is no content to replay.
    pub fn accumulated(&self) -> Option<EncodedMessage> {
        let total: usize = self.chunks.iter().map(Vec::len).sum();
        if total == 0 {
            return None;
        }
        let mut all = Vec::with_capacity(total);
        for chunk in &self.chunks {
            all.extend_from_slice(chunk);
        }
        Some(EncodedMessage(BASE64.encode(&all)))
    }
}

/// Terminal dimensions, as carried in the `size` field of broadcast
/// requests and the `size` Socket.IO event the viewer listens for.
#[derive(Debug, Clone, Copy, serde::Serialize)]
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
