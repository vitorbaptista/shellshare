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

import os
import urllib.error
import urllib.parse
import urllib.request

from conftest import SERVER_URL, broadcast_message, open_records, random_id


def _get_bin(room):
    """GET /r/<room>.bin -> (status, body bytes)."""
    url = f"{SERVER_URL}/r/{urllib.parse.quote(room, safe='')}.bin"
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


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

    status, body = _get_bin(unique_room)
    assert status == 200
    assert len(body) > 0, "no history bytes served"
    # server-blind: the plaintext never appears in the relayed ciphertext
    assert marker.encode() not in body
    # ...but it round-trips with the share-link key
    assert marker.encode() in open_records(key, body)


def test_bin_unknown_room_is_404():
    status, _ = _get_bin(f"no-such-room-{random_id()}")
    assert status == 404
