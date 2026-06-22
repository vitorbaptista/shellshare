"""
RED acceptance test for shellshare#164: a late joiner to a long-running
full-screen TUI must still see the static parts of the screen, not only the
cells that keep changing.

A full-screen TUI paints the whole screen once (enter alt screen, clear, draw
the chrome) and then emits only small incremental updates forever. The server
keeps a bounded replay log for late joiners (`MessageHistory`,
`MAX_HISTORY_MESSAGES = 200`), so after >200 incremental frames the initial full
paint is evicted and a late joiner replays only the changing region - a mostly
blank screen with a lonely counter.

The fix is broadcaster-side encrypted keyframes (issue #164): the CLI runs a
headless terminal emulator and periodically broadcasts a sealed full-screen
redraw, so a late joiner always reconstructs the whole screen.

This test fails today (the viewer never renders the static chrome) and should
pass once keyframes land. The assertion is made against the real viewer
(xterm.js via `window.shellshareText()`), so it is independent of exactly how a
keyframe serializes the screen - it only asserts what a user actually sees.
"""

import subprocess
import time

from conftest import (
    CLI_COMMAND,
    SERVER_URL,
    SocketListener,
    parse_share_key,
    wait_for_content,
    wait_for_terminal_text,
)
from playwright.sync_api import sync_playwright

# Painted once at the top of the screen, then never re-sent.
STATIC_MARKER = "DASHBOARD"
# Enter alt screen + clear, then a 3-line frame whose top line carries the
# marker; this is the "base paint" a real TUI does once on startup.
BASE_PAINT = (
    "\x1b[?1049h\x1b[2J\x1b[H"
    "\x1b[1;1H+==== DASHBOARD ====+"
    "\x1b[2;1H| loading...        |"
    "\x1b[3;1H+===================+"
)
# Comfortably more than MAX_HISTORY_MESSAGES (200) so the base paint is evicted
# well before a late joiner connects. Each update is its own history frame.
NUM_UPDATES = 300
LAST_COUNTER = f"C={NUM_UPDATES - 1:05d}"  # "C=00299"
FIRST_COUNTER = "C=00000"
# Spacing so each write lands in its own stdin read -> its own history frame
# (the CLI coalesces only what is already queued).
STEP_SECONDS = 0.02


def _start_broadcaster(room, password):
    """Start a long-lived encrypted `--stdin` broadcast; return (proc, key).

    `--stdin` broadcasts raw stdin bytes directly (no inner shell/PTY), so the
    test emits an exact alt-screen byte stream and controls frame boundaries
    from Python.
    """
    proc = subprocess.Popen(
        CLI_COMMAND + ["--stdin", "-s", SERVER_URL, "-r", room, "-W", password],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    # The share link (with the #key fragment) is printed at startup, before any
    # stdin is consumed.
    deadline = time.time() + 10
    seen = ""
    key = None
    while time.time() < deadline and key is None:
        line = proc.stderr.readline()
        if not line:
            break
        seen += line
        key = parse_share_key(seen)
    assert key, f"No share key printed by broadcaster: {seen!r}"
    return proc, key


def _finish(proc):
    try:
        proc.stdin.close()
    except (BrokenPipeError, OSError):
        pass
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


def test_late_joiner_sees_static_chrome_of_long_running_tui(
    unique_room, unique_password
):
    proc, key = _start_broadcaster(unique_room, unique_password)
    try:
        # Paint the full screen once, then update only one cell, many times.
        proc.stdin.write(BASE_PAINT)
        proc.stdin.flush()
        for i in range(NUM_UPDATES):
            proc.stdin.write(f"\x1b[5;1HC={i:05d}")
            proc.stdin.flush()
            time.sleep(STEP_SECONDS)

        # Precondition: the base paint really was evicted. A late joiner's
        # snapshot carries the recent counter but no longer the earliest one.
        peek = SocketListener(unique_room)
        peek.connect()
        peek.set_key(key)
        assert wait_for_content(
            peek, lambda s: LAST_COUNTER in s, timeout=10
        ), "late joiner never received the live counter"
        snapshot = peek.get_accumulated_messages()
        peek.disconnect()
        assert FIRST_COUNTER not in snapshot, (
            "history was not evicted - raise NUM_UPDATES "
            f"(snapshot head: {snapshot[:80]!r})"
        )

        # Behavior under test: a freshly-loaded viewer renders the static
        # chrome, not just the changing cell.
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page()
            page.goto(f"{SERVER_URL}/r/{unique_room}#{key}")
            # Sanity: the viewer is alive and rendering the changing region.
            wait_for_terminal_text(page, LAST_COUNTER)
            # RED today: the static chrome was evicted and never re-sent, so
            # this times out. Keyframes (#164) make it pass.
            wait_for_terminal_text(page, STATIC_MARKER, timeout=8000)
            browser.close()
    finally:
        _finish(proc)


def test_no_keyframes_leak_into_scrollback_after_alt_exit(
    unique_room, unique_password
):
    """A TUI that enters the alternate screen, saves a cursor, then EXITS and
    resumes normal scrolling must NOT get keyframes injected into its
    scrollback.

    avt's screen dump emits the alternate-screen switch (`?1047h`) not only
    while on the alt screen but also afterwards, whenever a cursor was ever
    saved there (`vim`/`less`/`htop`/git pager all do). So the emit gate must
    key off the CURRENT screen (an *unpaired* `?1047h`), not merely "the dump
    mentions `?1047h`" - otherwise every common TUI keeps re-injecting a
    full-screen redraw into a plain scrolling session, corrupting connected
    viewers. (Caught in the #164 implementation review.)
    """
    proc, key = _start_broadcaster(unique_room, unique_password)
    try:
        # Brief alt-screen TUI that SAVES a non-home cursor (ESC 7), kept under
        # the keyframe cadence so no (legitimate) keyframe fires while on alt.
        # The cursor must be off home before the save: avt's default saved
        # context is the home position, so a home-position save stays "default"
        # and would not leave the lingering alt context that triggers the bug.
        proc.stdin.write("\x1b[?1049h\x1b[2J\x1b[5;10H\x1b7\x1b[HALTMARK")
        proc.stdin.flush()
        for i in range(5):
            proc.stdin.write(f"\x1b[3;1Ht={i}")
            proc.stdin.flush()
            time.sleep(STEP_SECONDS)
        proc.stdin.write("\x1b[?1049l")  # exit the alternate screen
        proc.stdin.flush()
        # Resume normal scrolling well past a keyframe interval.
        for i in range(120):
            proc.stdin.write(f"LINE-{i:04d}\r\n")
            proc.stdin.flush()
            time.sleep(STEP_SECONDS)

        late = SocketListener(unique_room)
        late.connect()
        late.set_key(key)
        assert wait_for_content(
            late, lambda s: "LINE-0119" in s, timeout=10
        ), "late joiner never received the scrolled output"
        snapshot = late.get_accumulated_messages()
        late.disconnect()
        # A keyframe normalizes avt's alt switch to `ESC[?1047`; the broadcaster
        # itself only ever sent `ESC[?1049{h,l}`. So any `?1047` in the snapshot
        # is a keyframe wrongly injected into a primary-screen session.
        assert "\x1b[?1047" not in snapshot, (
            "keyframe leaked into post-alt-exit scrollback "
            f"(snapshot tail: {snapshot[-120:]!r})"
        )
    finally:
        _finish(proc)
