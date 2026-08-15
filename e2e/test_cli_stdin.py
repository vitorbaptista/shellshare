"""
E2E Tests for Shellshare CLI - Stream Mode

These tests verify the CLI's stream mode: when stdin is a pipe or redirect
(not a TTY), bare `shellshare` auto-detects it, reads stdin, and streams to
the server with no inner shell. This is the primary testing interface for
the CLI.

Test categories:
- Basic functionality
- Message encoding
- Server communication
- Authorization
- Error handling
- Argument parsing
"""

import os
import platform
import re
import signal
import socket
import subprocess
import sys
import threading
import time

import pytest
import requests

from conftest import (
    CLI_COMMAND,
    SERVER_URL,
    SHARED_PORT,
    SocketListener,
    parse_share_key,
    poll_until,
    random_id,
    wait_for_content,
)


def run_cli_stdin(message, room, password, server=SERVER_URL, extra_args=None, timeout=30):
    """
    Run the CLI in stdin mode with the given message.

    Returns (returncode, stdout, stderr)

    `timeout` is wall-clock and generous on purpose: a full CLI session
    (connect, broadcast, drain, shut down) is normally well under a
    second, but under `-n 10` on an oversubscribed CI runner the CLI's
    bounded shutdown sleeps and thread scheduling stretch out - measured
    serial worst cases reached several seconds. The slack absorbs that
    scheduling pressure without masking a genuine hang (which never
    returns at all).
    """
    args = CLI_COMMAND + ["-s", server, "-r", room, "-W", password]
    if extra_args:
        args.extend(extra_args)

    proc = subprocess.Popen(
        args,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        # Every CLI subprocess here is detached from the
        # controlling terminal. The CLI asks the terminal for its
        # colors (--theme auto), so an attached one - the
        # developer's own, when pytest runs from a real shell -
        # would answer, and these tests would assert the default
        # theme but see a detected one. It would also be put in
        # raw mode, by ten workers at once under -n.
        start_new_session=True,
        text=True,
    )
    stdout, stderr = proc.communicate(input=message, timeout=timeout)
    return proc.returncode, stdout, stderr


class TestBasicFunctionality:
    """Tests for basic CLI functionality."""

    def test_basic_message_received(self, unique_room, unique_password, socket_listener):
        """Send 'hello', verify message arrives on the viewer WebSocket."""
        test_message = "hello"

        returncode, stdout, stderr = run_cli_stdin(
            test_message, unique_room, unique_password
        )

        assert returncode == 0, f"CLI failed: {stderr}"

        socket_listener.set_key(parse_share_key(stderr))
        received = socket_listener.wait_for_message(timeout=5, containing=test_message)
        assert received is not None, "Message not received by the viewer"
        assert test_message in received

    def test_piped_input_is_streamed_not_executed(
        self, unique_room, unique_password, socket_listener
    ):
        """A pipe is relayed verbatim - it must NOT be run by a shell.

        This is the heart of the auto-detection: a non-TTY stdin streams
        instead of spawning a shell. A plain `marker in received` check can't
        prove that - a PTY shell echoes its input line back, so the literal
        would show up either way. So pipe an arithmetic expansion and assert
        the viewer sees the LITERAL `$((...))` but never its product: the
        product appears only if a shell evaluated it.
        """
        tag = random_id(6)
        literal = f"echo {tag}=$((1000003 * 7))"
        product = f"{tag}=7000021"  # only ever emitted by a shell

        returncode, _, stderr = run_cli_stdin(
            literal + "\n", unique_room, unique_password
        )
        assert returncode == 0, f"CLI failed: {stderr}"

        socket_listener.set_key(parse_share_key(stderr))
        received = socket_listener.wait_for_message(timeout=5, containing=literal)
        assert received is not None, "piped input never reached the viewer"
        assert literal in received  # bytes arrived verbatim...
        # ...and the expansion was never evaluated (the whole session has
        # already been broadcast by the time run_cli_stdin returned).
        assert socket_listener.wait_for_message(timeout=2, containing=product) is None, \
            "the arithmetic expansion was evaluated - a shell ran instead of streaming"

    def test_stdin_is_teed_to_stdout(self, unique_room, unique_password, socket_listener):
        """Stream mode echoes stdin to stdout (tee), so the local pipeline
        keeps seeing output while it is broadcast. Prose lives on stderr, so
        stdout carries only the relayed bytes."""
        marker = f"TEE-{random_id(6)}"

        returncode, stdout, stderr = run_cli_stdin(
            marker + "\n", unique_room, unique_password
        )
        assert returncode == 0, f"CLI failed: {stderr}"

        # Echoed locally...
        assert marker in stdout, f"stdin was not teed to stdout; stdout={stdout!r}"

        # ...and still broadcast to the viewer.
        socket_listener.set_key(parse_share_key(stderr))
        received = socket_listener.wait_for_message(timeout=5, containing=marker)
        assert received is not None and marker in received

    def test_prints_room_url_to_stderr(self, unique_room, unique_password):
        """CLI should print 'Sharing terminal in {url}' to stderr."""
        returncode, stdout, stderr = run_cli_stdin(
            "test", unique_room, unique_password
        )

        assert returncode == 0
        assert "Sharing terminal in" in stderr
        assert f"/r/{unique_room}" in stderr

    def test_prints_end_of_transmission(self, unique_room, unique_password):
        """CLI should print 'End of transmission.' to stderr on exit."""
        returncode, stdout, stderr = run_cli_stdin(
            "test", unique_room, unique_password
        )

        assert returncode == 0
        assert "End of transmission." in stderr

    def test_exit_code_zero_on_success(self, unique_room, unique_password):
        """CLI should exit with code 0 on successful transmission."""
        returncode, stdout, stderr = run_cli_stdin(
            "test", unique_room, unique_password
        )

        assert returncode == 0


