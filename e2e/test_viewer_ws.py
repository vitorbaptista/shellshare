"""
Viewer WebSocket tests for the Shellshare backend.

These tests verify the raw-WebSocket viewer protocol at
`GET /ws/v/r/{room}` - the replacement for the Socket.IO viewer
transport (test_socketio.py covers the transitional Socket.IO stack
and is removed together with it).

The protocol contract:
- The room is in the URL; there is no join handshake.
- On connect the server pushes the room snapshot, in order:
  `{"size": {...}}` as a JSON text frame (only if the room has a size),
  the accumulated history as ONE binary frame (only if non-empty),
  `{"broadcasting": true|false}`, and `{"usersCount": N}`.
  usersCount always arrives, so it doubles as the snapshot-delivered
  signal.
- After that: binary frames are live terminal bytes; JSON text frames
  carry control events (size, usersCount re-broadcasts, broadcasting
  transitions).
- usersCount re-broadcasts are COALESCED server-side: intermediate
  values may be skipped, but the count converges to the true value.
  Tests assert convergence, never exact event sequences.

Tests from test_socketio.py dropped without a port (pure Socket.IO
transport mechanics with no raw-WebSocket equivalent):
- test_http_polling_is_rejected: guarded against the engine.io polling
  encoder corrupting binary events; the raw-WS viewer endpoint has no
  polling fallback to reject.
- test_can_connect_to_server: tested the bare Socket.IO handshake with
  no room; a viewer connection always names a room in the URL, so every
  other test already covers "can connect".
- test_join_with_full_url: tested that the `join` event payload accepted
  the '/r/<room>' path form; the raw-WS URL *is* the path, there is no
  alternate join-payload form to normalize.
- test_join_same_room_twice: tested that re-emitting `join` was
  idempotent; there is no join event. Replaced by
  test_reconnect_delivers_history_once_per_connection, which pins the
  closest behavioral equivalent: each new connection gets the history
  snapshot exactly once.
- test_single_client_multiple_rooms: tested Socket.IO namespace
  multiplexing (one connection, many rooms); a raw-WS connection is
  bound to one room by construction. The behavioral core (one process
  watching two rooms receives each room's messages) is kept as
  test_two_connections_two_rooms.
"""

import json
import time
import urllib.parse

import websocket

from conftest import (
    SERVER_URL,
    SocketListener,
    broadcast_message as ws_broadcast,
    decode_message,
    random_id,
    wait_for_server,
)

_DEFAULT_SIZE = {"rows": 24, "cols": 80}
_UNSET = object()  # Sentinel for distinguishing None from unset


def broadcast(room_id, message, password, size=_UNSET, server_url=SERVER_URL):
    """Broadcast a message over the WebSocket ingest.

    Args:
        size: If not provided, defaults to {"rows": 24, "cols": 80}.
              Pass None explicitly to send no size at all.
    """
    if size is _UNSET:
        size = _DEFAULT_SIZE
    return ws_broadcast(server_url, room_id, password, message, size=size)


def viewer_connect(room_id, server_url=SERVER_URL, timeout=15):
    """Open a raw viewer WebSocket to /ws/v/r/{room}."""
    ws_base = server_url.replace("http://", "ws://")
    return websocket.create_connection(
        f"{ws_base}/ws/v/r/{room_id}", timeout=timeout
    )


def collect_snapshot(ws, timeout=15):
    """Read frames until the usersCount snapshot terminator.

    Returns an ordered list of (kind, payload) tuples where kind is one
    of 'size', 'binary', 'broadcasting', 'usersCount'. usersCount always
    ends the snapshot, so this returns the complete on-connect push.
    """
    events = []
    deadline = time.time() + timeout
    while time.time() < deadline:
        frame = ws.recv()
        if isinstance(frame, bytes):
            events.append(("binary", frame))
            continue
        control = json.loads(frame)
        if "size" in control:
            events.append(("size", control["size"]))
        if "broadcasting" in control:
            events.append(("broadcasting", control["broadcasting"]))
        if "usersCount" in control:
            events.append(("usersCount", control["usersCount"]))
            return events
    raise TimeoutError(f"No usersCount snapshot terminator; got: {events}")


def make_listener(room_id, server_url=SERVER_URL):
    """Create and connect a SocketListener (caller must disconnect)."""
    listener = SocketListener(room_id, server_url=server_url)
    listener.connect()
    return listener


