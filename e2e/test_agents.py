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
import re
import subprocess
from html import unescape

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


def visible_html_text(document):
    """Approximate what a no-JavaScript text extractor can read."""
    without_scripts = re.sub(
        r"<(script|style)\b[^>]*>.*?</\1>", " ", document,
        flags=re.IGNORECASE | re.DOTALL,
    )
    return " ".join(unescape(re.sub(r"<[^>]+>", " ", without_scripts)).split())

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
        assert "## When to use shellshare" in response.text
        assert "## Developer resources" in response.text
        for resource in ["/docs", "/about", "/contact", "/privacy"]:
            assert resource in response.text

    def test_home_page_is_complete_without_javascript(self):
        """The raw response must carry semantic, substantial page content."""
        wait_for_server(SERVER_URL)
        response = requests.get(SERVER_URL)
        assert response.status_code == 200
        h1 = re.search(r"<h1\b[^>]*>(.*?)</h1>", response.text,
                       flags=re.IGNORECASE | re.DOTALL)
        assert h1, "The server-rendered homepage must contain an H1"
        assert "shellshare" in visible_html_text(h1.group(1)).lower().replace(" ", "")
        assert len(visible_html_text(response.text)) >= 500

    @pytest.mark.parametrize("accept,expected_type", [
        ("text/markdown", "text/markdown; charset=utf-8"),
        ("text/markdown, text/html;q=0.8", "text/markdown; charset=utf-8"),
        ("text/html", "text/html; charset=utf-8"),
        ("text/markdown;q=0, text/html", "text/html; charset=utf-8"),
        ("text/html;q=0.5, text/*;q=1", "text/markdown; charset=utf-8"),
        ("*/*", "text/html; charset=utf-8"),
    ])
    def test_markdown_content_negotiation(self, accept, expected_type):
        """Canonical URLs honor q-values, specificity, and the HTML default."""
        wait_for_server(SERVER_URL)
        response = requests.get(SERVER_URL, headers={"Accept": accept})
        assert response.status_code == 200
        assert response.headers["content-type"] == expected_type
        vary = {part.strip().lower() for part in response.headers["vary"].split(",")}
        assert {"accept", "accept-encoding"} <= vary
        if expected_type.startswith("text/markdown"):
            assert response.text.startswith("# Shellshare")
        else:
            assert response.text.startswith("<!DOCTYPE html>")

    def test_unsatisfiable_accept_returns_406(self):
        wait_for_server(SERVER_URL)
        response = requests.get(SERVER_URL, headers={"Accept": "application/pdf"})
        assert response.status_code == 406
        assert response.headers["content-type"] == "text/plain; charset=utf-8"
        assert "Accept" in response.headers["vary"]
        assert "text/markdown" in response.text

    @pytest.mark.parametrize("path,title", [
        ("/docs", "Shellshare Developer Documentation"),
        ("/about", "About Shellshare"),
        ("/contact", "Contact Shellshare"),
        ("/privacy", "Shellshare Privacy"),
    ])
    def test_public_content_pages_have_substantial_html(self, path, title):
        wait_for_server(SERVER_URL)
        html_response = requests.get(f"{SERVER_URL}{path}")
        assert html_response.status_code == 200
        assert html_response.headers["content-type"] == "text/html; charset=utf-8"
        assert "Shellshare" in re.search(
            r"<title>(.*?)</title>", html_response.text, re.DOTALL
        ).group(1)
        h1 = re.search(r"<h1\b[^>]*>(.*?)</h1>", html_response.text,
                       flags=re.IGNORECASE | re.DOTALL)
        assert h1 and title in visible_html_text(h1.group(1))
        assert len(visible_html_text(html_response.text)) >= 500

    def test_developer_docs_name_integration_boundaries(self):
        wait_for_server(SERVER_URL)
        response = requests.get(f"{SERVER_URL}/docs")
        for term in ["API", "authentication", "OpenAPI", "webhook", "MCP",
                     "WebSocket", "--json"]:
            assert term in response.text

    def test_unknown_paths_return_recoverable_markdown_404(self):
        wait_for_server(SERVER_URL)
        response = requests.get(f"{SERVER_URL}/some-path-that-does-not-exist")
        assert response.status_code == 404
        assert response.headers["content-type"] == "text/markdown; charset=utf-8"
        assert response.text.startswith("# 404:")
        for resource in ["/docs", "/llms.txt", "/sitemap.xml"]:
            assert resource in response.text

    def test_html_and_markdown_have_distinct_etags(self):
        wait_for_server(SERVER_URL)
        html_response = requests.get(SERVER_URL, headers={"Accept": "text/html"})
        markdown_response = requests.get(
            SERVER_URL, headers={"Accept": "text/markdown"}
        )
        assert html_response.headers["etag"] != markdown_response.headers["etag"]
        replay = requests.get(
            SERVER_URL,
            headers={
                "Accept": "text/markdown",
                "If-None-Match": markdown_response.headers["etag"],
            },
        )
        assert replay.status_code == 304
        assert "Accept" in replay.headers["vary"]

    def test_rooms_are_noindex_rather_than_disallowed(self):
        """Rooms stay out of search indexes without an agent allowlist.

        `noindex` needs the page to be fetchable to work at all, so a
        `Disallow: /r/` would block the one directive that says what we
        mean - and telling a crawler apart from a human's assistant
        opening a pasted link would mean naming every assistant forever.
        The header is about the resource instead of who asked.
        """
        wait_for_server(SERVER_URL)
        robots = requests.get(f"{SERVER_URL}/robots.txt")
        assert robots.status_code == 200
        assert "Sitemap:" in robots.text
        # Directives only, so a comment mentioning one cannot trip this
        directives = [l.strip() for l in robots.text.splitlines() if not l.startswith("#")]
        assert not [l for l in directives if l.startswith("Disallow")]

        page = requests.get(f"{SERVER_URL}/r/{random_id()}")
        assert page.headers.get("x-robots-tag") == "noindex"
        # ...and on the bytes, which are not HTML and so cannot carry
        # the page's noindex meta tag
        assert requests.get(f"{SERVER_URL}/r/{random_id()}.bin").status_code == 404
        room, password = random_id(), random_id()
        subprocess.run(
            CLI_COMMAND + ["--json", "-s", SERVER_URL, "-r", room, "-W", password],
            input="indexed?\n", capture_output=True, text=True, timeout=CLI_SESSION_TIMEOUT,
        )
        assert requests.get(f"{SERVER_URL}/r/{room}.bin").headers.get("x-robots-tag") == "noindex"

    def test_room_page_and_llms_txt_inline_the_same_reader(self):
        """A pasted share link teaches an agent to read the stream.

        The room page is a JavaScript app: fetched as text it would say
        nothing about what it holds. templates/agent.mjs is the one
        reader and both surfaces inline it, so neither costs a second
        request - what this pins is that the two stay the same bytes.
        Two things break that silently and neither fails the boot:
        deleting a placeholder (nothing observes whether the
        substitution matched) and losing the HTML escaping the page
        needs to survive a parser.
        """
        wait_for_server(SERVER_URL)
        page = requests.get(f"{SERVER_URL}/r/{random_id()}")
        assert page.status_code == 200

        # Newlines normalized on both sides: a Windows checkout serves
        # CRLF, and what this pins is the code, not the line endings
        embedded = page.text.replace("\r\n", "\n")
        docs = requests.get(f"{SERVER_URL}/llms.txt").text.replace("\r\n", "\n")
        code = docs.split("```js\n", 1)[1].split("\n```", 1)[0].strip()
        assert "function decode(" in code, "llms.txt no longer inlines the reader"

        # Compared escaped, not unescaped: the reader holds `<` that an
        # HTML parser would eat as a tag, so unescaping the page first
        # would erase the very property this pins
        escaped = code.replace("&", "&amp;").replace("<", "&lt;")
        assert escaped in embedded, "room page drifted from the llms.txt reader"

        # Not served on its own: whoever is reading already has it
        assert requests.get(f"{SERVER_URL}/agent.mjs").status_code == 404

        # Inside <main>: a whole family of extractors (pandoc among
        # them) keeps only <main> and discards the rest of the page
        assert (
            embedded.index('<main class="main">')
            < embedded.index(escaped)
            < embedded.index("</main>")
        )
        # ...and ahead of the terminal, so the agent meets it first
        assert embedded.index(escaped) < embedded.index('<div id="terminal">')
        # The prose must stay OUTSIDE the <details> holding the decoder.
        # Collapsed details are absent from a browser's innerText, so an
        # agent reading rendered text sees only what is outside it - the
        # prose, which names /llms.txt and keeps that agent unstuck.
        # Fold the two together and that reader silently gets nothing.
        # (<summary> is a <details>'s first child, so prose ahead of it
        # is prose outside it)
        assert embedded.index("AI agent?") < embedded.index("<summary")

    def test_inlined_reader_runs(self, tmp_path, unique_room, unique_password):
        """The reader we hand agents actually reads a broadcast.

        Everything above pins where the reader lands; this pins that it
        works, running the copy taken verbatim from /llms.txt. Each
        assertion is a way it once returned a wrong answer while
        reporting success, which no amount of lint or page-shape testing
        caught: output truncated at a pipe's 64KB buffer, an unreachable
        server read as a finished broadcast, a link stripped of its
        #fragment printing ciphertext as if it were the terminal, and a
        hostile escape sequence reaching the reader's own terminal.
        """
        wait_for_server(SERVER_URL)
        docs = requests.get(f"{SERVER_URL}/llms.txt").text.replace("\r\n", "\n")
        code = docs.split("```js\n", 1)[1].split("\n```", 1)[0]
        assert "function decode(" in code, "llms.txt no longer inlines the reader"
        reader = tmp_path / "agent.mjs"
        reader.write_text(code, encoding="utf-8", newline="\n")

        # Comfortably past a pipe buffer, and marked so truncation shows.
        # Then two OSC 52s, either of which would write the reader's
        # clipboard: one terminated, which the shaped pattern matches,
        # and one left open, which only the catch-all ESC drop can
        # catch - and the catch-all is what covers a sequence split
        # across two frames, where no shaped pattern ever matches.
        lines = [f"LINE-{i:04d}-" + "x" * 40 for i in range(4000)]
        proc = subprocess.run(
            CLI_COMMAND + ["--json", "-s", SERVER_URL, "-r", unique_room, "-W", unique_password],
            input="\n".join(lines) + "\x1b]52;c;aGFjaw==\x07\x1b]52;c;dW50ZXJt\n",
            capture_output=True,
            text=True,
            timeout=CLI_SESSION_TIMEOUT,
        )
        assert proc.returncode == 0
        first = parse_json_events(proc.stdout)[0]
        assert first["event"] == "sharing"
        url = first["url"]

        def run(*args):
            return subprocess.run(
                ["node", str(reader), *args],
                capture_output=True,
                text=True,
                timeout=CLI_SESSION_TIMEOUT,
            )

        # capture_output pipes stdout, which is how an agent runs it -
        # and `process.exit()` with anything still buffered in that pipe
        # drops it and still reports success
        got = run(url)
        assert got.returncode == 0, got.stderr
        assert lines[0] in got.stdout
        assert lines[-1] in got.stdout, "reader truncated its own output"
        assert "\x1b" not in got.stdout, "an escape sequence survived the strip"

        # --follow reaches the same output over the WebSocket, and ends
        # by itself once the broadcaster is gone. The server learns that
        # when the ingest socket closes, which can lag the CLI's exit -
        # and until it does there is nothing to end the follow, so wait
        # for the event rather than racing it.
        watcher = SocketListener(unique_room)
        watcher.connect()
        try:
            assert watcher.wait_for_broadcasting(False, timeout=15), \
                "server still thinks the broadcaster is attached"
        finally:
            watcher.disconnect()
        live = run(url, "--follow")
        assert live.returncode == 0, live.stderr
        assert lines[0] in live.stdout
        assert lines[-1] in live.stdout

        # A link that lost its #fragment is not a plaintext broadcast:
        # printing the ciphertext would look like a successful read
        keyless = run(url.split("#", 1)[0])
        assert keyless.returncode == 1
        assert "#key" in keyless.stderr
        assert not keyless.stdout

        # ...and a server that was never there is not a broadcast that
        # ended. --follow is the path that got this wrong: a socket that
        # never opened closed like any other, and reported success.
        dead = run("http://127.0.0.1:9/r/nope", "--follow")
        assert dead.returncode == 1
        assert "cannot reach" in dead.stderr

    def test_sitemap_served(self):
        wait_for_server(SERVER_URL)
        response = requests.get(f"{SERVER_URL}/sitemap.xml")
        assert response.status_code == 200
        assert "<urlset" in response.text
        for path in ["/", "/docs", "/about", "/contact", "/privacy",
                     "/llms.txt"]:
            assert f"<loc>https://shellshare.net{path}</loc>" in response.text

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
