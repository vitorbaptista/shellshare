"""
Socket.IO Tests for Shellshare Backend

These tests verify the real-time WebSocket functionality:
- Joining rooms
- Receiving messages in real-time
- Getting existing room data on join
- User count updates

Edge cases tested:
- Join with/without room prefix
- Empty room names
- Multiple room joins
- Reconnection behavior
- Invalid room names

These tests are critical for ensuring the Go rewrite maintains
identical real-time behavior.
"""

import base64
import json
import threading
import time
import urllib.parse
import urllib.request

import socketio

SERVER_URL = "http://localhost:3000"


def random_id(length=12):
    """Generate a random ID for room names."""
    import random
    import string
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


def encode_message(text):
    """Encode a message the same way the CLI does."""
    quoted = urllib.parse.quote(text)
    return base64.b64encode(quoted.encode()).decode()


def decode_message(encoded):
    """Decode a base64 + URL-encoded message."""
    decoded_b64 = base64.b64decode(encoded).decode('ascii')
    return urllib.parse.unquote(decoded_b64)


_DEFAULT_SIZE = {"rows": 24, "cols": 80}
_UNSET = object()  # Sentinel for distinguishing None from unset


def broadcast_message(room_id, message, password, size=_UNSET):
    """Broadcast a message via HTTP POST.
    
    Args:
        size: If not provided, defaults to {"rows": 24, "cols": 80}.
              Pass None explicitly to send null in JSON.
    """
    import http.client
    
    if size is _UNSET:
        size = _DEFAULT_SIZE
    
    parsed = urllib.parse.urlparse(SERVER_URL)
    conn = http.client.HTTPConnection(parsed.netloc)
    
    body = json.dumps({
        "message": encode_message(message),
        "size": size
    }).encode()
    
    headers = {
        "Content-Type": "application/json",
        "Authorization": password
    }
    
    conn.request('POST', f'/r/{room_id}', body=body, headers=headers)
    response = conn.getresponse()
    status = response.status
    conn.close()
    return status


class TestSocketIOConnection:
    """Tests for basic Socket.IO connection."""
    
    def test_can_connect_to_server(self):
        """Should be able to establish Socket.IO connection."""
        wait_for_server(SERVER_URL)
        
        sio = socketio.Client()
        connected = threading.Event()
        
        @sio.event
        def connect():
            connected.set()
        
        sio.connect(SERVER_URL)
        
        assert connected.wait(timeout=5), "Failed to connect"
        
        sio.disconnect()
    
    def test_can_join_room(self):
        """Should be able to join a room."""
        wait_for_server(SERVER_URL)
        
        room_id = f"test-{random_id()}"
        sio = socketio.Client()
        
        sio.connect(SERVER_URL)
        
        # Join room (no error means success)
        sio.emit('join', f'/r/{room_id}')
        
        # Give server time to process
        time.sleep(0.5)
        
        sio.disconnect()


class TestSocketIOMessages:
    """Tests for receiving messages via Socket.IO."""
    
    def test_receive_message_after_join(self):
        """Should receive messages broadcast after joining."""
        wait_for_server(SERVER_URL)
        
        room_id = f"test-{random_id()}"
        password = f"secret-{random_id()}"
        test_message = f"Hello-{random_id()}"
        
        sio = socketio.Client()
        received_messages = []
        message_received = threading.Event()
        
        @sio.on('message')
        def on_message(data):
            received_messages.append(data)
            message_received.set()
        
        sio.connect(SERVER_URL)
        sio.emit('join', f'/r/{room_id}')
        
        # Wait for join to complete
        time.sleep(0.5)
        
        # Broadcast a message via HTTP
        status = broadcast_message(room_id, test_message, password)
        assert status == 200, f"Broadcast failed: {status}"
        
        # Wait for message
        assert message_received.wait(timeout=5), "Message not received"
        
        # Verify message content
        assert len(received_messages) > 0, "No messages received"
        decoded = decode_message(received_messages[-1])
        assert test_message in decoded, f"Expected '{test_message}' in '{decoded}'"
        
        sio.disconnect()
    
    def test_receive_size_after_join(self):
        """Should receive terminal size after joining."""
        wait_for_server(SERVER_URL)
        
        room_id = f"test-{random_id()}"
        password = f"secret-{random_id()}"
        expected_size = {"rows": 30, "cols": 120}
        
        sio = socketio.Client()
        received_sizes = []
        size_received = threading.Event()
        
        @sio.on('size')
        def on_size(data):
            received_sizes.append(data)
            size_received.set()
        
        sio.connect(SERVER_URL)
        sio.emit('join', f'/r/{room_id}')
        
        # Wait for join to complete
        time.sleep(0.5)
        
        # Broadcast a message with size
        status = broadcast_message(room_id, "Test", password, size=expected_size)
        assert status == 200, f"Broadcast failed: {status}"
        
        # Wait for size
        assert size_received.wait(timeout=5), "Size not received"
        
        # Verify size
        assert len(received_sizes) > 0, "No size received"
        assert received_sizes[-1] == expected_size, \
            f"Expected {expected_size}, got {received_sizes[-1]}"
        
        sio.disconnect()
    
    def test_receive_existing_data_on_join(self):
        """Should receive existing room data when joining."""
        wait_for_server(SERVER_URL)
        
        room_id = f"test-{random_id()}"
        password = f"secret-{random_id()}"
        test_message = f"Existing-{random_id()}"
        
        # First, broadcast some messages
        broadcast_message(room_id, test_message, password)
        
        # Wait for message to be stored
        time.sleep(0.5)
        
        # Now connect and join
        sio = socketio.Client()
        received_messages = []
        message_received = threading.Event()
        
        @sio.on('message')
        def on_message(data):
            received_messages.append(data)
            message_received.set()
        
        sio.connect(SERVER_URL)
        sio.emit('join', f'/r/{room_id}')
        
        # Wait for existing data
        assert message_received.wait(timeout=5), "Existing message not received"
        
        # Verify we got the existing message
        assert len(received_messages) > 0, "No messages received"
        decoded = decode_message(received_messages[0])
        assert test_message in decoded, f"Expected '{test_message}' in '{decoded}'"
        
        sio.disconnect()


