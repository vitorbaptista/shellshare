"""
E2E tests driving the CLI under a real controlling TTY.

Unlike test_cli_script.py (which pipes stdin), these tests run the CLI the
way a user does: attached to a genuine pseudo-terminal. That makes the
user-facing behaviors testable that pipes structurally cannot reach:

- raw-mode local echo through the CLI's inner PTY
- terminal resize (TIOCSWINSZ -> SIGWINCH -> size broadcast)
- Ctrl+C handling (SIGINT cleanup, double-Ctrl+C force quit)
- room survival on clean exit
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

import requests

# pty.fork() in a multi-threaded process warns about fork safety; the child
# execs the CLI immediately, so the risky window is negligible here.
pytestmark = pytest.mark.filterwarnings(
    "ignore:.*use of forkpty.*:DeprecationWarning"
)

import re

from conftest import (
    CLI_PATH,
    SERVER_URL,
    SocketListener,
    broadcast_message,
    parse_share_key,
    poll_until,
    wait_for_content,
)


def size_dims(size):
    """The (cols, rows) of a size message, ignoring extras like theme."""
    return (size.get("cols"), size.get("rows")) if size else None


def give_listener_the_key(cli, listener):
    """Parse the share key off the CLI's own screen and hand it to the
    listener so it can decrypt broadcast content.

    Broadcasts are always end-to-end encrypted; the key lives only in the
    share-link fragment the CLI prints to its own terminal. Without it a
    listener reading message CONTENT sees ciphertext.
    """
    key = parse_share_key(cli.screen)
    assert key, f"No share key on CLI screen. Screen: {cli.screen!r}"
    listener.set_key(key)
    return key


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
        give_listener_the_key(cli, socket_listener)

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

    def test_room_survives_clean_exit(
        self, unique_room, unique_password, tty_cli
    ):
        cli = tty_cli(unique_room, unique_password)
        assert cli.wait_for_screen("Sharing terminal in")
        cli.send("echo populating-history\n")
        cli.send("exit\n")
        assert cli.wait_exit()

        # The room keeps its claim: another password cannot take the name
        status = broadcast_message(
            SERVER_URL, unique_room, f"other-{unique_password}", "claimed"
        )
        assert status == 401, (
            f"Room name was released on exit; reclaim returned {status}"
        )

        # ...and the link the user already shared still shows the session.
        # Polled: shutdown's ack drain gives up after 1s, so on a loaded
        # runner the last frame can still be in flight when the process
        # is reaped
        assert poll_until(
            lambda: len(
                requests.get(f"{SERVER_URL}/r/{unique_room}.bin").content
            ) > 0,
            timeout=5,
        ), "the room did not outlive the CLI with its history"

    def test_oversized_terminal_warns_user(
        self, unique_room, unique_password, tty_cli
    ):
        cli = tty_cli(unique_room, unique_password, cols=200, rows=50)
        assert cli.wait_for_screen("too big"), (
            f"No oversized-terminal warning. Screen: {cli.screen!r}"
        )
        cli.send("exit\n")
        cli.wait_exit()

    def test_terminal_output_includes_phone_qr_code(
        self, unique_room, unique_password, tty_cli
    ):
        cli = tty_cli(unique_room, unique_password)
        assert cli.wait_for_screen("Scan this QR code with your phone:"), (
            f"No QR prompt on CLI screen. Screen: {cli.screen!r}"
        )
        # Keep scraping: stdout is line-buffered on a TTY, so the prompt line
        # lands in the PTY before the QR rows underneath it are written. A
        # bare assert here catches the gap between the two writes.
        assert poll_until(
            lambda: any(ch in cli.screen for ch in "▀▄█"), timeout=10
        ), f"No terminal QR block characters on CLI screen. Screen: {cli.screen!r}"
        cli.send("exit\n")
        cli.wait_exit()

    def test_status_reprints_link_and_qr_from_inside_session(
        self, unique_room, unique_password, tty_cli
    ):
        cli = tty_cli(unique_room, unique_password)
        assert cli.wait_for_screen("Sharing terminal in"), (
            f"CLI did not start sharing. Screen: {cli.screen!r}"
        )
        key = parse_share_key(cli.screen)
        assert key, f"No share key on CLI screen. Screen: {cli.screen!r}"

        # `shellshare status`, run from inside the shared shell, recovers
        # the link from the environment the broadcaster exported into the
        # shell it spawned - nothing was written to disk.
        cli.send(f"{CLI_PATH} status\n")
        assert cli.wait_for_screen("Sharing this terminal at"), (
            f"status did not reprint the link. Screen: {cli.screen!r}"
        )
        # A second QR block appears (the first was the startup banner's).
        assert poll_until(
            lambda: cli.screen.count("Scan this QR code with your phone:") >= 2,
            timeout=10,
        ), f"status did not print a QR code. Screen: {cli.screen!r}"
        # The recovered link carries the same #fragment key: the exact URL
        # rode through the environment, so there is no re-derivation that
        # could silently diverge from the live broadcast.
        assert cli.screen.count(key) >= 2, (
            f"status link key did not match the broadcast. Screen: {cli.screen!r}"
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
        give_listener_the_key(cli, socket_listener)

        # First broadcast carries the initial size
        cli.send("echo before-resize\n")
        assert wait_for_content(
            socket_listener, lambda s: "before-resize" in s, timeout=10
        )
        assert poll_until(
            lambda: size_dims(socket_listener.get_last_size()) == (80, 24),
            timeout=5,
        ), f"Initial size wrong: {socket_listener.get_last_size()}"

        # Resize the window; SIGWINCH is delivered by the kernel
        cli.resize(100, 30)
        # Output triggers the next POST, which carries the new size
        cli.send("echo after-resize\n")

        assert poll_until(
            lambda: size_dims(socket_listener.get_last_size()) == (100, 30),
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
        give_listener_the_key(cli, socket_listener)
        cli.send("echo pre-interrupt\n")
        assert wait_for_content(
            socket_listener, lambda s: "pre-interrupt" in s, timeout=10
        )

        os.kill(cli.pid, signal.SIGINT)

        assert cli.wait_exit(), "CLI did not exit on SIGINT"
        # Ctrl+C ends the transmission but leaves the room: the link the
        # user already shared keeps working, and the name stays claimed
        assert broadcast_message(
            SERVER_URL, unique_room, f"other-{unique_password}", "x"
        ) == 401, "Room name was released on SIGINT"

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


class OscRespondingTty:
    """A PTY that answers OSC color queries - i.e. one that pretends to
    be a real terminal emulator.

    A bare PTY (what `pty.fork` and `script` give you) has nothing behind
    it, so it never answers, which exercises only the fallback. To reach
    the detection path at all the harness has to play the emulator and
    reply the way real ones were measured to.
    """

    # What this emulator claims to be: Tokyo Night, the palette a real
    # Alacritty answered with while the feature was designed
    BACKGROUND = "#1a1b26"
    FOREGROUND = "#a9b1d6"
    PALETTE = [
        "#32344a", "#f7768e", "#9ece6a", "#e0af68",
        "#7aa2f7", "#ad8ee6", "#449dab", "#787c99",
        "#444b6a", "#ff7a93", "#b9f27c", "#ff9e64",
        "#7da6ff", "#bb9af7", "#0db9d7", "#acb0d0",
    ]

    QUERY_RE = re.compile(rb"\x1b\](10|11|4;(\d+));\?(?:\x07|\x1b\\)")

    def __init__(self, room, password, terminator, server=SERVER_URL):
        self.terminator = terminator
        winsize = struct.pack("HHHH", 24, 80, 0, 0)
        pid, master = pty.fork()
        if pid == 0:  # child: become the CLI
            try:
                fcntl.ioctl(0, termios.TIOCSWINSZ, winsize)
                env = dict(os.environ)
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
        self._screen = b""
        self._lock = threading.Lock()
        self._reader = threading.Thread(target=self._serve, daemon=True)
        self._reader.start()

    def _color_reply(self, prefix, hex_color):
        """`ESC ] <prefix> ; rgb:RRRR/GGGG/BBBB <terminator>` - the
        16-bit-per-component form terminals actually answer with, which
        the CLI has to scale back down to 8."""
        parts = "/".join(
            f"{int(hex_color[i:i + 2], 16) * 0x101:04x}" for i in (1, 3, 5)
        )
        return f"\x1b]{prefix};rgb:{parts}".encode() + self.terminator

    def _serve(self):
        """Drain the CLI's output, answering any color query in it."""
        while True:
            try:
                data = os.read(self.master, 4096)
            except OSError:
                return
            if not data:
                return
            with self._lock:
                self._screen += data
            for match in self.QUERY_RE.finditer(data):
                kind, index = match.group(1), match.group(2)
                if index is not None:
                    slot = int(index)
                    reply = self._color_reply(f"4;{slot}", self.PALETTE[slot])
                elif kind == b"11":
                    reply = self._color_reply("11", self.BACKGROUND)
                else:
                    reply = self._color_reply("10", self.FOREGROUND)
                try:
                    os.write(self.master, reply)
                except OSError:
                    return

    @property
    def screen(self):
        with self._lock:
            return self._screen.decode("utf-8", errors="replace")

    def close(self):
        for step in (
            lambda: os.write(self.master, b"exit\n"),
            lambda: os.waitpid(self.pid, 0),
            lambda: os.close(self.master),
        ):
            try:
                step()
            except (OSError, ChildProcessError):
                pass


