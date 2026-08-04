//! HTML pages and static assets, embedded at compile time.
//!
//! Everything the browser loads that is not room data lives here: the
//! home page, the viewer page, and the static assets under `public/`.
//! The sibling `binaries` module plays the same role for the
//! downloadable CLI binary.
//!
//! Pages are rendered once at startup to make first paint a single
//! round trip: local stylesheets are inlined into the HTML (resolving
//! `@import` chains), and the remaining asset references get a
//! content-hash query (`?v=...`) so they can be cached forever. The
//! pages themselves are served with `no-cache` plus an `ETag`, so a
//! deploy takes effect immediately at the cost of a cheap 304.

use std::collections::HashSet;
use std::sync::OnceLock;

use axum::{
    body::Body,
    http::{header, HeaderMap, Method, Request, StatusCode},
    response::{IntoResponse, Response},
};
use rust_embed::Embed;
use sha2::{Digest, Sha256};

/// Hash-versioned assets never change under a given URL
const CACHE_IMMUTABLE: &str = "public, max-age=31536000, immutable";
/// Stable-URL assets (favicons, robots.txt, ...) may be a day stale
const CACHE_ONE_DAY: &str = "public, max-age=86400";
/// HTML revalidates on every load so deploys show up immediately
const CACHE_REVALIDATE: &str = "public, no-cache";

/// Asset directories whose references get a `?v=<hash>` query and
/// immutable caching. Everything else keeps a stable URL: the favicons
/// are flipped by name in the viewer script, and robots.txt and the
/// OG image are fetched by agents that never see our HTML.
const VERSIONED_PREFIXES: [&str; 2] = ["javascript/", "font/"];

/// Embedded static files from the public directory
#[derive(Embed, Clone)]
#[folder = "public/"]
struct StaticAssets;

/// Embedded view templates
#[derive(Embed, Clone)]
#[folder = "templates/"]
struct Templates;

/// A page rendered once at startup
struct Page {
    html: String,
    etag: String,
    /// `Link: rel=preload` header so the CDN can emit 103 Early Hints
    /// for the assets the page is about to request
    preload: Option<String>,
}

fn index_page() -> &'static Page {
    static PAGE: OnceLock<Page> = OnceLock::new();
    PAGE.get_or_init(|| render_page("index.html", &[]))
}

fn room_page() -> &'static Page {
    static PAGE: OnceLock<Page> = OnceLock::new();
    PAGE.get_or_init(|| {
        render_page(
            "room.html",
            &[
                // Note: Cloudflare caches Early Hints per exact URL,
                // and every room URL is unique, so the 103 only helps
                // from the second request to a given room onwards
                ("javascript/vendor/xterm.js", "script"),
                ("javascript/vendor/xterm-addon-webgl.js", "script"),
                ("javascript/vendor/xterm-addon-unicode11.js", "script"),
                // One Inconsolata file (latin + latin-ext + vietnamese);
                // the (large) Nerd Font fallback still loads on demand
                ("font/Inconsolata.woff2", "font"),
            ],
        )
    })
}

/// `/llms.txt`, with `agent.mjs` inlined into its fenced `js` block.
///
/// A template rather than a static file for that one reason: an agent
/// reading the docs gets the reader in the same response, the way the
/// room page does. Plain text, so the code goes in unescaped.
fn llms_txt() -> &'static (String, String) {
    static PAGE: OnceLock<(String, String)> = OnceLock::new();
    PAGE.get_or_init(|| {
        let file = Templates::get("llms.txt").expect("template llms.txt not embedded");
        let text = String::from_utf8(file.data.into_owned()).expect("llms.txt is not UTF-8");
        assert!(
            text.contains("{{AGENT_DECODER}}"),
            "template llms.txt lost its {{{{AGENT_DECODER}}}} placeholder"
        );
        // Same sweep the pages get: a misspelled placeholder would
        // otherwise ship to every agent as literal text
        assert!(
            !text.replace("{{AGENT_DECODER}}", "").contains("{{"),
            "template llms.txt: unknown placeholder (misspelled?)"
        );
        let text = text.replace("{{AGENT_DECODER}}", agent_source().trim_end());
        let etag = format!("\"{}\"", hex::encode(Sha256::digest(text.as_bytes())));
        (text, etag)
    })
}

