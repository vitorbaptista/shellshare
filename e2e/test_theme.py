"""
E2E Tests for the --theme CLI option.

The theme name travels inside the `size` control message (the server
forwards it verbatim), and the viewer maps it to terminal colors from
themes.json. These tests cover the wire format, CLI validation, and the
colors actually rendered in a browser.
"""

import re
import subprocess
import time
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright

from conftest import (
    CLI_COMMAND,
    SERVER_URL,
    SocketListener,
    random_id,
    wait_for_terminal_text,
)

SHARE_LINK_RE = re.compile(r"Sharing terminal in (\S+)")

# Background colors from themes.json, as computed CSS values
DRACULA_BG = "rgb(40, 42, 54)"
SOLARIZED_LIGHT_BG = "rgb(253, 246, 227)"
TANGO_BG = "rgb(18, 19, 20)"  # the default theme


def run_cli_stdin(message, room, password, server=SERVER_URL, extra_args=None, timeout=30):
    """Run the CLI in stdin mode. Returns (returncode, stdout, stderr).

    `timeout` is wall-clock and generous: a full CLI session is normally
    sub-second, but under `-n 10` on an oversubscribed CI runner the CLI's
    bounded shutdown sleeps and thread scheduling stretch to several
    seconds. The slack absorbs that without masking a genuine hang.
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


def terminal_background(page):
    """The #terminal container's computed background color."""
    return page.evaluate(
        "getComputedStyle(document.getElementById('terminal')).backgroundColor"
    )


def page_background(page):
    """The page body's computed background color - the chrome around the
    terminal, which the viewer tints to match the theme."""
    return page.evaluate(
        "getComputedStyle(document.body).backgroundColor"
    )


def parse_rgb(value):
    """('rgb(r, g, b)' as served by getComputedStyle) -> (r, g, b)."""
    match = re.match(r"rgb\((\d+),\s*(\d+),\s*(\d+)\)", value)
    assert match, f"unexpected color value: {value!r}"
    return tuple(int(c) for c in match.groups())


def perceived_luminance(rgb):
    """sRGB perceived brightness in 0..1 - mirrors the viewer script."""
    r, g, b = rgb
    return (0.2126 * r + 0.7152 * g + 0.0722 * b) / 255


ROOM_CSS = Path(__file__).resolve().parent.parent / "public" / "stylesheet" / "room.css"


def css_page_fallbacks():
    """The fallback hex literals for the --page-* custom properties in
    room.css (e.g. `var(--page-bg, #222324)` -> {'--page-bg': '#222324'}).
    These paint the page before the viewer script applies the theme."""
    text = ROOM_CSS.read_text()
    found = re.findall(r"var\((--page-[a-z]+),\s*(#[0-9a-fA-F]{6})\)", text)
    # First fallback wins (page-bg appears on both html and body, identical)
    fallbacks = {}
    for prop, value in found:
        fallbacks.setdefault(prop, value.lower())
    return fallbacks


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

    def test_no_theme_flag_defaults_to_tango(self, unique_room, unique_password):
        listener = SocketListener(unique_room)
        listener.connect()

        returncode, stdout, stderr = run_cli_stdin(
            "test", unique_room, unique_password
        )
        assert returncode == 0, f"CLI failed: {stderr}"

        assert listener.wait_for_size(timeout=5), "No size event received"
        size = listener.get_last_size()
        listener.disconnect()

        assert size.get("theme") == "tango"


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
        help_text = stdout + stderr
        assert "--theme" in help_text
        assert "default: tango" in help_text
        # The help must list what's available
        for theme in ["asciinema", "dracula", "solarized-dark", "tango"]:
            assert theme in help_text, f"'{theme}' missing from help: {help_text}"


