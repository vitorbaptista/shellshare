"""
Shared fixtures and constants for CLI E2E tests.

This module provides:
- CLI path constants for testing the shellshare CLI
- Socket.IO listener class for verifying messages via WebSocket
- Pytest fixtures for unique rooms, passwords, and socket listeners
"""

import json
import os
import random
import socket
import string
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

import pytest
import socketio
import websocket

# Constants
# Use the Rust binary from target/release (or target/debug for development)
_PROJECT_ROOT = Path(__file__).parent.parent
_EXE_SUFFIX = ".exe" if sys.platform == "win32" else ""
_RELEASE_PATH = _PROJECT_ROOT / "target" / "release" / f"shellshare{_EXE_SUFFIX}"
_DEBUG_PATH = _PROJECT_ROOT / "target" / "debug" / f"shellshare{_EXE_SUFFIX}"

# Prefer release build, fall back to debug
if _RELEASE_PATH.exists():
    CLI_PATH = _RELEASE_PATH
else:
    CLI_PATH = _DEBUG_PATH

CLI_COMMAND = [str(CLI_PATH)]


def _free_port():
    """Ask the OS for a free TCP port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _resolve_shared_port():
    """Pick the shared server's port, returning (port, pinned).

    SHELLSHARE_E2E_PORT pins the port (pytest_configure then reuses any
    server already answering there). Without it the suite picks a free
    port, so whatever happens to occupy :3000 never breaks a test run.
    """
    env_port = os.environ.get("SHELLSHARE_E2E_PORT")
    if env_port is not None:
        try:
            port = int(env_port)
            if not 1 <= port <= 65535:
                raise ValueError
        except ValueError:
            raise RuntimeError(
                "SHELLSHARE_E2E_PORT must be a port number (1-65535), got: "
                f"{env_port!r}"
            ) from None
        return port, True
    port = _free_port()
    # xdist workers re-import this module in fresh local processes
    # spawned after this point (popen gateways, xdist's default); the
    # env var carries the controller's choice so every worker agrees
    # on the port
    os.environ["SHELLSHARE_E2E_PORT"] = str(port)
    return port, False


# _PORT_WAS_PINNED is only meaningful in the xdist controller: workers
# always see the env var (the controller sets it) but never consult the
# flag - they return early from pytest_configure
SHARED_PORT, _PORT_WAS_PINNED = _resolve_shared_port()
# 127.0.0.1, not localhost: on Windows localhost can resolve to ::1 first
# while the server listens on IPv4 (see ServerHandle.url)
SERVER_URL = f"http://127.0.0.1:{SHARED_PORT}"


def _server_responds(url, timeout=1):
    """Check whether a shellshare server answers at the given URL."""
    try:
        with urllib.request.urlopen(url, timeout=timeout):
            return True
    except urllib.error.HTTPError:
        # An HTTP error response still means a server is up
        return True
    except Exception:
        return False


def _spawn_server(port, *extra_args):
    """Spawn a shellshare server process on the given port.

    Output goes to a log file (kept on the proc as `log_path`) so a
    startup failure - e.g. the port got taken between picking it and
    binding - is reported by wait_for_server instead of timing out
    with no clue.
    """
    if not CLI_PATH.exists():
        raise RuntimeError(
            f"shellshare binary not found at {CLI_PATH}. "
            "Run `cargo build --release` first."
        )
    # mkstemp, not a fixed name: a stale log owned by another user on a
    # shared machine would make a fixed path unopenable
    fd, log_path = tempfile.mkstemp(
        prefix=f"shellshare-e2e-{port}-", suffix=".log"
    )
    with os.fdopen(fd, "wb") as log:
        proc = subprocess.Popen(
            [str(CLI_PATH), "server", "--host", "127.0.0.1", "--port", str(port)]
            + [str(a) for a in extra_args],
            stdout=log,
            stderr=subprocess.STDOUT,
        )
    proc.log_path = Path(log_path)
    return proc


def pytest_configure(config):
    """Start the shared server, or adopt a pinned one already running.

    Runs only in the xdist controller (or in a single-process run), so the
    server is started exactly once no matter how many workers there are.
    Reusing a running server only applies to a pinned port: an auto-picked
    port was free moments ago, so anything answering there now is a
    stranger that grabbed it in the meantime - spawn and let the bind
    failure surface instead of silently testing the wrong server.
    """
    if hasattr(config, "workerinput"):  # xdist worker: controller handles it
        return
    config._shellshare_server = None
    if _PORT_WAS_PINNED and _server_responds(SERVER_URL):
        return
    config._shellshare_server = _spawn_server(SHARED_PORT)
    wait_for_server(SERVER_URL, proc=config._shellshare_server)


def pytest_unconfigure(config):
    proc = getattr(config, "_shellshare_server", None)
    if proc is not None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()  # reap; kill() alone leaves a zombie


@dataclass
class ServerHandle:
    """A dedicated shellshare server owned by a single test."""
    port: int
    proc: subprocess.Popen

    @property
    def url(self):
        # 127.0.0.1, not localhost: the server binds IPv4 only, and on
        # Windows localhost can resolve to ::1 first, making fresh
        # Socket.IO connections slow or flaky
        return f"http://127.0.0.1:{self.port}"


@pytest.fixture
def dedicated_server():
    """Factory fixture: spawn servers with custom flags on free ports.

    Usage:
        server = dedicated_server("--cleanup-interval", 1, "--room-ttl", 2)
    """
    handles = []

    def start(*extra_args):
        port = _free_port()
        proc = _spawn_server(port, *extra_args)
        handle = ServerHandle(port=port, proc=proc)
        handles.append(handle)
        wait_for_server(handle.url, proc=proc)
        return handle

    yield start

    for handle in handles:
        handle.proc.terminate()
        try:
            handle.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            handle.proc.kill()
            handle.proc.wait()  # reap; kill() alone leaves a zombie


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
    with listener._condition:
        start = time.time()
        while True:
            accumulated = listener.get_accumulated_messages_unlocked()
            if predicate(accumulated):
                return True
            remaining = timeout - (time.time() - start)
            if remaining <= 0:
                return False
            listener._condition.wait(timeout=remaining)


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


def _server_log_tail(proc, limit=2000):
    """Tail of the server's log file, or '' when unavailable."""
    log_path = getattr(proc, "log_path", None)
    if log_path is None:
        return ""
    try:
        text = log_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return "\n" + text[-limit:]


