"""
Shared fixtures and constants for CLI E2E tests.

This module provides:
- CLI path constants for testing the shellshare CLI
- Socket.IO listener class for verifying messages via WebSocket
- Pytest fixtures for unique rooms, passwords, and socket listeners
"""

import base64

import random
import string
import sys
import threading
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

import pytest
import socketio

# Constants
# Use the Rust binary from target/release (or target/debug for development)
_PROJECT_ROOT = Path(__file__).parent.parent
_RELEASE_PATH = _PROJECT_ROOT / "target" / "release" / "shellshare"
_DEBUG_PATH = _PROJECT_ROOT / "target" / "debug" / "shellshare"

# Prefer release build, fall back to debug
if _RELEASE_PATH.exists():
    CLI_PATH = _RELEASE_PATH
else:
    CLI_PATH = _DEBUG_PATH

CLI_COMMAND = [str(CLI_PATH)]
SERVER_URL = "http://localhost:3000"


def random_id(length=12):
    """Generate a random ID for room names."""
    return ''.join(random.choices(string.ascii_lowercase + string.digits, k=length))


def wait_for_content(listener, predicate, timeout=5):
    """
    Wait until accumulated messages satisfy a predicate.

    Args:
        listener: SocketListener instance
        predicate: Function that takes accumulated messages string and returns bool
        timeout: Maximum seconds to wait

    Returns:
        True if predicate was satisfied, False if timeout expired
    """
    start = time.time()
    while time.time() - start < timeout:
        accumulated = listener.get_accumulated_messages()
        if predicate(accumulated):
            return True
        remaining = timeout - (time.time() - start)
        if remaining > 0:
            listener._message_event.wait(timeout=min(0.5, remaining))
            listener._message_event.clear()
    return False


def poll_until(predicate, timeout=5, interval=0.1):
    """
    Poll until predicate returns True or timeout expires.

    Args:
        predicate: Function that returns bool
        timeout: Maximum seconds to wait
        interval: Polling interval in seconds

    Returns:
        True if predicate became True, False if timeout expired
    """
    start = time.time()
    while time.time() - start < timeout:
        if predicate():
            return True
        time.sleep(interval)
    return False


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


@dataclass
class SocketListener:
    """
    Connects to a room and captures message/size/usersCount events.

    Usage:
        listener = SocketListener(room_id)
        listener.connect()
        # ... run CLI ...
        msg = listener.wait_for_message(timeout=5)
        listener.disconnect()
    """
    room_id: str
    server_url: str = SERVER_URL

    # Internal state
    _sio: socketio.Client = field(default=None, init=False, repr=False)
    _messages: list = field(default_factory=list, init=False, repr=False)
    _sizes: list = field(default_factory=list, init=False, repr=False)
    _user_counts: list = field(default_factory=list, init=False, repr=False)
    _message_event: threading.Event = field(default_factory=threading.Event, init=False, repr=False)
    _size_event: threading.Event = field(default_factory=threading.Event, init=False, repr=False)
    _user_count_event: threading.Event = field(default_factory=threading.Event, init=False, repr=False)
    _connected: bool = field(default=False, init=False, repr=False)

    def connect(self, wait_for_join=True):
        """Connect to the server and join the room."""
        wait_for_server(self.server_url)

        self._sio = socketio.Client()

        @self._sio.on('message')
        def on_message(data):
            self._messages.append(data)
            self._message_event.set()

        @self._sio.on('size')
        def on_size(data):
            self._sizes.append(data)
            self._size_event.set()

        @self._sio.on('usersCount')
        def on_users_count(count):
            self._user_counts.append(count)
            self._user_count_event.set()

        self._sio.connect(self.server_url)
        self._sio.emit('join', f'/r/{self.room_id}')
        self._connected = True

        if wait_for_join:
            # Wait for usersCount to confirm we've joined
            self._user_count_event.wait(timeout=5)
            # Clear the event for future user count changes
            self._user_count_event.clear()

    def disconnect(self):
        """Disconnect from the server."""
        if self._sio and self._connected:
            self._sio.disconnect()
            self._connected = False

    def wait_for_message(self, timeout=5, containing=None):
        """
        Wait for a message to arrive.

        Args:
            timeout: Maximum seconds to wait
            containing: If provided, wait for a message containing this substring

        Returns:
            The decoded message text, or None if timeout
        """
        start = time.time()
        while time.time() - start < timeout:
            if self._messages:
                # Check all messages
                for raw_msg in self._messages:
                    decoded = decode_message(raw_msg)
                    if containing is None or containing in decoded:
                        return decoded

            # Wait for more messages
            remaining = timeout - (time.time() - start)
            if remaining > 0:
                self._message_event.wait(timeout=min(0.5, remaining))
                self._message_event.clear()

        return None

    def get_accumulated_messages(self) -> str:
        """Get all messages concatenated together (decoded)."""
        return ''.join(decode_message(m) for m in self._messages)

    def wait_for_user_count(self, expected_count, timeout=5) -> bool:
        """Wait for a specific user count."""
        start = time.time()
        while time.time() - start < timeout:
            if self._user_counts and self._user_counts[-1] == expected_count:
                return True
            remaining = timeout - (time.time() - start)
            if remaining > 0:
                self._user_count_event.wait(timeout=min(0.5, remaining))
                self._user_count_event.clear()
        return False

    def get_last_user_count(self) -> int:
        """Get the last received user count."""
        return self._user_counts[-1] if self._user_counts else 0

    def get_last_size(self) -> dict:
        """Get the last received terminal size."""
        return self._sizes[-1] if self._sizes else None

    def wait_for_size(self, timeout=5) -> bool:
        """Wait for a size event to arrive."""
        start = time.time()
        while time.time() - start < timeout:
            if self._sizes:
                return True
            remaining = timeout - (time.time() - start)
            if remaining > 0:
                self._size_event.wait(timeout=min(0.5, remaining))
                self._size_event.clear()
        return False

    def clear_messages(self):
        """Clear accumulated messages."""
        self._messages.clear()
        self._message_event.clear()


# Pytest fixtures

@pytest.fixture
def unique_room() -> str:
    """Generate a unique room ID for this test."""
    return f"test-{random_id()}"


@pytest.fixture
def unique_password() -> str:
    """Generate a unique password for this test."""
    return f"secret-{random_id()}"


@pytest.fixture
def socket_listener(unique_room):
    """
    Create a SocketListener that connects to the unique_room.

    The listener connects before the test runs and disconnects after.
    """
    listener = SocketListener(unique_room)
    listener.connect()
    yield listener
    listener.disconnect()


@pytest.fixture(scope="session", autouse=True)
def ensure_server_running():
    """Ensure the server is running before tests start."""
    wait_for_server(SERVER_URL)


@pytest.fixture
def cli_command():
    """Return the CLI command list for running shellshare."""
    return CLI_COMMAND.copy()


@pytest.fixture
def server_url():
    """Return the server URL."""
    return SERVER_URL
