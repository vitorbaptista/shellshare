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
    broadcast_message,
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


def terminal_background(page):
    """The #terminal container's computed background color."""
    return page.evaluate(
        "getComputedStyle(document.getElementById('terminal')).backgroundColor"
    )


def wait_for_terminal_background(page, expected, timeout=10000):
    """Wait until the terminal is painted in `expected` ("rgb(r, g, b)").

    The theme arrives in the size message, which is independent of the
    output `wait_for_terminal_text` watches for. Sampling the colour the
    instant text appears is a race: on a slow runner it reads the
    pre-theme default instead, and the page chrome derived from it with
    it. Both are set in one synchronous apply, so this settles both.
    """
    page.wait_for_function(
        "expected => getComputedStyle(document.getElementById('terminal'))"
        ".backgroundColor === expected",
        arg=expected,
        timeout=timeout,
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
    # Explicit utf-8: room.css has non-ASCII bytes, and Windows would
    # otherwise default to cp1252 and choke decoding them
    text = ROOM_CSS.read_text(encoding="utf-8")
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

    def test_no_theme_flag_falls_back_to_tango(self, unique_room, unique_password):
        """`--theme auto` (the default) asks the terminal for its colors,
        but there is no terminal to answer here - no controlling tty, and
        no emulator behind it. The fallback must be the theme the CLI
        always defaulted to, and it must send a plain name with no
        `colors`, so a detection-less environment broadcasts exactly as
        it did before detection existed."""
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
        assert "colors" not in size

    def test_explicit_theme_is_never_overridden_by_detection(
        self, unique_room, unique_password
    ):
        """An explicit --theme is a choice, so it goes on the wire as a
        name and detection is not even attempted."""
        listener = SocketListener(unique_room)
        listener.connect()

        returncode, stdout, stderr = run_cli_stdin(
            "test", unique_room, unique_password, extra_args=["--theme", "nord"]
        )
        assert returncode == 0, f"CLI failed: {stderr}"

        assert listener.wait_for_size(timeout=5), "No size event received"
        size = listener.get_last_size()
        listener.disconnect()

        assert size.get("theme") == "nord"
        assert "colors" not in size


class TestDetectedColorsRender:
    """A detected theme has no name, so it travels as `colors` by value.

    This is the lockstep contract between the OSC detection in
    src/cli/termtheme.rs and applyTheme in public/javascript/room.js:
    detector emits {foreground, background, palette} and the viewer must
    render exactly those. Driven through the raw ingest socket rather
    than the CLI, because no terminal answers OSC queries under pytest.
    """

    # Tokyo Night, as an Alacritty actually answered it - the reply that
    # motivated sending colors by value instead of snapping to a preset
    DETECTED = {
        "foreground": "#a9b1d6",
        "background": "#1a1b26",
        "palette": [
            "#32344a", "#f7768e", "#9ece6a", "#e0af68",
            "#7aa2f7", "#ad8ee6", "#449dab", "#787c99",
            "#444b6a", "#ff7a93", "#b9f27c", "#ff9e64",
            "#7da6ff", "#bb9af7", "#0db9d7", "#acb0d0",
        ],
    }

    def test_detected_colors_render_in_browser(self, dedicated_server):
        server = dedicated_server()
        room_id = f"detected-{random_id()}"
        password = f"secret-{random_id()}"

        broadcast_message(
            server.url, room_id, password,
            text="detected output\r\n",
            size={"cols": 80, "rows": 24, "colors": self.DETECTED},
        )

        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page()
            page.goto(f"{server.url}/r/{room_id}")
            wait_for_terminal_text(page, "detected output")
            wait_for_terminal_background(page, "rgb(26, 27, 38)")
            # The page chrome follows the detected background too, so a
            # by-value theme is not a second-class one
            assert perceived_luminance(parse_rgb(page_background(page))) < 0.5
            browser.close()

    @pytest.mark.parametrize("colors", [
        {"foreground": "#a9b1d6", "background": "red",
         "palette": DETECTED["palette"]},
        {"foreground": "#a9b1d6", "background": "#1a1b26",
         "palette": ["#000000"]},
    ], ids=["non-hex", "short-palette"])
    def test_malformed_colors_fall_back_to_named_theme(
        self, dedicated_server, colors
    ):
        """`colors` comes from the broadcaster and lands in CSS custom
        properties, so anything that is not a full set of #rrggbb is
        rejected outright - the named theme alongside it still applies,
        rather than the page being half-styled."""
        server = dedicated_server()
        room_id = f"badcolors-{random_id()}"
        password = f"secret-{random_id()}"

        broadcast_message(
            server.url, room_id, password,
            text="fallback output\r\n",
            size={
                "cols": 80, "rows": 24,
                "theme": "dracula", "colors": colors,
            },
        )

        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page()
            page.goto(f"{server.url}/r/{room_id}")
            wait_for_terminal_text(page, "fallback output")
            wait_for_terminal_background(page, DRACULA_BG)
            browser.close()


class TestThemeValidation:
    """An invalid theme must fail before the room is claimed."""

    def test_unknown_theme_fails_with_available_list(self):
        proc = subprocess.Popen(
            CLI_COMMAND + ["--theme", "no-such-theme"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
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
            start_new_session=True,
            text=True,
        )
        stdout, stderr = proc.communicate(timeout=10)
        help_text = stdout + stderr
        assert "--theme" in help_text
        assert "default: auto" in help_text
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
                "-s", server.url, "-r", room_id, "-W", password,
            ] + theme_args,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            start_new_session=True,
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
                wait_for_terminal_background(page, expected_bg)
                browser.close()
        finally:
            proc.stdin.close()
            proc.wait(timeout=10)

    @pytest.mark.parametrize("theme_args, light, theme_bg", [
        (["--theme", "solarized-light"], True, SOLARIZED_LIGHT_BG),
        ([], False, TANGO_BG),  # the default theme (tango) is dark
    ], ids=["light", "default-dark"])
    def test_page_chrome_follows_theme(
        self, dedicated_server, theme_args, light, theme_bg
    ):
        """The page chrome (body background) tracks the theme so a light
        terminal doesn't sit on a dark page or a dark one on white, yet is
        offset from the terminal so the terminal still reads as a distinct
        surface. Driven client-side from the theme in the size message."""
        server = dedicated_server()
        room_id = f"chrome-{random_id()}"
        password = f"secret-{random_id()}"

        proc = subprocess.Popen(
            CLI_COMMAND + [
                "-s", server.url, "-r", room_id, "-W", password,
            ] + theme_args,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            start_new_session=True,
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
                wait_for_terminal_background(page, theme_bg)
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
                "-s", server.url, "-r", room_id, "-W", password,
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            start_new_session=True,
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
                "-s", server.url, "-r", room_id, "-W", password,
                "--theme", "solarized-light",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            start_new_session=True,
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

