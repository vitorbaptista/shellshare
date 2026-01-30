//! HTTP client for shellshare CLI
//!
//! Handles communication with the shellshare server.

use crate::cli::terminal::TerminalSize;
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
    server_url: String,
    room_path: String,
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
            .build()?;

        Ok(Self {
            inner,
            server_url: server_url.to_string(),
            room_path: room_path.to_string(),
            password: password.to_string(),
        })
    }

    /// POST a message to the server with retry logic
    pub fn post_message(&self, message: &str, size: TerminalSize) -> Result<(), HttpError> {
        const MAX_RETRIES: u32 = 3;
        const RETRY_DELAY: Duration = Duration::from_millis(500);

        let url = format!("{}/{}", self.server_url, self.room_path);
        let body = json!({
            "message": message,
            "size": {
                "cols": size.cols,
                "rows": size.rows
            }
        });

        let mut last_error = None;

        for attempt in 0..MAX_RETRIES {
            if attempt > 0 {
                thread::sleep(RETRY_DELAY);
            }

            match self.do_post(&url, &body) {
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
    fn do_post(&self, url: &str, body: &serde_json::Value) -> Result<(), HttpError> {
        let response = self
            .inner
            .post(url)
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
        let url = format!("{}/{}", self.server_url, self.room_path);

        let response = self
            .inner
            .delete(&url)
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