class TestSocketIOUserCount:
    """Tests for user count updates."""
    
    def test_receive_user_count_on_join(self):
        """Should receive user count when joining a room."""
        wait_for_server(SERVER_URL)
        
        room_id = f"test-{random_id()}"
        
        sio = socketio.Client()
        user_counts = []
        count_received = threading.Event()
        
        @sio.on('usersCount')
        def on_users_count(count):
            user_counts.append(count)
            count_received.set()
        
        sio.connect(SERVER_URL)
        sio.emit('join', f'/r/{room_id}')
        
        # Wait for user count
        assert count_received.wait(timeout=5), "User count not received"
        
        # Should be 1 (ourselves)
        assert len(user_counts) > 0, "No user count received"
        assert user_counts[-1] == 1, f"Expected 1 user, got {user_counts[-1]}"
        
        sio.disconnect()
    
    def test_user_count_increases_with_multiple_clients(self):
        """User count should increase when more clients join."""
        wait_for_server(SERVER_URL)
        
        room_id = f"test-{random_id()}"
        
        # First client
        sio1 = socketio.Client()
        user_counts1 = []
        
        @sio1.on('usersCount')
        def on_users_count1(count):
            user_counts1.append(count)
        
        sio1.connect(SERVER_URL)
        sio1.emit('join', f'/r/{room_id}')
        time.sleep(0.5)
        
        # Second client
        sio2 = socketio.Client()
        
        sio2.connect(SERVER_URL)
        sio2.emit('join', f'/r/{room_id}')
        time.sleep(0.5)
        
        # Client 1 should have received count=2 at some point
        assert 2 in user_counts1, f"Expected count 2, got counts: {user_counts1}"
        
        sio1.disconnect()
        sio2.disconnect()
    
    def test_user_count_decreases_on_disconnect(self):
        """User count should decrease when a client disconnects."""
        wait_for_server(SERVER_URL)
        
        room_id = f"test-{random_id()}"
        
        # First client (will stay connected)
        sio1 = socketio.Client()
        user_counts1 = []
        
        @sio1.on('usersCount')
        def on_users_count1(count):
            user_counts1.append(count)
        
        sio1.connect(SERVER_URL)
        sio1.emit('join', f'/r/{room_id}')
        time.sleep(0.5)
        
        # Second client (will disconnect)
        sio2 = socketio.Client()
        
        sio2.connect(SERVER_URL)
        sio2.emit('join', f'/r/{room_id}')
        time.sleep(0.5)
        
        # Disconnect second client
        sio2.disconnect()
        time.sleep(0.5)
        
        # Client 1 should have received count=1 after client 2 disconnected
        assert user_counts1[-1] == 1, f"Expected final count 1, got: {user_counts1}"
        
        sio1.disconnect()


class TestSocketIOMultipleRooms:
    """Tests for multiple rooms isolation."""
    
    def test_messages_isolated_to_room(self):
        """Messages should only be received by clients in the same room."""
        wait_for_server(SERVER_URL)
        
        room_id1 = f"test-{random_id()}"
        room_id2 = f"test-{random_id()}"
        password = f"secret-{random_id()}"
        
        # Client 1 in room 1
        sio1 = socketio.Client()
        messages1 = []
        
        @sio1.on('message')
        def on_message1(data):
            messages1.append(data)
        
        sio1.connect(SERVER_URL)
        sio1.emit('join', f'/r/{room_id1}')
        
        # Client 2 in room 2
        sio2 = socketio.Client()
        messages2 = []
        
        @sio2.on('message')
        def on_message2(data):
            messages2.append(data)
        
        sio2.connect(SERVER_URL)
        sio2.emit('join', f'/r/{room_id2}')
        
        time.sleep(0.5)
        
        # Broadcast to room 1 only
        test_message = f"Room1Only-{random_id()}"
        broadcast_message(room_id1, test_message, password)
        
        time.sleep(1)
        
        # Client 1 should have received the message
        assert len(messages1) > 0, "Client 1 should receive message"
        decoded1 = decode_message(messages1[-1])
        assert test_message in decoded1, "Client 1 should have the test message"
        
        # Client 2 should NOT have received the message
        # (messages2 should be empty or not contain our test message)
        for msg in messages2:
            decoded = decode_message(msg)
            assert test_message not in decoded, \
                "Client 2 should NOT receive message from room 1"
        
        sio1.disconnect()
        sio2.disconnect()