class TestViewerConnection:
    """Tests for the basic viewer connection and snapshot delivery."""

    def test_connect_delivers_snapshot(self, unique_room):
        """Connecting to a room delivers the snapshot, ending in
        usersCount - the raw-WS equivalent of a confirmed join."""
        wait_for_server(SERVER_URL)

        listener = make_listener(unique_room)
        try:
            # connect(wait_for_join=True) already waited for usersCount
            assert listener.get_last_user_count() >= 1, \
                "Snapshot should report at least ourselves"
        finally:
            listener.disconnect()

    def test_connect_nonexistent_room(self, unique_room):
        """Connecting to a room nobody ever broadcast to succeeds: the
        snapshot has broadcasting:false, no size, no history."""
        wait_for_server(SERVER_URL)

        ws = viewer_connect(unique_room)
        try:
            events = collect_snapshot(ws)
        finally:
            ws.close()

        kinds = [k for k, _ in events]
        assert "usersCount" in kinds, "usersCount must always arrive"
        assert "size" not in kinds, f"No size for an empty room, got {events}"
        assert "binary" not in kinds, f"No history for an empty room, got {events}"
        broadcasting = [p for k, p in events if k == "broadcasting"]
        assert broadcasting == [False], \
            f"Nonexistent room must report broadcasting:false, got {events}"


class TestViewerMessages:
    """Tests for receiving messages over the viewer WebSocket."""

    def test_receive_message_after_connect(self, unique_room, unique_password):
        """Viewers receive messages broadcast after they connect."""
        wait_for_server(SERVER_URL)

        test_message = f"Hello-{random_id()}"
        listener = make_listener(unique_room)
        try:
            status = broadcast(unique_room, test_message, unique_password)
            assert status == 200, f"Broadcast failed: {status}"

            received = listener.wait_for_message(timeout=15, containing=test_message)
            assert received is not None, "Message not received"
            assert test_message in received
        finally:
            listener.disconnect()

    def test_receive_size_after_connect(self, unique_room, unique_password):
        """Viewers receive terminal size sent after they connect."""
        wait_for_server(SERVER_URL)

        expected_size = {"rows": 30, "cols": 120}
        listener = make_listener(unique_room)
        try:
            status = broadcast(unique_room, "Test", unique_password, size=expected_size)
            assert status == 200, f"Broadcast failed: {status}"

            assert listener.wait_for_size(timeout=15), "Size not received"
            assert listener.get_last_size() == expected_size
        finally:
            listener.disconnect()

    def test_receive_existing_data_on_connect(self, unique_room, unique_password):
        """Connecting to a room with history delivers that history."""
        wait_for_server(SERVER_URL)

        test_message = f"Existing-{random_id()}"
        status = broadcast(unique_room, test_message, unique_password)
        assert status == 200, f"Broadcast failed: {status}"

        listener = make_listener(unique_room)
        try:
            received = listener.wait_for_message(timeout=15, containing=test_message)
            assert received is not None, "Existing message not received"
        finally:
            listener.disconnect()


