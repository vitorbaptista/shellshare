//! End-to-end encryption for broadcasts.
//!
//! Terminal output is sealed on the broadcaster into self-delimiting
//! records that the server stores and relays as opaque bytes; only the
//! viewer page decrypts them, with a key carried in the share link's
//! URL fragment - which browsers never send to the server.
//!
//! Record layout: `[u32 BE: N = len(nonce || ciphertext || tag)]`
//! `[12-byte nonce][ciphertext || 16-byte tag]`, AES-256-GCM with a
//! fresh random nonce per record. Records decrypt independently, so a
//! late joiner can start from any whole-record boundary in the
//! history, and chunks dropped by the replay buffer cap never tear a
//! record. Must stay in lockstep with the viewer's parser in
//! `templates/room.html`.
//!
//! Threat model: an **honest-but-curious server that serves the
//! unmodified viewer page**. Under that assumption it cannot read or
//! forge terminal content - only ciphertext is stored, acked, and
//! relayed. The limits to be honest about:
//!
//! - The server delivers the decrypting JavaScript. A *malicious*
//!   server can serve a modified page that exfiltrates the key from
//!   the URL fragment, defeating the guarantee. This is inherent to
//!   any web-delivered E2E scheme; only an out-of-band viewer escapes
//!   it.
//! - No cross-record binding (records carry no sequence numbers, so
//!   late joiners can decrypt an arbitrary history suffix). An active
//!   attacker can therefore reorder, replay, drop, or truncate
//!   records - garbling a display it can already withhold entirely -
//!   but cannot forge or read content.
//! - Record lengths and timing are visible. Each record's plaintext
//!   length is exact (GCM adds no padding), so for an interactive
//!   shell the per-keystroke timing and size leak to the server and
//!   can reveal information about typed input (the classic
//!   keystroke-timing side channel). Room name is visible too.

use aes_gcm::aead::rand_core::RngCore;
use aes_gcm::aead::{Aead, OsRng};
use aes_gcm::{Aes256Gcm, KeyInit, Nonce};

const NONCE_LEN: usize = 12;

/// Seals terminal output into encrypted records the server can't read.
pub struct Encryptor {
    cipher: Aes256Gcm,
}

impl Encryptor {
    /// A fresh random key; returns the encryptor and the hex-encoded
    /// key for the share link's fragment.
    pub fn generate() -> (Self, String) {
        let key = Aes256Gcm::generate_key(&mut OsRng);
        let cipher = Aes256Gcm::new(&key);
        (Self { cipher }, hex::encode(key))
    }

    /// Seal one chunk of terminal output into one record.
    ///
    /// # Panics
    ///
    /// Panics if encryption fails or the sealed record exceeds the
    /// `u32` length prefix - both only on inputs far beyond any frame
    /// this client produces (chunks are capped well under `u32::MAX`
    /// by the sender's batching, and AES-GCM's plaintext limit is
    /// orders of magnitude larger still).
    pub fn seal(&self, plaintext: &[u8]) -> Vec<u8> {
        let mut nonce_bytes = [0u8; NONCE_LEN];
        OsRng.fill_bytes(&mut nonce_bytes);
        let nonce = Nonce::from(nonce_bytes);
        let ciphertext = self
            .cipher
            .encrypt(&nonce, plaintext)
            .expect("AES-GCM encryption of an in-memory chunk cannot fail");
        let len = u32::try_from(NONCE_LEN + ciphertext.len())
            .expect("sealed record exceeds u32 length prefix");
        let mut record = Vec::with_capacity(4 + NONCE_LEN + ciphertext.len());
        record.extend_from_slice(&len.to_be_bytes());
        record.extend_from_slice(&nonce_bytes);
        record.extend_from_slice(&ciphertext);
        record
    }
}