class TestSocketIOMessageAccumulation:
    """Tests for message accumulation and retrieval."""
    
    def test_messages_accumulate_in_order(self):
        """Multiple messages should accumulate and be concatenated."""
        wait_for_server(SERVER_URL)
        
        room_id = f"test-{random_id()}"
        password = f"secret-{random_id()}"
        
        # Send multiple messages
        messages_sent = ["Line1\n", "Line2\n", "Line3\n"]
        for msg in messages_sent:
            broadcast_message(room_id, msg, password)
            time.sleep(0.1)  # Small delay between messages
        
        time.sleep(0.5)
        
        # Now connect and join
        sio = socketio.Client()
        received_messages = []
        message_received = threading.Event()
        
        @sio.on('message')
        def on_message(data):
            received_messages.append(data)
            message_received.set()
        
        sio.connect(SERVER_URL)
        sio.emit('join', f'/r/{room_id}')
        
        # Wait for existing data
        message_received.wait(timeout=5)
        
        # Decode and verify all messages are present
        assert len(received_messages) > 0, "No messages received"
        full_content = decode_message(received_messages[0])
        
        for msg in messages_sent:
            assert msg.strip() in full_content, \
                f"Expected '{msg.strip()}' in accumulated content"
        
        sio.disconnect()


class TestSocketIOJoinEdgeCases:
    """Tests for edge cases when joining rooms."""
    
    def test_join_without_room_prefix(self):
        """Join with 'roomname' instead of '/r/roomname'."""
        wait_for_server(SERVER_URL)
        
        room_id = f"test-{random_id()}"
        password = f"secret-{random_id()}"
        test_message = f"NoPrefix-{random_id()}"
        
        sio = socketio.Client()
        received_messages = []
        message_received = threading.Event()
        
        @sio.on('message')
        def on_message(data):
            received_messages.append(data)
            message_received.set()
        
        sio.connect(SERVER_URL)
        # Join WITHOUT the /r/ prefix
        sio.emit('join', room_id)
        
        time.sleep(0.5)
        
        # Broadcast via HTTP (which uses /r/ prefix)
        status = broadcast_message(room_id, test_message, password)
        assert status == 200, f"Broadcast failed: {status}"
        
        # Wait for message - might or might not be received depending on
        # whether server strips prefix or not
        message_received.wait(timeout=2)
        
        # Document actual behavior
        # The server has stripPrefix() which removes /r/ prefix
        # So joining with "roomname" should actually work
        if len(received_messages) > 0:
            decoded = decode_message(received_messages[-1])
            has_message = test_message in decoded
        else:
            has_message = False
        
        # Document whether prefix is required or not
        print(f"Join without prefix - message received: {has_message}")
        
        sio.disconnect()
    
    def test_join_with_full_url(self):
        """Join with full URL path '/r/roomname'."""
        wait_for_server(SERVER_URL)
        
        room_id = f"test-{random_id()}"
        password = f"secret-{random_id()}"
        test_message = f"FullPath-{random_id()}"
        
        sio = socketio.Client()
        received_messages = []
        message_received = threading.Event()
        
        @sio.on('message')
        def on_message(data):
            received_messages.append(data)
            message_received.set()
        
        sio.connect(SERVER_URL)
        # Join WITH the /r/ prefix (standard way)
        sio.emit('join', f'/r/{room_id}')
        
        time.sleep(0.5)
        
        # Broadcast via HTTP
        status = broadcast_message(room_id, test_message, password)
        assert status == 200, f"Broadcast failed: {status}"
        
        # Should definitely receive message
        assert message_received.wait(timeout=5), "Message not received with full path"
        
        assert len(received_messages) > 0, "No messages received"
        decoded = decode_message(received_messages[-1])
        assert test_message in decoded, f"Expected message not found"
        
        sio.disconnect()
    
    def test_join_empty_room(self):
        """Join with empty string."""
        wait_for_server(SERVER_URL)
        
        sio = socketio.Client()
        connected = threading.Event()
        error_occurred = threading.Event()
        
        @sio.event
        def connect():
            connected.set()
        
        @sio.event
        def connect_error(data):
            error_occurred.set()
        
        sio.connect(SERVER_URL)
        assert connected.wait(timeout=5), "Failed to connect"
        
        # Join with empty string - should not crash
        sio.emit('join', '')
        
        # Give server time to process
        time.sleep(0.5)
        
        # Connection should still be alive
        assert sio.connected, "Connection dropped after joining empty room"
        
        sio.disconnect()
    
    def test_join_with_slashes(self):
        """Join with room name containing slashes."""
        wait_for_server(SERVER_URL)
        
        room_id = f"/r/test/nested/{random_id()}"
        
        sio = socketio.Client()
        user_counts = []
        count_received = threading.Event()
        
        @sio.on('usersCount')
        def on_users_count(count):
            user_counts.append(count)
            count_received.set()
        
        sio.connect(SERVER_URL)
        sio.emit('join', room_id)
        
        # Wait for user count (indicates successful join)
        count_received.wait(timeout=2)
        
        # Document whether nested paths work
        print(f"Join with slashes - user counts received: {user_counts}")
        
        sio.disconnect()


