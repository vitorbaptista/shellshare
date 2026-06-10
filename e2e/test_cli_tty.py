"""
E2E tests driving the CLI under a real controlling TTY.

Unlike test_cli_script.py (which pipes stdin), these tests run the CLI the
way a user does: attached to a genuine pseudo-terminal. That makes the
user-facing behaviors testable that pipes structurally cannot reach:

- raw-mode local echo through the CLI's inner PTY
- terminal resize (TIOCSWINSZ -> SIGWINCH -> size broadcast)
- Ctrl+C handling (SIGINT cleanup, double-Ctrl+C force quit)
- room deletion on clean exit
"""

import sys

import pytest

if sys.platform == "win32":
    pytest.skip(
        "requires Unix PTY (pty/fcntl/termios)", allow_module_level=True
    )

import fcntl
import os
import pty
import signal
import struct
import termios
import threading
import time

# pty.fork() in a multi-threaded process warns about fork safety; the child
# execs the CLI immediately, so the risky window is negligible here.
pytestmark = pytest.mark.filterwarnings(
    "ignore:.*use of forkpty.*:DeprecationWarning"
)

from conftest import (
    CLI_PATH,
    SERVER_URL,
    SocketListener,
    http_post_message,
    poll_until,
    wait_for_content,
)


class TtyCli:
    """Run the shellshare CLI attached to a real controlling TTY.

    A background thread continuously drains the master side so the CLI
    never blocks writing to stdout, and accumulates everything the user
    would see on screen.
    """

    def __init__(self, room, password, server=SERVER_URL, cols=80, rows=24):
        winsize = struct.pack("HHHH", rows, cols, 0, 0)
        pid, master = pty.fork()
        if pid == 0:  # child: become the CLI
            try:
                # Set the terminal size BEFORE exec (fd 0 is the PTY
                # slave here). Doing it from the parent after fork races
                # the CLI's startup size read.
                fcntl.ioctl(0, termios.TIOCSWINSZ, winsize)
                env = dict(os.environ)
                # Predictable, prompt-light shell for the CLI to spawn
                env["SHELL"] = "/bin/sh"
                env["PS1"] = "$ "
                env["TERM"] = "xterm"
                os.execve(
                    str(CLI_PATH),
                    [str(CLI_PATH), "-s", server, "-r", room, "-W", password],
                    env,
                )
            finally:
                os._exit(127)

        self.pid = pid
        self.master = master
        self.exit_status = None
        self._screen = b""
        self._lock = threading.Lock()

        self._reader = threading.Thread(target=self._drain, daemon=True)
        self._reader.start()

    def _drain(self):
        while True:
            try:
                data = os.read(self.master, 4096)
            except OSError:
                return
            if not data:
                return
            with self._lock:
                self._screen += data

    @property
    def screen(self):
        """Everything the user has seen so far, decoded leniently."""
        with self._lock:
            return self._screen.decode("utf-8", errors="replace")

    def send(self, data):
        """Type into the terminal."""
        if isinstance(data, str):
            data = data.encode()
        os.write(self.master, data)

    def resize(self, cols, rows):
        """Resize the terminal window. The kernel delivers SIGWINCH."""
        winsize = struct.pack("HHHH", rows, cols, 0, 0)
        fcntl.ioctl(self.master, termios.TIOCSWINSZ, winsize)

    def wait_for_screen(self, substring, timeout=10):
        """Wait until the given text has appeared on the user's screen."""
        return poll_until(lambda: substring in self.screen, timeout=timeout)

    def wait_exit(self, timeout=10):
        """Wait for the CLI process to exit. Returns True if it did."""

        def reap():
            if self.exit_status is not None:
                return True
            pid, status = os.waitpid(self.pid, os.WNOHANG)
            if pid == self.pid:
                self.exit_status = status
                return True
            return False

        return poll_until(reap, timeout=timeout)

    def close(self):
        """Force-clean any leftovers; safe to call multiple times."""
        if self.exit_status is None:
            try:
                os.kill(self.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            try:
                os.waitpid(self.pid, 0)
            except ChildProcessError:
                pass
            self.exit_status = -1
        try:
            os.close(self.master)
        except OSError:
            pass


@pytest.fixture
def tty_cli():
    """Factory for TtyCli instances with guaranteed cleanup."""
    instances = []

    def start(room, password, **kwargs):
        cli = TtyCli(room, password, **kwargs)
        instances.append(cli)
        return cli

    yield start

    for cli in instances:
        cli.close()


class TestTtyBroadcast:
    """The core user flow: share a terminal, viewers see what you see."""

    def test_command_output_reaches_viewer_and_local_screen(
        self, unique_room, unique_password, socket_listener, tty_cli
    ):
        cli = tty_cli(unique_room, unique_password)
        assert cli.wait_for_screen("Sharing terminal in"), (
            f"CLI did not start sharing. Screen: {cli.screen!r}"
        )

        cli.send("echo tty-roundtrip-marker\n")

        assert wait_for_content(
            socket_listener, lambda s: "tty-roundtrip-marker" in s, timeout=10
        ), "Viewer never received the command output"
        assert cli.wait_for_screen("tty-roundtrip-marker"), (
            "Local screen did not show the command output"
        )

        cli.send("exit\n")
        assert cli.wait_exit(), "CLI did not exit after shell exit"
        assert cli.wait_for_screen("End of transmission."), (
            f"Missing end-of-transmission message. Screen: {cli.screen!r}"
        )

    def test_room_is_deleted_on_clean_exit(
        self, unique_room, unique_password, tty_cli
    ):
        cli = tty_cli(unique_room, unique_password)
        assert cli.wait_for_screen("Sharing terminal in")
        cli.send("echo populating-history\n")
        cli.send("exit\n")
        assert cli.wait_exit()

        # The room was deleted: a different password can claim it again
        status = http_post_message(
            SERVER_URL, unique_room, f"other-{unique_password}", "claimed"
        )
        assert status == 200, (
            "Room password was not released on exit; "
            f"reclaim attempt returned {status}"
        )

        # And a late joiner sees none of the old session's history
        listener = SocketListener(unique_room)
        listener.connect()
        try:
            time.sleep(1)  # give history (if any) time to arrive
            assert "populating-history" not in listener.get_accumulated_messages()
        finally:
            listener.disconnect()

    def test_oversized_terminal_warns_user(
        self, unique_room, unique_password, tty_cli
    ):
        cli = tty_cli(unique_room, unique_password, cols=200, rows=50)
        assert cli.wait_for_screen("too big"), (
            f"No oversized-terminal warning. Screen: {cli.screen!r}"
        )
        cli.send("exit\n")
        cli.wait_exit()


class TestTtyResize:
    """Resizing the terminal must propagate to viewers."""

    def test_resize_broadcasts_new_size(
        self, unique_room, unique_password, socket_listener, tty_cli
    ):
        cli = tty_cli(unique_room, unique_password, cols=80, rows=24)
        assert cli.wait_for_screen("Sharing terminal in")

        # First broadcast carries the initial size
        cli.send("echo before-resize\n")
        assert wait_for_content(
            socket_listener, lambda s: "before-resize" in s, timeout=10
        )
        assert poll_until(
            lambda: socket_listener.get_last_size() == {"cols": 80, "rows": 24},
            timeout=5,
        ), f"Initial size wrong: {socket_listener.get_last_size()}"

        # Resize the window; SIGWINCH is delivered by the kernel
        cli.resize(100, 30)
        # Output triggers the next POST, which carries the new size
        cli.send("echo after-resize\n")

        assert poll_until(
            lambda: socket_listener.get_last_size() == {"cols": 100, "rows": 30},
            timeout=10,
        ), (
            "Viewer never received the new size; "
            f"last size: {socket_listener.get_last_size()}"
        )

        cli.send("exit\n")
        cli.wait_exit()


class TestTtySignals:
    """Ctrl+C behaviors, exactly as the user's keyboard produces them."""

    def test_sigint_cleans_up_and_exits(
        self, unique_room, unique_password, socket_listener, tty_cli
    ):
        cli = tty_cli(unique_room, unique_password)
        assert cli.wait_for_screen("Sharing terminal in")
        cli.send("echo pre-interrupt\n")
        assert wait_for_content(
            socket_listener, lambda s: "pre-interrupt" in s, timeout=10
        )

        os.kill(cli.pid, signal.SIGINT)

        assert cli.wait_exit(), "CLI did not exit on SIGINT"
        # The Ctrl+C handler deletes the room: it can be claimed anew
        assert poll_until(
            lambda: http_post_message(
                SERVER_URL, unique_room, f"other-{unique_password}", "x"
            )
            == 200,
            timeout=5,
        ), "Room was not cleaned up on SIGINT"

    def test_double_ctrlc_force_quits(
        self, unique_room, unique_password, tty_cli
    ):
        cli = tty_cli(unique_room, unique_password)
        assert cli.wait_for_screen("Sharing terminal in")
        # Wait for the shell PROMPT, not just the banner: a Ctrl+C that
        # lands while the shell is still starting kills it outright
        # (SIGINT default disposition) instead of exercising the
        # double-tap force-quit path
        assert cli.wait_for_screen("$ "), (
            f"Shell prompt never appeared. Screen: {cli.screen!r}"
        )

        # Two Ctrl+C keypresses in quick succession (separate reads:
        # the CLI detects the double-tap across reads within 500ms)
        cli.send(b"\x03")
        time.sleep(0.1)
        cli.send(b"\x03")

        assert cli.wait_exit(timeout=10), (
            "Double Ctrl+C did not force-quit the CLI"
        )