/// GET /llms.txt
pub async fn llms_handler(headers: HeaderMap) -> Response {
    let (text, etag) = llms_txt();
    let response = Response::builder()
        .header(header::CACHE_CONTROL, CACHE_ONE_DAY)
        .header(header::ETAG, etag);
    if none_match(&headers, etag) {
        return response.status(StatusCode::NOT_MODIFIED).body(Body::empty()).unwrap();
    }
    response
        .status(StatusCode::OK)
        // Without the charset browsers decode UTF-8 as Latin-1
        .header(header::CONTENT_TYPE, "text/plain; charset=utf-8")
        .body(Body::from(text.clone()))
        .unwrap()
}

/// Render both pages, panicking on any broken template or stylesheet
/// reference. Called at server startup so mistakes fail the boot
/// instead of the first request.
pub fn warm() {
    index_page();
    room_page();
    llms_txt();
}

fn render_page(name: &str, preloads: &[(&str, &str)]) -> Page {
    let template =
        Templates::get(name).unwrap_or_else(|| panic!("template {name} not embedded"));
    let html = String::from_utf8(template.data.into_owned())
        .unwrap_or_else(|_| panic!("template {name} is not UTF-8"));

    // A misspelled placeholder would ship to every visitor as literal
    // text, so require the template's to be ones we substitute below.
    // Checked on the template rather than the rendered page: the page
    // carries injected llms.txt prose, which would otherwise be able to
    // fail this boot with a message blaming the template
    assert!(
        !html
            .replace("{{THEMES_JSON}}", "")
            .replace("{{AGENT_DECODER}}", "")
            .contains("{{"),
        "template {name}: unknown placeholder (misspelled?)"
    );

    // Inline the theme definitions so the viewer can color the
    // terminal without an extra request
    let html = html.replace("{{THEMES_JSON}}", crate::themes::THEMES_JSON);
    // Inline the reader an agent needs for this room, so a fetch of the
    // share link alone is enough. Substituted before the asset rewrites
    // below, which are harmless over it (see agent_decoder_escaped)
    let html = html.replace("{{AGENT_DECODER}}", &agent_decoder_escaped());
    let html = inline_stylesheets(&html);
    let html = version_asset_urls(html);

    // Catch template/CSS drift at boot: an `@import` the inliner did
    // not recognize would 404 (it resolves relative to the page URL
    // once inlined), and a local stylesheet link the inliner did not
    // match would silently skip inlining and versioning
    assert!(
        !html.contains("@import"),
        "template {name}: inlined CSS still contains an @import the inliner does not handle"
    );
    assert!(
        !html
            .match_indices("<link")
            .map(|(start, _)| &html[start..start + html[start..].find('>').unwrap_or(0)])
            .any(|tag| {
                tag.contains("stylesheet") && (tag.contains("href=\"/") || tag.contains("href='/"))
            }),
        "template {name}: a local stylesheet link was not inlined (attribute order?)"
    );

    let etag = format!("\"{}\"", hex::encode(Sha256::digest(html.as_bytes())));
    let preload = (!preloads.is_empty()).then(|| {
        preloads
            .iter()
            .map(|(path, kind)| {
                let url = versioned_url(path);
                // Font preloads must be anonymous-CORS even same-origin
                let cors = if *kind == "font" { "; crossorigin" } else { "" };
                format!("<{url}>; rel=preload; as={kind}{cors}")
            })
            .collect::<Vec<_>>()
            .join(", ")
    });

    Page { html, etag, preload }
}

/// `templates/agent.mjs`: the reader an agent runs against a share link.
///
/// Inlined into both `llms.txt` and every room page, so neither costs a
/// second request and the two cannot drift. Deliberately not in
/// `public/`, which would serve it as a third copy at its own URL:
/// whoever is reading already has it. Panics at boot (`bind()` warms
/// the pages before taking a port) rather than serving an empty brief
/// to every agent until someone notices.
fn agent_source() -> String {
    let file = Templates::get("agent.mjs").expect("agent.mjs not embedded");
    let code = String::from_utf8(file.data.into_owned()).expect("agent.mjs is not UTF-8");
    assert!(
        code.contains("function decode("),
        "agent.mjs no longer holds the decoder"
    );
    code
}