class TestSocketIOMultipleRoomJoin:
    """Tests for clients joining multiple rooms."""
    
    def test_single_client_multiple_rooms(self):
        """Same client joining multiple rooms should receive messages from all."""
        wait_for_server(SERVER_URL)
        
        room_id1 = f"test-{random_id()}"
        room_id2 = f"test-{random_id()}"
        password = f"secret-{random_id()}"
        
        sio = socketio.Client()
        all_messages = []
        
        @sio.on('message')
        def on_message(data):
            all_messages.append(data)
        
        sio.connect(SERVER_URL)
        
        # Join both rooms
        sio.emit('join', f'/r/{room_id1}')
        sio.emit('join', f'/r/{room_id2}')
        
        time.sleep(0.5)
        
        # Broadcast to room 1
        msg1 = f"Room1-{random_id()}"
        broadcast_message(room_id1, msg1, password)
        
        # Broadcast to room 2
        msg2 = f"Room2-{random_id()}"
        broadcast_message(room_id2, msg2, password)
        
        time.sleep(1)
        
        # Should have received messages from both rooms
        all_decoded = [decode_message(m) for m in all_messages]
        all_content = ''.join(all_decoded)
        
        assert msg1 in all_content, f"Should receive message from room 1"
        assert msg2 in all_content, f"Should receive message from room 2"
        
        sio.disconnect()
    
    def test_user_count_per_room(self):
        """User count should be per-room, not global."""
        wait_for_server(SERVER_URL)
        
        room_id1 = f"test-{random_id()}"
        room_id2 = f"test-{random_id()}"
        
        # Client 1 joins room 1 only
        sio1 = socketio.Client()
        counts1 = []
        
        @sio1.on('usersCount')
        def on_count1(count):
            counts1.append(count)
        
        sio1.connect(SERVER_URL)
        sio1.emit('join', f'/r/{room_id1}')
        time.sleep(0.5)
        
        # Client 2 joins room 2 only
        sio2 = socketio.Client()
        counts2 = []
        
        @sio2.on('usersCount')
        def on_count2(count):
            counts2.append(count)
        
        sio2.connect(SERVER_URL)
        sio2.emit('join', f'/r/{room_id2}')
        time.sleep(0.5)
        
        # Each room should show 1 user, not 2
        assert counts1[-1] == 1, f"Room 1 should have 1 user, got {counts1}"
        assert counts2[-1] == 1, f"Room 2 should have 1 user, got {counts2}"
        
        sio1.disconnect()
        sio2.disconnect()


class TestSocketIOReconnection:
    """Tests for reconnection behavior."""
    
    def test_rejoin_after_disconnect(self):
        """Client should be able to rejoin after disconnecting."""
        wait_for_server(SERVER_URL)
        
        room_id = f"test-{random_id()}"
        password = f"secret-{random_id()}"
        
        # First connection
        sio1 = socketio.Client()
        counts1 = []
        
        @sio1.on('usersCount')
        def on_count1(count):
            counts1.append(count)
        
        sio1.connect(SERVER_URL)
        sio1.emit('join', f'/r/{room_id}')
        time.sleep(0.5)
        
        assert 1 in counts1, f"First join should show 1 user"
        
        # Disconnect
        sio1.disconnect()
        time.sleep(0.5)
        
        # Reconnect
        sio2 = socketio.Client()
        counts2 = []
        
        @sio2.on('usersCount')
        def on_count2(count):
            counts2.append(count)
        
        sio2.connect(SERVER_URL)
        sio2.emit('join', f'/r/{room_id}')
        time.sleep(0.5)
        
        # Should be 1 user again (previous client fully disconnected)
        assert counts2[-1] == 1, f"Rejoin should show 1 user, got {counts2}"
        
        sio2.disconnect()
    
    def test_existing_data_available_after_rejoin(self):
        """Messages should persist and be available when rejoining."""
        wait_for_server(SERVER_URL)
        
        room_id = f"test-{random_id()}"
        password = f"secret-{random_id()}"
        test_message = f"Persistent-{random_id()}"
        
        # Broadcast a message
        status = broadcast_message(room_id, test_message, password)
        assert status == 200, f"Broadcast failed: {status}"
        
        time.sleep(0.5)
        
        # First client connects and gets the message
        sio1 = socketio.Client()
        messages1 = []
        msg_received1 = threading.Event()
        
        @sio1.on('message')
        def on_msg1(data):
            messages1.append(data)
            msg_received1.set()
        
        sio1.connect(SERVER_URL)
        sio1.emit('join', f'/r/{room_id}')
        
        assert msg_received1.wait(timeout=5), "First client should receive message"
        sio1.disconnect()
        
        time.sleep(0.5)
        
        # Second client connects later - should still get the message
        sio2 = socketio.Client()
        messages2 = []
        msg_received2 = threading.Event()
        
        @sio2.on('message')
        def on_msg2(data):
            messages2.append(data)
            msg_received2.set()
        
        sio2.connect(SERVER_URL)
        sio2.emit('join', f'/r/{room_id}')
        
        assert msg_received2.wait(timeout=5), "Second client should receive message"
        
        decoded = decode_message(messages2[0])
        assert test_message in decoded, "Message should persist across reconnects"
        
        sio2.disconnect()


