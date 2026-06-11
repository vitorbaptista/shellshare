//! HTML pages and static assets, embedded at compile time.
//!
//! Everything the browser loads that is not room data lives here: the
//! home page, the viewer page, and the static assets under `public/`.
//! The sibling `binaries` module plays the same role for the
//! downloadable CLI binary.

use axum::{
    body::Body,
    extract::Path,
    http::{header, Method, Request, StatusCode},
    response::{IntoResponse, Response},
};
use rust_embed::Embed;

/// Embedded static files from the public directory
#[derive(Embed, Clone)]
#[folder = "public/"]
struct StaticAssets;

/// Embedded view templates
#[derive(Embed, Clone)]
#[folder = "templates/"]
struct Templates;

/// GET / - Home page
pub async fn index_handler() -> impl IntoResponse {
    match Templates::get("index.html") {
        Some(content) => {
            let html = String::from_utf8_lossy(&content.data).into_owned();

            Response::builder()
                .status(StatusCode::OK)
                .header(header::CONTENT_TYPE, "text/html; charset=utf-8")
                .body(Body::from(html))
                .unwrap()
        }
        None => template_missing(),
    }
}

/// GET /r/:room - Viewer page
pub async fn room_page_handler(Path(_room): Path<String>) -> impl IntoResponse {
    match Templates::get("room.html") {
        Some(content) => {
            // Inline the theme definitions so the viewer can color the
            // terminal without an extra request
            let html = String::from_utf8_lossy(&content.data)
                .replace("{{THEMES_JSON}}", crate::themes::THEMES_JSON);
            Response::builder()
                .status(StatusCode::OK)
                .header(header::CONTENT_TYPE, "text/html; charset=utf-8")
                .body(Body::from(html))
                .unwrap()
        }
        None => template_missing(),
    }
}

/// Serve embedded static files
pub async fn serve_static(req: Request<Body>) -> impl IntoResponse {
    let path = req.uri().path().trim_start_matches('/');
    let method = req.method();

    match StaticAssets::get(path) {
        Some(content) => {
            // Only allow GET and HEAD for existing static files
            if method != Method::GET && method != Method::HEAD {
                return Response::builder()
                    .status(StatusCode::METHOD_NOT_ALLOWED)
                    .header(header::ALLOW, "GET, HEAD")
                    .header(header::CONTENT_TYPE, "text/plain; charset=utf-8")
                    .body(Body::from("Method Not Allowed"))
                    .unwrap();
            }
            let mime = mime_guess::from_path(path).first_or_octet_stream();
            let etag = format!("\"{}\"", hex::encode(content.metadata.sha256_hash()));
            Response::builder()
                .status(StatusCode::OK)
                .header(header::CONTENT_TYPE, mime.as_ref())
                .header(header::CACHE_CONTROL, "public, no-cache")
                .header(header::ETAG, etag)
                .body(Body::from(content.data.into_owned()))
                .unwrap()
        }
        None => Response::builder()
            .status(StatusCode::NOT_FOUND)
            .header(header::CONTENT_TYPE, "text/plain; charset=utf-8")
            .body(Body::from("Not Found"))
            .unwrap(),
    }
}

fn template_missing() -> Response {
    Response::builder()
        .status(StatusCode::INTERNAL_SERVER_ERROR)
        .header(header::CONTENT_TYPE, "text/plain; charset=utf-8")
        .body(Body::from("Template not found"))
        .unwrap()
}
