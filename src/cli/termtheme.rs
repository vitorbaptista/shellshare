//! Ask the terminal what it actually looks like, so a broadcast can be
//! shown in the broadcaster's own colors instead of a preset guess.
//!
//! Terminals answer three OSC queries with their current colors: OSC 10
//! (foreground), OSC 11 (background) and OSC 4 (each of the 16 ANSI
//! palette entries). The reply comes back on the terminal's input side,
//! so a query is a write followed by a read on the same tty.
//!
//! Everything here is best-effort. A terminal that does not implement
//! the queries simply never answers, so [`detect`] is a timeout away
//! from returning `None` and the caller keeps the preset default -
//! that fallback, not detection, is what runs on Windows, in CI, and
//! under any terminal older than the queries.
//!
//! Measured behaviour that shaped this module:
//!
//! - **Both reply terminators occur in practice.** Alacritty ends its
//!   reply with `ST` (`ESC \`), while tmux in front of that same
//!   Alacritty ends it with `BEL`. Accepting only one of them works
//!   perfectly on the developer's machine and hangs on half of everyone
//!   else's, so both are accepted regardless of which we sent.
//! - **tmux is not in the way.** It forwards the outer terminal's real
//!   colors rather than substituting its own, so no special case.
//! - **The full sweep is cheap when it is answered** (~20ms for all 18
//!   queries), and bounded by a single timeout when it is not, because
//!   a silent OSC 11 ends the attempt before the other 17 are sent.

// Terminal control is ioctl/termios work, so the queries and the raw
// mode around them are unsafe by nature - as in `script.rs`.
#![allow(unsafe_code)]

use std::fmt::Write;
use std::time::{Duration, Instant};

/// How long to wait for one reply, as termios counts it: deciseconds,
/// in `VTIME`. A terminal that implements the queries answers in
/// microseconds; this budget only bounds how long a terminal that never
/// will costs us at startup.
const REPLY_TIMEOUT_DECISECONDS: libc::cc_t = 1;

/// Ceiling on the whole sweep. `REPLY_TIMEOUT` alone does not bound it:
/// it restarts on every byte read, and the tty carries the user's
/// keystrokes as well as the replies, so a paste or a held-down key
/// arriving mid-detection would keep resetting it. A terminal that
/// answers at all answers all 18 queries in ~20ms.
const DETECT_BUDGET: Duration = Duration::from_millis(500);

/// Cap on one reply. A well-formed one is ~25 bytes; anything longer is
/// the input queue, not an answer, and must not grow without bound.
const MAX_REPLY_BYTES: usize = 256;

/// A terminal's own colors, in the shape `themes.json` uses so the
/// viewer can apply a detected theme and a named one through the same
/// path (see `public/javascript/room.js`).
#[derive(Debug, Clone, PartialEq, Eq, serde::Serialize)]
pub struct TerminalColors {
    pub foreground: String,
    pub background: String,
    /// The 16 ANSI colors, as `#rrggbb`
    pub palette: Vec<String>,
}

/// Query the controlling terminal for its colors.
///
/// Returns `None` whenever the answer would be incomplete or
/// unavailable - no controlling terminal, no reply, a partial palette.
/// A partial theme is worse than a preset one: missing ANSI slots
/// render as xterm's defaults against the detected background, which
/// can be unreadable. All or nothing.
#[cfg(unix)]
pub fn detect() -> Option<TerminalColors> {
    let tty = Tty::open()?;
    let deadline = Instant::now() + DETECT_BUDGET;

    // OSC 11 first as a probe: if the terminal does not answer this
    // one, it will not answer the other 17 either, and bailing here is
    // the difference between one timeout and eighteen.
    let background = tty.query_color("11", deadline)?;
    let foreground = tty.query_color("10", deadline)?;

    let mut palette = Vec::with_capacity(16);
    for i in 0..16 {
        palette.push(tty.query_color(&format!("4;{i}"), deadline)?);
    }

    Some(TerminalColors {
        foreground,
        background,
        palette,
    })
}

#[cfg(windows)]
pub fn detect() -> Option<TerminalColors> {
    // ConPTY does not answer OSC color queries, so there is nothing to
    // ask. The caller's preset default is the whole Windows story.
    None
}

/// The controlling terminal, opened read/write and put in raw mode for
/// the duration of the queries.
///
/// `/dev/tty` rather than stdin/stdout on purpose: detection has to
/// work when either is a pipe, which is the normal case for the very
/// invocations this feature is for (`dmesg | shellshare`, `--json`
/// redirected into a file).
#[cfg(unix)]
struct Tty {
    fd: std::os::fd::RawFd,
    original: libc::termios,
}

