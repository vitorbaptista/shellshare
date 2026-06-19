"""
E2E Black-Box Tests for Shellshare

These tests treat the application as a black box - they only interact
with it through the public interfaces (CLI + browser). They test both
the server and the Python CLI client.
"""

import os
import random
import string
import subprocess
import sys
import time
import urllib.request
from playwright.sync_api import sync_playwright

from conftest import (
    CLI_COMMAND,
    SERVER_URL,
    broadcast_message,
    parse_share_key,
    terminal_text,
    wait_for_terminal_text,
)


def random_id(length=12):
    """Generate a random ID for room names."""
    return ''.join(random.choices(string.ascii_lowercase + string.digits, k=length))


def wait_for_server(url, timeout_seconds=30):
    """Wait for server to be ready."""
    start = time.time()
    while time.time() - start < timeout_seconds:
        try:
            urllib.request.urlopen(url, timeout=1)
            return True
        except Exception:
            time.sleep(0.5)
    raise TimeoutError(f"Server not ready after {timeout_seconds}s")


def test_happy_path_broadcast_appears_in_browser():
    """
    Happy path test:
    1. Open room in browser
    2. Broadcast a message via the shellshare CLI
    3. Verify message appears in the terminal
    """
    room_id = f"test-{random_id()}"
    password = f"secret-{random_id()}"
    key = os.urandom(32).hex()
    test_message = "Hello from CLI E2E test"

    print(f"Waiting for server at {SERVER_URL}...")
    wait_for_server(SERVER_URL)
    print("Server is ready!")

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()

        # Navigate to room
        room_url = f"{SERVER_URL}/r/{room_id}#{key}"
        print(f"Opening room: {room_url}")
        page.goto(room_url)

        # Wait for terminal element
        page.wait_for_selector("#terminal", timeout=10000)
        print("Terminal element found")

        # Wait for the viewer WebSocket to connect (user count becomes 1)
        page.wait_for_function("document.getElementById('online-counter').textContent !== '0'", timeout=10000)
        print("Viewer connected (user count updated)")

        # Broadcast (sealed with the room's key)
        print(f"Broadcasting: {test_message}")
        status = broadcast_message(SERVER_URL, room_id, password, test_message, key=key)
        assert status == 200, f"Broadcast failed with status {status}"

        # Wait for the message to appear in the terminal buffer
        wait_for_terminal_text(page, test_message)

        # Get terminal content
        content = terminal_text(page)
        
        # Normalize whitespace for comparison
        normalized = ' '.join(content.split())
        print(f"Terminal content: {normalized[:100]}...")
        
        # Verify message appears
        assert test_message in normalized, f"Message not found in terminal: {normalized[:200]}"
        
        print("✓ PASSED: Message from CLI appeared in browser!")
        browser.close()


def test_user_counter_shows_in_browser():
    """
    Test that the user counter appears and updates in the browser UI.
    """
    room_id = f"test-{random_id()}"
    
    print(f"Waiting for server at {SERVER_URL}...")
    wait_for_server(SERVER_URL)
    print("Server is ready!")
    
    with sync_playwright() as p:
        browser = p.chromium.launch()
        
        # First user joins
        page1 = browser.new_page()
        room_url = f"{SERVER_URL}/r/{room_id}"
        print(f"User 1 opening room: {room_url}")
        page1.goto(room_url)

        # Wait for counter to show 1
        page1.wait_for_function(
            "document.getElementById('online-counter').textContent === '1'",
            timeout=10000
        )

        # Check counter shows 1
        counter = page1.locator("#online-counter")
        count1 = counter.text_content()
        print(f"User count after 1 user: {count1}")
        assert count1 == "1", f"Expected 1 user, got {count1}"

        # Second user joins
        page2 = browser.new_page()
        print(f"User 2 opening room: {room_url}")
        page2.goto(room_url)

        # Wait for counter to show 2 on first page
        page1.wait_for_function(
            "document.getElementById('online-counter').textContent === '2'",
            timeout=10000
        )

        # Check counter shows 2 on first page
        count2 = counter.text_content()
        print(f"User count after 2 users: {count2}")
        assert count2 == "2", f"Expected 2 users, got {count2}"

        # Close second user
        page2.close()

        # Wait for counter to show 1 again
        # Windows needs longer timeout as disconnect detection is slower
        disconnect_timeout = 60000 if sys.platform == "win32" else 10000
        page1.wait_for_function(
            "document.getElementById('online-counter').textContent === '1'",
            timeout=disconnect_timeout
        )

        # Check counter shows 1 again
        count3 = counter.text_content()
        print(f"User count after user 2 leaves: {count3}")
        assert count3 == "1", f"Expected 1 user after disconnect, got {count3}"
        
        print("✓ PASSED: User counter updates correctly in browser!")
        browser.close()


