"""
E2E tests for the optional PostHog analytics.

These spawn dedicated servers pointing --posthog-host at a local mock
that records every capture request. Delivery is fire-and-forget on the
server side, so assertions poll the mock instead of expecting events
immediately after the triggering action.
"""

import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from conftest import (
    SocketListener,
    broadcast_message,
    poll_until,
    random_id,
    ws_connect_room,
)

POSTHOG_KEY = "phc_test_key"
POSTHOG_SALT = "e2e-test-salt"


class MockPostHog:
    """A local stand-in for PostHog's capture endpoint."""

    def __init__(self):
        self.events = []
        self._lock = threading.Lock()

        mock = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length))
                with mock._lock:
                    mock.events.append(body)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"status": 1}')

            def log_message(self, *args):
                pass  # keep pytest output clean

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(
            target=self._server.serve_forever, daemon=True
        )
        self._thread.start()

    @property
    def url(self):
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def events_named(self, name):
        with self._lock:
            return [e for e in self.events if e.get("event") == name]

    def shutdown(self):
        self._server.shutdown()
        self._server.server_close()


@pytest.fixture
def mock_posthog():
    mock = MockPostHog()
    yield mock
    mock.shutdown()


def analytics_server(dedicated_server, mock_posthog, *extra_args, environment=None):
    # ENV is scrubbed unless the test sets one, so a label in the
    # developer's shell can't leak into the no-environment assertions
    env = {k: v for k, v in os.environ.items() if k != "ENV"}
    if environment is not None:
        env["ENV"] = environment
    return dedicated_server(
        "--posthog-key", POSTHOG_KEY,
        "--posthog-host", mock_posthog.url,
        "--posthog-salt", POSTHOG_SALT,
        *extra_args,
        env=env,
    )


def test_full_lifecycle_emits_the_three_events(dedicated_server, mock_posthog):
    server = analytics_server(dedicated_server, mock_posthog)
    room = random_id()
    password = "secret-password"

    # Broadcast: claim the room, send output, have a viewer join, exit
    # cleanly (the delete control message, as the CLI sends on exit)
    ws = ws_connect_room(server.url, room, password)
    try:
        ws.send_binary(b"hello viewers")
        ws.recv()  # ack

        listener = SocketListener(room, server_url=server.url)
        listener.connect()
        try:
            assert poll_until(
                lambda: mock_posthog.events_named("viewer_joined"), timeout=10
            ), "viewer_joined never reached the mock"
        finally:
            listener.disconnect()

        ws.send(json.dumps({"delete": True}))
    finally:
        ws.close()

    assert poll_until(
        lambda: mock_posthog.events_named("broadcast_started")
        and mock_posthog.events_named("broadcast_ended"),
        timeout=10,
    ), "broadcast_started/broadcast_ended never reached the mock"

    started = mock_posthog.events_named("broadcast_started")[0]
    ended = mock_posthog.events_named("broadcast_ended")[0]
    viewed = mock_posthog.events_named("viewer_joined")[0]

    # Broadcast events share the stable bc: identity (a machine running
    # the CLI); the viewer join is an anonymous per-join id, never the
    # broadcaster. Every event carries the plaintext room name, which is
    # what ties the viewer join back to the broadcast events.
    assert started["distinct_id"].startswith("bc:")
    assert ended["distinct_id"] == started["distinct_id"]
    assert viewed["distinct_id"].startswith("viewer:")
    assert viewed["distinct_id"] != started["distinct_id"]
    assert started["properties"]["room"] == room
    assert ended["properties"]["room"] == room
    assert viewed["properties"]["room"] == room
    # This segment claimed the room - a fresh share, not a reconnect
    assert started["properties"]["new_room"] is True

    assert started["api_key"] == POSTHOG_KEY
    assert isinstance(ended["properties"]["duration_seconds"], (int, float))
    assert viewed["properties"]["user_count"] >= 1
    assert viewed["properties"]["broadcasting"] is True

    for event in (started, ended, viewed):
        assert event["properties"]["$process_person_profile"] is False
        assert event["properties"]["$geoip_disable"] is True
        # No ENV set: no environment label on any event
        assert "environment" not in event["properties"]
        # The room name is sent openly, but the password is a secret
        # and may never appear anywhere in any payload
        assert password not in json.dumps(event)


def test_viewer_rejoin_and_dead_rooms_are_not_counted(dedicated_server, mock_posthog):
    server = analytics_server(dedicated_server, mock_posthog)
    room = random_id()

    # A join to a room that doesn't exist (dead link) is not an audience
    listener = SocketListener(room, server_url=server.url)
    listener.connect()
    listener.disconnect()

    ws = ws_connect_room(server.url, room, "pw")
    try:
        listener = SocketListener(room, server_url=server.url)
        listener.connect()
        try:
            assert poll_until(
                lambda: mock_posthog.events_named("viewer_joined"), timeout=10
            )
            # The dead-link join may not have produced an event: exactly
            # one for the single live join. (The old Socket.IO re-emitted
            # join needed a dedupe to test; a raw-WS viewer's room rides
            # the URL, so a duplicate join cannot exist.)
            time.sleep(1)
            assert len(mock_posthog.events_named("viewer_joined")) == 1
        finally:
            listener.disconnect()
    finally:
        ws.close()


