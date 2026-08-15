"""
E2E Tests for the viewer's SGR colour handling

Colours can be written two ways: the classic `38;2;R;G;B` and the ITU
sub-parameter form `38:2:R:G:B`, which modern TUIs (herdr, ratatui apps)
emit. The vendored xterm.js reads the colon form as
`38:2:<colour-space>:R:G:B` and takes R,G,B from the 4th/5th/6th slots,
so a short `38:2:R:G:B` shifts every channel one place: a blue #89b4fa
arrives as rgb(180,250,0), a yellow-green, and whole UIs come out wrong.
The viewer normalizes those params before writing to the terminal.

Test categories:
- Both spellings of one colour render identically (foreground and
  background), including when the sequence is split across frames
"""

import os

import pytest
from playwright.sync_api import sync_playwright

from conftest import (
    SERVER_URL,
    broadcast_message,
    random_id,
    wait_for_server,
)

# Catppuccin blue - the colour herdr paints its active tab with
BLUE = 0x89B4FA


def cell_colors(page, row, col):
    """The (fg, bg) of one rendered cell, as xterm.js resolved them."""
    return page.evaluate(
        """([row, col]) => {
            const line = window.term.buffer.active.getLine(row);
            if (!line) return null;
            const cell = line.getCell(col);
            if (!cell) return null;
            return {
                fg: cell.isFgRGB() ? cell.getFgColor() : -1,
                bg: cell.isBgRGB() ? cell.getBgColor() : -1,
            };
        }""",
        [row, col],
    )


@pytest.mark.parametrize(
    "payload,label",
    [
        ("\x1b[48:2:137:180:250m \x1b[0m", "one frame"),
        # The same sequence split so the params land in two frames: the
        # viewer must hold the unterminated tail rather than rewrite half
        # a sequence.
        (None, "split across frames"),
    ],
)
def test_colon_form_colour_matches_semicolon_form(payload, label):
    """`38:2:R:G:B` and `38;2;R;G;B` are the same colour, so they must
    render the same. Without normalization the colon form renders
    rgb(180,250,0)."""
    room_id = f"test-{random_id()}"
    password = f"secret-{random_id()}"
    key = os.urandom(32).hex()

    wait_for_server(SERVER_URL)

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.goto(f"{SERVER_URL}/r/{room_id}#{key}")
        page.wait_for_selector("#terminal", timeout=10000)
        page.wait_for_function(
            "document.getElementById('online-counter').textContent !== '0'",
            timeout=10000,
        )

        if payload is None:
            # Split mid-sequence, right inside the colour parameters.
            broadcast_message(SERVER_URL, room_id, password, "\x1b[48:2:137", key=key)
            broadcast_message(
                SERVER_URL, room_id, password, ":180:250m \x1b[0m", key=key
            )
        else:
            broadcast_message(SERVER_URL, room_id, password, payload, key=key)
        # Same colour, classic spelling, on the next line.
        broadcast_message(
            SERVER_URL, room_id, password,
            "\r\n\x1b[48;2;137;180;250m \x1b[0m", key=key,
        )

        page.wait_for_function(
            """() => {
                const l = window.term.buffer.active.getLine(1);
                return l && l.getCell(0) && l.getCell(0).isBgRGB();
            }""",
            timeout=10000,
        )

        colon = cell_colors(page, 0, 0)
        semi = cell_colors(page, 1, 0)
        browser.close()

    assert semi["bg"] == BLUE, f"semicolon form is the control: {semi}"
    assert colon["bg"] == BLUE, (
        f"colon form ({label}) rendered {colon['bg']:#08x}, expected "
        f"{BLUE:#08x} - the colour-space slot shift is back"
    )