class TestSocketIOEdgeCases:
    """Additional edge cases for Socket.IO."""
    
    def test_join_same_room_twice(self):
        """Joining the same room twice should not duplicate user count."""
        wait_for_server(SERVER_URL)
        
        room_id = f"test-{random_id()}"
        
        sio = socketio.Client()
        user_counts = []
        
        @sio.on('usersCount')
        def on_count(count):
            user_counts.append(count)
        
        sio.connect(SERVER_URL)
        
        # Join same room twice
        sio.emit('join', f'/r/{room_id}')
        time.sleep(0.5)
        sio.emit('join', f'/r/{room_id}')
        time.sleep(0.5)
        
        # User count should still be 1 (not 2)
        # The last count should be 1
        assert user_counts[-1] == 1, f"Double join should not duplicate count, got {user_counts}"
        
        sio.disconnect()
    
    def test_unicode_room_name(self):
        """Room names with unicode characters."""
        wait_for_server(SERVER_URL)
        
        room_id = f"テスト-{random_id()}"
        # URL-encode for both HTTP and Socket.IO to use same room
        # (server uses req.url which preserves encoding)
        encoded_room_id = urllib.parse.quote(room_id, safe='')
        password = f"secret-{random_id()}"
        test_message = f"Unicode-{random_id()}"
        
        sio = socketio.Client()
        messages = []
        msg_received = threading.Event()
        
        @sio.on('message')
        def on_msg(data):
            messages.append(data)
            msg_received.set()
        
        sio.connect(SERVER_URL)
        # Use encoded room for Socket.IO too (server uses req.url which keeps encoding)
        sio.emit('join', f'/r/{encoded_room_id}')
        
        time.sleep(0.5)
        
        # Broadcast to unicode room (use encoded version for HTTP)
        status = broadcast_message(encoded_room_id, test_message, password)
        assert status == 200, f"Broadcast to unicode room failed: {status}"
        
        assert msg_received.wait(timeout=5), "Should receive message in unicode room"
        
        decoded = decode_message(messages[-1])
        assert test_message in decoded, "Message content should match"
        
        sio.disconnect()
    
    def test_very_long_room_name(self):
        """Room names that are very long."""
        wait_for_server(SERVER_URL)
        
        room_id = "x" * 500  # 500 char room name
        
        sio = socketio.Client()
        user_counts = []
        count_received = threading.Event()
        
        @sio.on('usersCount')
        def on_count(count):
            user_counts.append(count)
            count_received.set()
        
        sio.connect(SERVER_URL)
        sio.emit('join', f'/r/{room_id}')
        
        # Should either work or fail gracefully
        count_received.wait(timeout=3)
        
        # Document behavior
        print(f"Long room name - counts received: {user_counts}")
        
        # Connection should still be alive
        assert sio.connected, "Connection should survive long room name"
        
        sio.disconnect()
    
    def test_special_chars_in_room_name(self):
        """Room names with special characters."""
        wait_for_server(SERVER_URL)
        
        room_id = f"test-room_name.with-special.chars-{random_id()}"
        password = f"secret-{random_id()}"
        test_message = f"Special-{random_id()}"
        
        sio = socketio.Client()
        messages = []
        msg_received = threading.Event()
        
        @sio.on('message')
        def on_msg(data):
            messages.append(data)
            msg_received.set()
        
        sio.connect(SERVER_URL)
        sio.emit('join', f'/r/{room_id}')
        
        time.sleep(0.5)
        
        # Broadcast to room with special chars
        status = broadcast_message(room_id, test_message, password)
        assert status == 200, f"Broadcast failed: {status}"
        
        assert msg_received.wait(timeout=5), "Should receive message"
        
        decoded = decode_message(messages[-1])
        assert test_message in decoded, "Message content should match"
        
        sio.disconnect()
    
    def test_rapid_join_leave(self):
        """Rapid joining and leaving rooms."""
        wait_for_server(SERVER_URL)
        
        sio = socketio.Client()
        sio.connect(SERVER_URL)
        
        # Rapidly join different rooms
        for i in range(10):
            room_id = f"test-rapid-{i}-{random_id()}"
            sio.emit('join', f'/r/{room_id}')
        
        time.sleep(1)
        
        # Connection should still be alive
        assert sio.connected, "Connection should survive rapid joins"
        
        sio.disconnect()
    
    def test_message_with_special_characters(self):
        """Messages with special characters should be preserved."""
        wait_for_server(SERVER_URL)
        
        room_id = f"test-{random_id()}"
        password = f"secret-{random_id()}"
        
        # Message with various special characters
        test_message = "Hello! @#$%^&*() 日本語 émoji 🎉 <script>alert('xss')</script>"
        
        sio = socketio.Client()
        messages = []
        msg_received = threading.Event()
        
        @sio.on('message')
        def on_msg(data):
            messages.append(data)
            msg_received.set()
        
        sio.connect(SERVER_URL)
        sio.emit('join', f'/r/{room_id}')
        
        time.sleep(0.5)
        
        status = broadcast_message(room_id, test_message, password)
        assert status == 200, f"Broadcast failed: {status}"
        
        assert msg_received.wait(timeout=5), "Should receive message"
        
        decoded = decode_message(messages[-1])
        # The exact characters should be preserved through encoding/decoding
        assert "Hello!" in decoded, "Message should contain 'Hello!'"
        assert "日本語" in decoded, "Message should contain Japanese characters"
        
        sio.disconnect()


