"""E2E tests for agent read-access: GET /r/:room.bin.

An AI agent (or any non-browser consumer) reads a broadcast without the
shellshare CLI. A one-shot snapshot is HTTP `GET /r/<room>.bin`, which
returns the room's accumulated history as raw bytes - opaque ciphertext
when the broadcast is encrypted (the default), so the server stays an
end-to-end-encryption-blind relay. The consumer decrypts client-side
with the key from the share link's #fragment.

Live following reuses the existing viewer WebSocket (`/ws/v/r/:room`,
covered by test_viewer_ws.py), so it needs no new server test here.
"""

import json
import os
import subprocess
import urllib.error
import urllib.parse
import urllib.request

from conftest import (
    CLI_COMMAND,
    CLI_SESSION_TIMEOUT,
    SERVER_URL,
    broadcast_message,
    open_records,
    parse_share_key,
    poll_until,
    random_id,
)


def _get_bin(room):
    """GET /r/<room>.bin -> (status, body bytes, headers)."""
    url = f"{SERVER_URL}/r/{urllib.parse.quote(room, safe='')}.bin"
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            return resp.status, resp.read(), resp.headers
    except urllib.error.HTTPError as e:
        return e.code, e.read(), e.headers


def test_bin_serves_ciphertext_that_decrypts_with_the_share_key(
    unique_room, unique_password
):
    """The snapshot is exactly what a viewer's history frame carries:
    ciphertext the server can't read, plaintext only with the link key."""
    key = os.urandom(32).hex()
    marker = f"BINSNAP-{random_id()}"

    # broadcast_message awaits the ack, so the record is durable on return
    broadcast_message(
        SERVER_URL, unique_room, unique_password, text=f"{marker}\n", key=key
    )

    status, body, headers = _get_bin(unique_room)
    assert status == 200
    # The snapshot is live, mutating data. The `.bin` suffix that makes
    # it agent-friendly is exactly what puts it on a CDN's default
    # cacheable-extension list, so the origin must say no-store or an
    # agent polling the room gets a month-old body back.
    assert "no-store" in headers.get("Cache-Control", ""), (
        "the snapshot is cacheable: a CDN will freeze it"
    )
    assert len(body) > 0, "no history bytes served"
    # server-blind: the plaintext never appears in the relayed ciphertext
    assert marker.encode() not in body
    # ...but it round-trips with the share-link key
    assert marker.encode() in open_records(key, body)


def test_room_outlives_the_process_and_a_rerun_starts_fresh(
    unique_room, unique_password
):
    """The reported bug, and the freshness that has to come with it.

    `dmesg | shellshare --json` must leave a link that still works after
    the process exits - no `sleep` holding it open. And because the room
    now survives, rerunning the same name has to replace the previous
    session rather than stack the new one under its scrollback.
    """
    first = f"RUN1-{random_id()}"
    second = f"RUN2-{random_id()}"

    def run(marker):
        proc = subprocess.run(
            CLI_COMMAND
            + ["--json", "-s", SERVER_URL, "-r", unique_room, "-W", unique_password],
            input=marker + "\n",
            capture_output=True,
            text=True,
            timeout=CLI_SESSION_TIMEOUT,
        )
        assert proc.returncode == 0
        # Same room name and password -> same derived key every run
        return parse_share_key(json.loads(proc.stdout.splitlines()[0])["url"])

    # The process is gone, but the link still resolves to its output.
    # Polled: shutdown's ack drain gives up after 1s, so the last frame
    # can still be in flight when the process is reaped
    key = run(first)
    assert poll_until(
        lambda: first.encode() in open_records(key, _get_bin(unique_room)[1]),
        timeout=5,
    ), "the room did not outlive the broadcaster with its output"

    key = run(second)
    assert poll_until(
        lambda: second.encode() in open_records(key, _get_bin(unique_room)[1]),
        timeout=5,
    ), "the second run's output never reached the room"
    plaintext = open_records(key, _get_bin(unique_room)[1])
    assert first.encode() not in plaintext, "the first run's history was not cleared"


def test_bin_unknown_room_is_404():
    status, _, _ = _get_bin(f"no-such-room-{random_id()}")
    assert status == 404
