//! Built-in viewer color themes, embedded from `themes.json`.
//!
//! The JSON file is the single source of truth: the CLI validates
//! `--theme` against its keys, and the server injects the file verbatim
//! into a JSON block on the viewer page, which the viewer script
//! (`public/javascript/room.js`) reads and maps to xterm.js colors.
//! Adding a theme means adding one entry to the file.
//!
//! A theme is `{foreground, background, palette}` where `palette` holds
//! the 16 ANSI colors as hex strings, matching asciinema's theme format.

/// Theme definitions, injected verbatim into the viewer page.
pub const THEMES_JSON: &str = include_str!("../themes.json");

/// The available theme names.
pub fn names() -> Vec<String> {
    serde_json::from_str::<serde_json::Map<String, serde_json::Value>>(THEMES_JSON)
        .expect("themes.json must be a JSON object keyed by theme name")
        .keys()
        .cloned()
        .collect()
}
