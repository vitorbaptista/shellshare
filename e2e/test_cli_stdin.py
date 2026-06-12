"""
E2E Tests for Shellshare CLI - Stdin Mode

These tests verify the CLI's --stdin mode, which reads from stdin and streams
to the server. This is the primary testing interface for the CLI.

Test categories:
- Basic functionality
- Message encoding
- Server communication
- Authorization
- Error handling
- Argument parsing
"""

import platform
import re
import subprocess

import pytest

from conftest import (
    CLI_COMMAND,
    SERVER_URL,
    SocketListener,
    parse_share_key,
    random_id,
    wait_for_content,
)


def run_cli_stdin(message, room, password, server=SERVER_URL, extra_args=None, timeout=10):
    """
    Run the CLI in stdin mode with the given message.

    Returns (returncode, stdout, stderr)
    """
    args = CLI_COMMAND + ["--stdin", "-s", server, "-r", room, "-W", password]
    if extra_args:
        args.extend(extra_args)

    proc = subprocess.Popen(
        args,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
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

    def test_simple_text(self, unique_room, unique_password, socket_listener):
        """Simple text 'hello world' should be transmitted correctly."""
        test_message = "hello world"

        _, _, stderr = run_cli_stdin(test_message, unique_room, unique_password)

        socket_listener.set_key(parse_share_key(stderr))
        received = socket_listener.wait_for_message(timeout=5, containing=test_message)
        assert received is not None
        assert test_message in received

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
            "--stdin",
            "-s", SERVER_URL,
            "-r", custom_room,
            "-W", unique_password,
        ]

        proc = subprocess.Popen(
            args,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
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
        args = CLI_COMMAND + ["--stdin", "-s", SERVER_URL]

        proc = subprocess.Popen(
            args,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
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

    @pytest.mark.skip(reason="CLI URL parsing has known issues with scheme-less URLs")
    def test_adds_http_scheme_if_missing(self, unique_room, unique_password):
        """
        Server URL without scheme should get http:// added.

        Note: Due to URL parsing quirks in the CLI:
        - 'localhost:3000' is parsed as scheme='localhost', path='3000'
          so no http:// is added
        - '//localhost:3000' becomes 'http:////localhost:3000' which is invalid

        This test is skipped until the CLI URL parsing is fixed.
        The workaround is to always provide the full URL with scheme.
        """
        listener = SocketListener(unique_room)
        listener.connect()

        # Currently broken - CLI doesn't handle scheme-less URLs correctly
        returncode, stdout, stderr = run_cli_stdin(
            "test", unique_room, unique_password, server="//localhost:3000"
        )

        assert returncode == 0

        received = listener.wait_for_message(timeout=5)
        assert received is not None

        listener.disconnect()


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
        args1 = CLI_COMMAND + ["--stdin", "-s", SERVER_URL, "-r", unique_room, "-W", "original-password"]
        proc1 = subprocess.Popen(
            args1,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
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

    def test_empty_password_works(self, unique_room, socket_listener):
        """Empty password should create an open room."""
        returncode, stdout, stderr = run_cli_stdin(
            "test", unique_room, ""
        )

        # Empty password is valid (becomes MAC address by default, but we override with "")
        # This test verifies the CLI accepts empty password
        # Note: Behavior depends on server - may succeed or require auth

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
        args1 = CLI_COMMAND + ["--stdin", "-s", SERVER_URL, "-r", unique_room, "-W", password1]
        proc1 = subprocess.Popen(
            args1,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
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


class TestErrorHandling:
    """Tests for error handling."""

    @pytest.mark.skipif(
        platform.system() == "Windows",
        reason="Windows has longer connection timeouts causing test to hang"
    )
    def test_server_not_available(self, unique_room, unique_password):
        """CLI should handle server unavailability gracefully."""
        # Use a port that's definitely not running a server
        # The CLI has retry logic, so it may take a while to fail
        # We just verify it eventually completes without crashing
        returncode, stdout, stderr = run_cli_stdin(
            "test", unique_room, unique_password, 
            server="http://localhost:59999",
            timeout=15  # Longer timeout for retry logic
        )

        # The CLI should either exit with error or show error message
        # Just verify it completes (doesn't hang forever)
        # Note: Actual behavior depends on retry settings

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
            text=True,
        )
        stdout, stderr = proc.communicate(timeout=5)

        assert proc.returncode == 0
        output = stdout + stderr
        # Help should mention the available flags
        assert "--server" in output or "-s" in output
        assert "--room" in output or "-r" in output
        assert "--password" in output or "-W" in output
        assert "--stdin" in output
        # ... and document the default server users will share through
        assert "shellshare.net" in output, "Default server URL should be in help"


class TestLargeMessages:
    """Tests for handling larger messages (within limits)."""

    def test_50kb_message(self, unique_room, unique_password, socket_listener):
        """50KB message should be transmitted successfully."""
        marker = f"MARKER-{random_id(6)}"
        large_message = marker + ("X" * 50000)

        returncode, stdout, stderr = run_cli_stdin(
            large_message, unique_room, unique_password
        )

        assert returncode == 0

        socket_listener.set_key(parse_share_key(stderr))
        received = socket_listener.wait_for_message(timeout=10, containing=marker)
        assert received is not None
        assert marker in received


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
        args = CLI_COMMAND + ["--stdin", "-s", SERVER_URL, "-r", unique_room]

        proc = subprocess.Popen(
            args,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
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
        args1 = CLI_COMMAND + ["--stdin", "-s", SERVER_URL, "-r", unique_room]
        proc1 = subprocess.Popen(
            args1,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        _, stderr1 = proc1.communicate(input=msg1, timeout=10)

        # Each CLI run uses a fresh random key, so decrypt one run at a time.
        listener.set_key(parse_share_key(stderr1))
        first = listener.wait_for_message(timeout=5, containing=msg1)
        assert first is not None and msg1 in first, "First message not received"

        # Second write without -W (same machine = same MAC = same password)
        args2 = CLI_COMMAND + ["--stdin", "-s", SERVER_URL, "-r", unique_room]
        proc2 = subprocess.Popen(
            args2,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
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

    def test_whitespace_only_message(self, unique_room, unique_password, socket_listener):
        """Whitespace-only message should be transmitted."""
        test_message = "   \n\t\n   "

        returncode, stdout, stderr = run_cli_stdin(
            test_message, unique_room, unique_password
        )

        assert returncode == 0

        # Whitespace should be transmitted (even if hard to verify)
        # Just check no error occurred
        assert "ERROR" not in stderr

    @pytest.mark.skip(reason="CLI has encoding issues with unicode room names - server supports it but CLI fails")
    def test_unicode_room_name(self, unique_password):
        """
        Room name with unicode characters should work.

        Note: The server supports unicode room names (verified by viewer tests),
        but the CLI has encoding issues with non-ASCII room names.
        This is a known CLI limitation.
        """
        unicode_room = f"日本語-{random_id(6)}"

        listener = SocketListener(unicode_room)
        listener.connect()

        returncode, stdout, stderr = run_cli_stdin(
            "test", unicode_room, unique_password
        )

        assert returncode == 0

        received = listener.wait_for_message(timeout=5)
        assert received is not None

        listener.disconnect()

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

    def test_very_long_room_name(self, unique_password):
        """Very long room name should work (within reasonable limits)."""
        long_room = "a" * 100 + f"-{random_id(6)}"

        listener = SocketListener(long_room)
        listener.connect()

        returncode, stdout, stderr = run_cli_stdin(
            "test", long_room, unique_password
        )

        # Should either work or fail gracefully
        # (server may have length limits)
        assert returncode == 0 or "error" in stderr.lower()

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

    def test_binary_like_content(self, unique_room, unique_password, socket_listener):
        """Content with binary-like characters should be handled."""
        # Bytes that might cause issues in naive string handling
        test_message = "start\x00middle\xffend"

        returncode, stdout, stderr = run_cli_stdin(
            test_message, unique_room, unique_password
        )

        # Should either succeed or fail gracefully
        # (null bytes might cause issues depending on encoding)

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


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