class TestViewerUserCount:
    """Tests for user count updates.

    usersCount re-broadcasts are coalesced server-side, so these tests
    assert convergence to the expected final value, never sequences.
    """

    def test_user_count_on_connect(self, unique_room):
        """A lone viewer's snapshot reports usersCount 1 (ourselves)."""
        wait_for_server(SERVER_URL)

        listener = make_listener(unique_room)
        try:
            assert listener.wait_for_user_count(1, timeout=15), \
                f"Expected count to converge to 1, got {listener.get_last_user_count()}"
        finally:
            listener.disconnect()

    def test_user_count_increases_with_multiple_clients(self, unique_room):
        """User count converges upward as more viewers connect."""
        wait_for_server(SERVER_URL)

        listener1 = make_listener(unique_room)
        listener2 = None
        try:
            assert listener1.wait_for_user_count(1, timeout=15), \
                "First viewer didn't converge to count=1"

            listener2 = make_listener(unique_room)
            assert listener1.wait_for_user_count(2, timeout=15), \
                f"First viewer didn't converge to count=2, got {listener1.get_last_user_count()}"
            assert listener2.wait_for_user_count(2, timeout=15), \
                f"Second viewer didn't converge to count=2, got {listener2.get_last_user_count()}"
        finally:
            listener1.disconnect()
            if listener2 is not None:
                listener2.disconnect()

    def test_user_count_decreases_on_disconnect(self, unique_room):
        """User count converges back down when a viewer disconnects."""
        wait_for_server(SERVER_URL)

        listener1 = make_listener(unique_room)
        try:
            listener2 = make_listener(unique_room)
            assert listener1.wait_for_user_count(2, timeout=15), \
                "First viewer didn't converge to count=2"

            listener2.disconnect()
            assert listener1.wait_for_user_count(1, timeout=15), \
                f"Expected count back to 1, got {listener1.get_last_user_count()}"
        finally:
            listener1.disconnect()

    def test_user_count_per_room(self, unique_room):
        """User count is per-room, not global."""
        wait_for_server(SERVER_URL)

        other_room = f"test-{random_id()}"
        listener1 = make_listener(unique_room)
        listener2 = make_listener(other_room)
        try:
            assert listener1.wait_for_user_count(1, timeout=15), \
                f"Room 1 should have 1 user, got {listener1.get_last_user_count()}"
            assert listener2.wait_for_user_count(1, timeout=15), \
                f"Room 2 should have 1 user, got {listener2.get_last_user_count()}"
        finally:
            listener1.disconnect()
            listener2.disconnect()


class TestViewerBroadcasting:
    """Tests for the broadcasting on/offline indicator."""

    def test_broadcasting_false_without_broadcaster(self, unique_room):
        """A room with no attached broadcaster reports broadcasting:false
        in the snapshot."""
        wait_for_server(SERVER_URL)

        listener = make_listener(unique_room)
        try:
            assert listener.wait_for_broadcasting(False, timeout=15), \
                f"Expected broadcasting:false, got {listener.get_last_broadcasting()}"
        finally:
            listener.disconnect()

    def test_broadcasting_current_state_on_connect(self, unique_room, unique_password):
        """A viewer connecting while a broadcaster is attached gets
        broadcasting:true as the current state in the snapshot."""
        from conftest import ws_connect_room

        wait_for_server(SERVER_URL)

        broadcaster = ws_connect_room(SERVER_URL, unique_room, unique_password)
        try:
            listener = make_listener(unique_room)
            try:
                assert listener.wait_for_broadcasting(True, timeout=15), \
                    f"Expected broadcasting:true, got {listener.get_last_broadcasting()}"
            finally:
                listener.disconnect()
        finally:
            broadcaster.close()

    def test_broadcasting_transitions(self, unique_room, unique_password):
        """Viewers see broadcasting flip true when a broadcaster attaches
        and back to false when it detaches."""
        from conftest import ws_connect_room

        wait_for_server(SERVER_URL)

        listener = make_listener(unique_room)
        try:
            assert listener.wait_for_broadcasting(False, timeout=15), \
                "Initial state should be broadcasting:false"

            broadcaster = ws_connect_room(SERVER_URL, unique_room, unique_password)
            try:
                assert listener.wait_for_broadcasting(True, timeout=15), \
                    "Broadcaster attach should flip broadcasting:true"
            finally:
                broadcaster.close()

            assert listener.wait_for_broadcasting(False, timeout=15), \
                "Broadcaster detach should flip broadcasting:false"
        finally:
            listener.disconnect()


class TestViewerMultipleRooms:
    """Tests for room isolation."""

    def test_messages_isolated_to_room(self, unique_room, unique_password):
        """Messages are only delivered to viewers of the same room."""
        wait_for_server(SERVER_URL)

        other_room = f"test-{random_id()}"
        listener1 = make_listener(unique_room)
        listener2 = make_listener(other_room)
        try:
            test_message = f"Room1Only-{random_id()}"
            status = broadcast(unique_room, test_message, unique_password)
            assert status == 200, f"Broadcast failed: {status}"

            received = listener1.wait_for_message(timeout=15, containing=test_message)
            assert received is not None, "Viewer 1 should receive the message"

            assert test_message not in listener2.get_accumulated_messages(), \
                "Viewer 2 should NOT receive a message from room 1"
        finally:
            listener1.disconnect()
            listener2.disconnect()

    def test_two_connections_two_rooms(self, unique_room, unique_password):
        """One process watching two rooms (one connection each) receives
        each room's messages. A raw-WS connection is bound to one room,
        so 'multiple rooms' means multiple connections."""
        wait_for_server(SERVER_URL)

        other_room = f"test-{random_id()}"
        msg1 = f"Room1-{random_id()}"
        msg2 = f"Room2-{random_id()}"

        listener1 = make_listener(unique_room)
        listener2 = make_listener(other_room)
        try:
            broadcast(unique_room, msg1, unique_password)
            broadcast(other_room, msg2, unique_password)

            assert listener1.wait_for_message(timeout=15, containing=msg1), \
                "Should receive message from room 1"
            assert listener2.wait_for_message(timeout=15, containing=msg2), \
                "Should receive message from room 2"
        finally:
            listener1.disconnect()
            listener2.disconnect()