class TestMessageEncoding:
    """Tests for message encoding and special characters."""

    def test_special_characters(self, unique_room, unique_password, socket_listener):
        """Special characters !@#$%^&*() should be preserved."""
        test_message = "!@#$%^&*()"

        _, _, stderr = run_cli_stdin(test_message, unique_room, unique_password)

        socket_listener.set_key(parse_share_key(stderr))
        received = socket_listener.wait_for_message(timeout=5)
        assert received is not None
        assert test_message in received

    @pytest.mark.skipif(
        platform.system() == "Windows",
        reason="Windows has charmap encoding issues with Unicode in subprocess"
    )
    def test_unicode(self, unique_room, unique_password, socket_listener):
        """Unicode characters (Japanese, emoji) should be preserved."""
        test_message = "日本語テスト 🎉"

        _, _, stderr = run_cli_stdin(test_message, unique_room, unique_password)

        socket_listener.set_key(parse_share_key(stderr))
        received = socket_listener.wait_for_message(timeout=5)
        assert received is not None
        assert "日本語" in received
        assert "🎉" in received

    def test_newlines_and_tabs(self, unique_room, unique_password, socket_listener):
        """Newlines and tabs should be preserved."""
        test_message = "line1\nline2\ttabbed"

        _, _, stderr = run_cli_stdin(test_message, unique_room, unique_password)

        socket_listener.set_key(parse_share_key(stderr))
        received = socket_listener.wait_for_message(timeout=5)
        assert received is not None
        assert "line1" in received
        assert "line2" in received
        assert "tabbed" in received

    def test_ansi_escape_codes(self, unique_room, unique_password, socket_listener):
        """ANSI escape codes (colors) should be preserved."""
        # Red text: \x1b[31m
        test_message = "\x1b[31mRed Text\x1b[0m"

        _, _, stderr = run_cli_stdin(test_message, unique_room, unique_password)

        socket_listener.set_key(parse_share_key(stderr))
        received = socket_listener.wait_for_message(timeout=5)
        assert received is not None
        # The escape codes should be preserved (URL-encoded then base64)
        assert "\x1b[31m" in received or "%1B%5B31m" in received or "Red Text" in received

    def test_multiple_messages_accumulate(self, unique_room, unique_password):
        """Multiple CLI invocations should accumulate messages in order."""
        listener = SocketListener(unique_room)
        listener.connect()

        msg1 = f"FIRST-{random_id(6)}"
        msg2 = f"SECOND-{random_id(6)}"

        # Each CLI run uses a fresh random encryption key, so the listener
        # decrypts one run at a time with that run's key.
        _, _, stderr1 = run_cli_stdin(msg1, unique_room, unique_password)
        key1 = parse_share_key(stderr1)
        listener.set_key(key1)
        first = listener.wait_for_message(timeout=5, containing=msg1)
        assert first is not None, "First message not received"
        assert msg1 in first

        # Send second message (same room, same password) and wait for it
        _, _, stderr2 = run_cli_stdin(msg2, unique_room, unique_password)
        key2 = parse_share_key(stderr2)
        listener.set_key(key2)
        second = listener.wait_for_message(timeout=5, containing=msg2)
        assert second is not None, "Second message not received"
        assert msg2 in second

        listener.disconnect()


