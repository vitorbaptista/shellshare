"""
Socket.IO Tests for Shellshare Backend

These tests verify the real-time WebSocket functionality:
- Joining rooms
- Receiving messages in real-time
- Getting existing room data on join
- User count updates

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


def broadcast_message(room_id, message, password, size=None):
    """Broadcast a message via HTTP POST."""
    import http.client
    
    if size is None:
        size = {"rows": 24, "cols": 80}
    
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


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