class TestViewerMessageAccumulation:
    """Tests for message accumulation and retrieval."""

    def test_messages_accumulate_in_order(self, unique_room, unique_password):
        """Multiple messages accumulate and are delivered concatenated in
        broadcast order."""
        wait_for_server(SERVER_URL)

        messages_sent = ["Line1\n", "Line2\n", "Line3\n"]
        for msg in messages_sent:
            status = broadcast(unique_room, msg, unique_password)
            assert status == 200, f"Broadcast failed: {status}"

        listener = make_listener(unique_room)
        try:
            full_content = listener.wait_for_message(timeout=15)
            assert full_content is not None, "No messages received"
            indices = [full_content.find(msg.strip()) for msg in messages_sent]
            for msg, idx in zip(messages_sent, indices):
                assert idx != -1, \
                    f"Expected '{msg.strip()}' in accumulated content"
            assert indices == sorted(indices), \
                f"Messages not in broadcast order: {indices}"
        finally:
            listener.disconnect()


class TestViewerEdgeCases:
    """Edge cases for the viewer endpoint."""

    def test_empty_room_name(self, unique_room):
        """Connecting with an empty room name is rejected cleanly at the
        handshake (404 - there is no route for an empty room segment)
        and the server keeps serving."""
        wait_for_server(SERVER_URL)

        try:
            ws = viewer_connect("")
        except websocket.WebSocketBadStatusException as exc:
            assert exc.status_code == 404, \
                f"Expected 404 for empty room name, got {exc.status_code}"
        else:
            ws.close()
            raise AssertionError("Empty room name should be rejected with 404")

        # The server is still healthy: a normal room works
        listener = make_listener(unique_room)
        try:
            assert listener.get_last_user_count() >= 1
        finally:
            listener.disconnect()

    def test_unicode_room_name(self, unique_password):
        """Room names with unicode characters (percent-encoded in both
        the viewer URL and the ingest URL) reach the same room."""
        wait_for_server(SERVER_URL)

        room_id = f"テスト-{random_id()}"
        encoded_room_id = urllib.parse.quote(room_id, safe='')
        test_message = f"Unicode-{random_id()}"

        listener = make_listener(encoded_room_id)
        try:
            status = broadcast(encoded_room_id, test_message, unique_password)
            assert status == 200, f"Broadcast to unicode room failed: {status}"

            received = listener.wait_for_message(timeout=15, containing=test_message)
            assert received is not None, "Should receive message in unicode room"
        finally:
            listener.disconnect()

    def test_very_long_room_name(self, unique_room):
        """A 500-char room name is accepted and delivers a normal
        snapshot; the server keeps serving afterwards."""
        wait_for_server(SERVER_URL)

        long_room = "x" * 500
        ws = viewer_connect(long_room)
        try:
            events = collect_snapshot(ws)
            assert any(k == "usersCount" for k, _ in events), \
                f"Long room name should deliver a normal snapshot, got {events}"
        finally:
            ws.close()

        # The server survived
        listener = make_listener(unique_room)
        try:
            assert listener.get_last_user_count() >= 1
        finally:
            listener.disconnect()

    def test_special_chars_in_room_name(self, unique_password):
        """Room names with dots, dashes, and underscores work."""
        wait_for_server(SERVER_URL)

        room_id = f"test-room_name.with-special.chars-{random_id()}"
        test_message = f"Special-{random_id()}"

        listener = make_listener(room_id)
        try:
            status = broadcast(room_id, test_message, unique_password)
            assert status == 200, f"Broadcast failed: {status}"

            received = listener.wait_for_message(timeout=15, containing=test_message)
            assert received is not None, "Should receive message"
        finally:
            listener.disconnect()

    def test_rapid_connect_disconnect(self, unique_room):
        """Rapidly opening and closing viewer connections to different
        rooms doesn't harm the server."""
        wait_for_server(SERVER_URL)

        for i in range(10):
            ws = viewer_connect(f"test-rapid-{i}-{random_id()}")
            ws.close()

        # The server is still healthy: a full snapshot still works
        listener = make_listener(unique_room)
        try:
            assert listener.get_last_user_count() >= 1
        finally:
            listener.disconnect()

    def test_message_with_special_characters(self, unique_room, unique_password):
        """Messages with special characters are preserved byte-for-byte."""
        wait_for_server(SERVER_URL)

        test_message = "Hello! @#$%^&*() 日本語 émoji 🎉 <script>alert('xss')</script>"

        listener = make_listener(unique_room)
        try:
            status = broadcast(unique_room, test_message, unique_password)
            assert status == 200, f"Broadcast failed: {status}"

            received = listener.wait_for_message(timeout=15, containing="Hello!")
            assert received is not None, "Should receive message"
            assert "日本語" in received, "Message should contain Japanese characters"
            assert "🎉" in received, "Message should contain the emoji"
        finally:
            listener.disconnect()


