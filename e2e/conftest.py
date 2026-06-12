"""
Shared fixtures and constants for CLI E2E tests.

This module provides:
- CLI path constants for testing the shellshare CLI
- Viewer WebSocket listener class for verifying broadcast data
- Pytest fixtures for unique rooms, passwords, and socket listeners
"""

import json
import os
import random
import re
import socket
import string
import struct
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
import websocket
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

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


def _spawn_server(port, *extra_args, env=None):
    """Spawn a shellshare server process on the given port.

    `env`, when given, fully replaces the child's environment (build it
    from os.environ to inherit).

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
            env=env,
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
        # WebSocket connections slow or flaky
        return f"http://127.0.0.1:{self.port}"


@pytest.fixture
def dedicated_server():
    """Factory fixture: spawn servers with custom flags on free ports.

    Usage:
        server = dedicated_server("--cleanup-interval", 1, "--room-ttl", 2)
    """
    handles = []

    def start(*extra_args, env=None):
        port = _free_port()
        proc = _spawn_server(port, *extra_args, env=env)
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


def broadcast_message(server_url, room, password, text=None, size=_UNSET, key=None):
    """Broadcast over the WebSocket ingest the way the CLI does.

    Returns an HTTP-like status so call sites read naturally: 200 when
    stored (the server's ack is awaited, so the data is durable when
    this returns), or the handshake's status (e.g. 401) when rejected.

    size: defaults to 80x24; pass None to send no size at all, or any
    JSON value to exercise the server's leniency (invalid sizes are
    forwarded but produce no ack and no event).

    key: when set (hex), `text` is sealed into an encrypted record before
    sending, so a real viewer with that key in the URL fragment can
    decrypt it. The server only ever sees ciphertext either way.
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
            payload = seal_record(key, text.encode()) if key else text.encode()
            ws.send_binary(payload)
            ws.recv()  # ack: the message is stored before we return
        return 200
    finally:
        ws.close()


def decode_message(data):
    """Decode a received message: raw terminal bytes from a binary
    WebSocket frame."""
    return bytes(data).decode("utf-8", errors="replace")


# Every broadcast is end-to-end encrypted, so the harness seals and opens
# records exactly like the CLI (src/cli/crypto.rs) and the viewer page:
# [u32 BE length][12-byte nonce][ciphertext + 16-byte tag], AES-256-GCM.
# A test that drives the real CLI gets ciphertext on the wire and must
# decrypt with the key from the share link; a test injecting bytes over a
# raw WebSocket controls whether to seal them.

def seal_record(key, plaintext):
    """Seal one chunk into a record, mirroring `Encryptor::seal`.

    `key` is the 32 raw key bytes (or its 64-char hex form).
    """
    if isinstance(key, str):
        key = bytes.fromhex(key)
    nonce = os.urandom(12)
    body = nonce + AESGCM(key).encrypt(nonce, plaintext, None)
    return struct.pack(">I", len(body)) + body


def open_records(key, data):
    """Decrypt a run of records back into terminal bytes.

    Undecryptable records are skipped (mixed-key history), matching the
    viewer; a structurally broken length frame stops the walk.
    """
    if isinstance(key, str):
        key = bytes.fromhex(key)
    aes = AESGCM(key)
    out = bytearray()
    offset = 0
    total = len(data)
    while offset < total:
        if total - offset < 4:
            break
        (record_len,) = struct.unpack_from(">I", data, offset)
        offset += 4
        # Minimum: 12-byte nonce + 16-byte tag
        if record_len < 28 or total - offset < record_len:
            break
        nonce = data[offset:offset + 12]
        ciphertext = data[offset + 12:offset + record_len]
        offset += record_len
        try:
            out += aes.decrypt(nonce, ciphertext, None)
        except Exception:
            continue
    return bytes(out)


def parse_share_key(text):
    """Extract the hex decryption key from a printed share link, or None.

    The CLI prints `... /r/<room>#<64-hex-key>`; the key lives only in the
    fragment, never on the wire.
    """
    match = re.search(r"/r/[^#\s]+#([0-9a-fA-F]{64})", text)
    return match.group(1) if match else None


def terminal_text(page):
    """The viewer terminal's rendered buffer as plain text.

    The viewer renders with xterm.js, whose GPU renderer leaves no text
    in the DOM to scrape; the page exposes window.shellshareText() to
    read the buffer instead.
    """
    return page.evaluate("window.shellshareText ? window.shellshareText() : ''")


def wait_for_terminal_text(page, text, timeout=10000):
    """Wait until the viewer terminal's buffer contains `text`."""
    page.wait_for_function(
        "text => window.shellshareText && window.shellshareText().includes(text)",
        arg=text,
        timeout=timeout,
    )


