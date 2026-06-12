"""
E2E Tests for Shellshare CLI - Serve Mode

`shellshare serve` starts a local server and broadcasts the terminal to it,
so a single command shares the terminal without any external server.

Test categories:
- Local server lifecycle (starts, serves pages, dies with the CLI)
- End-to-end broadcasting through the local server
- Port/host configuration
- Error handling (port already in use)
"""

import re
import socket
import subprocess
import urllib.request

import pytest

from conftest import (
    CLI_PATH,
    SocketListener,
    _free_port,
    broadcast_message,
    parse_share_key,
    poll_until,
    random_id,
    wait_for_server,
)


def read_share_key(proc, timeout=10):
    """Read the CLI's printed share link from stderr and return its key."""
    import time as _time

    deadline = _time.time() + timeout
    while _time.time() < deadline:
        line = proc.stderr.readline()
        if not line:
            break
        key = parse_share_key(line)
        if key is not None:
            return key
    return None


def serve_args(port, room, password, host=None):
    """Build the argv for `shellshare serve` in stdin mode."""
    args = [str(CLI_PATH), "serve", "--port", str(port), "--stdin",
            "-r", room, "-W", password]
    if host is not None:
        args.extend(["--host", host])
    return args


def start_serve(port, room, password, host=None):
    """Start `shellshare serve` with stdin held open; caller must finish it."""
    return subprocess.Popen(
        serve_args(port, room, password, host=host),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def finish(proc, message="", timeout=15):
    """Send the message, close stdin and wait for the CLI to exit.

    Kills the process on timeout so a hung CLI doesn't leak and hold its
    port for the rest of the (parallel) test run.
    """
    try:
        stdout, stderr = proc.communicate(input=message, timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        raise
    return proc.returncode, stdout, stderr


def port_open(port):
    """Check whether something is listening on the port on any loopback.

    The default host is "localhost", which may resolve to 127.0.0.1 or ::1
    depending on the machine, so probe both families.
    """
    for family, addr in ((socket.AF_INET, "127.0.0.1"), (socket.AF_INET6, "::1")):
        try:
            with socket.socket(family, socket.SOCK_STREAM) as s:
                s.settimeout(0.5)
                if s.connect_ex((addr, port)) == 0:
                    return True
        except OSError:
            continue
    return False


class TestLocalServerLifecycle:
    """The serve command runs its own HTTP server for the session."""

    def test_serves_viewer_page(self):
        """While serve is running, the room viewer page is reachable."""
        port = _free_port()
        room = f"serve-{random_id()}"
        proc = start_serve(port, room, "pw", host="127.0.0.1")
        try:
            wait_for_server(f"http://127.0.0.1:{port}")
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/r/{room}", timeout=5
            ) as resp:
                assert resp.status == 200
                body = resp.read().decode()
            assert "<html" in body.lower()
        finally:
            returncode, _, stderr = finish(proc)
        assert returncode == 0, f"CLI failed: {stderr}"

    def test_server_stops_when_cli_exits(self):
        """The local server must not outlive the CLI process."""
        port = _free_port()
        proc = start_serve(port, f"serve-{random_id()}", "pw", host="127.0.0.1")
        try:
            wait_for_server(f"http://127.0.0.1:{port}")
        finally:
            returncode, _, _ = finish(proc)
        assert returncode == 0
        assert poll_until(lambda: not port_open(port), timeout=5), (
            "local server still listening after CLI exit"
        )

    def test_prints_local_share_url(self):
        """The share link should point at the local server."""
        port = _free_port()
        room = f"serve-{random_id()}"
        proc = start_serve(port, room, "pw")
        returncode, _, stderr = finish(proc, "hello")
        assert returncode == 0
        assert f"Sharing terminal in http://localhost:{port}/r/{room}" in stderr
        assert "End of transmission." in stderr


class TestBroadcasting:
    """Messages stream end-to-end through the local server."""

    def test_message_received_via_local_server(self):
        """A viewer connected to the local server receives the broadcast."""
        port = _free_port()
        room = f"serve-{random_id()}"
        proc = start_serve(port, room, "pw", host="127.0.0.1")
        listener = SocketListener(room, server_url=f"http://127.0.0.1:{port}")
        try:
            wait_for_server(f"http://127.0.0.1:{port}")
            key = read_share_key(proc)
            assert key is not None, "no decryption key in CLI share link"
            listener.set_key(key)
            listener.connect()
            proc.stdin.write("hello from serve mode")
            proc.stdin.flush()
            received = listener.wait_for_message(
                timeout=10, containing="hello from serve mode"
            )
            assert received is not None, "Message not received by the viewer"
        finally:
            listener.disconnect()
            returncode, _, stderr = finish(proc)
        assert returncode == 0, f"CLI failed: {stderr}"


class TestConfiguration:
    """Host and port are configurable via the CLI."""

    def test_default_port_is_3000(self):
        """Without --port, serve binds localhost:3000."""
        if port_open(3000):
            pytest.skip("port 3000 already in use on this machine")
        proc = subprocess.Popen(
            [str(CLI_PATH), "serve", "--stdin", "-r", "default-port", "-W", "pw"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            assert poll_until(lambda: port_open(3000), timeout=10), (
                "serve did not bind the default port 3000"
            )
        finally:
            returncode, _, stderr = finish(proc)
        assert returncode == 0, f"CLI failed: {stderr}"
        assert "http://localhost:3000/r/default-port" in stderr

    def test_custom_host(self):
        """--host 127.0.0.1 binds and is used in the share URL."""
        port = _free_port()
        room = f"serve-{random_id()}"
        proc = start_serve(port, room, "pw", host="127.0.0.1")
        try:
            wait_for_server(f"http://127.0.0.1:{port}")
        finally:
            returncode, _, stderr = finish(proc)
        assert returncode == 0
        assert f"http://127.0.0.1:{port}/r/{room}" in stderr

    def test_host_0000_share_url_uses_localhost(self):
        """Binding 0.0.0.0 must not leak an unconnectable URL to the user."""
        port = _free_port()
        room = f"serve-{random_id()}"
        proc = start_serve(port, room, "pw", host="0.0.0.0")
        try:
            wait_for_server(f"http://127.0.0.1:{port}")
        finally:
            returncode, _, stderr = finish(proc)
        assert returncode == 0
        assert f"http://localhost:{port}/r/{room}" in stderr
        # Exposing the terminal beyond loopback deserves a heads-up
        assert "WARNING" in stderr

    def test_port_zero_picks_free_port(self):
        """--port 0 asks the OS for a free port; the URL shows the real one."""
        room = f"serve-{random_id()}"
        proc = start_serve(0, room, "pw", host="127.0.0.1")
        try:
            # The share URL is the first thing printed to stderr
            line = proc.stderr.readline()
            match = re.search(rf"http://127\.0\.0\.1:(\d+)/r/{room}", line)
            assert match, f"no share URL with a real port in: {line!r}"
            port = int(match.group(1))
            assert port != 0
            assert poll_until(lambda: port_open(port), timeout=5)
        finally:
            returncode, _, _ = finish(proc)
        assert returncode == 0

    def test_bracketed_ipv6_host(self):
        """--host '[::1]' must not double-bracket the URL or warn."""
        port = _free_port()
        room = f"serve-{random_id()}"
        proc = start_serve(port, room, "pw", host="[::1]")
        returncode, _, stderr = finish(proc, "hello")
        assert returncode == 0, f"CLI failed: {stderr}"
        assert f"http://[::1]:{port}/r/{room}" in stderr
        # ::1 is loopback: no exposure warning, and no broadcast errors
        assert "WARNING" not in stderr
        assert "ERROR" not in stderr

    def test_server_flag_is_ignored_with_warning(self):
        """Passing --server alongside serve warns instead of silently ignoring."""
        port = _free_port()
        room = f"serve-{random_id()}"
        proc = subprocess.Popen(
            serve_args(port, room, "pw", host="127.0.0.1")
            + ["--server", "https://example.com"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        returncode, _, stderr = finish(proc, "hello")
        assert returncode == 0
        assert "WARNING: --server is ignored" in stderr
        # It must still broadcast to the local server, not example.com
        assert f"http://127.0.0.1:{port}/r/{room}" in stderr


class TestRoomClaiming:
    """Serve mode claims its room before any output is shared."""

    def test_room_claimed_before_first_output(self):
        """Once the share URL is printed, nobody else can take the room."""
        port = _free_port()
        room = f"serve-{random_id()}"
        proc = start_serve(port, room, "pw", host="127.0.0.1")
        try:
            # The share URL is printed after the claim, so reading it
            # guarantees the room is already owned
            line = proc.stderr.readline()
            assert f"/r/{room}" in line, f"unexpected first stderr line: {line!r}"

            status = broadcast_message(
                f"http://127.0.0.1:{port}", room, "wrong-password", "hijack"
            )
            assert status == 401, (
                f"room was claimable by another password (got {status})"
            )
        finally:
            returncode, _, stderr = finish(proc, "hello")
        assert returncode == 0, f"CLI failed: {stderr}"


class TestErrorHandling:
    """Bind failures are reported up front, before sharing starts."""

    def test_port_in_use_fails_fast(self):
        """If the port is taken, serve exits non-zero with a clear error."""
        port = _free_port()
        blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        blocker.bind(("127.0.0.1", port))
        blocker.listen(1)
        try:
            proc = start_serve(port, f"serve-{random_id()}", "pw",
                               host="127.0.0.1")
            returncode, _, stderr = finish(proc)
        finally:
            blocker.close()
        assert returncode != 0
        assert "could not start local server" in stderr
        # It must fail before claiming to share anything
        assert "Sharing terminal in" not in stderr
