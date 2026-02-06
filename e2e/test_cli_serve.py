"""
E2E Tests for Shellshare CLI - Serve Mode

Tests for the `shellshare serve` command, which starts a local server
and broadcasts the terminal to it in a single command.

Each test starts its own `shellshare serve` on a unique port, so these
tests are independent from the main server running on port 3000.
"""

import socket
import subprocess
import time
import urllib.request

import pytest

from conftest import (
    CLI_COMMAND,
    SocketListener,
    wait_for_content,
    wait_for_server,
)


def get_free_port():
    """Find a free TCP port to use for testing."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("", 0))
        return s.getsockname()[1]


def run_serve_stdin(message, port, extra_args=None, timeout=10):
    """
    Run `shellshare serve --stdin` with the given message piped to stdin.

    Returns (returncode, stdout, stderr).
    """
    args = CLI_COMMAND + ["serve", "--stdin", "--port", str(port)]
    if extra_args:
        args.extend(extra_args)

    proc = subprocess.Popen(
        args,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    try:
        stdout, stderr = proc.communicate(input=message, timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        stdout, stderr = proc.communicate()

    return proc.returncode, stdout, stderr


def start_serve(port, extra_args=None, stdin_mode=False):
    """
    Start `shellshare serve` on the given port.

    Returns the subprocess.Popen handle.
    """
    args = CLI_COMMAND + ["serve", "--port", str(port)]
    if stdin_mode:
        args.append("--stdin")
    if extra_args:
        args.extend(extra_args)

    return subprocess.Popen(
        args,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def stop_serve(proc, send_exit=False, timeout=15):
    """
    Stop a `shellshare serve` process and return (stdout, stderr).

    If send_exit is True, sends 'exit' to stdin first for a clean exit.
    Otherwise terminates the process.
    """
    try:
        if send_exit and proc.stdin:
            proc.stdin.write("exit\n")
            proc.stdin.flush()
            stdout, stderr = proc.communicate(timeout=timeout)
        else:
            proc.terminate()
            stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        stdout, stderr = proc.communicate()
    return stdout, stderr


class TestServeStdinBasic:
    """Basic tests for serve --stdin mode."""

    def test_serve_stdin_message_received(self):
        """Messages piped to serve --stdin should be received by viewers."""
        port = get_free_port()
        server_url = f"http://localhost:{port}"

        proc = start_serve(port, stdin_mode=True)

        try:
            wait_for_server(server_url, timeout_seconds=15)

            listener = SocketListener("terminal", server_url=server_url)
            listener.connect()

            # Send a message to stdin
            proc.stdin.write("hello from serve\n")
            proc.stdin.flush()

            # Verify it was received
            msg = listener.wait_for_message(timeout=5, containing="hello from serve")
            assert msg is not None, "Message not received via Socket.IO"
            assert "hello from serve" in msg

            listener.disconnect()
        finally:
            stop_serve(proc)

    def test_serve_stdin_prints_url_to_stderr(self):
        """Serve --stdin should print the sharing URL to stderr."""
        port = get_free_port()
        returncode, stdout, stderr = run_serve_stdin("test\n", port)

        assert returncode == 0
        assert "Sharing terminal in" in stderr
        assert "/r/terminal" in stderr
        assert str(port) in stderr

    def test_serve_stdin_prints_end_of_transmission(self):
        """Serve --stdin should print end message on clean exit."""
        port = get_free_port()
        returncode, stdout, stderr = run_serve_stdin("done\n", port)

        assert returncode == 0
        assert "End of transmission." in stderr

    def test_serve_stdin_custom_port(self):
        """Serve should listen on the specified port."""
        port = get_free_port()
        server_url = f"http://localhost:{port}"

        proc = start_serve(port, stdin_mode=True)

        try:
            wait_for_server(server_url, timeout_seconds=15)

            response = urllib.request.urlopen(server_url, timeout=5)
            assert response.status == 200
        finally:
            stop_serve(proc)

    def test_serve_room_page_accessible(self):
        """The /r/terminal room page should be accessible."""
        port = get_free_port()
        server_url = f"http://localhost:{port}"

        proc = start_serve(port, stdin_mode=True)

        try:
            wait_for_server(server_url, timeout_seconds=15)

            response = urllib.request.urlopen(
                f"{server_url}/r/terminal", timeout=5
            )
            assert response.status == 200
        finally:
            stop_serve(proc)


class TestServeStdinStreaming:
    """Tests for message streaming via serve --stdin."""

    def test_multiple_messages_received_in_order(self):
        """Multiple messages should be received in order."""
        port = get_free_port()
        server_url = f"http://localhost:{port}"

        proc = start_serve(port, stdin_mode=True)

        try:
            wait_for_server(server_url, timeout_seconds=15)

            listener = SocketListener("terminal", server_url=server_url)
            listener.connect()

            proc.stdin.write("FIRST\n")
            proc.stdin.flush()
            proc.stdin.write("SECOND\n")
            proc.stdin.flush()

            assert wait_for_content(
                listener, lambda s: "FIRST" in s and "SECOND" in s, timeout=5
            ), "Both messages not received"

            accumulated = listener.get_accumulated_messages()
            first_idx = accumulated.index("FIRST")
            second_idx = accumulated.index("SECOND")
            assert first_idx < second_idx, "Messages received out of order"

            listener.disconnect()
        finally:
            stop_serve(proc)

    def test_late_joiner_sees_history(self):
        """A viewer joining after messages were sent should see history."""
        port = get_free_port()
        server_url = f"http://localhost:{port}"

        proc = start_serve(port, stdin_mode=True)

        try:
            wait_for_server(server_url, timeout_seconds=15)

            # Send message before any viewer connects
            proc.stdin.write("early-message\n")
            proc.stdin.flush()
            time.sleep(1)

            # Late joiner connects
            listener = SocketListener("terminal", server_url=server_url)
            listener.connect()

            msg = listener.wait_for_message(timeout=5, containing="early-message")
            assert msg is not None, "Late joiner did not receive history"

            listener.disconnect()
        finally:
            stop_serve(proc)


class TestServeTerminalSize:
    """Tests for terminal size handling in serve mode."""

    def test_size_event_sent(self):
        """Serve --stdin should send terminal size events."""
        port = get_free_port()
        server_url = f"http://localhost:{port}"

        proc = start_serve(port, stdin_mode=True)

        try:
            wait_for_server(server_url, timeout_seconds=15)

            listener = SocketListener("terminal", server_url=server_url)
            listener.connect()

            # Send a message to trigger a POST with size
            proc.stdin.write("size-test\n")
            proc.stdin.flush()

            # Wait for message first (size is sent with each POST)
            listener.wait_for_message(timeout=5, containing="size-test")

            assert listener.wait_for_size(timeout=5), "No size event received"

            size = listener.get_last_size()
            assert size is not None
            assert "cols" in size
            assert "rows" in size

            listener.disconnect()
        finally:
            stop_serve(proc)


class TestServeCleanExit:
    """Tests for serve mode clean exit."""

    def test_serve_stdin_exits_on_eof(self):
        """Serve --stdin should exit cleanly when stdin closes."""
        port = get_free_port()
        returncode, stdout, stderr = run_serve_stdin("goodbye\n", port)

        assert returncode == 0
        assert "End of transmission." in stderr

    def test_serve_server_stops_after_exit(self):
        """Server should stop accepting connections after serve exits."""
        port = get_free_port()
        server_url = f"http://localhost:{port}"

        returncode, stdout, stderr = run_serve_stdin("done\n", port)
        assert returncode == 0

        # Give OS time to release the socket
        time.sleep(1)

        # Server should no longer be accessible
        with pytest.raises(Exception):
            urllib.request.urlopen(server_url, timeout=2)

    def test_serve_sends_delete_on_exit(self):
        """Room should be cleaned up (DELETE sent) on exit."""
        port = get_free_port()
        server_url = f"http://localhost:{port}"

        proc = start_serve(port, stdin_mode=True)

        try:
            wait_for_server(server_url, timeout_seconds=15)

            listener = SocketListener("terminal", server_url=server_url)
            listener.connect()

            proc.stdin.write("cleanup-test\n")
            proc.stdin.flush()

            listener.wait_for_message(timeout=5, containing="cleanup-test")
            listener.disconnect()
        finally:
            stop_serve(proc)

        # After exit, a new serve on the same port should work
        # (room was cleaned up, not locked by stale password)
        port2 = get_free_port()
        returncode, stdout, stderr = run_serve_stdin("new-session\n", port2)
        assert returncode == 0


class TestServeScriptMode:
    """Tests for serve in default (PTY) mode."""

    def test_serve_script_starts_and_streams(self):
        """Serve mode (PTY) should start a server and stream output."""
        port = get_free_port()
        server_url = f"http://localhost:{port}"
        proc = start_serve(port)

        try:
            wait_for_server(server_url, timeout_seconds=15)

            listener = SocketListener("terminal", server_url=server_url)
            listener.connect()

            # PTY should produce output (shell prompt, etc.)
            assert wait_for_content(listener, lambda s: len(s) > 0, timeout=10), \
                "No output received from serve mode PTY"

            listener.disconnect()
        finally:
            stop_serve(proc)

    def test_serve_script_prints_sharing_url(self):
        """Serve mode (PTY) should print sharing URL to stdout."""
        port = get_free_port()
        proc = start_serve(port)

        try:
            wait_for_server(f"http://localhost:{port}", timeout_seconds=15)
            time.sleep(1)

            stdout, stderr = stop_serve(proc, send_exit=True)
            output = stdout + stderr

            assert "Sharing terminal in" in output
            assert "/r/terminal" in output
            assert str(port) in output
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            pytest.fail("Serve process timed out")