class TestViewerReconnection:
    """Tests for reconnection behavior."""

    def test_reconnect_after_disconnect(self, unique_room):
        """A viewer can reconnect after disconnecting; the count
        converges back to 1 (the previous connection is gone)."""
        wait_for_server(SERVER_URL)

        listener1 = make_listener(unique_room)
        assert listener1.wait_for_user_count(1, timeout=15), \
            "First connection didn't converge to 1 user"
        listener1.disconnect()

        listener2 = make_listener(unique_room)
        try:
            assert listener2.wait_for_user_count(1, timeout=15), \
                f"Reconnect should converge to 1 user, got {listener2.get_last_user_count()}"
        finally:
            listener2.disconnect()

    def test_existing_data_available_after_reconnect(self, unique_room, unique_password):
        """History persists and is delivered to a fresh connection."""
        wait_for_server(SERVER_URL)

        test_message = f"Persistent-{random_id()}"
        status = broadcast(unique_room, test_message, unique_password)
        assert status == 200, f"Broadcast failed: {status}"

        listener1 = make_listener(unique_room)
        assert listener1.wait_for_message(timeout=15, containing=test_message), \
            "First viewer should receive the message"
        listener1.disconnect()

        listener2 = make_listener(unique_room)
        try:
            received = listener2.wait_for_message(timeout=15, containing=test_message)
            assert received is not None, "Message should persist across reconnects"
        finally:
            listener2.disconnect()

    def test_reconnect_delivers_history_once_per_connection(self, unique_room, unique_password):
        """Each new connection gets the history snapshot exactly once
        (the raw-WS replacement for 're-join is idempotent': there is no
        join to repeat, but reconnecting must not duplicate or drop
        history)."""
        wait_for_server(SERVER_URL)

        marker = f"Once-{random_id()}"
        status = broadcast(unique_room, marker, unique_password)
        assert status == 200, f"Broadcast failed: {status}"

        for attempt in range(2):
            ws = viewer_connect(unique_room)
            try:
                events = collect_snapshot(ws)
            finally:
                ws.close()
            binaries = [p for k, p in events if k == "binary"]
            assert len(binaries) == 1, \
                f"Connection {attempt}: history must be ONE binary frame, got {len(binaries)}"
            decoded = decode_message(binaries[0])
            assert decoded.count(marker) == 1, \
                f"Connection {attempt}: history delivered {decoded.count(marker)} " \
                f"times, expected exactly once"


