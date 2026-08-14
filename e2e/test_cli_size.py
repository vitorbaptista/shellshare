"""
E2E Tests for Shellshare CLI - --cols/--rows size overrides

Headless runs (piped stdin, a wrapper spawning shellshare without a
terminal) fall back to 80x24 with no way to choose the geometry viewers
see. `--cols`/`--rows` pin each dimension independently; the override
must survive every size read path (initial connect and the periodic
re-reads), which is why the assertions look at the `size` control
message the viewer actually receives.

Test categories:
- Overrides reach the viewer's size event (exec and stream mode)
- Partial override: the unpinned dimension still falls back
"""

import subprocess
import sys

import pytest

from conftest import CLI_COMMAND, SERVER_URL, poll_until, size_dims


def run_headless(mode, room, password, extra_args):
    """Run a short headless broadcast (no TTY anywhere) to completion."""
    args = CLI_COMMAND + ["-s", SERVER_URL, "-r", room, "-W", password, *extra_args]
    kwargs = {}
    if mode == "exec":
        args += ["exec", "--", "printf", "size-test"]
        # Explicitly no stdin at all - the test's point is the headless
        # fallback, and pytest's own stdin must not leak in
        kwargs["stdin"] = subprocess.DEVNULL
    else:
        kwargs["input"] = "size-test\n"
    proc = subprocess.run(
        args,
        capture_output=True,
        text=True,
        timeout=60,
        **kwargs,
    )
    assert proc.returncode == 0, proc.stderr
    return proc


class TestSizeOverrides:
    @pytest.mark.skipif(sys.platform == "win32", reason="spawns a POSIX printf")
    def test_exec_override_reaches_viewer(
        self, unique_room, unique_password, socket_listener
    ):
        """Both flags pinned in exec mode: the viewer's size event must
        carry them instead of the 80x24 headless fallback."""
        run_headless(
            "exec", unique_room, unique_password, ["--cols", "132", "--rows", "43"]
        )
        assert socket_listener.wait_for_size(timeout=5)
        assert size_dims(socket_listener.get_last_size()) == (132, 43)

    def test_stream_override_and_partial_fallback(
        self, unique_room, unique_password, socket_listener
    ):
        """Stream mode re-reads the size continuously; a pinned dimension
        must stay pinned there too, and an unpinned one keeps the
        detected/fallback value (24 rows headless)."""
        run_headless("stream", unique_room, unique_password, ["--cols", "200"])
        assert socket_listener.wait_for_size(timeout=5)
        # The size the CLI last delivered must be pinned at 200 cols; rows
        # were not pinned, so the headless fallback (24) applies.
        assert poll_until(
            lambda: size_dims(socket_listener.get_last_size()) == (200, 24),
            timeout=5,
        ), socket_listener.get_last_size()
