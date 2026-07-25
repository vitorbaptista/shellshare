//! Periodic full-screen "keyframes" for late joiners (issue #164).
//!
//! A long-running full-screen TUI paints the screen once and then emits only
//! incremental updates. The server keeps a bounded replay history, so the
//! initial full paint is evicted and a late joiner sees only the changing
//! cells. This module runs a headless terminal emulator (`avt`) over the exact
//! broadcast byte stream and, on a cadence, produces a self-contained redraw of
//! the current screen. The redraw is broadcast as ordinary (sealed) output, so
//! it lands in history and a late joiner reconstructs the whole screen.
//!
//! Keyframes are produced ONLY for alternate-screen sessions (full-screen
//! TUIs). A normal scrolling shell keeps raw-history behavior untouched, so its
//! scrollback is never replaced by a visible-screen-only snapshot.

use crate::protocol::TermSize;

/// Emit a keyframe at most once per this many broadcast frames. Kept well below
/// the server's history bounds (`MAX_HISTORY_MESSAGES` and `MAX_HISTORY_BYTES`,
/// `src/server/rooms.rs`) so a recent keyframe survives in the window a late
/// joiner replays - the byte budget is derived from this number and the maximum
/// frame size, so moving any of them means redoing that arithmetic. Note this counts *fed frames*, not stored history
/// messages - the sender coalesces reads, so one fed frame need not be one
/// history message; the wide margin absorbs that.
const FRAMES_PER_KEYFRAME: u32 = 60;

/// Cap on carried bytes. A valid incomplete UTF-8 tail is at most 3 bytes;
/// anything larger means malformed input, so the carry is dropped to resync.
const MAX_CARRY: usize = 8;

/// Runs a headless terminal over the broadcast stream and produces keyframes.
pub struct Keyframer {
    vt: avt::Vt,
    cols: usize,
    rows: usize,
    /// Bytes that ended mid-UTF-8 sequence, carried to the next feed.
    carry: Vec<u8>,
    /// Broadcast frames fed since the last keyframe (or since start).
    frames_since: u32,
}

impl Keyframer {
    pub fn new(size: TermSize) -> Self {
        let cols = usize::from(size.cols.max(1));
        let rows = usize::from(size.rows.max(1));
        Self {
            vt: avt::Vt::new(cols, rows),
            cols,
            rows,
            carry: Vec::new(),
            frames_since: 0,
        }
    }

    /// Feed one broadcast frame's plaintext bytes and count it. Resizes the
    /// emulator first when the terminal size changed.
    pub fn feed(&mut self, data: &[u8], size: TermSize) {
        self.apply_resize(size);
        if data.is_empty() {
            return;
        }
        self.feed_bytes(data);
        self.frames_since = self.frames_since.saturating_add(1);
    }

    /// The keyframe to broadcast now, or `None`. Emits only when the cadence is
    /// due AND the session is on the alternate screen (a full-screen TUI).
    pub fn maybe_keyframe(&mut self) -> Option<Vec<u8>> {
        if self.frames_since < FRAMES_PER_KEYFRAME {
            return None;
        }
        // Reset on every due check, even when gated out below: a primary-screen
        // session must not bank a backlog of keyframes that all fire the instant
        // it enters the alternate screen.
        self.frames_since = 0;
        // `dump()` allocates a redraw of the whole screen; on a primary-screen
        // (scrolling) session this runs every `FRAMES_PER_KEYFRAME` frames only
        // to be discarded by the gate below. Acceptable for now (see #164).
        let dump = self.vt.dump();
        // Emit only while the session is CURRENTLY on the alternate screen (a
        // live full-screen TUI). avt's dump switches to the alternate buffer
        // with the 8-bit CSI `?1047h`, and switches back with `?1047l` when the
        // alternate buffer is no longer active but a cursor was once saved there
        // (which `vim`/`less`/`htop`/git pager all do). So an *unpaired* `?1047h`
        // means "on alt now"; a `?1047h`+`?1047l` pair means "exited alt, with a
        // lingering saved context" and must NOT keyframe - otherwise every
        // common TUI would re-inject a full-screen redraw into its scrollback
        // after quitting. A normal scrolling shell produces neither sequence.
        if !dump.contains("\u{9b}?1047h") || dump.contains("\u{9b}?1047l") {
            return None;
        }
        // avt uses the 8-bit CSI (U+009B); rewrite to the 7-bit `ESC [` form
        // that every terminal and xterm.js accept.
        Some(dump.replace('\u{9b}', "\x1b[").into_bytes())
    }

    fn apply_resize(&mut self, size: TermSize) {
        let cols = usize::from(size.cols.max(1));
        let rows = usize::from(size.rows.max(1));
        if cols != self.cols || rows != self.rows {
            self.vt.resize(cols, rows);
            self.cols = cols;
            self.rows = rows;
            // Force a fresh keyframe so the stored keyframe matches the new
            // size announced to viewers.
            self.frames_since = FRAMES_PER_KEYFRAME;
        }
    }

    /// Feed raw bytes to avt, which consumes `&str`. Terminal output may split
    /// a multibyte char across frames, so a valid UTF-8 prefix is fed now and
    /// an incomplete trailing sequence is carried; genuinely invalid bytes are
    /// skipped (xterm renders those as replacement chars too).
    fn feed_bytes(&mut self, data: &[u8]) {
        let mut buf = std::mem::take(&mut self.carry);
        buf.extend_from_slice(data);
        let mut from = 0;
        loop {
            match std::str::from_utf8(&buf[from..]) {
                Ok(s) => {
                    if !s.is_empty() {
                        self.vt.feed_str(s);
                    }
                    from = buf.len();
                    break;
                }
                Err(e) => {
                    let valid = e.valid_up_to();
                    if valid > 0 {
                        // `valid_up_to` guarantees this slice is valid UTF-8.
                        if let Ok(s) = std::str::from_utf8(&buf[from..from + valid]) {
                            self.vt.feed_str(s);
                        }
                    }
                    match e.error_len() {
                        // Incomplete trailing sequence: carry the remainder.
                        None => {
                            from += valid;
                            break;
                        }
                        // Invalid bytes: skip them and keep decoding.
                        Some(n) => from += valid + n,
                    }
                }
            }
        }
        self.carry = buf[from..].to_vec();
        if self.carry.len() > MAX_CARRY {
            self.carry.clear();
        }
    }
}