class TestSocketIOEventOrder:
    """Tests for event ordering and timing."""
    
    def test_join_events_order(self):
        """When joining a room with existing data, events should come in order."""
        wait_for_server(SERVER_URL)
        
        room_id = f"test-{random_id()}"
        password = f"secret-{random_id()}"
        
        # First, populate the room with data
        broadcast_message(room_id, "Initial message", password, size={"rows": 30, "cols": 100})
        time.sleep(0.5)
        
        # Now connect and track event order
        sio = socketio.Client()
        events = []
        all_received = threading.Event()
        
        @sio.on('size')
        def on_size(data):
            events.append(('size', data))
            check_done()
        
        @sio.on('message')
        def on_message(data):
            events.append(('message', data))
            check_done()
        
        @sio.on('usersCount')
        def on_count(count):
            events.append(('usersCount', count))
            check_done()
        
        def check_done():
            # We expect at least size, message, and usersCount
            event_types = [e[0] for e in events]
            if 'size' in event_types and 'message' in event_types and 'usersCount' in event_types:
                all_received.set()
        
        sio.connect(SERVER_URL)
        sio.emit('join', f'/r/{room_id}')
        
        assert all_received.wait(timeout=5), f"Not all events received: {events}"
        
        # Document the order of events
        event_types = [e[0] for e in events]
        print(f"Event order: {event_types}")
        
        # Verify all expected events received
        assert 'size' in event_types, "Should receive size event"
        assert 'message' in event_types, "Should receive message event"
        assert 'usersCount' in event_types, "Should receive usersCount event"
        
        sio.disconnect()
    
    def test_size_matches_last_broadcast(self):
        """Size received on join should match the last broadcasted size."""
        wait_for_server(SERVER_URL)
        
        room_id = f"test-{random_id()}"
        password = f"secret-{random_id()}"
        
        # Broadcast with different sizes
        broadcast_message(room_id, "Msg1", password, size={"rows": 20, "cols": 60})
        time.sleep(0.1)
        broadcast_message(room_id, "Msg2", password, size={"rows": 30, "cols": 100})
        time.sleep(0.1)
        final_size = {"rows": 40, "cols": 120}
        broadcast_message(room_id, "Msg3", password, size=final_size)
        time.sleep(0.5)
        
        # Now connect and check size
        sio = socketio.Client()
        sizes = []
        size_received = threading.Event()
        
        @sio.on('size')
        def on_size(data):
            sizes.append(data)
            size_received.set()
        
        sio.connect(SERVER_URL)
        sio.emit('join', f'/r/{room_id}')
        
        assert size_received.wait(timeout=5), "Size not received"
        
        # The size should match the LAST broadcast
        assert sizes[-1] == final_size, f"Expected {final_size}, got {sizes[-1]}"
        
        sio.disconnect()


class TestSocketIOEmptyRoom:
    """Tests for joining empty rooms."""
    
    def test_join_empty_room_no_message(self):
        """Joining an empty room should not receive message event."""
        wait_for_server(SERVER_URL)
        
        room_id = f"test-empty-{random_id()}"
        
        sio = socketio.Client()
        messages = []
        sizes = []
        user_counts = []
        events_done = threading.Event()
        
        @sio.on('message')
        def on_message(data):
            messages.append(data)
        
        @sio.on('size')
        def on_size(data):
            sizes.append(data)
        
        @sio.on('usersCount')
        def on_count(count):
            user_counts.append(count)
            events_done.set()  # usersCount is always sent
        
        sio.connect(SERVER_URL)
        sio.emit('join', f'/r/{room_id}')
        
        # Wait for usersCount (always sent)
        assert events_done.wait(timeout=5), "usersCount not received"
        
        # Give time for any other events
        time.sleep(0.5)
        
        # Should have received usersCount but NOT message or size
        assert len(user_counts) > 0, "Should receive usersCount"
        assert len(messages) == 0, f"Should NOT receive message for empty room, got {messages}"
        assert len(sizes) == 0, f"Should NOT receive size for empty room, got {sizes}"
        
        sio.disconnect()
    
    def test_join_after_delete_no_old_messages(self):
        """After DELETE, joining should not receive old messages."""
        wait_for_server(SERVER_URL)
        
        room_id = f"test-{random_id()}"
        password = f"secret-{random_id()}"
        test_message = "This will be deleted"
        
        # Create room with message
        status = broadcast_message(room_id, test_message, password)
        assert status == 200, f"Broadcast failed: {status}"
        
        time.sleep(0.5)
        
        # Delete room via HTTP
        import http.client
        parsed = urllib.parse.urlparse(SERVER_URL)
        conn = http.client.HTTPConnection(parsed.netloc)
        conn.request('DELETE', f'/r/{room_id}', headers={"Authorization": password})
        response = conn.getresponse()
        assert response.status == 202, f"DELETE failed: {response.status}"
        conn.close()
        
        time.sleep(0.5)
        
        # Now join - should not receive the old message
        sio = socketio.Client()
        messages = []
        user_counts = []
        count_received = threading.Event()
        
        @sio.on('message')
        def on_message(data):
            messages.append(data)
        
        @sio.on('usersCount')
        def on_count(count):
            user_counts.append(count)
            count_received.set()
        
        sio.connect(SERVER_URL)
        sio.emit('join', f'/r/{room_id}')
        
        # Wait for usersCount
        assert count_received.wait(timeout=5), "usersCount not received"
        
        # Give time for any message event
        time.sleep(0.5)
        
        # Should NOT have received the old message
        for msg in messages:
            decoded = decode_message(msg)
            assert test_message not in decoded, \
                f"Should NOT receive deleted message, but got: {decoded}"
        
        sio.disconnect()