/// `agent_source()` escaped for the room page's `<pre>`.
///
/// The escaping is what lets the reader live in the DOM as text, and
/// the DOM is the whole point: an HTML comment or a `<script>` would
/// need no escaping, but both are raw-text containers, and raw-text
/// containers are exactly what extractors strip - measured, and the
/// comment would also break open on any `-->` in a file full of
/// `--follow`. Nobody pays for this but a reader of the raw
/// bytes: anything that parses the HTML decodes the entities back.
///
/// Escaping `<` is load-bearing beyond correctness: it is what keeps
/// the snippet invisible to `inline_stylesheets` and to the `<link`
/// assertion, both of which run over the page afterwards. For the same
/// reason keep quoted `/javascript/...` and `/font/...` paths and the
/// literal `@import` out of `agent.mjs` - `version_asset_urls` would
/// rewrite the former, and the latter trips a boot assertion whose
/// message blames the stylesheet inliner.
fn agent_decoder_escaped() -> String {
    // Only `&` and `<` can end text content; `>` is literal there, and
    // `&` must go first or it would re-escape the escapes
    agent_source().replace('&', "&amp;").replace('<', "&lt;")
}

/// Replace local `<link rel="stylesheet" href="/...">` tags with
/// `<style>` blocks holding the file contents. `@import` chains are
/// resolved here too: left in place they would resolve relative to the
/// page URL and 404. Each stylesheet is included at most once.
fn inline_stylesheets(html: &str) -> String {
    const LINK_PREFIX: &str = "<link rel=\"stylesheet\" href=\"/";

    let mut out = String::with_capacity(html.len());
    let mut rest = html;
    let mut seen = HashSet::new();
    while let Some(start) = rest.find(LINK_PREFIX) {
        out.push_str(&rest[..start]);
        let after = &rest[start + LINK_PREFIX.len()..];
        let href_end = after.find('"').expect("unterminated href in stylesheet link");
        let tag_end = after[href_end..]
            .find('>')
            .expect("unterminated stylesheet link tag")
            + href_end
            + 1;
        out.push_str("<style>\n");
        out.push_str(&inline_css(&after[..href_end], &mut seen));
        out.push_str("</style>");
        rest = &after[tag_end..];
    }
    out.push_str(rest);
    out
}

/// Embedded stylesheet contents with `@import "..."` lines (as written
/// in our own CSS) recursively expanded, relative to the importing
/// file. Already-seen files expand to nothing: unlike a browser, which
/// re-applies a sheet at every position it is imported, only the first
/// occurrence keeps its place in the cascade — fine while shared
/// sheets (`base.css`) are imported once per page.
fn inline_css(path: &str, seen: &mut HashSet<String>) -> String {
    if !seen.insert(path.to_string()) {
        return String::new();
    }
    let file =
        StaticAssets::get(path).unwrap_or_else(|| panic!("stylesheet {path} not embedded"));
    let css = String::from_utf8(file.data.into_owned())
        .unwrap_or_else(|_| panic!("stylesheet {path} is not UTF-8"));
    let dir = path.rsplit_once('/').map_or("", |(dir, _)| dir);

    let mut out = String::new();
    for line in css.lines() {
        let import = line
            .trim()
            .strip_prefix("@import \"")
            .and_then(|rest| rest.strip_suffix("\";"));
        if let Some(import) = import {
            let resolved = if dir.is_empty() {
                import.to_string()
            } else {
                format!("{dir}/{import}")
            };
            out.push_str(&inline_css(&resolved, seen));
        } else {
            out.push_str(line);
            out.push('\n');
        }
    }
    out
}

/// Rewrite quoted references to versioned assets (`'/font/x.woff2'`,
/// `"/javascript/vendor/x.js"`) to carry the content-hash query
fn version_asset_urls(mut html: String) -> String {
    for path in StaticAssets::iter() {
        if !VERSIONED_PREFIXES.iter().any(|p| path.starts_with(p)) {
            continue;
        }
        let to = versioned_url(&path);
        for quote in ['\'', '"'] {
            html = html.replace(&format!("{quote}/{path}{quote}"), &format!("{quote}{to}{quote}"));
        }
    }
    html
}

