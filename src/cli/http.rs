//! HTTP client for shellshare CLI
//!
//! Handles communication with the shellshare server.

use crate::protocol::{EncodedMessage, TermSize};
use reqwest::blocking::Client as ReqwestClient;
use reqwest::StatusCode;
use serde_json::json;
use std::thread;
use std::time::Duration;

/// Error types for HTTP operations
#[derive(Debug)]
pub enum HttpError {
    /// Not authorized to broadcast to this room
    Unauthorized,
    /// Request was too large
    RequestTooLarge,
    /// Network or other error
    NetworkError(String),
}

impl std::fmt::Display for HttpError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::Unauthorized => {
                write!(f, "You're not authorized to share on this room.")
            }
            Self::RequestTooLarge => {
                write!(f, "You've wrote too much too fast. Please, slow down.")
            }
            Self::NetworkError(msg) => {
                write!(f, "There was an error connecting to the server: {msg}")
            }
        }
    }
}

impl std::error::Error for HttpError {}

/// HTTP client for communicating with the shellshare server
#[derive(Clone)]
pub struct Client {
    inner: ReqwestClient,
    /// Full room URL (`{server}/{room_path}`), shared by every request
    room_url: String,
    password: String,
}

impl Client {
    /// Create a new HTTP client
    pub fn new(
        server_url: &str,
        room_path: &str,
        password: &str,
    ) -> Result<Self, Box<dyn std::error::Error>> {
        let inner = ReqwestClient::builder()
            .timeout(Duration::from_secs(30))
            // Broadcasts are many small POSTs on a kept-alive connection;
            // Nagle's algorithm would only add latency
            .tcp_nodelay(true)
            .build()?;

        Ok(Self {
            inner,
            room_url: format!("{server_url}/{room_path}"),
            password: password.to_string(),
        })
    }

    /// Claim the room without sharing any output yet.
    ///
    /// A POST with neither message nor size claims the room with our
    /// password (first caller wins), closing the window where someone
    /// else could take the name between server start and first output.
    pub fn claim_room(&self) -> Result<(), HttpError> {
        self.do_post(&json!({}))
    }

    /// POST a message to the server with retry logic
    pub fn post_message(
        &self,
        message: &EncodedMessage,
        size: TermSize,
    ) -> Result<(), HttpError> {
        const MAX_RETRIES: u32 = 3;
        const RETRY_DELAY: Duration = Duration::from_millis(500);

        let body = json!({
            "message": message.as_str(),
            "size": size,
        });

        let mut last_error = None;

        for attempt in 0..MAX_RETRIES {
            if attempt > 0 {
                thread::sleep(RETRY_DELAY);
            }

            match self.do_post(&body) {
                Ok(()) => return Ok(()),
                Err(HttpError::Unauthorized) => return Err(HttpError::Unauthorized),
                Err(HttpError::RequestTooLarge) => return Err(HttpError::RequestTooLarge),
                Err(e) => {
                    last_error = Some(e);
                    // Continue to retry on network errors
                }
            }
        }

        Err(last_error.unwrap_or_else(|| HttpError::NetworkError("Unknown error".to_string())))
    }

    /// Perform a single POST request
    fn do_post(&self, body: &serde_json::Value) -> Result<(), HttpError> {
        let response = self
            .inner
            .post(&self.room_url)
            .header("Content-Type", "application/json")
            .header("Authorization", &self.password)
            .json(body)
            .send()
            .map_err(|e| HttpError::NetworkError(e.to_string()))?;

        match response.status() {
            StatusCode::OK | StatusCode::CREATED | StatusCode::ACCEPTED => Ok(()),
            StatusCode::UNAUTHORIZED => Err(HttpError::Unauthorized),
            StatusCode::PAYLOAD_TOO_LARGE => Err(HttpError::RequestTooLarge),
            status => Err(HttpError::NetworkError(format!(
                "Unexpected status code: {status}"
            ))),
        }
    }

    /// Send a DELETE request to clean up the room
    pub fn delete_room(&self) -> Result<(), HttpError> {
        let response = self
            .inner
            .delete(&self.room_url)
            .header("Authorization", &self.password)
            .send()
            .map_err(|e| HttpError::NetworkError(e.to_string()))?;

        match response.status() {
            StatusCode::OK | StatusCode::ACCEPTED | StatusCode::NO_CONTENT => Ok(()),
            StatusCode::UNAUTHORIZED => Err(HttpError::Unauthorized),
            status => Err(HttpError::NetworkError(format!(
                "Unexpected status code: {status}"
            ))),
        }
    }
}