class TestViewerEventOrder:
    """Tests for snapshot composition and ordering."""

    def test_snapshot_events_order(self, unique_room, unique_password):
        """Connecting to a populated room delivers size, history, and
        usersCount - with size first and usersCount last."""
        wait_for_server(SERVER_URL)

        status = broadcast(unique_room, "Initial message", unique_password,
                           size={"rows": 30, "cols": 100})
        assert status == 200, f"Broadcast failed: {status}"

        ws = viewer_connect(unique_room)
        try:
            events = collect_snapshot(ws)
        finally:
            ws.close()

        kinds = [k for k, _ in events]
        assert "size" in kinds, f"Should receive size, got {kinds}"
        assert "binary" in kinds, f"Should receive history, got {kinds}"
        assert "usersCount" in kinds, f"Should receive usersCount, got {kinds}"
        # size must precede history so the viewer can size the terminal
        # before replaying content; usersCount always terminates.
        assert kinds.index("size") < kinds.index("binary"), \
            f"size should come before history, got order: {kinds}"
        assert kinds[-1] == "usersCount", \
            f"usersCount terminates the snapshot, got order {kinds}"

    def test_size_matches_last_broadcast(self, unique_room, unique_password):
        """The snapshot size is the LAST broadcast size."""
        wait_for_server(SERVER_URL)

        broadcast(unique_room, "Msg1", unique_password, size={"rows": 20, "cols": 60})
        broadcast(unique_room, "Msg2", unique_password, size={"rows": 30, "cols": 100})
        final_size = {"rows": 40, "cols": 120}
        broadcast(unique_room, "Msg3", unique_password, size=final_size)

        listener = make_listener(unique_room)
        try:
            assert listener.wait_for_size(timeout=15), "Size not received"
            assert listener.get_last_size() == final_size, \
                f"Expected {final_size}, got {listener.get_last_size()}"
        finally:
            listener.disconnect()


class TestViewerEmptyRoom:
    """Tests for connecting to empty rooms."""

    def test_connect_after_delete_no_old_messages(self, unique_room, unique_password):
        """After DELETE, a fresh connection gets no old history."""
        import http.client

        wait_for_server(SERVER_URL)

        test_message = "This will be deleted"
        status = broadcast(unique_room, test_message, unique_password)
        assert status == 200, f"Broadcast failed: {status}"

        parsed = urllib.parse.urlparse(SERVER_URL)
        conn = http.client.HTTPConnection(parsed.netloc)
        conn.request('DELETE', f'/r/{unique_room}',
                     headers={"Authorization": unique_password})
        response = conn.getresponse()
        assert response.status == 202, f"DELETE failed: {response.status}"
        conn.close()

        ws = viewer_connect(unique_room)
        try:
            events = collect_snapshot(ws)
        finally:
            ws.close()

        for kind, payload in events:
            if kind == "binary":
                assert test_message not in decode_message(payload), \
                    "Should NOT receive deleted history"


class TestViewerSizePassthrough:
    """Tests for size object passthrough behavior."""

    def test_size_with_extra_fields_delivers_cols_and_rows(self, unique_room, unique_password):
        """Viewers get the broadcast cols/rows even when the size object
        carries fields they don't consume."""
        wait_for_server(SERVER_URL)

        custom_size = {
            "rows": 42,
            "cols": 132,
            "extra": "data",
            "nested": {"foo": "bar"}
        }

        listener = make_listener(unique_room)
        try:
            status = broadcast(unique_room, "test", unique_password, size=custom_size)
            assert status == 200, f"Broadcast failed: {status}"

            assert listener.wait_for_size(timeout=15), "Size not received"
            size = listener.get_last_size()
            # The dimensions the viewer renders with must survive; whether
            # the server forwards or drops unknown fields is its own business
            assert size["cols"] == 132, f"Expected cols=132, got {size}"
            assert size["rows"] == 42, f"Expected rows=42, got {size}"
        finally:
            listener.disconnect()

    def test_size_null_value_not_emitted(self, unique_room, unique_password):
        """A broadcast without a size produces no size event (only valid
        sizes with cols/rows are stored and emitted)."""
        wait_for_server(SERVER_URL)

        listener = make_listener(unique_room)
        try:
            status = broadcast(unique_room, "test", unique_password, size=None)
            assert status == 200, f"Broadcast failed: {status}"

            # The message arriving proves the broadcast was processed;
            # any size would have been sent before it
            assert listener.wait_for_message(timeout=15, containing="test"), \
                "Message not received"
            assert listener.get_last_size() is None, \
                f"Expected no size, got {listener.get_last_size()}"
        finally:
            listener.disconnect()


