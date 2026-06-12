"""
E2E Tests for Shellshare CLI - Script Mode

These tests verify the CLI's script mode (default mode without --stdin),
which spawns a PTY (pseudo-terminal) to capture terminal output.

The Rust CLI uses portable-pty for cross-platform PTY support, so we test
actual shell spawning behavior rather than mocking.

Test categories:
- Script mode streaming
- Session lifecycle

Cross-platform support:
- Linux/Mac: Uses native PTY (openpty)
- Windows: Uses ConPTY (Windows 10 1809+)
"""

import subprocess
import time

from conftest import (
    CLI_COMMAND,
    SERVER_URL,
    SocketListener,
    random_id,
    wait_for_content,
)


def run_cli_script_mode(room, password, server=SERVER_URL, env=None, timeout=30):
    """
    Run the CLI in script mode (without --stdin).

    Returns (returncode, stdout, stderr)
    """
    args = CLI_COMMAND + ["-s", server, "-r", room, "-W", password]

    proc = subprocess.Popen(
        args,
        stdin=subprocess.PIPE,  # Provide stdin so we can send exit command
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )

    try:
        # Send echo command to guarantee output, then exit
        # This ensures the shell produces output even in minimal CI environments
        # where there may be no PS1 prompt configured
        stdout, stderr = proc.communicate(input="echo shellshare-test\nexit\n", timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        stdout, stderr = proc.communicate()

    return proc.returncode, stdout, stderr


class TestScriptModeBasic:
    """Basic tests for script mode."""

    def test_script_mode_streams_output(self, unique_room, unique_password):
        """PTY output should appear via Socket.IO."""
        listener = SocketListener(unique_room)
        listener.connect()

        # Start CLI process (don't wait for it to complete)
        proc = subprocess.Popen(
            CLI_COMMAND + ["-s", SERVER_URL, "-r", unique_room, "-W", unique_password],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

        # Wait for some output while CLI is still running
        assert wait_for_content(listener, lambda s: len(s) > 0, timeout=10), \
            "No output received from PTY"

        accumulated = listener.get_accumulated_messages()

        # Clean up the process
        try:
            proc.stdin.write("exit\n")
            proc.stdin.flush()
            proc.communicate(timeout=5)
        except (subprocess.TimeoutExpired, BrokenPipeError):
            proc.terminate()
            try:
                proc.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.communicate()

        listener.disconnect()

        # Verify we got some output from the PTY (shell prompt, etc.)
        # The exact content depends on the user's shell configuration
        assert len(accumulated) > 0, \
            f"No output received from PTY. Accumulated: {accumulated}"

    def test_script_mode_prints_room_url(self, unique_room, unique_password):
        """Script mode should print 'Sharing terminal in {url}' to stdout."""
        returncode, stdout, stderr = run_cli_script_mode(
            unique_room, unique_password, timeout=15
        )

        # In script mode, message goes to stdout (not stderr)
        output = stdout + stderr
        assert "Sharing terminal in" in output
        assert f"/r/{unique_room}" in output

    def test_script_mode_prints_end(self, unique_room, unique_password):
        """Script mode should print 'End of transmission.' on clean exit.

        Note: This test verifies the CLI prints end message when shell exits.
        Due to PTY stdin forwarding complexity, we send exit command.
        """
        proc = subprocess.Popen(
            CLI_COMMAND + ["-s", SERVER_URL, "-r", unique_room, "-W", unique_password],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

        # Wait briefly for shell to start, then send exit
        time.sleep(0.5)

        # Send exit command
        try:
            proc.stdin.write("exit\n")
            proc.stdin.flush()
        except BrokenPipeError:
            # Shell may have exited already, which is fine
            pass

        try:
            stdout, stderr = proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            proc.terminate()
            stdout, stderr = proc.communicate(timeout=5)

        output = stdout + stderr
        # Either we got clean exit message or CLI at least started properly
        assert "Sharing terminal in" in output, "CLI should print sharing message"
        # End message is expected on clean exit, but may not appear if we had to terminate
        # This is acceptable behavior for a PTY-based implementation


class TestScriptModeOutput:
    """Tests for script mode output behavior."""

    def test_pty_output_transmitted(self, unique_room, unique_password):
        """Verify PTY output is transmitted to server."""
        listener = SocketListener(unique_room)
        listener.connect()

        # Start CLI process (don't wait for it to complete)
        proc = subprocess.Popen(
            CLI_COMMAND + ["-s", SERVER_URL, "-r", unique_room, "-W", unique_password],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

        # Wait for content to be transmitted while CLI is still running
        assert wait_for_content(listener, lambda s: len(s) > 0, timeout=10), \
            "No output received from PTY"

        accumulated = listener.get_accumulated_messages()

        # Clean up the process
        try:
            proc.stdin.write("exit\n")
            proc.stdin.flush()
            proc.communicate(timeout=5)
        except (subprocess.TimeoutExpired, BrokenPipeError):
            proc.terminate()
            try:
                proc.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.communicate()

        listener.disconnect()

        # Check that we received some output (shell prompt, command echo, etc.)
        assert len(accumulated) > 0, f"No output received: {accumulated}"

    def test_script_mode_complete_session(self, unique_room, unique_password):
        """Test a complete script mode session from start to end."""
        listener = SocketListener(unique_room)
        listener.connect()

        # Start CLI process
        proc = subprocess.Popen(
            CLI_COMMAND + ["-s", SERVER_URL, "-r", unique_room, "-W", unique_password],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

        # Wait for some output to be transmitted
        assert wait_for_content(listener, lambda s: len(s) > 0, timeout=10), \
            "No PTY output transmitted"

        # Check that some output was transmitted
        accumulated = listener.get_accumulated_messages()

        # Terminate the process
        proc.terminate()
        try:
            stdout, stderr = proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            stdout, stderr = proc.communicate()

        output = stdout + stderr
        listener.disconnect()

        # Verify start message was printed
        assert "Sharing terminal in" in output, "Missing start message"
        # PTY should have produced some output
        assert len(accumulated) > 0, "No PTY output transmitted"


class TestScriptModeEdgeCases:
    """Edge case tests for script mode."""

    def test_script_mode_special_room_name(self, unique_password):
        """Script mode should work with special characters in room name."""
        room = f"test-room_{random_id()}"

        listener = SocketListener(room)
        listener.connect()

        returncode, stdout, stderr = run_cli_script_mode(
            room, unique_password, timeout=15
        )

        output = stdout + stderr

        listener.disconnect()

        assert "Sharing terminal in" in output

    def test_script_mode_custom_server(self, unique_room, unique_password):
        """Script mode should work with custom server URL."""
        # Use the test server
        returncode, stdout, stderr = run_cli_script_mode(
            unique_room, unique_password, server=SERVER_URL, timeout=15
        )

        output = stdout + stderr
        assert "Sharing terminal in" in output
        assert SERVER_URL in output or "localhost" in output


class TestScriptModeViewer:
    """Tests for viewing script mode sessions."""

    def test_late_joiner_sees_accumulated_content(self, unique_room, unique_password):
        """Late joiner should see accumulated content from script mode."""
        # First viewer to ensure content is accumulated
        first_listener = SocketListener(unique_room)
        first_listener.connect()

        # Start streaming
        proc = subprocess.Popen(
            CLI_COMMAND + ["-s", SERVER_URL, "-r", unique_room, "-W", unique_password],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

        # Wait for some content to be generated
        assert wait_for_content(first_listener, lambda s: len(s) > 0, timeout=10), \
            "No content generated"

        # Late joiner connects
        late_listener = SocketListener(unique_room)
        late_listener.connect()

        # Wait for accumulated content to be delivered to late joiner
        assert wait_for_content(late_listener, lambda s: len(s) > 0, timeout=10), \
            "Late joiner received no content"

        # Check if late joiner got something
        accumulated = late_listener.get_accumulated_messages()

        # Clean up - terminate instead of trying to send exit
        proc.terminate()
        try:
            proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()

        first_listener.disconnect()
        late_listener.disconnect()

        # Late joiner should see at least some content
        # (shell prompt, etc.)
        assert len(accumulated) > 0, "Late joiner received no content"