fn versioned_url(path: &str) -> String {
    asset_version(path).map_or_else(
        || format!("/{path}"),
        |version| format!("/{path}?v={version}"),
    )
}

/// Length of the content-hash prefix used as the cache-busting version
const VERSION_BYTES: usize = 8;

/// Short content hash used as the cache-busting version
fn asset_version(path: &str) -> Option<String> {
    StaticAssets::get(path).map(|file| hex::encode(&file.metadata.sha256_hash()[..VERSION_BYTES]))
}

/// GET / - Home page
pub async fn index_handler(headers: HeaderMap) -> impl IntoResponse {
    serve_page(index_page(), &headers)
}

/// The viewer page response for `GET /r/:room`. Not an axum handler:
/// `room_get_handler` calls it directly (after branching to the `.bin`
/// raw-history response), so it takes a plain borrow and no extractors.
pub fn room_page_response(headers: &HeaderMap) -> Response {
    let mut response = serve_page(room_page(), headers);
    let (name, value) = noindex_header();
    response.headers_mut().insert(name, value);
    response
}

/// `X-Robots-Tag: noindex` - keep rooms out of search indexes.
///
/// The directive rather than a `robots.txt` `Disallow`, because only
/// this one says what we actually want. `Disallow` says "do not fetch",
/// which stops a crawler from ever reading the `noindex` and so leaves a
/// leaked room URL listable by URL alone; and telling a search crawler
/// apart from a human's assistant opening a pasted link means naming
/// every agent, forever. This is about the resource, not who asked.
pub const fn noindex_header() -> (header::HeaderName, header::HeaderValue) {
    (
        header::HeaderName::from_static("x-robots-tag"),
        header::HeaderValue::from_static("noindex"),
    )
}

fn serve_page(page: &Page, request_headers: &HeaderMap) -> Response {
    let mut response = Response::builder()
        .header(header::CACHE_CONTROL, CACHE_REVALIDATE)
        .header(header::ETAG, &page.etag);
    if let Some(preload) = &page.preload {
        response = response.header(header::LINK, preload);
    }
    if none_match(request_headers, &page.etag) {
        return response.status(StatusCode::NOT_MODIFIED).body(Body::empty()).unwrap();
    }
    response
        .status(StatusCode::OK)
        .header(header::CONTENT_TYPE, "text/html; charset=utf-8")
        .body(Body::from(page.html.clone()))
        .unwrap()
}

/// Whether the request's `If-None-Match` matches the entity tag. The
/// `contains` comparison also accepts the weak form (`W/"..."`) that
/// CDN compression may have rewritten our strong tag into.
fn none_match(headers: &HeaderMap, etag: &str) -> bool {
    headers
        .get(header::IF_NONE_MATCH)
        .and_then(|value| value.to_str().ok())
        .is_some_and(|value| value == "*" || value.contains(etag))
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
            // Immutable only when the URL carries the current content
            // hash: that URL provably never serves different bytes. A
            // stale or absent version (e.g. an already-open page
            // requesting a pre-deploy URL) falls back to the daily TTL.
            let hash = content.metadata.sha256_hash();
            let current = hex::encode(&hash[..VERSION_BYTES]);
            let immutable = req.uri().query().is_some_and(|query| {
                query
                    .split('&')
                    .any(|param| param.strip_prefix("v=") == Some(&current))
            });
            let cache_control = if immutable { CACHE_IMMUTABLE } else { CACHE_ONE_DAY };
            let etag = format!("\"{}\"", hex::encode(hash));

            let response = Response::builder()
                .header(header::CACHE_CONTROL, cache_control)
                .header(header::ETAG, &etag);
            if none_match(req.headers(), &etag) {
                return response.status(StatusCode::NOT_MODIFIED).body(Body::empty()).unwrap();
            }
            let mime = mime_guess::from_path(path).first_or_octet_stream();
            // Text files are UTF-8; without an explicit charset browsers
            // fall back to Latin-1 and garble non-ASCII (e.g. llms.txt)
            let content_type = if mime.type_() == mime_guess::mime::TEXT
                && mime.get_param(mime_guess::mime::CHARSET).is_none()
            {
                format!("{mime}; charset=utf-8")
            } else {
                mime.to_string()
            };
            response
                .status(StatusCode::OK)
                .header(header::CONTENT_TYPE, content_type)
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
