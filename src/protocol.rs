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
use percent_encoding::{percent_encode, NON_ALPHANUMERIC};

/// A message in wire format (URL-encoded then Base64-encoded).
///
/// The only ways to obtain one are [`EncodedMessage::encode`] (from raw
/// bytes) and [`EncodedMessage::from_wire`] (from a string that is already
/// in wire format, e.g. received in a broadcast request). This keeps raw
/// and encoded strings from being confused at compile time.
#[derive(Clone, Debug)]
pub struct EncodedMessage(String);

impl EncodedMessage {
    /// Encode raw bytes into wire format.
    #[allow(clippy::option_if_let_else)] // if-let is more readable here
    pub fn encode(data: &[u8]) -> Self {
        // First, try to interpret as UTF-8 string for URL encoding.
        // If it's not valid UTF-8, we encode each byte.
        let url_encoded = if let Ok(s) = std::str::from_utf8(data) {
            url_encode_python_style(s)
        } else {
            data.iter()
                .map(|&b| {
                    if b.is_ascii_alphanumeric()
                        || b == b'_'
                        || b == b'.'
                        || b == b'-'
                        || b == b'~'
                    {
                        (b as char).to_string()
                    } else {
                        format!("%{b:02X}")
                    }
                })
                .collect::<String>()
        };

        Self(BASE64.encode(url_encoded.as_bytes()))
    }

    /// Wrap a string that is already in wire format (as received from a
    /// broadcasting client). No validation: the server forwards messages
    /// verbatim and the viewer is the one that decodes them.
    pub const fn from_wire(wire: String) -> Self {
        Self(wire)
    }

    /// The wire-format string, as sent over HTTP and Socket.IO.
    pub fn as_str(&self) -> &str {
        &self.0
    }

    /// Concatenate many wire messages into a single one, for replaying
    /// history to late joiners.
    ///
    /// Works at the Base64 layer: each message decodes to URL-encoded
    /// bytes, which concatenate cleanly and re-encode as one message.
    /// Messages that fail to decode are skipped.
    pub fn accumulate(messages: &[Self]) -> Option<Self> {
        if messages.is_empty() {
            return None;
        }

        let mut accumulated = Vec::new();
        for msg in messages {
            if let Ok(decoded) = BASE64.decode(&msg.0) {
                accumulated.extend(decoded);
            }
        }

        if accumulated.is_empty() {
            None
        } else {
            Some(Self(BASE64.encode(&accumulated)))
        }
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

/// URL-encode a string like Python's `urllib.parse.quote(s, safe="")`.
///
/// With an empty `safe` set, `quote()` leaves only these unencoded:
/// - ASCII letters (a-z, A-Z)
/// - ASCII digits (0-9)
/// - '_', '.', '-', '~'
///
/// Everything else - including '/', which `quote()`'s default `safe='/'`
/// would preserve - is percent-encoded.
fn url_encode_python_style(s: &str) -> String {
    // percent_encoding's NON_ALPHANUMERIC encodes everything except ASCII
    // alphanumeric. We need to also NOT encode: _ . - ~
    let encoded = percent_encode(s.as_bytes(), NON_ALPHANUMERIC).to_string();

    // Decode the safe characters that Python's quote() doesn't encode
    encoded
        .replace("%5F", "_")
        .replace("%2E", ".")
        .replace("%2D", "-")
        .replace("%7E", "~")
}