class TestSocketIOSizePassthrough:
    """Tests for size object passthrough behavior."""
    
    def test_size_object_exact_passthrough(self):
        """Size object should be passed through exactly as sent."""
        wait_for_server(SERVER_URL)
        
        room_id = f"test-{random_id()}"
        password = f"secret-{random_id()}"
        
        # Custom size with extra fields
        custom_size = {
            "rows": 42,
            "cols": 132,
            "extra": "data",
            "nested": {"foo": "bar"}
        }
        
        sio = socketio.Client()
        sizes = []
        size_received = threading.Event()
        
        @sio.on('size')
        def on_size(data):
            sizes.append(data)
            size_received.set()
        
        sio.connect(SERVER_URL)
        sio.emit('join', f'/r/{room_id}')
        
        time.sleep(0.5)
        
        # Broadcast with custom size
        status = broadcast_message(room_id, "test", password, size=custom_size)
        assert status == 200, f"Broadcast failed: {status}"
        
        assert size_received.wait(timeout=5), "Size not received"
        
        # Size should match exactly
        assert sizes[-1] == custom_size, f"Expected {custom_size}, got {sizes[-1]}"
        
        sio.disconnect()
    
    def test_size_null_value(self):
        """Null size should be passed through."""
        wait_for_server(SERVER_URL)
        
        room_id = f"test-{random_id()}"
        password = f"secret-{random_id()}"
        
        sio = socketio.Client()
        sizes = []
        size_received = threading.Event()
        
        @sio.on('size')
        def on_size(data):
            sizes.append(data)
            size_received.set()
        
        sio.connect(SERVER_URL)
        sio.emit('join', f'/r/{room_id}')
        
        time.sleep(0.5)
        
        # Broadcast with null size
        status = broadcast_message(room_id, "test", password, size=None)
        assert status == 200, f"Broadcast failed: {status}"
        
        assert size_received.wait(timeout=5), "Size not received"
        
        # Size should be null
        assert sizes[-1] is None, f"Expected None, got {sizes[-1]}"
        
        sio.disconnect()


class TestSocketIOMessageFormat:
    """Tests for message format and encoding."""
    
    def test_message_exact_base64_format(self):
        """Message should be in base64(url_encode(text)) format."""
        wait_for_server(SERVER_URL)
        
        room_id = f"test-{random_id()}"
        password = f"secret-{random_id()}"
        test_message = "Hello World!"
        
        sio = socketio.Client()
        raw_messages = []
        message_received = threading.Event()
        
        @sio.on('message')
        def on_message(data):
            raw_messages.append(data)
            message_received.set()
        
        sio.connect(SERVER_URL)
        sio.emit('join', f'/r/{room_id}')
        
        time.sleep(0.5)
        
        # Broadcast
        status = broadcast_message(room_id, test_message, password)
        assert status == 200, f"Broadcast failed: {status}"
        
        assert message_received.wait(timeout=5), "Message not received"
        
        # Verify raw message is base64
        raw = raw_messages[-1]
        assert isinstance(raw, str), f"Message should be string, got {type(raw)}"
        
        # Decode and verify
        decoded = decode_message(raw)
        assert test_message in decoded, f"Expected '{test_message}' in '{decoded}'"
        
        sio.disconnect()
    
    def test_accumulated_messages_format(self):
        """Accumulated messages should be concatenated correctly."""
        wait_for_server(SERVER_URL)
        
        room_id = f"test-{random_id()}"
        password = f"secret-{random_id()}"
        
        # Send 3 distinct messages
        msg1, msg2, msg3 = "FIRST", "SECOND", "THIRD"
        broadcast_message(room_id, msg1, password)
        time.sleep(0.1)
        broadcast_message(room_id, msg2, password)
        time.sleep(0.1)
        broadcast_message(room_id, msg3, password)
        time.sleep(0.5)
        
        # New client joins
        sio = socketio.Client()
        messages = []
        message_received = threading.Event()
        
        @sio.on('message')
        def on_message(data):
            messages.append(data)
            message_received.set()
        
        sio.connect(SERVER_URL)
        sio.emit('join', f'/r/{room_id}')
        
        assert message_received.wait(timeout=5), "Message not received"
        
        # Decode the accumulated message
        decoded = decode_message(messages[0])
        
        # All three should be present and in order
        idx1 = decoded.find(msg1)
        idx2 = decoded.find(msg2)
        idx3 = decoded.find(msg3)
        
        assert idx1 != -1, f"'{msg1}' not found in '{decoded}'"
        assert idx2 != -1, f"'{msg2}' not found in '{decoded}'"
        assert idx3 != -1, f"'{msg3}' not found in '{decoded}'"
        assert idx1 < idx2 < idx3, f"Messages not in order: {idx1}, {idx2}, {idx3}"
        
        sio.disconnect()