class TestTtyThemeDetection:
    """`--theme auto` (the default) asks the terminal for its colors.

    Only reachable with a real controlling TTY and something answering
    on it, which is why this lives here rather than in test_theme.py.
    """

    @pytest.mark.parametrize(
        "terminator", [b"\x07", b"\x1b\\"], ids=["bel", "st"]
    )
    def test_detected_colors_go_on_the_wire(
        self, unique_room, unique_password, terminator
    ):
        """Both reply terminators must be accepted: a terminal answers
        with ST directly and with BEL through tmux, so honouring only one
        of them silently loses detection for half of all users."""
        listener = SocketListener(unique_room)
        listener.connect()
        cli = OscRespondingTty(unique_room, unique_password, terminator)
        try:
            assert listener.wait_for_size(timeout=10), "No size event received"
            size = listener.get_last_size()
        finally:
            cli.close()
            listener.disconnect()

        assert size.get("colors") == {
            "foreground": OscRespondingTty.FOREGROUND,
            "background": OscRespondingTty.BACKGROUND,
            "palette": OscRespondingTty.PALETTE,
        }
        # A detected theme has no name, so it replaces the named one
        assert "theme" not in size

    def test_detection_leaves_type_ahead_for_the_shell(self, unique_room,
                                                       unique_password):
        """Detection must not swallow what the user typed ahead.

        Piping (`dmesg | shellshare`) is the case that made this worth a
        test: the CLI reads its input from the pipe, so keystrokes at the
        terminal belong to the parent shell, and eating them fails
        silently - the user just never runs the command they typed. Both
        halves of detection could: reading a reply reads past whatever is
        queued, and flushing the input queue on the way out discards it.
        """
        room, password = unique_room, unique_password
        # A pipeline, so the CLI's stdin is not the terminal; then read a
        # line from the tty, which must be the type-ahead typed below.
        script = (
            f"printf 'streamed\\n' | {CLI_PATH} -s {SERVER_URL} "
            f"-r {room} -W {password} >/dev/null 2>&1; "
            'IFS= read -r -t 5 line < /dev/tty; echo "RESULT:[$line]"'
        )
        pid, master = pty.fork()
        if pid == 0:
            try:
                os.execve("/bin/sh", ["/bin/sh", "-c", script],
                          dict(os.environ, TERM="xterm"))
            finally:
                os._exit(127)

        # Type before the CLI has even started - the queue detection
        # would otherwise read past. Nothing here answers the OSC
        # queries, so this also covers the timeout path, where the reply
        # may still be in flight and the input queue gets flushed.
        os.write(master, b"TYPEAHEAD\n")

        screen = b""
        try:
            while True:
                try:
                    data = os.read(master, 4096)
                except OSError:
                    break
                if not data:
                    break
                screen += data
        finally:
            try:
                os.waitpid(pid, 0)
            except (ChildProcessError, OSError):
                pass
            try:
                os.close(master)
            except OSError:
                pass

        text = screen.decode("utf-8", errors="replace")
        assert "RESULT:[TYPEAHEAD]" in text, (
            f"type-ahead did not survive detection. Screen: {text!r}"
        )
