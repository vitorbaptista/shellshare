"""
E2E tests for the room name in the viewer page title.

The viewer puts a user-chosen room name in the tab title, but leaves an
auto-generated id out (it's noise). It can't see the broadcaster's CLI,
so it guesses from the name's length: an auto id is exactly 18 chars
(see generate_room_id in src/cli/mod.rs). The title is set from the URL
alone, so these load the viewer page directly - no broadcaster required.
"""

import re
import subprocess

import pytest
from playwright.sync_api import sync_playwright

from conftest import CLI_COMMAND, SERVER_URL

DEFAULT_TITLE = "shellshare - live terminal broadcast"
SHARE_LINK_RE = re.compile(r"Sharing terminal in (\S+)")


@pytest.mark.parametrize(
    "room, expected_title",
    [
        # Human-chosen names: shown in the title.
        ("standup", "standup - shellshare"),
        ("team-demo", "team-demo - shellshare"),
        ("Q3Review", "Q3Review - shellshare"),
        # Auto-generated length (exactly 18 chars): hidden.
        ("aB3dEf6hIj9kLm2nOp", DEFAULT_TITLE),
        ("abcdefghijklmnopqr", DEFAULT_TITLE),
        # 17 chars: just shy of the generated length, so shown.
        ("abcdefghijklmnopq", "abcdefghijklmnopq - shellshare"),
    ],
)
def test_room_name_in_page_title(room, expected_title):
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.goto(f"{SERVER_URL}/r/{room}")
        page.wait_for_selector("#terminal", timeout=10000)
        assert page.title() == expected_title
        browser.close()


def test_real_auto_generated_room_keeps_default_title(unique_password):
    """Lockstep guard: the viewer's heuristic encodes the length of
    generate_room_id (src/cli/mod.rs). Run the real CLI so an actual
    auto-generated id - not a hand-written look-alike - lands in the URL,
    and assert the title stays default. If the generator's length drifts
    out of the heuristic's recognition, this fails instead of silently
    mistitling every shared room."""
    # The title comes from the URL alone, so the broadcast need not stay
    # live; just capture the printed link and visit it.
    returncode, stdout, stderr = _run_cli("hello", unique_password)
    assert returncode == 0, f"CLI failed: {stderr}"

    match = SHARE_LINK_RE.search(stdout + stderr)
    assert match, f"No share link in CLI output: {stdout}\n{stderr}"
    share_url = match.group(1)

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.goto(share_url)
        page.wait_for_selector("#terminal", timeout=10000)
        assert page.title() == DEFAULT_TITLE
        browser.close()


def _run_cli(message, password, server=SERVER_URL, timeout=30):
    """Run the CLI in stdin mode with no -r, so the room id is
    auto-generated. Returns (returncode, stdout, stderr)."""
    proc = subprocess.Popen(
        CLI_COMMAND + ["-s", server, "-W", password],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    stdout, stderr = proc.communicate(input=message, timeout=timeout)
    return proc.returncode, stdout, stderr
