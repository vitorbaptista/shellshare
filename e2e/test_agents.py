"""
E2E tests for the machine-readable surfaces aimed at scripts and AI agents:

- The CLI's --json output contract (piped stream mode and exec mode)
- The `exec` subcommand: single command, live broadcast, exit code propagation
- `shellshare status --json`: recovering a live session's link from the env
- The discovery endpoints the website serves: /llms.txt, /robots.txt,
  /sitemap.xml, and the structured data on the home page
"""

import json
import os
import platform
import subprocess

import pytest
import requests

from conftest import (
    CLI_COMMAND,
    CLI_SESSION_TIMEOUT,
    SERVER_URL,
    SocketListener,
    parse_share_key,
    random_id,
    wait_for_content,
    wait_for_server,
)

IS_WINDOWS = platform.system() == "Windows"

def parse_json_events(stdout):
    """Parse the shellshare events out of stdout. Exec mode interleaves the
    command's raw PTY output between the event lines, and that output may
    leave stray bytes (e.g. a carriage return) on the event's line, so scan
    each line for a JSON object that has an "event" key (without assuming
    the object's key order)."""
    events = []
    for line in stdout.splitlines():
        for start in (i for i, ch in enumerate(line) if ch == '{'):
            try:
                obj = json.loads(line[start:])
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict) and "event" in obj:
                events.append(obj)
                break
    return events


class TestJsonContract:
    """--json: newline-delimited JSON events on stdout."""

    def test_stdin_mode_emits_sharing_and_end_events(self, unique_room, unique_password):
        # A distinctive marker lets us prove stdin is NOT teed to stdout in
        # --json mode: that mode owns stdout for the event protocol, so the
        # relayed bytes must never leak there and corrupt the JSON stream.
        marker = "hello-agents-marker"
        proc = subprocess.run(
            CLI_COMMAND
            + ["--json", "-s", SERVER_URL, "-r", unique_room, "-W", unique_password],
            input=marker + "\n",
            capture_output=True,
            text=True,
            timeout=CLI_SESSION_TIMEOUT,
        )
        assert proc.returncode == 0

        # The fed input must not appear on stdout, and every non-empty stdout
        # line must be parseable JSON (no teed log bytes interleaved).
        assert marker not in proc.stdout, f"stdin leaked to --json stdout: {proc.stdout!r}"
        lines = [line for line in proc.stdout.splitlines() if line.strip()]
        events = [json.loads(line) for line in lines]

        first = events[0]
        assert first["event"] == "sharing"
        # The share URL carries the decryption key in its #fragment, so
        # an agent that relays it gives the viewer everything to decrypt
        assert first["url"].startswith(f"{SERVER_URL}/r/{unique_room}#")
        assert parse_share_key(first["url"]) is not None
        assert first["room"] == unique_room
        assert first["server"] == SERVER_URL

        assert events[-1] == {"event": "end", "exit_code": 0}

    def test_json_mode_suppresses_prose(self, unique_room, unique_password):
        proc = subprocess.run(
            CLI_COMMAND
            + ["--json", "-s", SERVER_URL, "-r", unique_room, "-W", unique_password],
            input="hi\n",
            capture_output=True,
            text=True,
            timeout=CLI_SESSION_TIMEOUT,
        )
        assert "Sharing terminal in" not in proc.stdout + proc.stderr
        assert "Scan this QR code" not in proc.stdout + proc.stderr
        assert "End of transmission" not in proc.stdout + proc.stderr


