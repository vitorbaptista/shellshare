//! Optional, privacy-preserving usage analytics (`PostHog`).
//!
//! Disabled unless the operator configures an API key AND a salt; with
//! either missing this module is a no-op and nothing ever leaves the
//! server. Design constraints, in order:
//!
//! - **Never slow the broadcast path**: events go through a bounded
//!   channel with `try_send` (full channel = event dropped) into one
//!   background task that does the HTTP work. Losing events is fine;
//!   blocking ingest is not.
//! - **No PII**: no IP addresses (`PostHog` only ever sees this server's
//!   own requests, sent with `$geoip_disable`), no room names, no
//!   passwords. The only identifier is `HMAC-SHA256(salt, password)`:
//!   the default client password is MAC-derived, so the HMAC is stable
//!   per machine (recurring-user detection) while the secret salt keeps
//!   the small MAC space safe from enumeration and keeps custom
//!   passwords uncrackable from the analytics data.
//! - **Stateless server**: the salt is operator-supplied, not generated,
//!   so identities survive restarts and server moves (reuse the salt).
//!
//! The counts are best-effort, not bookkeeping: a few edge cases skew
//! them slightly (a handshake that claims a room but never upgrades
//! emits `room_created` with no matching `broadcast_ended`; a room
//! resurrected by a ping after a delete race or a mid-broadcast TTL
//! eviction emits neither), and a broadcast interrupted by reconnects
//! reports one `broadcast_ended` per live segment. Good enough for
//! "how much is shellshare used".

use hmac::{Hmac, Mac};
use sha2::Sha256;
use std::time::Duration;
use tokio::sync::mpsc;
use tracing::{debug, info, warn};

/// Default `PostHog` ingestion endpoint (US cloud).
pub const DEFAULT_POSTHOG_HOST: &str = "https://us.i.posthog.com";

/// Events waiting for the sender task. Volume is tiny (a handful per
/// broadcast/viewer), so a small buffer only ever fills if `PostHog` is
/// unreachable and events pile up behind timeouts - exactly when
/// dropping them is the right call.
const QUEUE_CAPACITY: usize = 256;

/// Per-request timeout: `PostHog` being slow must never pin the sender
/// task (and thus the queue) for long.
const REQUEST_TIMEOUT: Duration = Duration::from_secs(5);

/// Operator-provided analytics settings.
pub struct Config {
    pub api_key: String,
    pub host: String,
    pub salt: String,
}

/// One analytics event, queued for delivery.
struct Event {
    name: &'static str,
    distinct_id: String,
    properties: serde_json::Value,
}

/// Handle for emitting analytics events. Cheap to clone; a disabled
/// handle (no config) drops every event without any work.
#[derive(Clone, Default)]
pub struct Analytics {
    inner: Option<Inner>,
}

#[derive(Clone)]
struct Inner {
    tx: mpsc::Sender<Event>,
    salt: String,
}

impl Analytics {
    /// Build the handle and spawn the sender task. `None` config (or a
    /// config that can't produce an HTTP client) yields a no-op handle.
    ///
    /// Must be called from within a Tokio runtime.
    pub fn new(config: Option<Config>) -> Self {
        let Some(config) = config else {
            return Self { inner: None };
        };

        // reqwest is built without a default TLS provider (the in-tree
        // provider is ring, shared with the client's WebSocket TLS);
        // install it process-wide, tolerating an already-installed one
        let _ = rustls::crypto::ring::default_provider().install_default();

        let client = match reqwest::Client::builder()
            .timeout(REQUEST_TIMEOUT)
            .use_rustls_tls()
            .build()
        {
            Ok(client) => client,
            Err(e) => {
                warn!("Analytics disabled: could not build HTTP client: {e}");
                return Self { inner: None };
            }
        };

        info!("Analytics enabled, sending to {}", config.host);
        let (tx, rx) = mpsc::channel(QUEUE_CAPACITY);
        tokio::spawn(sender_task(client, config.api_key, config.host, rx));

        Self {
            inner: Some(Inner {
                tx,
                salt: config.salt,
            }),
        }
    }