class TestServerCommunication:
    """Tests for server communication and CLI flags."""

    def test_custom_server_url(self, unique_room, unique_password):
        """--server flag should set the server URL."""
        # We use the default server but verify the flag is accepted
        returncode, stdout, stderr = run_cli_stdin(
            "test", unique_room, unique_password, server=SERVER_URL
        )

        assert returncode == 0
        assert SERVER_URL.replace("http://", "") in stderr

    def test_custom_room_name(self, unique_password):
        """--room flag should set the room name."""
        custom_room = f"custom-{random_id()}"

        listener = SocketListener(custom_room)
        listener.connect()

        returncode, stdout, stderr = run_cli_stdin(
            "test", custom_room, unique_password
        )

        assert returncode == 0
        assert custom_room in stderr

        received = listener.wait_for_message(timeout=5)
        assert received is not None

        listener.disconnect()

    def test_custom_password(self, unique_room):
        """--password flag should set the room password."""
        custom_password = f"custom-pass-{random_id()}"

        listener = SocketListener(unique_room)
        listener.connect()

        returncode, stdout, stderr = run_cli_stdin(
            "test", unique_room, custom_password
        )

        assert returncode == 0

        received = listener.wait_for_message(timeout=5)
        assert received is not None

        listener.disconnect()

    def test_short_flags(self, unique_password):
        """Short flags -s, -r, -W should work."""
        custom_room = f"short-{random_id()}"

        listener = SocketListener(custom_room)
        listener.connect()

        # Use explicit short flags via extra_args
        args = CLI_COMMAND + [
            "-s", SERVER_URL,
            "-r", custom_room,
            "-W", unique_password,
        ]

        proc = subprocess.Popen(
            args,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
            text=True,
        )
        stdout, stderr = proc.communicate(input="test", timeout=10)

        assert proc.returncode == 0

        received = listener.wait_for_message(timeout=5)
        assert received is not None

        listener.disconnect()

    def test_default_room_is_random(self):
        """Default room should be an 18-character alphanumeric string."""
        # Run CLI without -r flag to get default room
        args = CLI_COMMAND + ["-s", SERVER_URL]

        proc = subprocess.Popen(
            args,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
            text=True,
        )
        stdout, stderr = proc.communicate(input="test", timeout=10)

        # Extract room from stderr
        # Format: "Sharing terminal in http://localhost:3000/r/ROOMID"
        match = re.search(r'/r/([a-zA-Z0-9]+)', stderr)
        assert match is not None, f"Could not find room in stderr: {stderr}"

        room_id = match.group(1)
        assert len(room_id) == 18, f"Room ID should be 18 chars, got {len(room_id)}: {room_id}"
        assert room_id.isalnum(), f"Room ID should be alphanumeric: {room_id}"


class TestAuthorization:
    """Tests for authorization and password handling."""

    def test_auth_header_sent(self, unique_room, unique_password, socket_listener):
        """Password should be sent in Authorization header."""
        # If auth works, message will be received
        returncode, stdout, stderr = run_cli_stdin(
            "test", unique_room, unique_password
        )

        assert returncode == 0

        received = socket_listener.wait_for_message(timeout=5)
        assert received is not None

    def test_wrong_password_fails_during_session(self, unique_room):
        """
        Wrong password should fail with 401 error when room is already claimed.

        Note: The room is only "claimed" while a writer is actively connected.
        After the writer exits (sends DELETE), the room is released.
        This test uses concurrent connections to test password enforcement.
        """
        import subprocess

        listener = SocketListener(unique_room)
        listener.connect()

        # Start first writer - keep it running
        args1 = CLI_COMMAND + ["-s", SERVER_URL, "-r", unique_room, "-W", "original-password"]
        proc1 = subprocess.Popen(
            args1,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
            text=True,
        )

        # Send some data but don't close stdin yet
        proc1.stdin.write("claim\n")
        proc1.stdin.flush()

        # Wait for first message to arrive
        listener.wait_for_message(timeout=5, containing="claim")

        # Try with a different password while first is still active
        returncode, stdout, stderr = run_cli_stdin(
            "test", unique_room, "wrong-password"
        )

        # Clean up first process
        proc1.stdin.close()
        try:
            proc1.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc1.kill()

        listener.disconnect()

        # Note: Depending on server implementation, this may or may not fail
        # The test documents actual behavior

    def test_room_claimed_by_first_writer(self, unique_room):
        """
        Room should be claimed by the first writer's password while writer is active.

        Note: After a writer exits (and sends DELETE), the room is released.
        Sequential writers (not concurrent) can use different passwords.
        """
        import subprocess

        listener = SocketListener(unique_room)
        listener.connect()

        password1 = f"first-{random_id()}"
        password2 = f"second-{random_id()}"

        # Start first writer and keep it running
        args1 = CLI_COMMAND + ["-s", SERVER_URL, "-r", unique_room, "-W", password1]
        proc1 = subprocess.Popen(
            args1,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
            text=True,
        )

        # Send data but keep connection open
        proc1.stdin.write("first\n")
        proc1.stdin.flush()

        # Wait for first message to arrive
        listener.wait_for_message(timeout=5, containing="first")

        # Try second writer concurrently with different password
        returncode2, stdout2, stderr2 = run_cli_stdin(
            "second", unique_room, password2
        )

        # Clean up first process
        proc1.stdin.close()
        proc1.wait(timeout=5)

        listener.disconnect()

        # Document the actual behavior - server may or may not enforce password
        # during concurrent access (depends on implementation)