class TestExecSubcommand:
    """exec: run one command, broadcast it, exit with its exit code."""

    @pytest.mark.skipif(IS_WINDOWS, reason="recipe uses a POSIX shell")
    def test_exec_broadcasts_command_output(self, unique_room, unique_password):
        marker = f"exec-marker-{random_id()}"
        listener = SocketListener(unique_room)
        listener.connect()
        try:
            proc = subprocess.run(
                CLI_COMMAND
                + ["exec", "--json", "-s", SERVER_URL, "-r", unique_room, "-W", unique_password]
                + ["--", "echo", marker],
                capture_output=True,
                text=True,
                timeout=CLI_SESSION_TIMEOUT,
                stdin=subprocess.DEVNULL,
            )
            assert proc.returncode == 0
            # The broadcast is encrypted: take the key from the JSON
            # share link so the listener can read the command's output
            events = parse_json_events(proc.stdout)
            listener.set_key(parse_share_key(events[0]["url"]))
            # The command's output reaches the viewers...
            assert wait_for_content(listener, lambda text: marker in text), \
                "viewer never received the exec'd command output"
        finally:
            listener.disconnect()

        # ...and the local stdout carries the JSON contract around it
        assert events[0]["event"] == "sharing"
        assert events[0]["url"].startswith(f"{SERVER_URL}/r/{unique_room}#")
        assert events[-1] == {"event": "end", "exit_code": 0}, \
            f"stdout was: {proc.stdout!r}"

    @pytest.mark.skipif(IS_WINDOWS, reason="recipe uses a POSIX shell")
    def test_exec_propagates_exit_code(self, unique_room, unique_password):
        proc = subprocess.run(
            CLI_COMMAND
            + ["exec", "--json", "-s", SERVER_URL, "-r", unique_room, "-W", unique_password]
            + ["--", "sh", "-c", "exit 3"],
            capture_output=True,
            text=True,
            timeout=CLI_SESSION_TIMEOUT,
            stdin=subprocess.DEVNULL,
        )
        assert proc.returncode == 3
        events = parse_json_events(proc.stdout)
        assert events[-1] == {"event": "end", "exit_code": 3}, \
            f"stdout was: {proc.stdout!r}"

    def test_exec_requires_command(self):
        proc = subprocess.run(
            CLI_COMMAND + ["exec", "--"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert proc.returncode != 0


class TestDiscoveryEndpoints:
    """The static surfaces agents and crawlers use to find/learn shellshare."""

    def test_llms_txt_served(self):
        wait_for_server(SERVER_URL)
        response = requests.get(f"{SERVER_URL}/llms.txt")
        assert response.status_code == 200
        # charset is required: without it browsers decode UTF-8 as Latin-1
        assert response.headers["content-type"] == "text/plain; charset=utf-8"
        assert "shellshare" in response.text
        # The agent contract must be documented
        assert "--json" in response.text
        assert '"event":"sharing"' in response.text.replace(" ", "")

    def test_robots_txt_allows_site_disallows_rooms(self):
        wait_for_server(SERVER_URL)
        response = requests.get(f"{SERVER_URL}/robots.txt")
        assert response.status_code == 200
        assert "Disallow: /r/" in response.text
        assert "Sitemap:" in response.text

    def test_sitemap_served(self):
        wait_for_server(SERVER_URL)
        response = requests.get(f"{SERVER_URL}/sitemap.xml")
        assert response.status_code == 200
        assert "<urlset" in response.text
        assert "https://shellshare.net/" in response.text

    def test_home_page_has_structured_data(self):
        wait_for_server(SERVER_URL)
        response = requests.get(SERVER_URL)
        assert response.status_code == 200
        assert 'application/ld+json' in response.text
        assert '"SoftwareApplication"' in response.text
        assert '"FAQPage"' in response.text


class TestStatusContract:
    """`shellshare status --json`: recover the live session's link.

    The broadcaster exports the share URL into the shell it spawns
    (`SHELLSHARE_URL`); `status` reads it back. No server or broadcast is
    needed to exercise the contract - just the environment.
    """

    def test_status_json_reads_session_from_env(self):
        url = f"{SERVER_URL}/r/demo-status#{'a' * 64}"
        proc = subprocess.run(
            CLI_COMMAND + ["status", "--json"],
            capture_output=True,
            text=True,
            timeout=CLI_SESSION_TIMEOUT,
            env={
                **os.environ,
                "SHELLSHARE_URL": url,
                "SHELLSHARE_ROOM": "demo-status",
            },
        )
        assert proc.returncode == 0
        assert json.loads(proc.stdout.strip()) == {
            "url": url,
            "room": "demo-status",
        }

    def test_status_json_outside_session_errors(self):
        env = {k: v for k, v in os.environ.items() if k != "SHELLSHARE_URL"}
        proc = subprocess.run(
            CLI_COMMAND + ["status", "--json"],
            capture_output=True,
            text=True,
            timeout=CLI_SESSION_TIMEOUT,
            env=env,
        )
        # The --json contract: errors on stderr, nothing on stdout, exit 1.
        assert proc.returncode == 1
        assert proc.stdout.strip() == ""
        assert "ERROR" in proc.stderr
