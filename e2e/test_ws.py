"""
E2E tests for the WebSocket ingest transport.

The CLI broadcasts over `GET /ws/r/:room`: binary frames carry raw
terminal bytes, text frames carry JSON control messages (`size`,
`delete`). The room is claimed - or its password verified - at the
handshake.

Also covers the reliability behaviors built on top of this transport:
the CLI buffering output across a server restart, and the browser
viewer re-joining after a reconnect.
"""

import json
import subprocess
import time

import pytest
import websocket
from playwright.sync_api import sync_playwright

import os

import requests

from conftest import (
    CLI_COMMAND,
    SERVER_URL,
    SocketListener,
    _free_port,
    _spawn_server,
    broadcast_message,
    parse_share_key,
    random_id,
    wait_for_content,
    wait_for_server,
    ws_connect_room,
)


def read_share_key(proc, timeout=10):
    """Read the CLI's printed share link from (bytes) stderr; return key hex."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        line = proc.stderr.readline()
        if not line:
            break
        key = parse_share_key(line.decode(errors="replace"))
        if key is not None:
            return key
    return None


def ws_url(server_url, room):
    return f"{server_url.replace('http://', 'ws://')}/ws/r/{room}"


def ws_connect(room, password, server_url=SERVER_URL, timeout=5):
    return websocket.create_connection(
        ws_url(server_url, room),
        header={"Authorization": password},
        timeout=timeout,
    )


class TestWsIngest:
    def test_binary_frame_reaches_viewer(self, unique_room, unique_password, socket_listener):
        ws = ws_connect(unique_room, unique_password)
        try:
            ws.send_binary("Hello over WebSocket!".encode())
            msg = socket_listener.wait_for_message(timeout=5, containing="Hello over WebSocket!")
            assert msg is not None
        finally:
            ws.close()

    def test_binary_frames_accumulate_in_history(self, unique_room, unique_password):
        ws = ws_connect(unique_room, unique_password)
        try:
            for i in range(5):
                ws.send_binary(f"chunk-{i};".encode())
        finally:
            ws.close()

        late = SocketListener(unique_room)
        late.connect()
        try:
            ok = wait_for_content(late, lambda acc: "chunk-4;" in acc, timeout=5)
            assert ok, "history never arrived"
            accumulated = late.get_accumulated_messages()
            assert accumulated.index("chunk-0;") < accumulated.index("chunk-4;")
        finally:
            late.disconnect()

    def test_connect_claims_room(self, unique_room, unique_password):
        ws = ws_connect(unique_room, unique_password)
        try:
            # The handshake claimed the room: another password is rejected,
            # the claiming password is accepted
            assert broadcast_message(SERVER_URL, unique_room, "other-password", "x") == 401
            assert broadcast_message(SERVER_URL, unique_room, unique_password, "x") == 200
        finally:
            ws.close()

    def test_wrong_password_rejected_at_handshake(self, unique_room, unique_password):
        assert broadcast_message(SERVER_URL, unique_room, unique_password, "claimed") == 200

        with pytest.raises(websocket.WebSocketBadStatusException) as excinfo:
            ws_connect(unique_room, "wrong-password")
        assert excinfo.value.status_code == 401

    def test_size_control_frame(self, unique_room, unique_password, socket_listener):
        ws = ws_connect(unique_room, unique_password)
        try:
            ws.send(json.dumps({"size": {"cols": 123, "rows": 45}}))
            ws.send_binary(b"after resize")
            assert socket_listener.wait_for_message(timeout=5, containing="after resize")
            assert socket_listener.get_last_size() == {"cols": 123, "rows": 45}
        finally:
            ws.close()

    def test_delete_control_frame(self, unique_room, unique_password):
        ws = ws_connect(unique_room, unique_password)
        ws.send_binary(b"to be deleted")
        ws.send(json.dumps({"delete": True}))
        ws.close()

        # The room is gone: no history for a late joiner, and the name is
        # free to claim with a different password
        late = SocketListener(unique_room)
        late.connect()
        try:
            assert late.wait_for_message(timeout=2, containing="to be deleted") is None
        finally:
            late.disconnect()
        assert broadcast_message(SERVER_URL, unique_room, "another-password", "x") == 200

    def test_history_is_bounded_by_bytes_not_just_frames(
        self, unique_room, unique_password
    ):
        """A room's stored history has a memory ceiling.

        Rooms outlive their broadcaster, so the server holds every room
        broadcast within the TTL. Capping frames alone left the ceiling at
        frames x 64KB; a byte budget bounds it regardless of how big each
        frame is. The newest output must survive the trimming - that is
        what a late joiner is there to see.
        """
        newest = f"NEWEST-{random_id()}"
        keeper = f"KEEPER-{random_id()}"
        ws = ws_connect_room(SERVER_URL, unique_room, unique_password)
        try:
            # 64KB is what the CLI actually coalesces up to (MAX_BATCH), and
            # 160 of them stay under the 200-message cap, so only the byte
            # budget can be what trims this
            for _ in range(160):
                ws.send_binary(b"x" * (64 * 1024))
                ws.recv()  # ack
            # Comfortably inside the budget, so it must still be there:
            # without this a "keep only the newest chunk" implementation
            # would satisfy the size assertion below
            ws.send_binary(keeper.encode())
            ws.recv()
            ws.send_binary(b"y" * (64 * 1024))
            ws.recv()
            ws.send_binary(newest.encode())
            ws.recv()
        finally:
            ws.close()

        resp = requests.get(f"{SERVER_URL}/r/{unique_room}.bin")
        assert resp.status_code == 200
        assert newest.encode() in resp.content, "the newest output was trimmed away"
        assert keeper.encode() in resp.content, (
            "history kept only the tail; earlier in-budget output was dropped"
        )
        # The guarantee is the budget plus at most one final frame
        assert len(resp.content) <= 4 * 1024 * 1024 + 128 * 1024, (
            f"history kept {len(resp.content)} bytes of a 10MB broadcast; "
            "the byte budget is not bounding it"
        )

    def test_utf8_sequence_split_across_frames(self, unique_room, unique_password, socket_listener):
        # 'é' is two bytes; send one per frame. Viewers decode the stream,
        # not individual messages, so the character must survive.
        encoded = "café".encode()
        ws = ws_connect(unique_room, unique_password)
        try:
            ws.send_binary(encoded[:3])
            ws.send_binary(encoded[3:])
            ok = wait_for_content(socket_listener, lambda acc: "café" in acc, timeout=5)
            assert ok, "split UTF-8 sequence did not survive the pipeline"
        finally:
            ws.close()


class TestReliability:
    def test_cli_resumes_after_server_restart(self):
        """Output written while the server is down is buffered and
        delivered after the CLI reconnects."""
        port = _free_port()
        room, password = f"test-{random_id()}", f"secret-{random_id()}"
        server = _spawn_server(port)
        url = f"http://127.0.0.1:{port}"
        cli = None
        listener = None
        try:
            wait_for_server(url)

            cli = subprocess.Popen(
                CLI_COMMAND + ["-s", url, "-r", room, "-W", password],
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
            key = read_share_key(cli)
            assert key is not None, "no decryption key in CLI share link"
            cli.stdin.write(b"before restart;")
            cli.stdin.flush()

            listener = SocketListener(room, server_url=url)
            listener.set_key(key)
            listener.connect()
            assert wait_for_content(listener, lambda acc: "before restart;" in acc, timeout=10)
            listener.disconnect()
            listener = None

            # Take the server down; this write fails and is buffered
            server.terminate()
            server.wait(timeout=5)
            cli.stdin.write(b"while down;")
            cli.stdin.flush()
            time.sleep(1.0)

            # Bring the server back on the same port; the next write
            # triggers the reconnect, which replays the buffered chunk
            server = _spawn_server(port)
            wait_for_server(url)
            deadline = time.time() + 15
            listener = SocketListener(room, server_url=url)
            listener.set_key(key)
            while time.time() < deadline:
                cli.stdin.write(b"after restart;")
                cli.stdin.flush()
                time.sleep(0.5)
                try:
                    listener.connect()
                    break
                except Exception:
                    continue  # room not re-claimed yet

            ok = wait_for_content(
                listener,
                lambda acc: "while down;" in acc and "after restart;" in acc,
                timeout=15,
            )
            assert ok, "buffered output was not replayed after reconnect"
        finally:
            if cli is not None:
                cli.stdin.close()
                try:
                    cli.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    cli.kill()
                    cli.wait()
            if listener is not None:
                listener.disconnect()
            server.terminate()
            try:
                server.wait(timeout=5)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait()

    def test_browser_viewer_survives_server_restart(self):
        """The viewer reconnects its WebSocket instead of
        going silently dead."""
        port = _free_port()
        room, password = f"test-{random_id()}", f"secret-{random_id()}"
        server = _spawn_server(port)
        url = f"http://127.0.0.1:{port}"
        key = os.urandom(32).hex()
        try:
            wait_for_server(url)

            with sync_playwright() as p:
                browser = p.chromium.launch()
                page = browser.new_page()
                page.goto(f"{url}/r/{room}#{key}")
                page.wait_for_function(
                    "document.getElementById('online-counter').textContent !== '0'",
                    timeout=10000,
                )

                assert broadcast_message(
                    url, room, password, "first-message", key=key
                ) == 200
                page.wait_for_function(
                    "window.shellshareText && window.shellshareText().includes('first-message')",
                    timeout=10000,
                )

                server.terminate()
                server.wait(timeout=5)
                server = _spawn_server(port)
                wait_for_server(url)

                # Re-claim the room and broadcast; the viewer must pick it
                # up once the viewer WebSocket reconnects and resyncs
                deadline = time.time() + 20
                seen = False
                while time.time() < deadline and not seen:
                    broadcast_message(url, room, password, "second-message;", key=key)
                    seen = page.evaluate(
                        "window.shellshareText && "
                        "window.shellshareText().includes('second-message')"
                    )
                    time.sleep(0.5)
                assert seen, "viewer did not recover after the server restart"

                browser.close()
        finally:
            server.terminate()
            try:
                server.wait(timeout=5)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait()
