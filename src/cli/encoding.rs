//! Message encoding for shellshare
//!
//! This module provides encoding that matches the Python CLI:
//! 1. URL-encode the data (like Python's `urllib.parse.quote()`)
//! 2. Base64-encode the result

use base64::{engine::general_purpose::STANDARD as BASE64, Engine as _};
use percent_encoding::{percent_encode, NON_ALPHANUMERIC};

/// Encode a message for transmission to the server.
///
/// This matches Python's encoding:
/// ```python
/// urlencoded = urllib_quote(data).encode('utf-8')
/// encoded_str = base64.b64encode(urlencoded).decode('utf-8')
/// ```
///
/// Python's `urllib.parse.quote()` by default:
/// - Does NOT encode: letters, digits, '_', '.', '-', '~'
/// - DOES encode: everything else, including '/'
///
/// We use `NON_ALPHANUMERIC` which encodes everything except ASCII alphanumeric,
/// then manually decode the few safe characters to match Python exactly.
#[allow(clippy::option_if_let_else)] // if-let is more readable here
pub fn encode_message(data: &[u8]) -> String {
    // First, try to interpret as UTF-8 string for URL encoding
    // If it's not valid UTF-8, we encode each byte
    let url_encoded = if let Ok(s) = std::str::from_utf8(data) {
        // URL-encode using Python's default safe chars (letters, digits, '_.-~')
        url_encode_python_style(s)
    } else {
        // For binary data, encode each byte
        data.iter()
            .map(|&b| {
                if b.is_ascii_alphanumeric() || b == b'_' || b == b'.' || b == b'-' || b == b'~' {
                    (b as char).to_string()
                } else {
                    format!("%{b:02X}")
                }
            })
            .collect::<String>()
    };

    // Base64 encode the URL-encoded string
    BASE64.encode(url_encoded.as_bytes())
}

/// URL-encode a string using Python's `urllib.parse.quote()` defaults.
///
/// Python's `quote()` with no arguments leaves these characters unencoded:
/// - ASCII letters (a-z, A-Z)
/// - ASCII digits (0-9)
/// - '_', '.', '-', '~'
fn url_encode_python_style(s: &str) -> String {
    // percent_encoding's NON_ALPHANUMERIC encodes everything except ASCII alphanumeric
    // We need to also NOT encode: _ . - ~
    let encoded = percent_encode(s.as_bytes(), NON_ALPHANUMERIC).to_string();

    // Decode the safe characters that Python's quote() doesn't encode
    encoded
        .replace("%5F", "_")
        .replace("%2E", ".")
        .replace("%2D", "-")
        .replace("%7E", "~")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_simple_text() {
        // "hello" -> URL-encode unchanged -> base64 encode
        // "hello" -> "hello" -> "aGVsbG8="
        let encoded = encode_message(b"hello");
        assert_eq!(encoded, BASE64.encode(b"hello"));
    }

    #[test]
    fn test_special_chars() {
        // Space gets URL-encoded to %20
        // "hello world" -> "hello%20world" -> base64
        let encoded = encode_message(b"hello world");
        let expected = BASE64.encode(b"hello%20world");
        assert_eq!(encoded, expected);
    }

    #[test]
    fn test_safe_chars() {
        // Python's safe chars: _ . - ~
        let input = b"a_b.c-d~e";
        let encoded = encode_message(input);
        // These should not be URL-encoded
        let expected = BASE64.encode(b"a_b.c-d~e");
        assert_eq!(encoded, expected);
    }

    #[test]
    fn test_newline() {
        // Newline %0A
        let encoded = encode_message(b"\n");
        let expected = BASE64.encode(b"%0A");
        assert_eq!(encoded, expected);
    }
}