class TestSocketIOStripPrefix:
    """Tests for stripPrefix behavior."""
    
    def test_strip_prefix_removes_r_prefix(self):
        """stripPrefix should remove /r/ prefix."""
        wait_for_server(SERVER_URL)
        
        room_id = f"test-{random_id()}"
        password = f"secret-{random_id()}"
        test_message = f"Prefix-{random_id()}"
        
        # Two clients: one joins with prefix, one without
        sio1 = socketio.Client()
        sio2 = socketio.Client()
        messages1 = []
        messages2 = []
        msg1_received = threading.Event()
        msg2_received = threading.Event()
        
        @sio1.on('message')
        def on_msg1(data):
            messages1.append(data)
            msg1_received.set()
        
        @sio2.on('message')
        def on_msg2(data):
            messages2.append(data)
            msg2_received.set()
        
        sio1.connect(SERVER_URL)
        sio2.connect(SERVER_URL)
        
        # One joins with /r/ prefix
        sio1.emit('join', f'/r/{room_id}')
        # One joins without prefix (server has /r/ as roomPrefix)
        sio2.emit('join', f'/{room_id}')
        
        time.sleep(0.5)
        
        # Broadcast
        status = broadcast_message(room_id, test_message, password)
        assert status == 200, f"Broadcast failed: {status}"
        
        msg1_received.wait(timeout=5)
        msg2_received.wait(timeout=2)
        
        # Client with /r/ prefix should definitely receive
        assert len(messages1) > 0, "Client with /r/ prefix should receive message"
        
        # Document whether client without /r/ prefix receives
        # (depends on exact stripPrefix implementation)
        
        sio1.disconnect()
        sio2.disconnect()


class TestSocketIOLargeMessage:
    """Tests for large message handling."""
    
    def test_large_message_delivery(self):
        """Large messages should be delivered correctly."""
        wait_for_server(SERVER_URL)
        
        room_id = f"test-{random_id()}"
        password = f"secret-{random_id()}"
        
        # 50KB message (under 300KB limit)
        large_message = "X" * 50000
        
        sio = socketio.Client()
        messages = []
        message_received = threading.Event()
        
        @sio.on('message')
        def on_message(data):
            messages.append(data)
            message_received.set()
        
        sio.connect(SERVER_URL)
        sio.emit('join', f'/r/{room_id}')
        
        time.sleep(0.5)
        
        status = broadcast_message(room_id, large_message, password)
        assert status == 200, f"Broadcast failed: {status}"
        
        assert message_received.wait(timeout=10), "Large message not received"
        
        decoded = decode_message(messages[-1])
        assert large_message in decoded, "Large message content should match"
        
        sio.disconnect()


class TestSocketIOMultipleClients:
    """Tests for multiple client scenarios."""
    
    def test_three_clients_same_room(self):
        """Three clients in same room should all receive messages."""
        wait_for_server(SERVER_URL)
        
        room_id = f"test-{random_id()}"
        password = f"secret-{random_id()}"
        test_message = f"Broadcast-{random_id()}"
        
        clients = []
        message_lists = []
        events = []
        
        for i in range(3):
            sio = socketio.Client()
            messages = []
            evt = threading.Event()
            
            def make_handler(msgs, event):
                def handler(data):
                    msgs.append(data)
                    event.set()
                return handler
            
            sio.on('message', make_handler(messages, evt))
            sio.connect(SERVER_URL)
            sio.emit('join', f'/r/{room_id}')
            
            clients.append(sio)
            message_lists.append(messages)
            events.append(evt)
        
        time.sleep(0.5)
        
        # Broadcast
        status = broadcast_message(room_id, test_message, password)
        assert status == 200, f"Broadcast failed: {status}"
        
        # All clients should receive
        for i, evt in enumerate(events):
            assert evt.wait(timeout=5), f"Client {i} did not receive message"
        
        for i, messages in enumerate(message_lists):
            assert len(messages) > 0, f"Client {i} has no messages"
            decoded = decode_message(messages[-1])
            assert test_message in decoded, f"Client {i} missing test message"
        
        for sio in clients:
            sio.disconnect()


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
