//! Cross-platform binary embedding and serving
//!
//! When built with the `embed-binaries` feature, this module embeds
//! binaries for all supported platforms, allowing any server instance
//! to serve downloads for all operating systems.

use axum::{
    body::Body,
    http::{header, HeaderMap, StatusCode},
    response::{IntoResponse, Response},
};

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Platform {
    LinuxX86_64,
    WindowsX86_64,
    MacosX86_64,
    MacosAarch64,
}

impl Platform {
    /// Supports: linux, windows, win, mac, macos, mac-intel, mac-arm
    pub fn from_query_param(os: &str) -> Option<Self> {
        match os.to_lowercase().as_str() {
            "linux" => Some(Self::LinuxX86_64),
            "windows" | "win" => Some(Self::WindowsX86_64),
            "mac" | "macos" | "mac-intel" => Some(Self::MacosX86_64),
            "mac-arm" => Some(Self::MacosAarch64),
            _ => None,
        }
    }

    pub fn from_user_agent(user_agent: &str) -> Option<Self> {
        let ua = user_agent.to_lowercase();

        if ua.contains("windows") {
            return Some(Self::WindowsX86_64);
        }

        if ua.contains("mac") || ua.contains("darwin") {
            if ua.contains("arm64") || ua.contains("aarch64") {
                return Some(Self::MacosAarch64);
            }
            // Default to Intel for compatibility (runs on ARM via Rosetta)
            return Some(Self::MacosX86_64);
        }

        if ua.contains("linux") {
            return Some(Self::LinuxX86_64);
        }

        None
    }

    pub const fn filename(self) -> &'static str {
        match self {
            Self::WindowsX86_64 => "shellshare.exe",
            Self::LinuxX86_64 | Self::MacosX86_64 | Self::MacosAarch64 => "shellshare",
        }
    }

    #[cfg(feature = "embed-binaries")]
    pub fn binary_data(self) -> Option<&'static [u8]> {
        Some(match self {
            Self::LinuxX86_64 => {
                include_bytes!(concat!(
                    env!("SHELLSHARE_BINARIES_DIR"),
                    "/shellshare-linux-x86_64"
                ))
            }
            Self::WindowsX86_64 => {
                include_bytes!(concat!(
                    env!("SHELLSHARE_BINARIES_DIR"),
                    "/shellshare-windows-x86_64.exe"
                ))
            }
            Self::MacosX86_64 => {
                include_bytes!(concat!(
                    env!("SHELLSHARE_BINARIES_DIR"),
                    "/shellshare-macos-x86_64"
                ))
            }
            Self::MacosAarch64 => {
                include_bytes!(concat!(
                    env!("SHELLSHARE_BINARIES_DIR"),
                    "/shellshare-macos-aarch64"
                ))
            }
        })
    }

    /// `None` without the `embed-binaries` feature.
    #[cfg(not(feature = "embed-binaries"))]
    #[allow(clippy::unused_self)] // self is used in the embed-binaries version
    pub const fn binary_data(self) -> Option<&'static [u8]> {
        None
    }
}

#[derive(Debug, serde::Deserialize)]
pub struct BinaryDownloadQuery {
    /// Optional OS override: linux, windows, win, mac, macos, mac-intel, mac-arm
    pub os: Option<String>,
}

/// Serve a binary for the requested platform
///
/// Priority:
/// 1. Query parameter `?os=` (explicit override)
/// 2. User-Agent detection (only if `embed-binaries` feature enabled)
/// 3. Fallback to serving current binary (always works)
pub async fn serve_binary(
    query: axum::extract::Query<BinaryDownloadQuery>,
    headers: HeaderMap,
) -> impl IntoResponse {
    let platform_from_query = query.os.as_deref().and_then(Platform::from_query_param);

    let user_agent = headers
        .get(header::USER_AGENT)
        .and_then(|v| v.to_str().ok())
        .unwrap_or("");
    let platform_from_ua = Platform::from_user_agent(user_agent);

    let requested_platform = platform_from_query.or(platform_from_ua);

    if let Some(platform) = requested_platform {
        if let Some(data) = platform.binary_data() {
            return Response::builder()
                .status(StatusCode::OK)
                .header(header::CONTENT_TYPE, "application/octet-stream")
                .header(
                    header::CONTENT_DISPOSITION,
                    format!("attachment; filename=\"{}\"", platform.filename()),
                )
                .header(header::CACHE_CONTROL, "public, max-age=2678400")
                .header(header::ETAG, format!("\"{}\"", data.len()))
                .body(Body::from(data.to_vec()))
                .unwrap();
        }
    }

    serve_self_binary().await
}

#[allow(clippy::option_if_let_else)] // Nested match is clearer here
async fn serve_self_binary() -> Response {
    match std::env::current_exe() {
        Ok(exe_path) => match tokio::fs::read(&exe_path).await {
            Ok(data) => Response::builder()
                .status(StatusCode::OK)
                .header(header::CONTENT_TYPE, "application/octet-stream")
                .header(
                    header::CONTENT_DISPOSITION,
                    "attachment; filename=\"shellshare\"",
                )
                .header(header::CACHE_CONTROL, "public, max-age=2678400")
                .header(header::ETAG, format!("\"{}\"", data.len()))
                .body(Body::from(data))
                .unwrap(),
            Err(_) => Response::builder()
                .status(StatusCode::INTERNAL_SERVER_ERROR)
                .header(header::CONTENT_TYPE, "text/plain; charset=utf-8")
                .body(Body::from("Failed to read binary"))
                .unwrap(),
        },
        Err(_) => Response::builder()
            .status(StatusCode::INTERNAL_SERVER_ERROR)
            .header(header::CONTENT_TYPE, "text/plain; charset=utf-8")
            .body(Body::from("Failed to locate binary"))
            .unwrap(),
    }
}
