"""
E2E Tests for the --theme CLI option.

The theme name travels inside the `size` control message (the server
forwards it verbatim), and the viewer maps it to terminal colors from
themes.json. These tests cover the wire format, CLI validation, and the
colors actually rendered in a browser.
"""

import subprocess
import time

from playwright.sync_api import sync_playwright, expect

from conftest import CLI_COMMAND, SERVER_URL, SocketListener, random_id

# Background colors from themes.json, as computed CSS values
DRACULA_BG = "rgb(40, 42, 54)"
SOLARIZED_LIGHT_BG = "rgb(253, 246, 227)"
DEFAULT_BG = "rgb(0, 0, 0)"


def run_cli_stdin(message, room, password, server=SERVER_URL, extra_args=None, timeout=10):
    """Run the CLI in stdin mode. Returns (returncode, stdout, stderr)."""
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


def terminal_background(page):
    """The #terminal container's computed background color."""
    return page.evaluate(
        "getComputedStyle(document.getElementById('terminal')).backgroundColor"
    )


class TestThemeWireFormat:
    """The theme must ride inside the size event, verbatim."""

    def test_theme_arrives_in_size_event(self, unique_room, unique_password):
        listener = SocketListener(unique_room)
        listener.connect()

        returncode, stdout, stderr = run_cli_stdin(
            "test", unique_room, unique_password, extra_args=["--theme", "dracula"]
        )
        assert returncode == 0, f"CLI failed: {stderr}"

        assert listener.wait_for_size(timeout=5), "No size event received"
        size = listener.get_last_size()
        listener.disconnect()

        assert size.get("theme") == "dracula"
        assert "cols" in size and "rows" in size

    def test_no_theme_flag_sends_no_theme(self, unique_room, unique_password):
        listener = SocketListener(unique_room)
        listener.connect()

        returncode, stdout, stderr = run_cli_stdin(
            "test", unique_room, unique_password
        )
        assert returncode == 0, f"CLI failed: {stderr}"

        assert listener.wait_for_size(timeout=5), "No size event received"
        size = listener.get_last_size()
        listener.disconnect()

        assert "theme" not in size


class TestThemeValidation:
    """An invalid theme must fail before the room is claimed."""

    def test_unknown_theme_fails_with_available_list(self):
        proc = subprocess.Popen(
            CLI_COMMAND + ["--stdin", "--theme", "no-such-theme"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        stdout, stderr = proc.communicate(input="", timeout=10)

        assert proc.returncode != 0
        assert "no-such-theme" in stderr
        # The error must list what IS available
        for theme in ["asciinema", "dracula", "solarized-dark", "tango"]:
            assert theme in stderr, f"'{theme}' missing from error: {stderr}"

    def test_theme_flag_in_help(self):
        proc = subprocess.Popen(
            CLI_COMMAND + ["--help"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        stdout, stderr = proc.communicate(timeout=10)
        assert "--theme" in stdout + stderr


class TestThemeRendering:
    """The browser must render the broadcast in the chosen colors.

    These use dedicated servers: the shared one on :3000 may be an older
    binary whose viewer page predates themes.
    """

    def test_theme_colors_render_in_browser(self, dedicated_server):
        server = dedicated_server()
        room_id = f"theme-{random_id()}"
        password = f"secret-{random_id()}"

        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page()
            page.goto(f"{server.url}/r/{room_id}")
            page.wait_for_selector("#terminal", timeout=10000)
            page.wait_for_function(
                "document.getElementById('online-counter').textContent !== '0'",
                timeout=10000,
            )

            returncode, stdout, stderr = run_cli_stdin(
                "themed output", room_id, password, server=server.url,
                extra_args=["--theme", "dracula"],
            )
            assert returncode == 0, f"CLI failed: {stderr}"

            expect(page.locator("#terminal")).to_contain_text(
                "themed output", timeout=10000
            )
            assert terminal_background(page) == DRACULA_BG
            browser.close()

    def test_late_joiner_sees_theme(self, dedicated_server):
        """The theme is replayed with the stored size, so a viewer who
        joins after the broadcast started still gets the colors."""
        server = dedicated_server()
        room_id = f"theme-late-{random_id()}"
        password = f"secret-{random_id()}"

        # The broadcaster must stay alive: the room (and its stored
        # size+theme) is deleted when the CLI exits
        proc = subprocess.Popen(
            CLI_COMMAND + [
                "--stdin", "-s", server.url, "-r", room_id, "-W", password,
                "--theme", "solarized-light",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        try:
            proc.stdin.write("early content\n")
            proc.stdin.flush()
            time.sleep(2)  # let the CLI deliver before the viewer joins

            with sync_playwright() as p:
                browser = p.chromium.launch()
                page = browser.new_page()
                page.goto(f"{server.url}/r/{room_id}")
                page.wait_for_selector("#terminal", timeout=10000)

                expect(page.locator("#terminal")).to_contain_text(
                    "early content", timeout=10000
                )
                assert terminal_background(page) == SOLARIZED_LIGHT_BG
                browser.close()
        finally:
            proc.stdin.close()
            proc.wait(timeout=10)

    def test_default_colors_without_theme(self, dedicated_server):
        server = dedicated_server()
        room_id = f"theme-default-{random_id()}"
        password = f"secret-{random_id()}"

        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page()
            page.goto(f"{server.url}/r/{room_id}")
            page.wait_for_selector("#terminal", timeout=10000)
            page.wait_for_function(
                "document.getElementById('online-counter').textContent !== '0'",
                timeout=10000,
            )

            returncode, stdout, stderr = run_cli_stdin(
                "plain output", room_id, password, server=server.url
            )
            assert returncode == 0, f"CLI failed: {stderr}"

            expect(page.locator("#terminal")).to_contain_text(
                "plain output", timeout=10000
            )
            assert terminal_background(page) == DEFAULT_BG
            browser.close()