def wait_for_server(url, timeout_seconds=30, proc=None):
    """Wait for server to be ready.

    Given the server's process, a crash before it answers fails
    immediately with the server's output instead of timing out, and a
    timeout includes that output too.
    """
    start = time.time()
    while time.time() - start < timeout_seconds:
        if proc is not None and proc.poll() is not None:
            raise RuntimeError(
                f"server exited with code {proc.returncode} "
                f"before answering at {url}{_server_log_tail(proc)}"
            )
        try:
            urllib.request.urlopen(url, timeout=1)
            return True
        except Exception:
            time.sleep(0.5)
    tail = _server_log_tail(proc) if proc is not None else ""
    raise TimeoutError(f"Server not ready after {timeout_seconds}s{tail}")


_UNSET = object()  # Sentinel for distinguishing None from unset


def ws_connect_room(server_url, room, password, timeout=5):
    """Open a broadcasting WebSocket the way the CLI does. The room is
    claimed (or its password verified) at the handshake; a mismatch
    raises websocket.WebSocketBadStatusException with status 401."""
    return websocket.create_connection(
        f"{server_url.replace('http://', 'ws://')}/ws/r/{room}",
        header={"Authorization": password},
        timeout=timeout,
    )


def broadcast_message(server_url, room, password, text=None, size=_UNSET):
    """Broadcast over the WebSocket ingest the way the CLI does.

    Returns an HTTP-like status so call sites read naturally: 200 when
    stored (the server's ack is awaited, so the data is durable when
    this returns), or the handshake's status (e.g. 401) when rejected.

    size: defaults to 80x24; pass None to send no size at all, or any
    JSON value to exercise the server's leniency (invalid sizes are
    forwarded but produce no ack and no event).
    """
    if size is _UNSET:
        size = {"cols": 80, "rows": 24}
    try:
        ws = ws_connect_room(server_url, room, password)
    except websocket.WebSocketBadStatusException as e:
        return e.status_code
    try:
        if size is not None:
            ws.send(json.dumps({"size": size}))
            if isinstance(size, dict) and "cols" in size and "rows" in size:
                ws.recv()  # ack: the size is stored before we return
        if text:
            ws.send_binary(text.encode())
            ws.recv()  # ack: the message is stored before we return
        return 200
    finally:
        ws.close()