@dataclass
class SocketListener:
    """
    Connects to a room and captures message/size/usersCount events.

    Every broadcast is encrypted, so a listener reading CLI-driven
    content must be given the room's key (from the share link) via
    `set_key`; then the message accessors decrypt transparently. Without
    a key the accessors return the raw bytes as before, which is what
    raw-WebSocket tests (injecting plaintext directly) want.

    Usage:
        listener = SocketListener(room_id)
        listener.connect()
        # ... run CLI, capture its share-link key ...
        listener.set_key(parse_share_key(stderr))
        msg = listener.wait_for_message(timeout=5, containing="hello")
        listener.disconnect()
    """
    room_id: str
    server_url: str = SERVER_URL

    # Internal state - all protected by _condition
    _ws: object = field(default=None, init=False, repr=False)
    _reader: threading.Thread = field(default=None, init=False, repr=False)
    _messages: list = field(default_factory=list, init=False, repr=False)
    _sizes: list = field(default_factory=list, init=False, repr=False)
    _user_counts: list = field(default_factory=list, init=False, repr=False)
    _broadcasting: list = field(default_factory=list, init=False, repr=False)
    _condition: threading.Condition = field(default_factory=threading.Condition, init=False, repr=False)
    _connected: bool = field(default=False, init=False, repr=False)
    _key: bytes = field(default=None, init=False, repr=False)

    def set_key(self, key_hex):
        """Decrypt subsequent content reads with this hex key (or None to
        read raw bytes). Decryption is lazy, so it's fine to set the key
        after the CLI has started and the messages have arrived."""
        with self._condition:
            self._key = bytes.fromhex(key_hex) if key_hex else None

    def connect(self, wait_for_join=True):
        """Connect to the room's viewer WebSocket.

        The room is the URL - there is no join handshake to confirm.
        The server pushes the room snapshot on connect and always ends
        it with usersCount, so `wait_for_join` waits for that as proof
        the snapshot was delivered."""
        wait_for_server(self.server_url)

        ws_base = self.server_url.replace("http://", "ws://")
        self._ws = websocket.create_connection(
            f"{ws_base}/ws/v/r/{self.room_id}", timeout=30
        )
        self._connected = True
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

        if not wait_for_join:
            return

        with self._condition:
            deadline = time.time() + 10
            while not self._user_counts:
                remaining = deadline - time.time()
                if remaining <= 0 or not self._connected:
                    break
                self._condition.wait(timeout=remaining)
            if self._user_counts:
                return
        # Don't leak the connection: this raise typically happens in
        # fixture setup, where teardown (and its disconnect) never runs
        self.disconnect()
        raise TimeoutError(
            f"viewer connect to room {self.room_id!r} on {self.server_url} "
            f"got no usersCount snapshot"
        )

    def _read_loop(self):
        """Capture frames: binary is terminal bytes, JSON text frames
        carry size/usersCount/broadcasting control events."""
        ws = self._ws
        while True:
            try:
                frame = ws.recv()
            except (websocket.WebSocketException, OSError, ValueError):
                break
            with self._condition:
                if isinstance(frame, bytes):
                    self._messages.append(frame)
                else:
                    try:
                        control = json.loads(frame)
                    except ValueError:
                        continue
                    if "size" in control:
                        self._sizes.append(control["size"])
                    if "usersCount" in control:
                        self._user_counts.append(control["usersCount"])
                    if "broadcasting" in control:
                        self._broadcasting.append(control["broadcasting"])
                self._condition.notify_all()
        with self._condition:
            self._connected = False
            self._condition.notify_all()

    def disconnect(self):
        """Disconnect from the server."""
        if self._ws and self._connected:
            self._connected = False
            try:
                self._ws.close()
            except (websocket.WebSocketException, OSError):
                pass
        if self._reader and self._reader.is_alive():
            self._reader.join(timeout=5)

    def wait_for_message(self, timeout=5, containing=None):
        """
        Wait until the received content (decrypted when a key is set)
        is non-empty, or contains `containing` if given.

        Returns:
            The accumulated decoded text, or None if timeout
        """
        with self._condition:
            start = time.time()
            while True:
                acc = self.get_accumulated_messages_unlocked()
                if acc and (containing is None or containing in acc):
                    return acc
                remaining = timeout - (time.time() - start)
                if remaining <= 0:
                    return None
                self._condition.wait(timeout=remaining)

    def get_accumulated_messages(self) -> str:
        """Get all messages concatenated together (decoded)."""
        with self._condition:
            return self.get_accumulated_messages_unlocked()

    def get_accumulated_messages_unlocked(self) -> str:
        """All messages concatenated and decoded. Caller must hold lock.

        Concatenates at the byte level first, so a UTF-8 sequence split
        across two messages still decodes; when a key is set the whole
        run of records is decrypted before decoding.
        """
        raw = b"".join(bytes(m) for m in self._messages)
        if self._key is not None:
            raw = open_records(self._key, raw)
        return raw.decode("utf-8", errors="replace")

    def get_raw_bytes(self) -> bytes:
        """All received message payloads concatenated, undecoded.

        The decoding accessors above assume plaintext UTF-8; encrypted
        rooms (--encrypt) relay opaque ciphertext records, and
        assertions on them must stay at the byte level.
        """
        with self._condition:
            return b"".join(bytes(m) for m in self._messages)

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