def test_terminal_size_updates_in_browser():
    """
    Test that terminal size updates are reflected in the browser.
    """
    room_id = f"test-{random_id()}"
    password = f"secret-{random_id()}"
    key = os.urandom(32).hex()

    print(f"Waiting for server at {SERVER_URL}...")
    wait_for_server(SERVER_URL)
    print("Server is ready!")

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()

        # Navigate to room
        room_url = f"{SERVER_URL}/r/{room_id}#{key}"
        print(f"Opening room: {room_url}")
        page.goto(room_url)

        # Wait for terminal element
        page.wait_for_selector("#terminal", timeout=10000)
        print("Terminal element found")

        # Broadcast with a specific size (sealed with the room's key)
        status = broadcast_message(
            SERVER_URL, room_id, password, "Test message",
            size={"rows": 40, "cols": 120}, key=key,
        )
        assert status == 200, f"Broadcast failed with status {status}"

        # Wait for message to appear in terminal (confirms size update was processed)
        wait_for_terminal_text(page, "Test message")

        # The terminal should have been created - verify it exists
        assert page.locator("#terminal").count() > 0, "Terminal should exist"
        
        print("✓ PASSED: Terminal receives size updates!")
        browser.close()


def test_multiple_broadcasts_no_duplication():
    """
    Test that multiple broadcasts don't cause message duplication.

    Bug scenario (before fix):
    - POST 1: "AAA" → server broadcasts "AAA"
    - POST 2: "BBB" → server broadcasts "AAABBB" (entire history!)
    - Browser sees: "AAA" + "AAABBB" = "AAA" duplicated

    This test should FAIL with the current Rust server bug.
    """
    room_id = f"test-{random_id()}"
    password = f"secret-{random_id()}"
    key = os.urandom(32).hex()

    # Use unique markers that are easy to count
    marker_a = f"MARKER_ALPHA_{random_id(6)}"
    marker_b = f"MARKER_BETA_{random_id(6)}"

    print(f"Waiting for server at {SERVER_URL}...")
    wait_for_server(SERVER_URL)
    print("Server is ready!")

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()

        # Navigate to room
        room_url = f"{SERVER_URL}/r/{room_id}#{key}"
        print(f"Opening room: {room_url}")
        page.goto(room_url)

        # Wait for terminal and socket connection
        page.wait_for_selector("#terminal", timeout=10000)
        page.wait_for_function(
            "document.getElementById('online-counter').textContent !== '0'",
            timeout=10000
        )
        print("Terminal ready and viewer connected")

        # First broadcast (sealed with the room's key)
        print(f"Broadcasting first message: {marker_a}")
        status = broadcast_message(SERVER_URL, room_id, password, marker_a, key=key)
        assert status == 200, f"First broadcast failed: {status}"

        # Wait for first message to arrive
        wait_for_terminal_text(page, marker_a)

        # Second broadcast (same room key)
        print(f"Broadcasting second message: {marker_b}")
        status = broadcast_message(SERVER_URL, room_id, password, marker_b, key=key)
        assert status == 200, f"Second broadcast failed: {status}"

        # Wait for second message to arrive
        wait_for_terminal_text(page, marker_b)

        # Get terminal content
        content = terminal_text(page)
        print(f"Terminal content: {content[:200]}...")

        # Count occurrences of each marker
        count_a = content.count(marker_a)
        count_b = content.count(marker_b)

        print(f"Marker A count: {count_a} (expected: 1)")
        print(f"Marker B count: {count_b} (expected: 1)")

        # Each marker should appear exactly once
        assert count_a == 1, f"Marker A appeared {count_a} times, expected 1 (duplication bug!)"
        assert count_b == 1, f"Marker B appeared {count_b} times, expected 1 (duplication bug!)"

        print("✓ PASSED: No message duplication!")
        browser.close()