def decode_message(data):
    """Decode a received message: raw terminal bytes, sent as a Socket.IO
    binary attachment."""
    return bytes(data).decode("utf-8", errors="replace")


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

    # Internal state - all protected by _condition
    _sio: socketio.Client = field(default=None, init=False, repr=False)
    _messages: list = field(default_factory=list, init=False, repr=False)
    _sizes: list = field(default_factory=list, init=False, repr=False)
    _user_counts: list = field(default_factory=list, init=False, repr=False)
    _broadcasting: list = field(default_factory=list, init=False, repr=False)
    _condition: threading.Condition = field(default_factory=threading.Condition, init=False, repr=False)
    _connected: bool = field(default=False, init=False, repr=False)

    def connect(self, wait_for_join=True):
        """Connect to the server and join the room."""
        wait_for_server(self.server_url)

        self._sio = socketio.Client()

        @self._sio.on('message')
        def on_message(data):
            with self._condition:
                self._messages.append(data)
                self._condition.notify_all()

        @self._sio.on('size')
        def on_size(data):
            with self._condition:
                self._sizes.append(data)
                self._condition.notify_all()

        @self._sio.on('usersCount')
        def on_users_count(count):
            with self._condition:
                self._user_counts.append(count)
                self._condition.notify_all()

        @self._sio.on('broadcasting')
        def on_broadcasting(live):
            with self._condition:
                self._broadcasting.append(live)
                self._condition.notify_all()

        # WebSocket only, like the viewer page: the server rejects HTTP
        # long-polling (its engine.io polling path corrupts binary frames)
        self._sio.connect(self.server_url, transports=["websocket"])
        self._connected = True

        if not wait_for_join:
            self._sio.emit('join', f'/r/{self.room_id}')
            return

        # Joining must be confirmed, not fire-and-forget: a bare emit has
        # no delivery guarantee, and a join lost in transit means the test
        # silently misses every event afterwards. The server confirms each
        # join with usersCount, and re-joining is idempotent, so emit and
        # re-emit until confirmed.
        attempts = 3
        per_attempt = 5
        for _ in range(attempts):
            self._sio.emit('join', f'/r/{self.room_id}')
            with self._condition:
                deadline = time.time() + per_attempt
                while not self._user_counts:
                    remaining = deadline - time.time()
                    if remaining <= 0:
                        break
                    self._condition.wait(timeout=remaining)
                if self._user_counts:
                    return
        # Don't leak the connected client: this raise typically happens in
        # fixture setup, where teardown (and its disconnect) never runs
        self.disconnect()
        raise TimeoutError(
            f"Socket.IO join to room {self.room_id!r} on {self.server_url} "
            f"not confirmed after {attempts} attempts"
        )

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
        def find_matching():
            for raw_msg in self._messages:
                decoded = decode_message(raw_msg)
                if containing is None or containing in decoded:
                    return decoded
            return None

        with self._condition:
            start = time.time()
            while True:
                result = find_matching()
                if result is not None:
                    return result
                remaining = timeout - (time.time() - start)
                if remaining <= 0:
                    return None
                self._condition.wait(timeout=remaining)

    def get_accumulated_messages(self) -> str:
        """Get all messages concatenated together (decoded)."""
        with self._condition:
            return self.get_accumulated_messages_unlocked()

    def get_accumulated_messages_unlocked(self) -> str:
        """Get all messages concatenated together (decoded). Caller must hold lock.

        Concatenates at the byte level before decoding, so a UTF-8
        sequence split across two messages still decodes correctly.
        """
        return b"".join(bytes(m) for m in self._messages).decode(
            "utf-8", errors="replace"
        )

    def wait_for_user_count(self, expected_count, timeout=5) -> bool:
        """Wait for a specific user count."""
        with self._condition:
            start = time.time()
            while True:
                if self._user_counts and self._user_counts[-1] == expected_count:
                    return True
                remaining = timeout - (time.time() - start)
                if remaining <= 0:
                    return False
                self._condition.wait(timeout=remaining)

    def get_last_user_count(self) -> int:
        """Get the last received user count."""
        with self._condition:
            return self._user_counts[-1] if self._user_counts else 0

    def wait_for_broadcasting(self, expected, timeout=5) -> bool:
        """Wait until the LATEST broadcasting state equals `expected`."""
        with self._condition:
            start = time.time()
            while True:
                if self._broadcasting and self._broadcasting[-1] == expected:
                    return True
                remaining = timeout - (time.time() - start)
                if remaining <= 0:
                    return False
                self._condition.wait(timeout=remaining)

    def get_last_broadcasting(self):
        """Last received broadcasting state, or None before the first."""
        with self._condition:
            return self._broadcasting[-1] if self._broadcasting else None

    def get_last_size(self) -> dict:
        """Get the last received terminal size."""
        with self._condition:
            return self._sizes[-1] if self._sizes else None

    def wait_for_size(self, timeout=5) -> bool:
        """Wait for a size event to arrive."""
        with self._condition:
            start = time.time()
            while True:
                if self._sizes:
                    return True
                remaining = timeout - (time.time() - start)
                if remaining <= 0:
                    return False
                self._condition.wait(timeout=remaining)

    def clear_messages(self):
        """Clear accumulated messages."""
        with self._condition:
            self._messages.clear()


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