class TestThemeRendering:
    """The browser must render the broadcast in the chosen colors.

    These use dedicated servers: the shared one (when pinned to a port
    with a pre-started server) may be an older binary whose viewer page
    predates themes.
    """

    @pytest.mark.parametrize("theme_args, expected_bg", [
        (["--theme", "dracula"], DRACULA_BG),
        ([], TANGO_BG),
    ], ids=["dracula", "default-tango"])
    def test_theme_colors_render_in_browser(self, dedicated_server, theme_args, expected_bg):
        """A broadcast renders in the chosen theme's colors - an explicit
        --theme and the default (tango) alike."""
        server = dedicated_server()
        room_id = f"theme-{random_id()}"
        password = f"secret-{random_id()}"

        proc = subprocess.Popen(
            CLI_COMMAND + [
                "--stdin", "-s", server.url, "-r", room_id, "-W", password,
            ] + theme_args,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            share_url = None
            for line in proc.stderr:
                match = SHARE_LINK_RE.search(line)
                if match:
                    share_url = match.group(1)
                    break
            assert share_url, "No share link in CLI output"

            with sync_playwright() as p:
                browser = p.chromium.launch()
                page = browser.new_page()
                page.goto(share_url)
                page.wait_for_selector("#terminal", timeout=10000)

                proc.stdin.write("themed output\n")
                proc.stdin.flush()

                wait_for_terminal_text(page, "themed output")
                assert terminal_background(page) == expected_bg
                browser.close()
        finally:
            proc.stdin.close()
            proc.wait(timeout=10)

    @pytest.mark.parametrize("theme_args, light", [
        (["--theme", "solarized-light"], True),
        ([], False),  # the default theme (tango) is dark
    ], ids=["light", "default-dark"])
    def test_page_chrome_follows_theme(self, dedicated_server, theme_args, light):
        """The page chrome (body background) tracks the theme so a light
        terminal doesn't sit on a dark page or a dark one on white, yet is
        offset from the terminal so the terminal still reads as a distinct
        surface. Driven client-side from the theme in the size message."""
        server = dedicated_server()
        room_id = f"chrome-{random_id()}"
        password = f"secret-{random_id()}"

        proc = subprocess.Popen(
            CLI_COMMAND + [
                "--stdin", "-s", server.url, "-r", room_id, "-W", password,
            ] + theme_args,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            share_url = None
            for line in proc.stderr:
                match = SHARE_LINK_RE.search(line)
                if match:
                    share_url = match.group(1)
                    break
            assert share_url, "No share link in CLI output"

            with sync_playwright() as p:
                browser = p.chromium.launch()
                page = browser.new_page()
                page.goto(share_url)
                page.wait_for_selector("#terminal", timeout=10000)

                proc.stdin.write("chrome output\n")
                proc.stdin.flush()

                wait_for_terminal_text(page, "chrome output")
                page_bg = parse_rgb(page_background(page))
                term_bg = parse_rgb(terminal_background(page))
                # Tracks the theme's tone...
                if light:
                    assert perceived_luminance(page_bg) > 0.6
                else:
                    assert perceived_luminance(page_bg) < 0.4
                # ...but is offset so the terminal isn't lost in the page
                assert page_bg != term_bg
                browser.close()
        finally:
            proc.stdin.close()
            proc.wait(timeout=10)

    def test_default_theme_css_fallback_matches_script(self, dedicated_server):
        """Lockstep: the pre-JS CSS fallbacks (room.css) must equal the
        chrome the viewer script computes for the DEFAULT theme, so the
        default-theme page shows no flash before JS runs. This drifts if
        the default theme, its colors in themes.json, the script's
        derivation, or the fallbacks themselves change out of sync - and
        the default is the common case (most broadcasts take no --theme),
        which can't be resolved server-side (the page is shared by every
        room). Uses the real browser script, not a reimplementation."""
        server = dedicated_server()
        room_id = f"default-chrome-{random_id()}"
        password = f"secret-{random_id()}"

        # No --theme: exercise the built-in default the fallbacks target
        proc = subprocess.Popen(
            CLI_COMMAND + [
                "--stdin", "-s", server.url, "-r", room_id, "-W", password,
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            share_url = None
            for line in proc.stderr:
                match = SHARE_LINK_RE.search(line)
                if match:
                    share_url = match.group(1)
                    break
            assert share_url, "No share link in CLI output"

            with sync_playwright() as p:
                browser = p.chromium.launch()
                page = browser.new_page()
                page.goto(share_url)
                page.wait_for_selector("#terminal", timeout=10000)

                proc.stdin.write("default chrome\n")
                proc.stdin.flush()

                wait_for_terminal_text(page, "default chrome")
                computed = page.evaluate(
                    "() => Object.fromEntries("
                    "  ['--page-bg', '--page-fg', '--page-muted', '--page-accent']"
                    "  .map(p => [p, document.documentElement.style"
                    "    .getPropertyValue(p).trim().toLowerCase()]))"
                )
                browser.close()
        finally:
            proc.stdin.close()
            proc.wait(timeout=10)

        fallbacks = css_page_fallbacks()
        assert computed == fallbacks, (
            "room.css --page-* fallbacks must match the script's default-theme "
            f"chrome (else the default page flashes before JS): script computed "
            f"{computed}, room.css falls back to {fallbacks}"
        )

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
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            share_url = None
            for line in proc.stderr:
                match = SHARE_LINK_RE.search(line)
                if match:
                    share_url = match.group(1)
                    break
            assert share_url, "No share link in CLI output"

            proc.stdin.write("early content\n")
            proc.stdin.flush()
            time.sleep(2)  # let the CLI deliver before the viewer joins

            with sync_playwright() as p:
                browser = p.chromium.launch()
                page = browser.new_page()
                page.goto(share_url)
                page.wait_for_selector("#terminal", timeout=10000)

                wait_for_terminal_text(page, "early content")
                assert terminal_background(page) == SOLARIZED_LIGHT_BG
                browser.close()
        finally:
            proc.stdin.close()
            proc.wait(timeout=10)

