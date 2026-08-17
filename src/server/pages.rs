//! HTML pages and static assets, embedded at compile time.
//!
//! Pages are rendered once at startup: every asset reference gets a
//! content-hash query (`?v=...`) so it can be cached forever, and the
//! local stylesheets a page links are flattened (resolving `@import`
//! chains, which left in place would resolve against the page URL and
//! 404) into one bundle served at its own versioned URL. The pages
//! themselves are served with `no-cache` plus an `ETag`, so a deploy
//! takes effect immediately at the cost of a cheap 304.
//!
//! Bundled rather than inlined, though inlining saves a round trip:
//! every room URL is distinct, so CSS and JS carried inside room HTML
//! are re-sent for every broadcast a viewer opens and can never be
//! reused. At their own immutable URLs they are fetched once and shared
//! by every room and both pages.

use std::borrow::Cow;
use std::collections::HashSet;
use std::ops::Range;
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

#[derive(Embed, Clone)]
#[folder = "public/"]
struct StaticAssets;

#[derive(Embed, Clone)]
#[folder = "templates/"]
struct Templates;

/// A page rendered once at startup
struct Page {
    html: String,
    etag: String,
    /// `Link: rel=preload` header so the CDN can emit 103 Early Hints
    /// for the assets the page is about to request. Never empty: every
    /// page has at least its stylesheet bundle to announce.
    preload: String,
    /// The page's flattened stylesheet, and the path it is served at.
    /// Owned by the page because it is derived from the page's own
    /// `<link>` tags; `stylesheet_bundle` finds it again by path.
    stylesheet: (String, Stylesheet),
}

/// A page's local stylesheets, flattened in cascade order with
/// `@import`s resolved and asset URLs versioned, served as one file.
struct Stylesheet {
    css: String,
    /// SHA-256 of `css`: the `ETag`, and the source of the `?v=` that
    /// earns the bundle its immutable caching
    hash: [u8; 32],
}

fn index_page() -> &'static Page {
    static PAGE: OnceLock<Page> = OnceLock::new();
    PAGE.get_or_init(|| render_page("index.html", &[]))
}