class TestViewerMessageFormat:
    """Tests for message format and payload integrity."""

    def test_message_binary_format(self, unique_room, unique_password):
        """Live messages reach viewers as binary frames carrying exactly
        the broadcast bytes."""
        wait_for_server(SERVER_URL)

        test_message = "Hello World!"

        ws = viewer_connect(unique_room)
        try:
            collect_snapshot(ws)  # drain the (empty) snapshot

            status = broadcast(unique_room, test_message, unique_password)
            assert status == 200, f"Broadcast failed: {status}"

            # Control frames (size, broadcasting transitions from the
            # short-lived broadcaster) may interleave; the first binary
            # frame is the live message
            deadline = time.time() + 15
            raw = None
            while time.time() < deadline:
                frame = ws.recv()
                if isinstance(frame, bytes):
                    raw = frame
                    break
            assert raw is not None, "No binary frame received"
            assert raw == test_message.encode(), \
                f"Expected {test_message.encode()!r}, got {raw!r}"
        finally:
            ws.close()


class TestViewerLargeMessage:
    """Tests for large message handling."""

    def test_large_message_delivery(self, unique_room, unique_password):
        """Large messages are delivered intact."""
        wait_for_server(SERVER_URL)

        large_message = "X" * 50000  # 50KB

        listener = make_listener(unique_room)
        try:
            status = broadcast(unique_room, large_message, unique_password)
            assert status == 200, f"Broadcast failed: {status}"

            received = listener.wait_for_message(timeout=15, containing=large_message)
            assert received is not None, "Large message not received intact"
        finally:
            listener.disconnect()


class TestViewerMultipleClients:
    """Tests for multiple viewer scenarios."""

    def test_three_clients_same_room(self, unique_room, unique_password):
        """Three viewers in the same room all receive a broadcast."""
        wait_for_server(SERVER_URL)

        test_message = f"Broadcast-{random_id()}"
        listeners = [make_listener(unique_room) for _ in range(3)]
        try:
            status = broadcast(unique_room, test_message, unique_password)
            assert status == 200, f"Broadcast failed: {status}"

            for i, listener in enumerate(listeners):
                received = listener.wait_for_message(timeout=15, containing=test_message)
                assert received is not None, f"Viewer {i} did not receive the message"
        finally:
            for listener in listeners:
                listener.disconnect()


class TestViewerLateJoinerHistory:
    """Tests for late joiner history.

    Viewers connecting to an existing room receive the message history
    so they don't see a blank screen.
    """

    def test_late_joiner_receives_multiple_messages_in_order(self, unique_room, unique_password):
        """A late joiner receives accumulated messages in broadcast order."""
        wait_for_server(SERVER_URL)

        messages_sent = [f"MSG1-{random_id()}", f"MSG2-{random_id()}", f"MSG3-{random_id()}"]
        for msg in messages_sent:
            status = broadcast(unique_room, msg, unique_password)
            assert status == 200, f"Broadcast failed: {status}"

        listener = make_listener(unique_room)
        try:
            decoded = listener.wait_for_message(timeout=15)
            assert decoded is not None, "Late joiner did not receive history"

            for msg in messages_sent:
                assert msg in decoded, f"Expected '{msg}' in accumulated history"

            idx1 = decoded.find(messages_sent[0])
            idx2 = decoded.find(messages_sent[1])
            idx3 = decoded.find(messages_sent[2])
            assert idx1 < idx2 < idx3, \
                f"Messages not in order: indices {idx1}, {idx2}, {idx3}"
        finally:
            listener.disconnect()

    def test_late_joiner_and_live_viewer_same_content(self, unique_room, unique_password):
        """A late joiner and a live viewer ultimately see the same content."""
        wait_for_server(SERVER_URL)

        messages_sent = [f"LIVE1-{random_id()}", f"LIVE2-{random_id()}"]

        live_viewer = make_listener(unique_room)
        late_joiner = None
        try:
            for msg in messages_sent:
                status = broadcast(unique_room, msg, unique_password)
                assert status == 200, f"Broadcast failed: {status}"

            for msg in messages_sent:
                assert live_viewer.wait_for_message(timeout=15, containing=msg), \
                    f"Live viewer missing '{msg}'"

            late_joiner = make_listener(unique_room)
            late_content = late_joiner.wait_for_message(timeout=15)
            assert late_content is not None, "Late joiner did not receive history"

            live_content = live_viewer.get_accumulated_messages()
            for msg in messages_sent:
                assert msg in live_content, f"Live viewer missing '{msg}'"
                assert msg in late_content, f"Late joiner missing '{msg}'"
        finally:
            live_viewer.disconnect()
            if late_joiner is not None:
                late_joiner.disconnect()


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