    /// A room was claimed by a broadcaster.
    pub fn room_created(&self, password: &str) {
        if let Some(inner) = &self.inner {
            inner.send(Event {
                name: "room_created",
                distinct_id: inner.broadcaster_id(password),
                properties: serde_json::json!({}),
            });
        }
    }

    /// A live segment ended: the room's last ingest connection detached
    /// (or the room was deleted under it). `duration` spans the segment,
    /// so one broadcast interrupted by reconnects yields several events
    /// whose durations sum to the time actually spent live.
    pub fn broadcast_ended(&self, password: &str, duration: Duration) {
        if let Some(inner) = &self.inner {
            inner.send(Event {
                name: "broadcast_ended",
                distinct_id: inner.broadcaster_id(password),
                properties: serde_json::json!({
                    // Fractional: short segments would otherwise all
                    // truncate to 0 and undercount reconnect-heavy runs
                    "duration_seconds": duration.as_secs_f64(),
                }),
            });
        }
    }

    /// A viewer joined an existing room - live (`broadcasting`) or a
    /// replay of one whose broadcaster dropped but wasn't evicted yet.
    /// Viewers send no credentials, so the identity is the room's, not
    /// the viewer's: this measures rooms that got an audience and how
    /// big it was, not unique viewers.
    pub fn viewer_joined(&self, room_name: &str, user_count: usize, broadcasting: bool) {
        if let Some(inner) = &self.inner {
            inner.send(Event {
                name: "viewer_joined",
                distinct_id: inner.room_id(room_name),
                properties: serde_json::json!({
                    "user_count": user_count,
                    "broadcasting": broadcasting,
                }),
            });
        }
    }
}

impl Inner {
    /// Queue an event; a full queue or closed channel drops it.
    fn send(&self, event: Event) {
        if let Err(e) = self.tx.try_send(event) {
            debug!("Analytics event dropped: {e}");
        }
    }

    /// Stable pseudonymous broadcaster identity. The `bc:` prefix keeps
    /// this id space disjoint from room-derived ids.
    fn broadcaster_id(&self, password: &str) -> String {
        format!("bc:{}", hmac_hex(&self.salt, password))
    }

    /// Stable pseudonymous room identity for viewer events.
    fn room_id(&self, room_name: &str) -> String {
        format!("room:{}", hmac_hex(&self.salt, room_name))
    }
}

/// `HMAC-SHA256(salt, value)` as lowercase hex.
fn hmac_hex(salt: &str, value: &str) -> String {
    // HMAC accepts keys of any length; new_from_slice cannot fail
    let mut mac = Hmac::<Sha256>::new_from_slice(salt.as_bytes())
        .expect("HMAC accepts any key length");
    mac.update(value.as_bytes());
    hex::encode(mac.finalize().into_bytes())
}

/// The single background task that delivers queued events to `PostHog`.
/// Failures are logged at debug and the event is dropped - analytics
/// must never demand a retry budget from the server.
async fn sender_task(
    client: reqwest::Client,
    api_key: String,
    host: String,
    mut rx: mpsc::Receiver<Event>,
) {
    let url = format!("{}/i/v0/e/", host.trim_end_matches('/'));
    while let Some(event) = rx.recv().await {
        let mut properties = event.properties;
        // Anonymous events: no person profiles (distinct_id-based
        // unique counts still work), and no GeoIP enrichment - PostHog
        // would otherwise tag events with this server's location
        properties["$process_person_profile"] = serde_json::Value::Bool(false);
        properties["$geoip_disable"] = serde_json::Value::Bool(true);
        let body = serde_json::json!({
            "api_key": api_key,
            "event": event.name,
            "distinct_id": event.distinct_id,
            "properties": properties,
        });
        match client.post(&url).json(&body).send().await {
            Ok(response) if response.status().is_success() => {}
            Ok(response) => {
                debug!(
                    "Analytics event {} rejected: {}",
                    event.name,
                    response.status()
                );
            }
            Err(e) => debug!("Analytics event {} failed: {e}", event.name),
        }
    }
}