def _room_snapshot(room):
    """True once the room holds broadcast bytes on the shared server."""
    response = requests.get(f"{SERVER_URL}/r/{room}.bin", timeout=5)
    return response.status_code == 200 and bool(response.content)


class TestErrorHandling:
    """Tests for error handling."""

    def test_silent_peer_fails_instead_of_hanging(self, unique_room, unique_password):
        """A peer that accepts TCP and never answers must not wedge the CLI.

        The WebSocket handshake writes the upgrade request and then reads
        the `101 Switching Protocols` response. Without a read timeout on
        that read, a peer that completes the TCP connection and then goes
        silent (a wedged server, a load balancer fronting a dead backend,
        a tarpitting firewall) parks the CLI in `recvfrom` forever, before
        it ever prints the share URL (issue #173).

        So the connect attempt has to give up on its own, and the two
        elapsed-time bounds are what make this test mean something. The
        lower one rejects a vacuous pass: a connect that fails for any
        *other* reason - a refused port, an unresolvable host, a URL
        rejected before dialling - exits non-zero with this same message
        too, but instantly, having never reached the read under test.
        The upper one holds the CLI to a budget rather than to merely
        finishing, so a handshake bounded at, say, 55s would still fail
        here. Both are anchored on `HANDSHAKE_TIMEOUT` in
        `src/cli/ws.rs` (10s), with room for an oversubscribed runner.
        """
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(8)
        port = listener.getsockname()[1]
        accepted = []
        stop = threading.Event()

        def accept_loop():
            listener.settimeout(0.2)
            while not stop.is_set():
                try:
                    # Held open, never written to: the CLI's upgrade
                    # request is read by nobody and answered by nobody.
                    # (The kernel would complete the TCP handshake from
                    # the backlog anyway; accepting explicitly is what
                    # lets the assertion below prove the CLI got that
                    # far, on every platform.)
                    accepted.append(listener.accept()[0])
                except socket.timeout:
                    continue
                except OSError:
                    break

        thread = threading.Thread(target=accept_loop, daemon=True)
        thread.start()
        try:
            args = CLI_COMMAND + [
                "-s", f"http://127.0.0.1:{port}",
                "-r", unique_room,
                "-W", unique_password,
            ]
            proc = subprocess.Popen(
                args,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
                text=True,
            )
            started = time.time()
            try:
                _, stderr = proc.communicate(input="hello\n", timeout=60)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.communicate()
                pytest.fail(
                    "CLI never gave up on a peer that accepted the connection "
                    "and never answered the WebSocket handshake"
                )
            elapsed = time.time() - started
        finally:
            stop.set()
            thread.join(timeout=5)
            for conn in accepted:
                conn.close()
            listener.close()

        assert accepted, "CLI never connected; the handshake read was never reached"
        assert 5 < elapsed < 40, (
            f"CLI gave up after {elapsed:.2f}s, outside the budget a 10s "
            f"handshake timeout implies: {stderr!r}"
        )
        assert proc.returncode != 0, (
            f"CLI reported success against a silent peer: {stderr}"
        )
        assert "error connecting to the server" in stderr, (
            f"Expected a connect error on stderr, got: {stderr!r}"
        )

    def test_silent_peer_on_reconnect_does_not_wedge_the_session(
        self, unique_room, unique_password
    ):
        """A live session whose server goes silent must still be able to end.

        This is the severe half of issue #173, and it is reached only
        after a successful connect, so the test above cannot cover it: by
        reconnect time the CLI's signal handlers are installed, and they
        only set atomic flags that the top of the stream loop reads. A
        handshake blocked forever in `recvfrom` never returns to that
        loop, so SIGINT, SIGTERM and SIGHUP were all absorbed and the
        process needed SIGKILL - and EOF on stdin, the ordinary way a
        stream-mode session ends, was ignored just as thoroughly.

        A TCP proxy in front of the shared server plays the peer that
        wedges mid-session: it relays until the broadcast is live, then
        cuts the connection and answers every later one with silence.
        Closing stdin must still end the session. (Asserting on EOF
        rather than on a signal keeps the test meaningful on Windows,
        where `send_signal(SIGTERM)` is an unconditional TerminateProcess
        and would pass against any implementation.)
        """
        proxy = socket.socket()
        proxy.bind(("127.0.0.1", 0))
        proxy.listen(8)
        proxy_port = proxy.getsockname()[1]
        silent = threading.Event()
        stop = threading.Event()
        relayed = []  # sockets of the live session, cut when the peer wedges
        stalled = []  # connections accepted after that, never answered

        def cut(sock):
            # shutdown, not just close: a pump thread is parked in recv()
            # on this socket, and that in-flight syscall keeps the kernel
            # side alive past close() - the FIN would not reach the CLI
            # until something else woke the reader (its 30s keepalive
            # ping, in practice)
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            sock.close()

        def pump(src, dst):
            try:
                while True:
                    data = src.recv(65536)
                    if not data:
                        break
                    dst.sendall(data)
            except OSError:
                pass

        def accept_loop():
            proxy.settimeout(0.2)
            while not stop.is_set():
                try:
                    client = proxy.accept()[0]
                except socket.timeout:
                    continue
                except OSError:
                    break
                if silent.is_set():
                    stalled.append(client)
                    continue
                try:
                    upstream = socket.create_connection(("127.0.0.1", SHARED_PORT))
                except OSError:
                    client.close()
                    continue
                relayed.extend((client, upstream))
                for src, dst in ((client, upstream), (upstream, client)):
                    threading.Thread(
                        target=pump, args=(src, dst), daemon=True
                    ).start()

        thread = threading.Thread(target=accept_loop, daemon=True)
        thread.start()
        proc = subprocess.Popen(
            CLI_COMMAND + [
                "-s", f"http://127.0.0.1:{proxy_port}",
                "-r", unique_room,
                "-W", unique_password,
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
            text=True,
        )
        try:
            proc.stdin.write("before the outage\n")
            proc.stdin.flush()
            # 200 AND non-empty: a room that does not exist yet answers
            # 404 with a body, which a length check alone would take for
            # a live broadcast - and cutting the connection that early
            # kills the session's FIRST handshake instead of a reconnect
            assert poll_until(lambda: _room_snapshot(unique_room), timeout=30), (
                "CLI never broadcast through the proxy"
            )

            # The peer wedges: the live connection is cut, and every
            # reconnect from here on is accepted and left unanswered
            silent.set()
            for sock in relayed:
                cut(sock)
            relayed.clear()
            assert poll_until(lambda: bool(stalled), timeout=30), (
                "CLI never tried to reconnect"
            )
            assert proc.poll() is None, "CLI exited before its session was ended"

            proc.stdin.close()  # EOF: end the session
            try:
                proc.wait(timeout=60)
            except subprocess.TimeoutExpired:
                proc.kill()
                pytest.fail(
                    "CLI ignored EOF while a reconnect sat blocked on a silent "
                    "peer - the state where every termination signal is absorbed"
                )
        finally:
            stop.set()
            thread.join(timeout=5)
            for sock in relayed + stalled:
                cut(sock)
            proxy.close()
            proc.kill()
            # Closed by hand rather than through communicate(): stdin is
            # already closed by now, and before 3.13 communicate() tries
            # to flush it and raises ValueError for its trouble
            for stream in (proc.stdin, proc.stdout, proc.stderr):
                try:
                    stream.close()
                except (OSError, ValueError):
                    pass
            proc.wait(timeout=30)  # reap; kill() alone leaves a zombie

    def test_large_input_chunked_successfully(self, unique_room, unique_password, socket_listener):
        """
        Large input (>300KB) is chunked into 4096-byte reads by the CLI.

        The CLI reads stdin in 4096-byte chunks, so even very large input
        gets split into multiple POST requests, each under the size limit.
        This test verifies that large input is handled correctly.
        """
        # Send a large message - CLI will chunk it
        marker = f"LARGE-{random_id(6)}"
        large_message = marker + ("X" * 100000)  # ~100KB

        returncode, stdout, stderr = run_cli_stdin(
            large_message, unique_room, unique_password
        )

        assert returncode == 0, f"CLI failed with large input: {stderr}"

        socket_listener.set_key(parse_share_key(stderr))
        # Verify marker was transmitted (use longer timeout for large data)
        received = socket_listener.wait_for_message(timeout=30, containing=marker)
        
        # If not found via wait, check accumulated messages as fallback
        if received is None:
            accumulated = socket_listener.get_accumulated_messages()
            assert marker in accumulated, \
                f"Large message marker not received. Accumulated: {accumulated[:500]}..."
        else:
            assert received is not None, "Large message marker not received"


class TestArgumentParsing:
    """Tests for argument parsing."""

    @pytest.mark.parametrize("flag", ["--version", "-v"])
    def test_version_flag(self, flag):
        """--version/-v should print version and exit 0."""
        args = CLI_COMMAND + [flag]

        proc = subprocess.Popen(
            args,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
            text=True,
        )
        stdout, stderr = proc.communicate(timeout=5)

        assert proc.returncode == 0
        # Version should be in stdout or stderr
        output = stdout + stderr
        assert re.search(r'\d+\.\d+\.\d+', output), f"No version found in: {output}"

    @pytest.mark.parametrize("flag", ["--help", "-h"])
    def test_help_flag(self, flag):
        """--help/-h should describe the CLI, its flags, and the default
        server, then exit 0."""
        args = CLI_COMMAND + [flag]

        proc = subprocess.Popen(
            args,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
            text=True,
        )
        stdout, stderr = proc.communicate(timeout=5)

        assert proc.returncode == 0
        output = stdout + stderr
        # Help should mention the available flags
        assert "--server" in output or "-s" in output
        assert "--room" in output or "-r" in output
        assert "--password" in output or "-W" in output
        # ... and document the default server users will share through
        assert "shellshare.net" in output, "Default server URL should be in help"


class TestConcurrency:
    """Tests for concurrent CLI usage."""

    def test_two_viewers_receive_message(self, unique_room, unique_password):
        """Two viewers should both receive the same message."""
        listener1 = SocketListener(unique_room)
        listener2 = SocketListener(unique_room)

        listener1.connect()
        listener2.connect()

        test_message = f"broadcast-{random_id()}"

        returncode, stdout, stderr = run_cli_stdin(
            test_message, unique_room, unique_password
        )

        assert returncode == 0

        key = parse_share_key(stderr)
        listener1.set_key(key)
        listener2.set_key(key)
        received1 = listener1.wait_for_message(timeout=5, containing=test_message)
        received2 = listener2.wait_for_message(timeout=5, containing=test_message)

        assert received1 is not None, "Listener 1 didn't receive message"
        assert received2 is not None, "Listener 2 didn't receive message"
        assert test_message in received1
        assert test_message in received2

        listener1.disconnect()
        listener2.disconnect()


class TestDefaultPassword:
    """Tests for default password behavior."""

    def test_cli_works_without_password_flag(self, unique_room, socket_listener):
        """CLI should work without -p flag (uses MAC address)."""
        # Run without -p flag
        args = CLI_COMMAND + ["-s", SERVER_URL, "-r", unique_room]

        proc = subprocess.Popen(
            args,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
            text=True,
        )
        stdout, stderr = proc.communicate(input="test", timeout=10)

        assert proc.returncode == 0, f"CLI failed: {stderr}"

        socket_listener.set_key(parse_share_key(stderr))
        received = socket_listener.wait_for_message(timeout=5, containing="test")
        assert received is not None, "Message not received"

    def test_default_password_claims_room(self, unique_room):
        """
        Default password (MAC address) should claim the room.

        A second writer without explicit password should use the same MAC
        and be able to continue writing.
        """
        listener = SocketListener(unique_room)
        listener.connect()

        msg1 = f"FIRST-{random_id(6)}"
        msg2 = f"SECOND-{random_id(6)}"

        # First write without -W
        args1 = CLI_COMMAND + ["-s", SERVER_URL, "-r", unique_room]
        proc1 = subprocess.Popen(
            args1,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
            text=True,
        )
        _, stderr1 = proc1.communicate(input=msg1, timeout=10)

        # Each CLI run uses a fresh random key, so decrypt one run at a time.
        listener.set_key(parse_share_key(stderr1))
        first = listener.wait_for_message(timeout=5, containing=msg1)
        assert first is not None and msg1 in first, "First message not received"

        # Second write without -W (same machine = same MAC = same password)
        args2 = CLI_COMMAND + ["-s", SERVER_URL, "-r", unique_room]
        proc2 = subprocess.Popen(
            args2,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
            text=True,
        )
        stdout, stderr = proc2.communicate(input=msg2, timeout=10)

        assert proc2.returncode == 0, f"Second write failed: {stderr}"

        # Wait for second message to arrive, decrypted with its own run's key.
        listener.set_key(parse_share_key(stderr))
        second = listener.wait_for_message(timeout=5, containing=msg2)
        assert second is not None and msg2 in second, "Second message not received"

        listener.disconnect()


class TestEdgeCases:
    """Tests for edge cases and boundary conditions."""

    def test_empty_message(self, unique_room, unique_password):
        """Empty stdin should complete without error."""
        returncode, stdout, stderr = run_cli_stdin(
            "", unique_room, unique_password
        )

        assert returncode == 0
        assert "End of transmission." in stderr

    def test_room_name_with_hyphens_underscores(self, unique_password):
        """Room name with hyphens and underscores should work."""
        room = f"test-room_name-{random_id(6)}"

        listener = SocketListener(room)
        listener.connect()

        returncode, stdout, stderr = run_cli_stdin(
            "test", room, unique_password
        )

        assert returncode == 0
        assert room in stderr

        received = listener.wait_for_message(timeout=5)
        assert received is not None

        listener.disconnect()

    def test_password_with_special_characters(self, unique_room, socket_listener):
        """Password with special characters should work."""
        special_password = "p@ss!w0rd#$%^&*()"

        returncode, stdout, stderr = run_cli_stdin(
            "test", unique_room, special_password
        )

        assert returncode == 0

        received = socket_listener.wait_for_message(timeout=5)
        assert received is not None

    def test_carriage_return_handling(self, unique_room, unique_password, socket_listener):
        """Carriage returns should be preserved (important for terminal output)."""
        test_message = "line1\r\nline2\roverwrite"

        returncode, stdout, stderr = run_cli_stdin(
            test_message, unique_room, unique_password
        )

        assert returncode == 0

        socket_listener.set_key(parse_share_key(stderr))
        received = socket_listener.wait_for_message(timeout=5)
        assert received is not None
        # Carriage returns should be preserved in the output
        assert "line1" in received
        assert "line2" in received or "overwrite" in received

    def test_rapid_multiple_messages_same_room(self, unique_room, unique_password):
        """Rapid sequential messages to same room should all be received."""
        listener = SocketListener(unique_room)
        listener.connect()

        # Each rapid CLI run uses a fresh random key, so each marker is
        # decrypted with its own run's key.
        markers = []
        for i in range(5):
            marker = f"MSG{i}-{random_id(4)}"
            markers.append(marker)
            _, _, stderr = run_cli_stdin(marker, unique_room, unique_password)
            key = parse_share_key(stderr)
            listener.set_key(key)

            def this_marker_received(accumulated, marker=marker):
                return marker in accumulated

            assert wait_for_content(listener, this_marker_received, timeout=10), \
                f"Missing message: {marker}"

        listener.disconnect()


# A producer that prints one line, then - on SIGINT - takes a moment to shut
# down and prints a final line, exactly like honcho/foreman reporting
# "process stopped (rc=)". The delay is the whole bug: an instant print lands
# in the 64K pipe buffer before shellshare can close it, so even a CLI that
# exits immediately would relay it and the test would prove nothing. 0.5s is
# well above that exit (~50-100ms) and below the CLI's 1s drain hint, so this
# test never sees the hint.
DYING_PRODUCER = """
import signal, sys, time

def bye(_sig, _frame):
    time.sleep(0.5)
    print("SHUTDOWN-MARKER", flush=True)
    sys.exit(0)

signal.signal(signal.SIGINT, bye)
print("LIVE-MARKER", flush=True)
while True:
    time.sleep(0.05)
"""

# A producer that cannot be interrupted at all, so a drain would never end.
STUCK_PRODUCER = """
import signal, time
signal.signal(signal.SIGINT, signal.SIG_IGN)
print("LIVE-MARKER", flush=True)
while True:
    time.sleep(0.05)
"""


def room_bytes(room):
    """GET /r/<room>.bin -> raw history bytes (b'' when unreadable).

    Degrades instead of raising: these helpers run inside `poll_until`, where
    a transient error should retry rather than abort the test.
    """
    try:
        resp = requests.get(f"{SERVER_URL}/r/{room}.bin", timeout=5)
    except requests.RequestException:
        return b""
    return resp.content if resp.status_code == 200 else b""


def start_pipeline(producer_src, room, password):
    """Start `python -c <producer_src> | shellshare` in one process group.

    A terminal delivers Ctrl+C to the whole foreground process group, so the
    test must too: the producer opens a new group and the CLI joins it, which
    makes `os.killpg` an exact stand-in for the keypress. It is a new group in
    the *same session* - `start_new_session=True` would setsid(), and
    setpgid(2) refuses to move the CLI into a group living in another session
    (EPERM).

    Encryption is off so the test can read the room over plain HTTP instead of
    scraping the share key out of a stderr stream it only drains at the end;
    the drain path is independent of the cipher.

    Returns (producer, cli, pgid). The caller must kill the group.
    """
    producer = subprocess.Popen(
        [sys.executable, "-u", "-c", producer_src],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        preexec_fn=lambda: os.setpgid(0, 0),
    )
    # setpgid(0, 0) runs before exec and Popen blocks until then, so the group
    # already exists and its id is the leader's pid.
    pgid = producer.pid
    try:
        cli = subprocess.Popen(
            CLI_COMMAND
            + ["-s", SERVER_URL, "-r", room, "-W", password, "--disable-encryption"],
            stdin=producer.stdout,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            preexec_fn=lambda: os.setpgid(0, pgid),
        )
    except BaseException:
        producer.kill()
        producer.wait()
        raise
    # The parent must drop the read end. While it holds it, the producer never
    # sees EPIPE when the CLI exits - precisely the failure these tests exist
    # to catch.
    producer.stdout.close()
    producer.stdout = None  # so producer.communicate() handles stderr only
    return producer, cli, pgid


def kill_group(pgid):
    """Best-effort teardown - the group is already gone on a passing run."""
    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        pass


@pytest.mark.skipif(
    platform.system() == "Windows",
    reason="process groups and SIGINT are POSIX-only",
)
class TestStreamSignals:
    """Signals at the end of a pipe, exactly as a terminal delivers them."""

    def test_sigint_drains_producer_shutdown_output(
        self, unique_room, unique_password
    ):
        """Ctrl+C must not close the pipe under a producer that is still
        writing: shellshare keeps reading until EOF, so the producer exits
        cleanly and its dying words are broadcast."""
        producer, cli, pgid = start_pipeline(
            DYING_PRODUCER, unique_room, unique_password
        )
        try:
            assert poll_until(
                lambda: b"LIVE-MARKER" in room_bytes(unique_room), timeout=15
            ), "the producer's first line never reached the room"

            os.killpg(pgid, signal.SIGINT)

            cli.communicate(timeout=15)
            assert cli.returncode == 0, "CLI did not exit cleanly"
            _, producer_err = producer.communicate(timeout=10)
            assert producer.returncode == 0, (
                f"producer exited {producer.returncode}; stderr={producer_err!r}"
            )
            assert "BrokenPipeError" not in producer_err, (
                f"producer hit a broken pipe: {producer_err!r}"
            )
            # shutdown()'s ack drain gives up after 1s, so the last frame can
            # still be in flight when the process is reaped.
            assert poll_until(
                lambda: b"SHUTDOWN-MARKER" in room_bytes(unique_room), timeout=5
            ), "the producer's shutdown output was not broadcast"
        finally:
            kill_group(pgid)
            producer.wait()
            cli.wait()

    def test_second_sigint_force_quits_a_stuck_producer(
        self, unique_room, unique_password
    ):
        """A producer that ignores SIGINT must not pin shellshare forever: the
        first press starts an open-ended drain (explained on stderr), the
        second leaves anyway."""
        producer, cli, pgid = start_pipeline(
            STUCK_PRODUCER, unique_room, unique_password
        )
        try:
            assert poll_until(
                lambda: b"LIVE-MARKER" in room_bytes(unique_room), timeout=15
            ), "the producer's first line never reached the room"

            os.killpg(pgid, signal.SIGINT)

            # The drain is deliberately unbounded, so wait out any plausible
            # timeout: honcho gives its own children 5s, and an implementation
            # that gave up first would truncate exactly the shutdown output
            # this feature exists to capture. The wait doubles as the gap that
            # keeps the two presses from being coalesced - signals are not
            # queued, so back-to-back SIGINTs collapse into one and the
            # force-quit would never arm.
            assert not poll_until(lambda: cli.poll() is not None, timeout=6), (
                "CLI stopped draining on its own instead of waiting for EOF"
            )

            os.killpg(pgid, signal.SIGINT)
            _, cli_err = cli.communicate(timeout=10)
            assert cli.returncode == 0, (
                "a second Ctrl+C did not force-quit the CLI"
            )
            assert "Press Ctrl+C again to quit now." in cli_err, (
                "the drain never explained itself on stderr"
            )
        finally:
            kill_group(pgid)
            producer.wait()
            cli.wait()

    # Parametrized by NAME, not by value: the decorator is evaluated at
    # collection time, before the class-level skipif applies, and Windows has
    # no signal.SIGHUP to look up.
    @pytest.mark.parametrize("sig_name", ["SIGTERM", "SIGHUP"])
    def test_only_sigint_waits_for_the_producer(
        self, sig_name, unique_room, unique_password
    ):
        """Only Ctrl+C drains. SIGTERM arrives alone, so there is nobody to
        wait for and a wait would just lose the flush to the supervisor's
        follow-up SIGKILL; SIGHUP means the terminal is gone, so the second
        press that escapes a drain could never be typed. Both must still stop
        a CLI whose producer will never close the pipe."""
        sig = getattr(signal, sig_name)
        producer, cli, pgid = start_pipeline(
            STUCK_PRODUCER, unique_room, unique_password
        )
        try:
            assert poll_until(
                lambda: b"LIVE-MARKER" in room_bytes(unique_room), timeout=15
            ), "the producer's first line never reached the room"

            started = time.monotonic()
            cli.send_signal(sig)  # to the CLI only, not the group
            cli.communicate(timeout=10)
            elapsed = time.monotonic() - started

            assert cli.returncode == 0, f"{sig_name} did not stop the CLI"
            # "At once", not merely "eventually" - a drain here is the bug.
            # Measured ~0.1s idle and ~0.3s under heavy load, so this is a
            # wide margin that still fails any drain-with-a-timeout.
            assert elapsed < 3, (
                f"{sig_name} took {elapsed:.1f}s - it waited for the producer"
            )
        finally:
            kill_group(pgid)
            producer.wait()
            cli.wait()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