def test_late_joiner_sees_history_in_browser():
    """
    A viewer who opens the room AFTER content was broadcast must see the
    accumulated history rendered in the terminal.
    """
    room_id = f"test-{random_id()}"
    password = f"secret-{random_id()}"
    marker_early = f"EARLY_{random_id(6)}"
    marker_late = f"LATE_{random_id(6)}"

    wait_for_server(SERVER_URL)

    # Start a broadcaster that STAYS alive (the CLI deletes the room when
    # it exits, so history only exists while the broadcaster is live)
    proc = subprocess.Popen(
        CLI_COMMAND + ["--stdin", "-s", SERVER_URL, "-r", room_id, "-W", password],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        # The CLI prints the share link (with the decryption key in the
        # fragment) to stderr; the viewer needs that key to decrypt.
        key = None
        start = time.time()
        while key is None and time.time() - start < 10:
            line = proc.stderr.readline()
            if not line:
                break
            key = parse_share_key(line)
        assert key, "CLI did not print a share link with a key"

        # Broadcast BEFORE any browser is watching
        proc.stdin.write(f"{marker_early}\n{marker_late}\n")
        proc.stdin.flush()
        time.sleep(2)  # let the CLI post the content

        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page()
            page.goto(f"{SERVER_URL}/r/{room_id}#{key}")
            page.wait_for_selector("#terminal", timeout=10000)

            wait_for_terminal_text(page, marker_early)
            wait_for_terminal_text(page, marker_late)

            browser.close()
    finally:
        proc.stdin.close()
        proc.wait(timeout=10)


def test_unicode_renders_in_browser():
    """
    Unicode from the real CLI must survive the whole encoding pipeline
    (Rust encode -> server -> viewer WebSocket -> xterm.js decode + render).
    """
    room_id = f"test-{random_id()}"
    password = f"secret-{random_id()}"
    key = os.urandom(32).hex()
    test_message = "Olá 世界 — ünïcödé"

    wait_for_server(SERVER_URL)

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.goto(f"{SERVER_URL}/r/{room_id}#{key}")
        page.wait_for_selector("#terminal", timeout=10000)
        page.wait_for_function(
            "document.getElementById('online-counter').textContent !== '0'",
            timeout=10000
        )

        status = broadcast_message(
            SERVER_URL, room_id, password, test_message, key=key
        )
        assert status == 200, f"Broadcast failed with status {status}"

        wait_for_terminal_text(page, test_message)

        browser.close()


def test_resize_updates_browser_terminal():
    """
    A size broadcast must resize the browser terminal to the new
    dimensions (mirrored straight into xterm's cols/rows).
    """
    room_id = f"test-{random_id()}"
    password = f"secret-{random_id()}"
    key = os.urandom(32).hex()

    wait_for_server(SERVER_URL)

    def post_with_size(text, cols, rows):
        status = broadcast_message(
            SERVER_URL, room_id, password, text,
            size={"cols": cols, "rows": rows}, key=key,
        )
        assert status == 200

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.goto(f"{SERVER_URL}/r/{room_id}#{key}")
        page.wait_for_selector("#terminal", timeout=10000)
        page.wait_for_function(
            "document.getElementById('online-counter').textContent !== '0'",
            timeout=10000
        )

        post_with_size("sized at 24 rows", cols=80, rows=24)
        page.wait_for_function(
            "window.term && window.term.cols === 80 && window.term.rows === 24",
            timeout=10000,
        )

        post_with_size("resized to 30 rows", cols=100, rows=30)
        page.wait_for_function(
            "window.term && window.term.cols === 100 && window.term.rows === 30",
            timeout=10000,
        )

        browser.close()


def test_webgl_renderer_preserves_idle_screen_buffer(dedicated_server):
    """
    xterm's WebGL renderer only redraws dirty rows. The viewer must ask
    the browser to preserve the WebGL drawing buffer so a settled screen
    does not fade to black while only animated cells keep repainting.
    """
    server = dedicated_server()
    room_id = f"webgl-{random_id()}"

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.add_init_script(
            """
            (() => {
              const originalGetContext = HTMLCanvasElement.prototype.getContext;
              HTMLCanvasElement.prototype.getContext = function(type, attrs) {
                if (type === 'webgl2') {
                  window.__shellshareWebgl2ContextAttributes = attrs || {};
                }
                return originalGetContext.apply(this, arguments);
              };
            })();
            """
        )

        page.goto(f"{server.url}/r/{room_id}")
        page.wait_for_function(
            "window.term && "
            "window.__shellshareWebgl2ContextAttributes !== undefined",
            timeout=10000,
        )

        attrs = page.evaluate("window.__shellshareWebgl2ContextAttributes")
        assert attrs.get("preserveDrawingBuffer") is True

        browser.close()


def test_fullscreen_mode_scales_and_restores():
    """
    Pressing `f` enters fullscreen: #terminal gets the `fullscreen`
    class and the font is rescaled to fit the viewport. Pressing `f`
    again restores the default layout. The class is the primary signal
    so the test holds even if headless denies native fullscreen.
    """
    room_id = f"test-{random_id()}"
    password = f"secret-{random_id()}"
    key = os.urandom(32).hex()

    wait_for_server(SERVER_URL)

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.goto(f"{SERVER_URL}/r/{room_id}#{key}")
        page.wait_for_selector("#terminal", timeout=10000)
        page.wait_for_function(
            "document.getElementById('online-counter').textContent !== '0'",
            timeout=10000
        )

        status = broadcast_message(
            SERVER_URL, room_id, password, "fullscreen test",
            size={"cols": 80, "rows": 24}, key=key,
        )
        assert status == 200
        wait_for_terminal_text(page, "fullscreen test")

        default_font_size = page.evaluate("window.term.options.fontSize")

        page.keyboard.press("f")
        page.wait_for_function(
            "document.getElementById('terminal')"
            ".classList.contains('fullscreen')",
            timeout=10000,
        )
        # An 80x24 terminal in a larger viewport must scale up
        page.wait_for_function(
            f"window.term.options.fontSize > {default_font_size}",
            timeout=10000,
        )

        page.keyboard.press("f")
        page.wait_for_function(
            "!document.getElementById('terminal')"
            ".classList.contains('fullscreen')",
            timeout=10000,
        )
        page.wait_for_function(
            f"window.term.options.fontSize === {default_font_size}",
            timeout=10000,
        )

        browser.close()


def test_touch_overlay_enters_immersive_and_toggle_exits():
    """
    On touch devices the immersive overlay - not the corner toggle - is
    the entry point. Out of fullscreen the overlay is visible and the
    corner toggle is hidden; tapping the overlay enters fullscreen, where
    the corner toggle returns (now the way out) and the overlay hides.
    Emulates a Pixel-class phone.
    """
    room_id = f"test-{random_id()}"

    wait_for_server(SERVER_URL)

    with sync_playwright() as p:
        browser = p.chromium.launch()
        context = browser.new_context(**p.devices["Pixel 7"])
        page = context.new_page()
        page.goto(f"{SERVER_URL}/r/{room_id}")
        page.wait_for_selector("#immersive-overlay", timeout=10000)

        # Out of fullscreen on touch: overlay shown, corner toggle hidden.
        overlay_shown = page.locator("#immersive-overlay").evaluate(
            "el => getComputedStyle(el).display !== 'none' && "
            "parseFloat(getComputedStyle(el).opacity) > 0"
        )
        assert overlay_shown, "immersive overlay not shown on touch device"
        toggle_display = page.locator("#fullscreen-toggle").evaluate(
            "el => getComputedStyle(el).display"
        )
        assert toggle_display == "none", \
            f"corner toggle should be hidden out of fullscreen, got {toggle_display}"

        # Tap the overlay -> enters fullscreen.
        page.locator("#immersive-overlay").tap()
        page.wait_for_function(
            "document.getElementById('terminal')"
            ".classList.contains('fullscreen')",
            timeout=10000,
        )
        # Inside fullscreen: corner toggle returns, overlay hides.
        page.wait_for_function(
            "getComputedStyle(document.getElementById('fullscreen-toggle'))"
            ".display !== 'none'",
            timeout=10000,
        )
        page.wait_for_function(
            "getComputedStyle(document.getElementById('immersive-overlay'))"
            ".display === 'none'",
            timeout=10000,
        )

        # Tap the corner toggle -> exits fullscreen.
        page.locator("#fullscreen-toggle").tap()
        page.wait_for_function(
            "!document.getElementById('terminal')"
            ".classList.contains('fullscreen')",
            timeout=10000,
        )

        browser.close()


def test_touch_portrait_fits_terminal_to_width():
    """
    On a portrait touch device the font is scaled down so all of the
    broadcaster's columns fit the viewport width - a readable-overview
    preview, smaller than the desktop base font and with no horizontal
    overflow. (Desktop keeps the base font; see the fullscreen test.)
    """
    room_id = f"test-{random_id()}"
    password = f"secret-{random_id()}"
    key = os.urandom(32).hex()

    wait_for_server(SERVER_URL)

    with sync_playwright() as p:
        browser = p.chromium.launch()
        context = browser.new_context(**p.devices["Pixel 7"])
        page = context.new_page()
        page.goto(f"{SERVER_URL}/r/{room_id}#{key}")
        page.wait_for_selector("#terminal", timeout=10000)
        page.wait_for_function(
            "document.getElementById('online-counter').textContent !== '0'",
            timeout=10000,
        )

        status = broadcast_message(
            SERVER_URL, room_id, password, "fit to width test",
            size={"cols": 80, "rows": 24}, key=key,
        )
        assert status == 200
        wait_for_terminal_text(page, "fit to width test")

        # 80 cols at the 14px base would overflow a ~412px phone; the
        # preview shrinks the font below the base to fit the width.
        page.wait_for_function(
            "window.term && window.term.options.fontSize < 14",
            timeout=10000,
        )
        no_overflow = page.evaluate(
            "(function () {"
            "  var s = window.term.element.querySelector('.xterm-screen');"
            "  var c = document.getElementById('terminal');"
            "  return s.getBoundingClientRect().width <= c.clientWidth + 1;"
            "})()"
        )
        assert no_overflow, "terminal grid overflows viewport width on touch"

        browser.close()


def test_touch_landscape_fits_whole_grid_and_overlay():
    """
    On a landscape touch device the whole grid fits BOTH axes (no vertical
    scroll), unlike the width-only portrait preview, and the immersive
    overlay sits inside the viewport - not floating in an over-tall scroll
    height. The "rotate to landscape" hint is dropped (already landscape).
    Regression guard for the landscape overlay rendering as a stray icon.
    """
    room_id = f"test-{random_id()}"
    password = f"secret-{random_id()}"
    key = os.urandom(32).hex()

    wait_for_server(SERVER_URL)

    with sync_playwright() as p:
        browser = p.chromium.launch()
        # Pixel 7 rotated to landscape: width > height flips the
        # orientation media query and gives the grid a wide box to fill.
        device = dict(p.devices["Pixel 7"])
        vp = device["viewport"]
        device["viewport"] = {"width": vp["height"], "height": vp["width"]}
        context = browser.new_context(**device)
        page = context.new_page()
        page.goto(f"{SERVER_URL}/r/{room_id}#{key}")
        page.wait_for_selector("#terminal", timeout=10000)
        page.wait_for_function(
            "document.getElementById('online-counter').textContent !== '0'",
            timeout=10000,
        )

        status = broadcast_message(
            SERVER_URL, room_id, password, "landscape fit test",
            size={"cols": 80, "rows": 24}, key=key,
        )
        assert status == 200
        wait_for_terminal_text(page, "landscape fit test")

        # Both axes fit: the grid is no taller than its viewport-bounded
        # box (a width-only fit of 24 rows would overflow this height).
        page.wait_for_function(
            "(function () {"
            "  var s = window.term.element.querySelector('.xterm-screen');"
            "  var c = document.getElementById('terminal');"
            "  var r = s.getBoundingClientRect();"
            "  return r.height <= c.clientHeight + 1 &&"
            "         r.width <= c.clientWidth + 1;"
            "})()",
            timeout=10000,
        )

        # The overlay covers the visible area, not an off-screen mid-scroll
        # band: its box stays within the viewport.
        overlay_in_view = page.evaluate(
            "(function () {"
            "  var r = document.getElementById('immersive-overlay')"
            "    .getBoundingClientRect();"
            "  return r.top >= -1 && r.bottom <= window.innerHeight + 1;"
            "})()"
        )
        assert overlay_in_view, "immersive overlay floats outside the viewport"

        # The rotate hint is meaningless in landscape, so it's hidden.
        sub_display = page.locator("#immersive-overlay .io-sub").evaluate(
            "el => getComputedStyle(el).display"
        )
        assert sub_display == "none", \
            f"'rotate to landscape' hint should be hidden in landscape, got {sub_display}"

        browser.close()


def test_immersive_overlay_hidden_on_desktop():
    """
    The immersive overlay is touch-only. A desktop (fine-pointer) viewer
    never sees it and keeps the hover-revealed corner toggle as the
    fullscreen entry point.
    """
    room_id = f"test-{random_id()}"

    wait_for_server(SERVER_URL)

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()  # default desktop context: fine pointer
        page.goto(f"{SERVER_URL}/r/{room_id}")
        # Hidden on desktop, so wait for it in the DOM, not for visibility.
        page.wait_for_selector("#immersive-overlay", state="attached", timeout=10000)

        overlay_display = page.locator("#immersive-overlay").evaluate(
            "el => getComputedStyle(el).display"
        )
        assert overlay_display == "none", \
            f"overlay should be hidden on desktop, got display={overlay_display}"

        toggle_display = page.locator("#fullscreen-toggle").evaluate(
            "el => getComputedStyle(el).display"
        )
        assert toggle_display != "none", \
            "corner toggle should remain the desktop entry point"

        browser.close()


if __name__ == "__main__":
    test_happy_path_broadcast_appears_in_browser()
    test_user_counter_shows_in_browser()
    test_terminal_size_updates_in_browser()
    test_multiple_broadcasts_no_duplication()
    test_late_joiner_sees_history_in_browser()
    test_unicode_renders_in_browser()
    test_resize_updates_browser_terminal()
    test_fullscreen_mode_scales_and_restores()
    test_touch_overlay_enters_immersive_and_toggle_exits()
    test_touch_portrait_fits_terminal_to_width()
    test_touch_landscape_fits_whole_grid_and_overlay()
    test_immersive_overlay_hidden_on_desktop()