fn room_page() -> &'static Page {
    static PAGE: OnceLock<Page> = OnceLock::new();
    PAGE.get_or_init(|| {
        let page = render_page(
            "room.html",
            &[
                // Cloudflare caches Early Hints per exact URL, and every
                // room URL is unique, so the 103 only helps from the
                // second request to a given room onwards
                ("javascript/vendor/xterm.js", "script"),
                ("javascript/vendor/xterm-addon-webgl.js", "script"),
                ("javascript/vendor/xterm-addon-unicode11.js", "script"),
                ("javascript/room.js", "script"),
                // One Inconsolata file (latin + latin-ext + vietnamese);
                // the (large) Nerd Font fallback still loads on demand
                ("font/Inconsolata.woff2", "font"),
            ],
        );
        // Losing either of these leaves a dead terminal on a page that
        // still looks fine, so they fail the boot instead. The `{{...}}`
        // sweep in `render_page` cannot catch them: it only rejects
        // placeholders we do not substitute, not markup deleted whole.
        assert!(
            page.html.contains("/javascript/room.js?v="),
            "template room.html no longer loads the viewer script"
        );
        let themes = page
            .html
            .split_once("id=\"themes\"")
            .and_then(|(_, rest)| rest.split_once('>'))
            .and_then(|(_, rest)| rest.split_once("</script>"))
            .map(|(json, _)| json.trim());
        assert!(
            themes.is_some_and(|json| json.starts_with('{')),
            "template room.html lost its themes JSON block"
        );
        // The viewer script sits outside `version_asset_urls`, which
        // only rewrites the HTML. An asset URL added there would ship
        // unversioned, on the daily TTL, and could drift from the
        // versioned URL the page and its stylesheet request.
        let file = StaticAssets::get("javascript/room.js")
            .expect("public/javascript/room.js not embedded");
        let script = String::from_utf8_lossy(&file.data);
        for prefix in VERSIONED_PREFIXES {
            assert!(
                !script.contains(&format!("/{prefix}")),
                "public/javascript/room.js references /{prefix}..., which nothing versions"
            );
        }
        page
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
    // text. Checked on the template rather than the rendered page, which
    // carries injected llms.txt prose that could otherwise fail this
    // boot with a message blaming the template
    assert!(
        !html
            .replace("{{THEMES_JSON}}", "")
            .replace("{{AGENT_DECODER}}", "")
            .contains("{{"),
        "template {name}: unknown placeholder (misspelled?)"
    );

    // The theme definitions ride in a JSON block rather than in the
    // viewer script, which is what lets that script stay a static,
    // immutably cached file. `<` would end the block early, so a theme
    // name carrying one must fail the boot, not the page.
    assert!(
        !crate::themes::THEMES_JSON.contains('<'),
        "themes.json contains '<', which would break out of its <script> block"
    );
    let html = html.replace("{{THEMES_JSON}}", crate::themes::THEMES_JSON);

    // Asset URLs inside the CSS (the `@font-face` sources) get versioned
    // here: the bundle is served verbatim afterwards, so this is the
    // only chance, and the room page preloads those same fonts - a miss
    // here would preload one URL and request another.
    let links = stylesheet_links(&html);
    assert!(!links.is_empty(), "template {name}: no stylesheet to bundle");
    let mut seen = HashSet::new();
    let mut css = String::new();
    for (_, href) in &links {
        css.push_str(&inline_css(href, &mut seen));
    }
    let css = version_asset_urls(css);
    assert!(
        !css.contains("@import"),
        "template {name}: bundled CSS still contains an @import the bundler does not handle"
    );
    let stylesheet = Stylesheet { hash: Sha256::digest(css.as_bytes()).into(), css };
    let bundle_path = format!(
        "stylesheet/{}.bundle.css",
        name.strip_suffix(".html").unwrap_or(name)
    );
    // The bundle is generated, and `asset_bytes` checks it before
    // `public/`, so a real file at this path would be unreachable
    assert!(
        StaticAssets::get(&bundle_path).is_none(),
        "public/{bundle_path} would be shadowed by the generated bundle"
    );
    let bundle_url = format!(
        "/{bundle_path}?v={}",
        hex::encode(&stylesheet.hash[..VERSION_BYTES])
    );
    let html = replace_stylesheet_links(&html, &links, &bundle_url);
    let html = version_asset_urls(html);

    // Catch template drift at boot. A local stylesheet link the bundler
    // did not match (attribute order?) would still be fetched
    // separately, unversioned and with its `@import`s unresolved; and a
    // script URL `version_asset_urls` left alone names an asset that is
    // not embedded, so it would 404 on every page load.
    assert!(
        !html
            .match_indices("<link")
            .map(|(start, _)| &html[start..start + html[start..].find('>').unwrap_or(0)])
            .any(|tag| {
                tag.contains("stylesheet")
                    && (tag.contains("href=\"/") || tag.contains("href='/"))
                    && !tag.contains(&bundle_url)
            }),
        "template {name}: a local stylesheet link was not bundled (attribute order?)"
    );
    for (start, _) in html.match_indices("=\"/javascript/") {
        let url = &html[start + 2..];
        let url = &url[..url.find('"').expect("unterminated script URL")];
        assert!(
            url.contains("?v="),
            "template {name}: {url} is not an embedded asset"
        );
    }

    // Last, so the reader is never a candidate for the rewrites and
    // scans above: `/llms.txt` ships the same bytes with no rewriting at
    // all, and anything applied here would drift the two copies.
    let html = html.replace("{{AGENT_DECODER}}", &agent_decoder_escaped());

    let etag = format!("\"{}\"", hex::encode(Sha256::digest(html.as_bytes())));
    // The stylesheet leads: it is the page's only render-blocking
    // resource, and unlike the room HTML its URL is shared by every
    // room, so Cloudflare's per-URL Early Hints cache is actually
    // effective for it.
    let preload = std::iter::once(format!("<{bundle_url}>; rel=preload; as=style"))
        .chain(preloads.iter().map(|(path, kind)| {
            // A typo here would emit a preload for a 404 forever
            assert!(
                asset_version(path).is_some(),
                "preload {path} is not an embedded asset"
            );
            let url = versioned_url(path);
            // Font preloads must be anonymous-CORS even same-origin
            let cors = if *kind == "font" { "; crossorigin" } else { "" };
            format!("<{url}>; rel=preload; as={kind}{cors}")
        }))
        .collect::<Vec<_>>()
        .join(", ");

    Page { html, etag, preload, stylesheet: (bundle_path, stylesheet) }
}

/// `templates/agent.mjs`: the reader an agent runs against a share link.
///
/// Inlined into both `llms.txt` and every room page, so neither costs a
/// second request and the two cannot drift. Deliberately not in
/// `public/`, which would serve it as a third copy at its own URL:
/// whoever is reading already has it.
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
/// `--follow`. Anything that parses the HTML decodes the entities back.
fn agent_decoder_escaped() -> String {
    // Only `&` and `<` can end text content; `>` is literal there, and
    // `&` must go first or it would re-escape the escapes
    agent_source().replace('&', "&amp;").replace('<', "&lt;")
}

/// The local `<link rel="stylesheet" href="/...">` tags, in document
/// order, as `(span in `html`, embedded path)`.
fn stylesheet_links(html: &str) -> Vec<(Range<usize>, String)> {
    const LINK_PREFIX: &str = "<link rel=\"stylesheet\" href=\"/";

    let mut links = Vec::new();
    let mut offset = 0;
    while let Some(found) = html[offset..].find(LINK_PREFIX) {
        let start = offset + found;
        let after = start + LINK_PREFIX.len();
        let rest = &html[after..];
        let href_end = rest.find('"').expect("unterminated href in stylesheet link");
        let close = rest[href_end..].find('>').expect("unterminated stylesheet link tag");
        // Bundling keeps only the href, so a tag carrying anything else
        // would have it silently dropped and its rules applied
        // unconditionally - `media="print"` is the one that bites.
        assert!(
            rest[href_end + 1..href_end + close].trim().trim_end_matches('/').is_empty(),
            "stylesheet link for {} carries attributes bundling would drop",
            &rest[..href_end]
        );
        let tag_end = after + href_end + close + 1;
        links.push((start..tag_end, rest[..href_end].to_string()));
        offset = tag_end;
    }
    links
}

/// Collapse the page's stylesheet links into a single link to the
/// bundle, in the position the first one held.
fn replace_stylesheet_links(html: &str, links: &[(Range<usize>, String)], url: &str) -> String {
    let link = format!("<link rel=\"stylesheet\" href=\"{url}\">");
    let mut out = String::with_capacity(html.len());
    let mut prev = 0;
    for (index, (span, _)) in links.iter().enumerate() {
        let before = &html[prev..span.start];
        if index == 0 {
            out.push_str(before);
            out.push_str(&link);
        } else {
            // A dropped tag takes its own line's indentation with it,
            // rather than leaving a blank line behind
            out.push_str(before.trim_end_matches([' ', '\t', '\r', '\n']));
        }
        prev = span.end;
    }
    out.push_str(&html[prev..]);
    out
}

/// The bundle behind `/stylesheet/<page>.bundle.css`, if that is what
/// the path names. Generated, so it is not in `StaticAssets`; a linear
/// scan is enough for two pages.
fn stylesheet_bundle(path: &str) -> Option<&'static Stylesheet> {
    [index_page(), room_page()]
        .into_iter()
        .find(|page| page.stylesheet.0 == path)
        .map(|page| &page.stylesheet.1)
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

fn asset_version(path: &str) -> Option<String> {
    StaticAssets::get(path).map(|file| hex::encode(&file.metadata.sha256_hash()[..VERSION_BYTES]))
}

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
    response = response.header(header::LINK, &page.preload);
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

pub async fn serve_static(req: Request<Body>) -> impl IntoResponse {
    let path = req.uri().path().trim_start_matches('/');
    let method = req.method();

    let Some((bytes, hash)) = asset_bytes(path) else {
        return Response::builder()
            .status(StatusCode::NOT_FOUND)
            .header(header::CONTENT_TYPE, "text/plain; charset=utf-8")
            .body(Body::from("Not Found"))
            .unwrap();
    };
    if method != Method::GET && method != Method::HEAD {
        return Response::builder()
            .status(StatusCode::METHOD_NOT_ALLOWED)
            .header(header::ALLOW, "GET, HEAD")
            .header(header::CONTENT_TYPE, "text/plain; charset=utf-8")
            .body(Body::from("Method Not Allowed"))
            .unwrap();
    }
    // Immutable only when the URL carries the current content hash: that
    // URL provably never serves different bytes.
    //
    // A version that is not ours names bytes this build does not have,
    // and we answer with what we do have. During a rolling deploy that
    // is how a viewer holding new HTML reaches an old replica - and
    // caching the answer for a day would pin a stale script against
    // fresh markup that no reload could fix, because the HTML
    // revalidates and the asset would not. An absent version means an
    // asset whose URL is stable by design, which still gets the day.
    let current = hex::encode(&hash[..VERSION_BYTES]);
    let mut versions = req
        .uri()
        .query()
        .into_iter()
        .flat_map(|query| query.split('&'))
        .filter_map(|param| param.strip_prefix("v="))
        .peekable();
    let cache_control = if versions.peek().is_none() {
        CACHE_ONE_DAY
    } else if versions.any(|version| version == current) {
        CACHE_IMMUTABLE
    } else {
        CACHE_REVALIDATE
    };
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
        .body(Body::from(bytes.into_owned()))
        .unwrap()
}

/// The bytes and content hash behind a static URL: an embedded file
/// from `public/`, or a page's generated stylesheet bundle.
///
/// Borrowed, not owned: this runs before the 405 and 304 branches, and
/// copying here would memcpy the 1.1MB Nerd Font to build every empty
/// `304`. Only the 200 path pays for a copy.
fn asset_bytes(path: &str) -> Option<(Cow<'static, [u8]>, [u8; 32])> {
    if let Some(sheet) = stylesheet_bundle(path) {
        return Some((Cow::Borrowed(sheet.css.as_bytes()), sheet.hash));
    }
    StaticAssets::get(path).map(|file| (file.data, file.metadata.sha256_hash()))
}