#[cfg(unix)]
impl Tty {
    fn open() -> Option<Self> {
        // SAFETY: a NUL-terminated path, and the fd is checked
        // before use. Spelled as a byte string rather than a `c"..."`
        // literal, which needs a newer Rust than this crate's MSRV.
        let fd = unsafe { libc::open(b"/dev/tty\0".as_ptr().cast(), libc::O_RDWR) };
        if fd < 0 {
            return None; // no controlling terminal (cron, CI, a daemon)
        }
        // Only the foreground process group may touch terminal
        // settings: `tcsetattr` from a background one raises SIGTTOU,
        // whose default action stops the process group. Without this
        // check `long-build | shellshare &` would suspend itself before
        // it ever printed a link - a job that worked before detection
        // existed, because stream mode never touched termios at all.
        // SAFETY: `fd` is an open terminal.
        if unsafe { libc::tcgetpgrp(fd) } != unsafe { libc::getpgrp() } {
            // SAFETY: `fd` is ours and open.
            unsafe { libc::close(fd) };
            return None;
        }
        // SAFETY: `fd` is a freshly opened terminal; `original` is
        // fully written by `tcgetattr` before it is read.
        unsafe {
            let mut original: libc::termios = std::mem::zeroed();
            if libc::tcgetattr(fd, &mut original) != 0 {
                libc::close(fd);
                return None;
            }
            // Raw mode so the replies arrive as bytes instead of being
            // line-buffered and echoed back at the user.
            let mut raw = original;
            libc::cfmakeraw(&mut raw);
            // cfmakeraw leaves VMIN=1, VTIME=0, which makes read() block
            // until a byte arrives - forever, on a terminal that never
            // answers. VMIN=0 with VTIME in deciseconds turns read()
            // into the timeout itself, so waiting never depends on
            // poll()'s semantics for tty devices (which differ on
            // macOS, where gating read() behind poll() still hung and
            // the share link was never printed).
            raw.c_cc[libc::VMIN] = 0;
            raw.c_cc[libc::VTIME] = REPLY_TIMEOUT_DECISECONDS;
            if libc::tcsetattr(fd, libc::TCSANOW, &raw) != 0 {
                libc::close(fd);
                return None;
            }
            Some(Self { fd, original })
        }
    }

    /// Ask for one color and parse the answer.
    ///
    /// `selector` is the OSC body identifying the color (`11`, `10`, or
    /// `4;<slot>`); the reply must echo it back. Verifying that is what
    /// stops a stale report - the late answer to a previous, timed-out
    /// query - from being accepted as this slot's color and silently
    /// mis-assigning the palette.
    fn query_color(&self, selector: &str, deadline: Instant) -> Option<String> {
        self.write_all(format!("\x1b]{selector};?\x07").as_bytes())?;
        let reply = self.read_reply(deadline)?;
        let expected = format!("\x1b]{selector};");
        reply
            .strip_prefix(expected.as_bytes())
            .and_then(parse_color)
    }

    fn write_all(&self, mut buf: &[u8]) -> Option<()> {
        while !buf.is_empty() {
            // SAFETY: `buf` is a valid slice of `buf.len()` bytes.
            let n = unsafe { libc::write(self.fd, buf.as_ptr().cast(), buf.len()) };
            if n <= 0 {
                return None;
            }
            #[allow(clippy::cast_sign_loss)] // n > 0 checked above
            {
                buf = &buf[n as usize..];
            }
        }
        Some(())
    }

    /// Read until the reply is terminated, or the terminal goes quiet.
    ///
    /// Both terminators are accepted no matter which we sent: the same
    /// terminal answers with `ST` directly and with `BEL` through tmux.
    fn read_reply(&self, deadline: Instant) -> Option<Vec<u8>> {
        let mut reply = Vec::new();
        loop {
            if Instant::now() >= deadline {
                return None;
            }
            let mut chunk = [0u8; 64];
            // SAFETY: `chunk` is a valid buffer of its own length.
            // VMIN=0/VTIME bound this: 0 means the terminal stayed
            // silent for the budget, i.e. it does not answer.
            let n = unsafe { libc::read(self.fd, chunk.as_mut_ptr().cast(), chunk.len()) };
            if n <= 0 {
                return None;
            }
            #[allow(clippy::cast_sign_loss)] // n > 0 checked above
            reply.extend_from_slice(&chunk[..n as usize]);
            if reply.ends_with(b"\x07") || reply.ends_with(b"\x1b\\") {
                return Some(reply);
            }
            if reply.len() > MAX_REPLY_BYTES {
                return None; // not a reply; stop draining the user's input
            }
        }
    }
}

#[cfg(unix)]
impl Drop for Tty {
    fn drop(&mut self) {
        // SAFETY: `fd` is ours and open; `original` came from
        // `tcgetattr` on it.
        unsafe {
            // Discard anything still unread before handing the terminal
            // back. On the timeout path the reply may yet arrive - a
            // slow terminal, an SSH round trip - and whatever is left in
            // the input queue is read next by the stdin forwarder, which
            // would type `\x1b]11;rgb:...` into the broadcast shell as
            // if the user had. TCSADRAIN alone drains output, not input.
            libc::tcflush(self.fd, libc::TCIFLUSH);
            libc::tcsetattr(self.fd, libc::TCSADRAIN, &self.original);
            libc::close(self.fd);
        }
    }
}

/// Parse `ESC ] <n> ; rgb:RRRR/GGGG/BBBB <terminator>` into `#rrggbb`.
///
/// Components are 1-4 hex digits wide depending on the terminal, and
/// are scaled to 8 bits rather than truncated so a terminal that
/// answers in 16-bit precision lands on the same color as one that
/// answers in 8.
#[cfg(unix)]
fn parse_color(reply: &[u8]) -> Option<String> {
    let text = std::str::from_utf8(reply).ok()?;
    let rgb = text.split("rgb:").nth(1)?;
    let rgb = rgb.trim_end_matches(['\x07', '\\', '\x1b']);

    let mut out = String::with_capacity(7);
    out.push('#');
    let mut components = 0;
    for part in rgb.split('/') {
        let hex: String = part.chars().take_while(char::is_ascii_hexdigit).collect();
        if hex.is_empty() || hex.len() > 4 {
            return None;
        }
        let value = u32::from_str_radix(&hex, 16).ok()?;
        let max = (1u32 << (4 * hex.len())) - 1;
        // Round rather than floor, so 16-bit `ffff` maps to `ff` and
        // not `fe`.
        let scaled = (value * 255 + max / 2) / max;
        write!(out, "{scaled:02x}").ok()?;
        components += 1;
    }
    (components == 3).then_some(out)
}
