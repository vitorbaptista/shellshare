"""
Synchronized output (DEC private mode 2026) in the viewer.

A TUI that repaints its whole screen wraps the repaint in BSU/ESU
(`CSI ?2026h` ... `CSI ?2026l`) to say "this is one frame, do not paint
anything until I'm done". Shellshare is the worst case for ignoring
that: the repaint is coalesced into WebSocket frames and crosses the
network, so an intermediate state can sit in the viewer for a whole
network round trip and gets painted as a visible flicker.

xterm.js 5.5 (currently vendored) ignores mode 2026 entirely, so this
test FAILS: the viewer paints the half-drawn screen. xterm.js 6.0 added
synchronized output support, so it should PASS after the upgrade.

The assertion is at the RENDER layer, not the buffer layer: xterm parses
into the buffer either way, and only defers painting. So the test hooks
`term.onRender` and records what each paint would have shown - the
intermediate marker must never appear in any painted frame.
"""

import time

from playwright.sync_api import sync_playwright

from conftest import (
    SERVER_URL,
    ws_connect_room,
)

# One "frame" of a full-screen repaint, split the way a real broadcast
# splits it: home + clear, the old content, then the new content.
_BSU = b"\x1b[?2026h"
_ESU = b"\x1b[?2026l"
_HOME_CLEAR = b"\x1b[H\x1b[2J"

_INTERMEDIATE = "HALF-DRAWN"
_FINAL = "COMPLETE-FRAME"

# Long enough that a renderer with no synchronized-output support is
# certain to have run at least one animation frame in the gap.
_INTER_FRAME_DELAY = 0.25


def _record_painted_frames(page):
    """Capture the buffer contents at every xterm render.

    window.term is exposed by the viewer for exactly this kind of
    inspection - the WebGL renderer leaves no text in the DOM.
    """
    page.wait_for_function("window.term !== undefined", timeout=10000)
    page.evaluate(
        """
        window.__paintedFrames = [];
        window.__paintDisposable = window.term.onRender(function () {
          var buf = window.term.buffer.active;
          var out = [];
          for (var i = 0; i < window.term.rows; i++) {
            var line = buf.getLine(buf.viewportY + i);
            if (line) out.push(line.translateToString(true));
          }
          window.__paintedFrames.push(out.join('\\n'));
        });
        """
    )


def test_synchronized_output_is_not_torn(unique_room, unique_password):
    ws = ws_connect_room(SERVER_URL, unique_room, unique_password)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        try:
            _run_sync_output_check(page, ws, unique_room)
        finally:
            browser.close()
            ws.close()


def _run_sync_output_check(page, ws, room):
    ws.send('{"size": {"cols": 80, "rows": 24}}')
    ws.recv()

    page.goto(f"{SERVER_URL}/r/{room}")
    _record_painted_frames(page)

    # Open the synchronized frame and draw the state the viewer must
    # never show, then stall the way the network does.
    ws.send_binary(_BSU + _HOME_CLEAR + _INTERMEDIATE.encode())
    ws.recv()
    time.sleep(_INTER_FRAME_DELAY)

    # Finish the frame and close it.
    ws.send_binary(_HOME_CLEAR + _FINAL.encode() + _ESU)
    ws.recv()

    page.wait_for_function(
        "text => window.shellshareText().includes(text)",
        arg=_FINAL,
        timeout=10000,
    )
    # Give the renderer room to paint the final frame.
    page.wait_for_timeout(250)

    painted = page.evaluate("window.__paintedFrames")
    torn = [f for f in painted if _INTERMEDIATE in f]
    assert not torn, (
        f"viewer painted the half-drawn frame {len(torn)} time(s) inside a "
        f"synchronized-output block ({len(painted)} paints total); "
        "xterm.js is ignoring DEC mode 2026"
    )
    assert any(_FINAL in f for f in painted), \
        "the completed frame was never painted - the test proved nothing"
