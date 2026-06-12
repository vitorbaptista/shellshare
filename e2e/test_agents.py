"""
E2E tests for the machine-readable surfaces aimed at scripts and AI agents:

- The CLI's --json output contract (stdin mode and exec mode)
- The `exec` subcommand: single command, live broadcast, exit code propagation
- The discovery endpoints the website serves: /llms.txt, /robots.txt,
  /sitemap.xml, and the structured data on the home page
"""

import json
import platform
import subprocess

import pytest
import requests

from conftest import (
    CLI_COMMAND,
    SERVER_URL,
    SocketListener,
    random_id,
    wait_for_content,
    wait_for_server,
)

IS_WINDOWS = platform.system() == "Windows"


def parse_json_events(stdout):
    """Parse the shellshare events out of stdout. Exec mode interleaves the
    command's raw PTY output between the event lines, and that output may
    leave stray bytes (e.g. a carriage return) on the event's line, so look
    for a JSON object anywhere in each line."""
    events = []
    for line in stdout.splitlines():
        start = line.find('{"event"')
        if start == -1:
            continue
        try:
            events.append(json.loads(line[start:]))
        except json.JSONDecodeError:
            pass
    return events


class TestJsonContract:
    """--json: newline-delimited JSON events on stdout."""

    def test_stdin_mode_emits_sharing_and_end_events(self, unique_room, unique_password):
        proc = subprocess.run(
            CLI_COMMAND
            + ["--stdin", "--json", "-s", SERVER_URL, "-r", unique_room, "-W", unique_password],
            input="hello agents\n",
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert proc.returncode == 0

        lines = [line for line in proc.stdout.splitlines() if line.strip()]
        first = json.loads(lines[0])
        assert first["event"] == "sharing"
        assert first["url"] == f"{SERVER_URL}/r/{unique_room}"
        assert first["room"] == unique_room
        assert first["server"] == SERVER_URL

        last = json.loads(lines[-1])
        assert last == {"event": "end", "exit_code": 0}

    def test_json_mode_suppresses_prose(self, unique_room, unique_password):
        proc = subprocess.run(
            CLI_COMMAND
            + ["--stdin", "--json", "-s", SERVER_URL, "-r", unique_room, "-W", unique_password],
            input="hi\n",
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert "Sharing terminal in" not in proc.stdout + proc.stderr
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
                timeout=20,
                stdin=subprocess.DEVNULL,
            )
            assert proc.returncode == 0
            # The command's output reaches the viewers...
            assert wait_for_content(listener, lambda text: marker in text), \
                "viewer never received the exec'd command output"
        finally:
            listener.disconnect()

        # ...and the local stdout carries the JSON contract around it
        events = parse_json_events(proc.stdout)
        assert events[0]["event"] == "sharing"
        assert events[0]["url"] == f"{SERVER_URL}/r/{unique_room}"
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
            timeout=20,
            stdin=subprocess.DEVNULL,
        )
        assert proc.returncode == 3
        events = parse_json_events(proc.stdout)
        assert events[-1] == {"event": "end", "exit_code": 3}, \
            f"stdout was: {proc.stdout!r}"

    def test_exec_rejects_stdin_flag(self, unique_room, unique_password):
        # --stdin would win over exec and silently drop the command
        proc = subprocess.run(
            CLI_COMMAND
            + ["exec", "--stdin", "--json", "-s", SERVER_URL]
            + ["-r", unique_room, "-W", unique_password, "--", "echo", "hi"],
            capture_output=True,
            text=True,
            timeout=15,
            stdin=subprocess.DEVNULL,
        )
        assert proc.returncode != 0
        assert "ERROR" in proc.stderr

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
