"""
E2E tests for the broadcaster aliveness indicator.

The server tracks ingest WebSocket connections per room and tells
viewers - on join and on every transition - whether a broadcaster's
client is currently attached via the Socket.IO `broadcasting` event.
"Live" means the connection is open, not that output is flowing: the
CLI pings even when idle.
"""

import json

from playwright.sync_api import sync_playwright

from conftest import (
    SERVER_URL,
    SocketListener,
    wait_for_server,
    ws_connect_room,
)


def attach_broadcaster(room_id, password, server_url=SERVER_URL):
    """Open an ingest connection and wait until the server has
    registered it (the ack proves the receive loop - which records the
    attachment before reading any frame - is running)."""
    ws = ws_connect_room(server_url, room_id, password)
    ws.send(json.dumps({"size": {"cols": 80, "rows": 24}}))
    ws.recv()  # ack
    return ws


class TestBroadcastingStatus:
    """Socket.IO `broadcasting` event behavior."""

    def test_join_nonexistent_room_reports_offline(self, unique_room):
        """A viewer joining a room nobody broadcasts to is told so."""
        wait_for_server(SERVER_URL)

        listener = SocketListener(unique_room)
        listener.connect()
        try:
            assert listener.wait_for_broadcasting(False), \
                "Expected broadcasting=false on join of an empty room"
        finally:
            listener.disconnect()

    def test_join_live_room_reports_broadcasting(self, unique_room, unique_password):
        """A viewer joining while the broadcaster is attached sees live."""
        wait_for_server(SERVER_URL)

        ws = attach_broadcaster(unique_room, unique_password)
        try:
            listener = SocketListener(unique_room)
            listener.connect()
            try:
                assert listener.wait_for_broadcasting(True), \
                    "Expected broadcasting=true on join of a live room"
            finally:
                listener.disconnect()
        finally:
            ws.close()

    def test_viewer_sees_broadcaster_come_and_go(self, unique_room, unique_password):
        """An already-joined viewer is notified of both transitions."""
        wait_for_server(SERVER_URL)

        listener = SocketListener(unique_room)
        listener.connect()
        try:
            # The room does not exist yet
            assert listener.wait_for_broadcasting(False)

            ws = attach_broadcaster(unique_room, unique_password)
            assert listener.wait_for_broadcasting(True), \
                "Expected broadcasting=true when the broadcaster attached"

            # The client closing its connection means the session ended,
            # even though it sent no data right before
            ws.close()
            assert listener.wait_for_broadcasting(False), \
                "Expected broadcasting=false when the broadcaster detached"
        finally:
            listener.disconnect()

    def test_room_with_history_but_no_broadcaster_is_offline(
        self, unique_room, unique_password
    ):
        """History alone doesn't make a room live: the one-shot
        broadcast below disconnects, so a later viewer sees offline."""
        wait_for_server(SERVER_URL)

        listener = SocketListener(unique_room)
        listener.connect()
        try:
            ws = attach_broadcaster(unique_room, unique_password)
            ws.send_binary(b"some output")
            ws.recv()  # ack
            assert listener.wait_for_broadcasting(True)
            ws.close()
            assert listener.wait_for_broadcasting(False)

            # A fresh viewer still gets the history, but is told offline
            late = SocketListener(unique_room)
            late.connect()
            try:
                assert late.wait_for_message(containing="some output")
                assert late.wait_for_broadcasting(False)
            finally:
                late.disconnect()
        finally:
            listener.disconnect()


class TestBroadcastingStatusInBrowser:
    """The indicator in the viewer page itself."""

    def test_indicator_follows_broadcaster(self, unique_room, unique_password):
        wait_for_server(SERVER_URL)

        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page()
            page.goto(f"{SERVER_URL}/r/{unique_room}")

            # The indicator is a colored dot; the state lives in the
            # class (color) and title (tooltip).
            # The initial markup already says offline, so prove the
            # join completed (we are the 1 user counted) before
            # asserting the server-reported state
            page.wait_for_function(
                "document.getElementById('online-counter').textContent === '1'",
                timeout=10000,
            )
            page.wait_for_function(
                "document.getElementById('broadcast-status').title === 'offline'",
                timeout=10000,
            )

            ws = attach_broadcaster(unique_room, unique_password)
            try:
                page.wait_for_function(
                    "document.getElementById('broadcast-status').title === 'online'"
                    " && document.getElementById('broadcast-status')"
                    ".classList.contains('online')",
                    timeout=10000,
                )
            finally:
                ws.close()

            page.wait_for_function(
                "document.getElementById('broadcast-status').title === 'offline'"
                " && document.getElementById('broadcast-status')"
                ".classList.contains('offline')",
                timeout=10000,
            )
            browser.close()

    def test_favicon_blinks_only_while_live(
        self, dedicated_server, unique_room, unique_password
    ):
        """While the broadcaster is attached the favicon's underscore
        cursor blinks (the page alternates between the two icon files);
        offline it sits still on the regular icon."""
        # Dedicated server: guarantees the freshly built binary, which
        # is the only one that embeds the new favicon-cursor-off.png
        server = dedicated_server()

        href = "document.getElementById('favicon').href"

        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page()
            page.goto(f"{server.url}/r/{unique_room}")

            page.wait_for_function(
                "document.getElementById('broadcast-status').title === 'offline'",
                timeout=10000,
            )
            assert page.evaluate(href).endswith("/favicon.png"), \
                "Offline room must show the static favicon"

            ws = attach_broadcaster(unique_room, unique_password, server.url)
            try:
                # The blink alternates, so seeing the cursor-off frame
                # and then the cursor-on frame again proves it's cycling
                page.wait_for_function(
                    f"{href}.endsWith('/favicon-cursor-off.png')",
                    timeout=10000,
                )
                page.wait_for_function(
                    f"{href}.endsWith('/favicon.png')",
                    timeout=10000,
                )
            finally:
                ws.close()

            page.wait_for_function(
                "document.getElementById('broadcast-status').title === 'offline'",
                timeout=10000,
            )
            # Offline stops the blink and restores the regular icon; a
            # window longer than the blink interval with zero href
            # mutations proves it stays put (a single sample could land
            # on the cursor-on frame of a still-running blink)
            page.wait_for_function(
                f"{href}.endsWith('/favicon.png')", timeout=10000
            )
            page.evaluate(
                "window.__faviconFlips = 0;"
                "new MutationObserver(function () { window.__faviconFlips++; })"
                ".observe(document.getElementById('favicon'),"
                "         {attributes: true, attributeFilter: ['href']})"
            )
            page.wait_for_timeout(1500)
            assert page.evaluate("window.__faviconFlips") == 0, \
                "Favicon must stop blinking once the broadcaster detaches"
            browser.close()