def test_abrupt_disconnect_emits_one_broadcast_ended(dedicated_server, mock_posthog):
    server = analytics_server(dedicated_server, mock_posthog)
    room = random_id()

    ws = ws_connect_room(server.url, room, "pw")
    ws.send_binary(b"output")
    ws.recv()  # ack
    # Drop the connection without the delete control message, like a
    # crashed client or a dead network
    ws.close()

    assert poll_until(
        lambda: mock_posthog.events_named("broadcast_ended"), timeout=10
    ), "broadcast_ended never reached the mock"
    time.sleep(1)
    assert len(mock_posthog.events_named("broadcast_ended")) == 1

    # The room outlives its broadcaster until TTL eviction; a viewer of
    # that replay still counts, flagged as not broadcasting
    listener = SocketListener(room, server_url=server.url)
    listener.connect()
    try:
        assert poll_until(
            lambda: mock_posthog.events_named("viewer_joined"), timeout=10
        ), "viewer_joined never reached the mock"
    finally:
        listener.disconnect()
    assert mock_posthog.events_named("viewer_joined")[0]["properties"][
        "broadcasting"
    ] is False

    # Reconnecting to the surviving room starts a second segment in the
    # same room: another broadcast_started, but not a new room
    ws = ws_connect_room(server.url, room, "pw")
    try:
        ws.send_binary(b"back online")
        ws.recv()  # ack
        assert poll_until(
            lambda: len(mock_posthog.events_named("broadcast_started")) == 2,
            timeout=10,
        ), "reconnect never emitted a second broadcast_started"
    finally:
        ws.close()

    first, second = mock_posthog.events_named("broadcast_started")
    assert first["properties"]["new_room"] is True
    assert second["properties"]["new_room"] is False
    assert first["properties"]["room"] == room
    assert second["properties"]["room"] == first["properties"]["room"]
    assert second["distinct_id"] == first["distinct_id"]


def test_clean_exit_emits_exactly_one_broadcast_ended(dedicated_server, mock_posthog):
    server = analytics_server(dedicated_server, mock_posthog)
    room = random_id()

    # Delete (the CLI's exit path) followed by the close must not report
    # the broadcast twice
    ws = ws_connect_room(server.url, room, "pw")
    try:
        ws.send(json.dumps({"delete": True}))
    finally:
        ws.close()

    assert poll_until(
        lambda: mock_posthog.events_named("broadcast_ended"), timeout=10
    )
    time.sleep(1)
    assert len(mock_posthog.events_named("broadcast_ended")) == 1


def test_environment_label_is_attached_to_every_event(dedicated_server, mock_posthog):
    server = analytics_server(dedicated_server, mock_posthog, environment="staging")
    room = random_id()

    ws = ws_connect_room(server.url, room, "pw")
    try:
        listener = SocketListener(room, server_url=server.url)
        listener.connect()
        try:
            assert poll_until(
                lambda: mock_posthog.events_named("viewer_joined"), timeout=10
            ), "viewer_joined never reached the mock"
        finally:
            listener.disconnect()
        ws.send(json.dumps({"delete": True}))
    finally:
        ws.close()

    assert poll_until(
        lambda: mock_posthog.events_named("broadcast_started")
        and mock_posthog.events_named("broadcast_ended"),
        timeout=10,
    ), "broadcast_started/broadcast_ended never reached the mock"

    with mock_posthog._lock:
        events = list(mock_posthog.events)
    assert events
    for event in events:
        assert event["properties"]["environment"] == "staging"


def test_no_salt_disables_analytics(dedicated_server, mock_posthog):
    server = dedicated_server(
        "--posthog-key", POSTHOG_KEY,
        "--posthog-host", mock_posthog.url,
    )
    room = random_id()

    assert broadcast_message(server.url, room, "pw", text="hi") == 200

    # Fire-and-forget delivery means absence can only be asserted after
    # a settle window
    time.sleep(1.5)
    assert mock_posthog.events == []


def test_serve_mode_sends_no_analytics(mock_posthog):
    # `shellshare serve` embeds the server with analytics hardcoded off;
    # the env vars that configure `shellshare server` must not leak in
    import subprocess

    from conftest import CLI_COMMAND, _free_port, wait_for_server

    port = _free_port()
    env = dict(
        os.environ,
        SHELLSHARE_POSTHOG_KEY=POSTHOG_KEY,
        SHELLSHARE_POSTHOG_HOST=mock_posthog.url,
        SHELLSHARE_POSTHOG_SALT=POSTHOG_SALT,
    )
    proc = subprocess.Popen(
        CLI_COMMAND + ["--stdin", "serve", "--host", "127.0.0.1", "--port", str(port)],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=env,
    )
    try:
        wait_for_server(f"http://127.0.0.1:{port}")
        proc.stdin.write(b"hello\n")
        proc.stdin.flush()
        time.sleep(1.5)
        assert mock_posthog.events == []
    finally:
        proc.stdin.close()
        proc.terminate()
        proc.wait(timeout=10)
