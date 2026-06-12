"""
E2E tests for end-to-end encryption, which is always on.

Every broadcast is sealed by the CLI into AES-256-GCM records before it
enters the replay buffer; the server stores, acks, and relays opaque
ciphertext; the viewer page decrypts with a key carried in the share
link's URL fragment, which browsers never send to the server.

Assertions on what the server relays must use raw bytes
(SocketListener.get_raw_bytes) or decrypt with the share-link key
(SocketListener.set_key / open_records); the browser decrypts on its
own via the fragment.
"""

import os
import re
import subprocess
import time

import pytest
from playwright.sync_api import sync_playwright

from conftest import (
    CLI_COMMAND,
    SERVER_URL,
    SocketListener,
    broadcast_message,
    open_records,
    parse_share_key,
    poll_until,
    random_id,
    terminal_text,
    wait_for_terminal_text,
)

KEY_FRAGMENT_RE = re.compile(r"#([0-9a-f]{64})$")


def parse_share_url(text):
    match = re.search(r"Sharing terminal in (\S+)", text)
    return match.group(1) if match else None


def run_cli(message, room, password, timeout=10):
    """Run the CLI in stdin mode until EOF.

    Returns (returncode, stderr, share_url).
    """
    args = CLI_COMMAND + ["--stdin", "-s", SERVER_URL, "-r", room, "-W", password]
    proc = subprocess.Popen(
        args,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    stdout, stderr = proc.communicate(input=message, timeout=timeout)
    return proc.returncode, stderr, parse_share_url(stderr)


def start_broadcaster(room, password, first_message):
    """Start a long-lived broadcast and deliver one message.

    The broadcaster must stay alive while viewers join: the room (and
    its history) is deleted when the CLI exits. Returns (proc,
    share_url); callers must finish_broadcaster() it.
    """
    proc = subprocess.Popen(
        CLI_COMMAND + ["--stdin", "-s", SERVER_URL, "-r", room, "-W", password],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    proc.stdin.write(first_message)
    proc.stdin.flush()
    # The share link is printed before the shell/stdin loop starts
    share_url = None
    deadline = time.time() + 10
    stderr_so_far = ""
    while time.time() < deadline and share_url is None:
        line = proc.stderr.readline()
        if not line:
            break
        stderr_so_far += line
        share_url = parse_share_url(stderr_so_far)
    assert share_url, f"No share link printed: {stderr_so_far}"
    return proc, share_url


def finish_broadcaster(proc):
    proc.stdin.close()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


class TestShareLink:
    def test_share_link_always_carries_hex_key_fragment(
        self, unique_room, unique_password
    ):
        returncode, stderr, share_url = run_cli(
            "hello", unique_room, unique_password
        )
        assert returncode == 0, f"CLI failed: {stderr}"
        assert share_url is not None, f"No share link in: {stderr}"
        assert f"/r/{unique_room}#" in share_url
        assert KEY_FRAGMENT_RE.search(share_url), (
            f"Fragment is not a 64-char hex key: {share_url}"
        )

    def test_keys_differ_across_rooms(self, unique_password):
        urls = [
            run_cli("x", f"key-{random_id()}", unique_password)[2]
            for _ in range(2)
        ]
        keys = [KEY_FRAGMENT_RE.search(u).group(1) for u in urls]
        assert keys[0] != keys[1]

    def test_named_room_key_is_stable_across_runs(
        self, unique_room, unique_password
    ):
        """The key is derived from this machine and the room name, so
        re-broadcasting to the same named room reproduces the same key -
        a link shared once keeps working after the broadcaster restarts.
        """
        _, _, url1 = run_cli("first\n", unique_room, unique_password)
        _, _, url2 = run_cli("second\n", unique_room, unique_password)
        key1, key2 = parse_share_key(url1), parse_share_key(url2)
        assert key1 is not None
        assert key1 == key2, "named-room key must be stable across runs"


class TestServerSeesOnlyCiphertext:
    def test_plaintext_never_reaches_the_server(
        self, unique_room, unique_password, socket_listener
    ):
        marker = f"TOP-SECRET-{random_id()}"

        returncode, stderr, share_url = run_cli(
            marker, unique_room, unique_password
        )
        assert returncode == 0, f"CLI failed: {stderr}"

        # Something must arrive (the sealed record)...
        assert poll_until(
            lambda: len(socket_listener.get_raw_bytes()) > 0, timeout=10
        ), "No data relayed to the viewer at all"
        # ...but the plaintext never does
        assert marker.encode() not in socket_listener.get_raw_bytes()
        # ...and it round-trips only with the share-link key
        key = parse_share_key(share_url)
        assert key is not None
        assert marker.encode() in open_records(
            key, socket_listener.get_raw_bytes()
        )

    def test_record_framing_and_overhead(
        self, unique_room, unique_password, socket_listener
    ):
        """One stdin chunk = one record: 4-byte length prefix counting
        the 12-byte nonce + ciphertext + 16-byte tag."""
        payload = "x" * 100

        run_cli(payload, unique_room, unique_password)

        assert poll_until(
            lambda: len(socket_listener.get_raw_bytes()) >= 4 + 28 + 100,
            timeout=10,
        ), "Sealed record did not arrive"
        data = socket_listener.get_raw_bytes()
        record_len = int.from_bytes(data[:4], "big")
        assert record_len == len(data) - 4, (
            f"Length prefix {record_len} != body {len(data) - 4}"
        )
        assert record_len == 12 + 100 + 16

    def test_listener_decrypts_with_share_key(
        self, unique_room, unique_password, socket_listener
    ):
        """The harness, given the share-link key, reads plaintext - the
        same capability a real viewer has and the server never does."""
        marker = f"ROUNDTRIP-{random_id()}"
        returncode, stderr, share_url = run_cli(
            marker, unique_room, unique_password
        )
        assert returncode == 0, f"CLI failed: {stderr}"

        socket_listener.set_key(parse_share_key(share_url))
        received = socket_listener.wait_for_message(timeout=5, containing=marker)
        assert received is not None and marker in received


class TestViewerDecryption:
    def test_viewer_decrypts_live_output(self, unique_password):
        room = f"enc-live-{random_id()}"
        marker = f"DECRYPTED-{random_id()}"
        proc, share_url = start_broadcaster(
            room, unique_password, f"{marker}\n"
        )
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch()
                page = browser.new_page()
                page.goto(share_url)
                wait_for_terminal_text(page, marker)
                # No error notice on the happy path
                assert page.is_hidden("#crypto-notice")
                browser.close()
        finally:
            finish_broadcaster(proc)

    def test_late_joiner_decrypts_history(self, unique_password):
        """History is a concatenation of whole records; a viewer who
        joins after several chunks must decrypt them all."""
        room = f"enc-late-{random_id()}"
        markers = [f"CHUNK{i}-{random_id(6)}" for i in range(3)]
        proc, share_url = start_broadcaster(
            room, unique_password, f"{markers[0]}\n"
        )
        try:
            for marker in markers[1:]:
                proc.stdin.write(f"{marker}\n")
                proc.stdin.flush()
            time.sleep(2)  # let the CLI deliver before the viewer joins

            with sync_playwright() as p:
                browser = p.chromium.launch()
                page = browser.new_page()
                page.goto(share_url)
                for marker in markers:
                    wait_for_terminal_text(page, marker)
                browser.close()
        finally:
            finish_broadcaster(proc)

    def test_missing_key_shows_notice_not_garbage(self, unique_password):
        room = f"enc-nokey-{random_id()}"
        marker = f"HIDDEN-{random_id()}"
        proc, share_url = start_broadcaster(
            room, unique_password, f"{marker}\n"
        )
        try:
            keyless_url = share_url.split("#")[0]
            with sync_playwright() as p:
                browser = p.chromium.launch()
                page = browser.new_page()
                page.goto(keyless_url)
                page.wait_for_selector("#crypto-notice:visible", timeout=10000)
                notice = page.text_content("#crypto-notice")
                assert "encrypted" in notice
                assert "missing" in notice
                # The terminal must stay empty: neither the plaintext
                # (it can't decrypt) nor rendered ciphertext garbage
                assert terminal_text(page).strip() == ""
                browser.close()
        finally:
            finish_broadcaster(proc)

    def test_wrong_key_shows_notice(self, unique_password):
        room = f"enc-wrongkey-{random_id()}"
        marker = f"HIDDEN-{random_id()}"
        proc, share_url = start_broadcaster(
            room, unique_password, f"{marker}\n"
        )
        try:
            wrong_url = share_url.split("#")[0] + "#" + ("ab" * 32)
            with sync_playwright() as p:
                browser = p.chromium.launch()
                page = browser.new_page()
                page.goto(wrong_url)
                page.wait_for_selector("#crypto-notice:visible", timeout=10000)
                notice = page.text_content("#crypto-notice")
                assert "decrypt" in notice
                assert marker not in terminal_text(page)
                browser.close()
        finally:
            finish_broadcaster(proc)

    def test_invalid_key_shows_notice(self, unique_password):
        room = f"enc-badkey-{random_id()}"
        proc, share_url = start_broadcaster(room, unique_password, "x\n")
        try:
            bad_url = share_url.split("#")[0] + "#not-hex-at-all"
            with sync_playwright() as p:
                browser = p.chromium.launch()
                page = browser.new_page()
                page.goto(bad_url)
                page.wait_for_selector("#crypto-notice:visible", timeout=10000)
                assert "invalid" in page.text_content("#crypto-notice")
                browser.close()
        finally:
            finish_broadcaster(proc)


class TestEdgeCases:
    def test_viewer_skips_records_it_cannot_decrypt(
        self, unique_room, unique_password
    ):
        """History can mix keys - e.g. a different machine (different
        derived key) re-broadcast to the same room name, leaving its
        records in history. A record the viewer's key can't open is
        skipped, not fatal: the records it CAN open still render, with
        no false 'wrong key' notice. Injected over raw WebSockets with
        two distinct keys, since one machine+room now yields one key."""
        key_a = os.urandom(32).hex()
        key_b = os.urandom(32).hex()
        old_marker = f"OTHERKEY-{random_id(6)}"
        new_marker = f"MYKEY-{random_id(6)}"

        # Two records in one room's history, sealed under different keys
        broadcast_message(
            SERVER_URL, unique_room, unique_password,
            text=f"{old_marker}\n", key=key_a,
        )
        broadcast_message(
            SERVER_URL, unique_room, unique_password,
            text=f"{new_marker}\n", key=key_b,
        )

        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page()
            page.goto(f"{SERVER_URL}/r/{unique_room}#{key_b}")
            # The key_b record decrypts and renders...
            wait_for_terminal_text(page, new_marker)
            # ...with no notice, and the key_a record never appears
            assert page.is_hidden("#crypto-notice")
            assert old_marker not in terminal_text(page)
            browser.close()

    def test_encrypted_viewer_survives_reconnect(self, unique_password):
        """After a viewer's socket drops and rejoins, the server replays
        history; the epoch logic must let that replay render, not discard
        it as stale in-flight decryption."""
        room = f"enc-reconnect-{random_id()}"
        marker = f"RECONNECT-{random_id(6)}"
        proc, share_url = start_broadcaster(
            room, unique_password, f"{marker}\n"
        )
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch()
                context = browser.new_context()
                page = context.new_page()
                page.goto(share_url)
                wait_for_terminal_text(page, marker)

                # Force a socket.io disconnect + reconnect
                context.set_offline(True)
                time.sleep(1)
                context.set_offline(False)

                # The replayed history must decrypt and render again
                wait_for_terminal_text(page, marker)
                assert page.is_hidden("#crypto-notice")
                browser.close()
        finally:
            finish_broadcaster(proc)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
