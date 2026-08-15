"""
Comprehensive API Tests for Shellshare Backend

These tests cover the HTTP API surface and the room semantics behind it.

The CLI broadcasts over WebSocket (GET /ws/r/:room): binary frames carry
raw terminal bytes, text frames carry JSON control messages. The room is
claimed - or its password verified - at the WS handshake. The legacy
POST /r/:room ingest route is retired and always returns 410 Gone.

Endpoints tested:
- GET / (home page, install options)
- GET /r/:room (room page)
- POST /r/:room (retired: 410 Gone)
- DELETE /r/:room (delete room)
- Static files (/bin/shellshare)

Authorization logic (now at the WS handshake):
- First connection to a room with a password "claims" that room
- Subsequent connections need the same password
- Different password returns 401

Edge cases tested:
- Missing/invalid headers
- Legacy POST bodies (well-formed, empty, malformed)
- Unsupported HTTP methods
- Room name edge cases
- Response body verification
"""

import http.client
import json
import re
import urllib.parse
from concurrent.futures import ThreadPoolExecutor

import pytest
import websocket

from conftest import (
    SERVER_URL,
    SocketListener,
    broadcast_message,
    random_id,
    wait_for_content,
    wait_for_server,
    ws_connect_room,
)

LEGACY_POST_GONE_BODY = (
    "This shellshare server no longer supports broadcasting over HTTP POST. "
    "Your client is too old: download the latest from this server's home page "
    "(it serves the binary at /bin/shellshare) and share again."
)


class CaseInsensitiveDict(dict):
    """A dict with case-insensitive key lookup."""
    def __getitem__(self, key):
        return super().__getitem__(key.lower())

    def get(self, key, default=None):
        return super().get(key.lower(), default)

    def __contains__(self, key):
        return super().__contains__(key.lower())


def make_request(method, path, headers=None, body=None):
    """Make an HTTP request and return (status, headers, body)."""
    parsed = urllib.parse.urlparse(SERVER_URL)
    conn = http.client.HTTPConnection(parsed.netloc)

    headers = headers or {}
    if body and isinstance(body, dict):
        body = json.dumps(body).encode()
        headers['Content-Type'] = 'application/json'

    conn.request(method, path, body=body, headers=headers)
    response = conn.getresponse()

    status = response.status
    # Use case-insensitive dict for headers (HTTP headers are case-insensitive)
    resp_headers = CaseInsensitiveDict({k.lower(): v for k, v in response.getheaders()})
    resp_body = response.read().decode('utf-8', errors='replace')

    conn.close()
    return status, resp_headers, resp_body


def make_raw_request(method, path, headers=None, body=None):
    """Make a raw HTTP request without automatic JSON conversion.

    Use this for testing malformed requests, wrong content types, etc.
    """
    parsed = urllib.parse.urlparse(SERVER_URL)
    conn = http.client.HTTPConnection(parsed.netloc)

    headers = headers or {}
    if body and isinstance(body, str):
        body = body.encode()

    conn.request(method, path, body=body, headers=headers)
    response = conn.getresponse()

    status = response.status
    # Use case-insensitive dict for headers (HTTP headers are case-insensitive)
    resp_headers = CaseInsensitiveDict({k.lower(): v for k, v in response.getheaders()})
    resp_body = response.read().decode('utf-8', errors='replace')

    conn.close()
    return status, resp_headers, resp_body


class TestHomePage:
    """Tests for GET / endpoint."""

    def test_home_page_returns_200(self):
        """GET / should return 200 OK."""
        wait_for_server(SERVER_URL)
        status, headers, body = make_request('GET', '/')
        assert status == 200, f"Expected 200, got {status}"
        assert 'text/html' in headers.get('Content-Type', ''), "Expected HTML response"


class TestInstallOptions:
    """Tests for the install option radios on the home page.

    The npx option (id="os-node") is checked by default regardless of
    User-Agent; the per-OS binary download options are present but unchecked.
    """

    def test_node_checked_by_default(self):
        """The npx install option should be pre-checked for any User-Agent."""
        wait_for_server(SERVER_URL)
        for ua in [
            "",
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        ]:
            status, headers, body = make_request('GET', '/', headers={"User-Agent": ua})
            assert status == 200, f"Expected 200, got {status}"
            node_input = re.search(r'<input[^>]*id="os-node"[^>]*>', body)
            assert node_input, "Could not find os-node input element"
            assert 'checked' in node_input.group(), \
                f"os-node should be checked for UA {ua!r}, got: {node_input.group()}"

    def test_npx_command_present(self):
        """The npx install command should be on the page."""
        wait_for_server(SERVER_URL)
        status, headers, body = make_request('GET', '/')
        assert status == 200, f"Expected 200, got {status}"
        assert 'npx shellshare' in body, "Expected npx install command on home page"

    def test_os_options_present_but_unchecked(self):
        """The per-OS binary options should exist and be unchecked by default."""
        wait_for_server(SERVER_URL)
        status, headers, body = make_request('GET', '/')
        assert status == 200, f"Expected 200, got {status}"
        for os_id in ["os-linux", "os-macos", "os-windows"]:
            os_input = re.search(rf'<input[^>]*id="{os_id}"[^>]*>', body)
            assert os_input, f"Could not find {os_id} input element"
            assert 'checked' not in os_input.group(), \
                f"{os_id} should NOT be checked by default, got: {os_input.group()}"


class TestRoomPage:
    """Tests for GET /r/:room endpoint."""

    def test_room_page_returns_200(self):
        """GET /r/:room should return 200 OK for any room name."""
        wait_for_server(SERVER_URL)
        room_id = f"test-{random_id()}"
        status, headers, body = make_request('GET', f'/r/{room_id}')
        assert status == 200, f"Expected 200, got {status}"
        assert 'text/html' in headers.get('Content-Type', ''), "Expected HTML response"

    def test_room_page_with_special_characters(self):
        """Room names can contain various characters."""
        wait_for_server(SERVER_URL)
        # Test with hyphens and underscores
        room_id = f"test-room_name-123"
        status, headers, body = make_request('GET', f'/r/{room_id}')
        assert status == 200, f"Expected 200, got {status}"


class TestLegacyPostRetired:
    """Tests for the retired POST /r/:room ingest route.

    The server no longer ingests broadcasts via HTTP POST: the route
    always returns 410 Gone with a fixed plain-text body pointing old
    clients at the binary download, regardless of request body or room.
    POSTing must have no side effects: it neither claims the room nor
    broadcasts anything to viewers.
    """

    @pytest.mark.parametrize("body,extra_headers", [
        pytest.param(
            json.dumps({"message": "SGVsbG8sIFdvcmxkIQ==",
                        "size": {"rows": 24, "cols": 80}}),
            {"Authorization": "a-password"},
            id="old-style-body",
        ),
        pytest.param("", {"Authorization": "a-password"}, id="empty-body"),
        pytest.param(
            "{ this is not valid json }",
            {"Authorization": "a-password"},
            id="malformed-json",
        ),
        pytest.param(
            json.dumps({"message": "SGVsbG8=",
                        "size": {"rows": 24, "cols": 80}}),
            {},
            id="no-authorization",
        ),
    ])
    def test_post_always_returns_410(self, body, extra_headers):
        """Any POST - well-formed, empty, malformed, unauthenticated -
        gets the same 410 Gone with the upgrade message."""
        wait_for_server(SERVER_URL)
        room_id = f"test-{random_id()}"

        status, headers, resp_body = make_raw_request(
            'POST', f'/r/{room_id}',
            headers={"Content-Type": "application/json", **extra_headers},
            body=body
        )

        assert status == 410, f"Expected 410, got {status}"
        assert resp_body == LEGACY_POST_GONE_BODY, f"Unexpected 410 body: {resp_body}"

    def test_post_does_not_claim_room(self):
        """A 410 POST must not claim the room: a WS connect with a
        different password afterwards still succeeds."""
        wait_for_server(SERVER_URL)
        room_id = f"test-{random_id()}"
        password1 = f"secret-{random_id()}"
        password2 = f"different-{random_id()}"

        body = {
            "message": "SGVsbG8=",
            "size": {"rows": 24, "cols": 80}
        }

        status, _, _ = make_request(
            'POST', f'/r/{room_id}',
            headers={"Authorization": password1},
            body=body
        )
        assert status == 410, f"Expected 410, got {status}"

        # The room is still unclaimed: any password can take it
        assert broadcast_message(SERVER_URL, room_id, password2, "claimed") == 200, \
            "POST must not claim the room"

    def test_post_does_not_broadcast(self, unique_room, socket_listener):
        """A 410 POST must not reach connected viewers."""
        wait_for_server(SERVER_URL)
        password = f"secret-{random_id()}"

        body = {
            "message": "SGVsbG8=",
            "size": {"rows": 24, "cols": 80}
        }

        status, _, _ = make_request(
            'POST', f'/r/{unique_room}',
            headers={"Authorization": password},
            body=body
        )
        assert status == 410, f"Expected 410, got {status}"

        msg = socket_listener.wait_for_message(timeout=2)
        assert msg is None, f"POST must not broadcast, but viewer received: {msg!r}"


class TestBroadcast:
    """Tests for broadcasting over the WebSocket ingest."""

    def test_broadcast_new_room_returns_200(self):
        """First broadcast to a new room should succeed."""
        wait_for_server(SERVER_URL)
        room_id = f"test-{random_id()}"
        password = f"secret-{random_id()}"

        status = broadcast_message(SERVER_URL, room_id, password, "Hello, World!")
        assert status == 200, f"Expected 200, got {status}"

    def test_broadcast_same_password_returns_200(self):
        """Subsequent broadcasts with same password should succeed."""
        wait_for_server(SERVER_URL)
        room_id = f"test-{random_id()}"
        password = f"secret-{random_id()}"

        # First broadcast claims the room
        status1 = broadcast_message(SERVER_URL, room_id, password, "First message")
        assert status1 == 200, f"First broadcast failed: {status1}"

        # Second broadcast with same password
        status2 = broadcast_message(SERVER_URL, room_id, password, "Second message")
        assert status2 == 200, f"Second broadcast failed: {status2}"

    def test_broadcast_wrong_password_returns_401(self):
        """Broadcast with wrong password should be rejected with 401."""
        wait_for_server(SERVER_URL)
        room_id = f"test-{random_id()}"
        password1 = f"secret-{random_id()}"
        password2 = f"different-{random_id()}"

        # First broadcast claims the room
        status1 = broadcast_message(SERVER_URL, room_id, password1, "Hello")
        assert status1 == 200, f"First broadcast failed: {status1}"

        # Second broadcast with different password is rejected at the handshake
        status2 = broadcast_message(SERVER_URL, room_id, password2, "Hello")
        assert status2 == 401, f"Expected 401, got {status2}"

    def test_broadcast_empty_password_claims_room(self):
        """Broadcast with empty password should claim room (anyone can write)."""
        wait_for_server(SERVER_URL)
        room_id = f"test-{random_id()}"

        # First broadcast with empty password
        status1 = broadcast_message(SERVER_URL, room_id, "", "Hello")
        assert status1 == 200, f"First broadcast failed: {status1}"

        # Second broadcast with empty password should also work
        status2 = broadcast_message(SERVER_URL, room_id, "", "Hello")
        assert status2 == 200, f"Second broadcast failed: {status2}"


class TestAuthorization:
    """Tests for room authorization logic at the WS handshake."""

    def test_first_connection_claims_room(self):
        """First connection to a room claims it with that password."""
        wait_for_server(SERVER_URL)
        room_id = f"test-{random_id()}"
        password = f"secret-{random_id()}"

        ws = ws_connect_room(SERVER_URL, room_id, password)
        try:
            # The handshake claimed the room: a wrong password is rejected
            assert broadcast_message(SERVER_URL, room_id, "wrong", "x") == 401, \
                "Should reject wrong password"
            # ... while the claiming password keeps working
            assert broadcast_message(SERVER_URL, room_id, password, "x") == 200, \
                "Claiming password should keep working"
        finally:
            ws.close()

    def test_authorization_is_room_specific(self):
        """Each room has its own authorization."""
        wait_for_server(SERVER_URL)
        room_id1 = f"test-{random_id()}"
        room_id2 = f"test-{random_id()}"
        password1 = f"secret-{random_id()}"
        password2 = f"secret-{random_id()}"

        # Claim room 1 with password 1
        status1 = broadcast_message(SERVER_URL, room_id1, password1, "Test")
        assert status1 == 200, "Failed to claim room 1"

        # Claim room 2 with password 2
        status2 = broadcast_message(SERVER_URL, room_id2, password2, "Test")
        assert status2 == 200, "Failed to claim room 2"

        # Password 1 should not work on room 2
        status3 = broadcast_message(SERVER_URL, room_id2, password1, "Test")
        assert status3 == 401, "Password 1 should not work on room 2"

    def test_missing_password_means_empty_password(self):
        """Connecting without an Authorization header claims the room
        with the empty-string password."""
        wait_for_server(SERVER_URL)
        room_id = f"test-{random_id()}"

        # Handshake with no Authorization header at all
        ws = websocket.create_connection(
            f"{SERVER_URL.replace('http://', 'ws://')}/ws/r/{room_id}",
            timeout=5,
        )
        ws.close()

        # The room was claimed as if with the empty password
        assert broadcast_message(SERVER_URL, room_id, "", "x") == 200, \
            "Empty password should match a header-less claim"
        assert broadcast_message(SERVER_URL, room_id, "something", "x") == 401, \
            "Non-empty password should be rejected after a header-less claim"

    def test_missing_password_on_claimed_room_is_rejected(self):
        """Connecting without auth on an already-claimed room should fail."""
        wait_for_server(SERVER_URL)
        room_id = f"test-{random_id()}"
        password = f"secret-{random_id()}"

        # Claim the room with a non-empty password
        assert broadcast_message(SERVER_URL, room_id, password, "Hello") == 200

        # Empty password is a different password: rejected
        assert broadcast_message(SERVER_URL, room_id, "", "Hello") == 401, \
            "Empty password should be rejected on a room claimed with a password"


class TestDeleteRoom:
    """Tests for DELETE /r/:room endpoint (unchanged by the WS migration)."""

    def test_delete_room_returns_202(self):
        """DELETE with valid auth should return 202 Accepted."""
        wait_for_server(SERVER_URL)
        room_id = f"test-{random_id()}"
        password = f"secret-{random_id()}"

        # First create the room
        assert broadcast_message(SERVER_URL, room_id, password, "Hello") == 200

        # Then delete it
        status, _, resp_body = make_request(
            'DELETE', f'/r/{room_id}',
            headers={"Authorization": password}
        )

        assert status == 202, f"Expected 202, got {status}"
        # Express sendStatus(202) sends "Accepted" as body
        assert resp_body == "Accepted", f"Expected 'Accepted' body, got: {resp_body}"

    def test_delete_wrong_password_returns_401(self):
        """DELETE with wrong password should return 401."""
        wait_for_server(SERVER_URL)
        room_id = f"test-{random_id()}"
        password1 = f"secret-{random_id()}"
        password2 = f"different-{random_id()}"

        # First create the room
        assert broadcast_message(SERVER_URL, room_id, password1, "Hello") == 200

        # Try to delete with wrong password
        status, _, resp_body = make_request(
            'DELETE', f'/r/{room_id}',
            headers={"Authorization": password2}
        )

        assert status == 401, f"Expected 401, got {status}"
        # Express sendStatus(401) sends "Unauthorized" as body
        assert resp_body == "Unauthorized", f"Expected 'Unauthorized' body, got: {resp_body}"

    def test_delete_unclaimed_room_returns_202(self):
        """DELETE on an unclaimed room should return 202."""
        wait_for_server(SERVER_URL)
        room_id = f"test-{random_id()}"
        password = f"secret-{random_id()}"

        # Try to delete a room that was never created
        status, _, _ = make_request(
            'DELETE', f'/r/{room_id}',
            headers={"Authorization": password}
        )

        # Should succeed (idempotent delete)
        assert status == 202, f"Expected 202, got {status}"

    def test_delete_without_authorization_header(self):
        """DELETE without Authorization header on unclaimed room."""
        wait_for_server(SERVER_URL)
        room_id = f"test-{random_id()}"

        # DELETE without any Authorization header on unclaimed room
        status, _, _ = make_request(
            'DELETE', f'/r/{room_id}',
            headers={}  # No Authorization header
        )

        assert status == 202, f"Expected 202 for DELETE without auth on unclaimed room, got {status}"


class TestDeleteEdgeCases:
    """Tests for DELETE edge cases and side effects."""

    def test_delete_room_twice(self):
        """DELETE same room twice should be idempotent."""
        wait_for_server(SERVER_URL)
        room_id = f"test-{random_id()}"
        password = f"secret-{random_id()}"

        # Create room
        assert broadcast_message(SERVER_URL, room_id, password, "Hello") == 200

        # Delete once
        status1, _, _ = make_request(
            'DELETE', f'/r/{room_id}',
            headers={"Authorization": password}
        )
        assert status1 == 202, f"First delete failed: {status1}"

        # Delete again
        status2, _, _ = make_request(
            'DELETE', f'/r/{room_id}',
            headers={"Authorization": password}
        )
        # Second delete should also succeed (idempotent)
        assert status2 == 202, f"Second delete should be idempotent, got {status2}"

    def test_broadcast_after_delete_reclaims_room(self):
        """After DELETE, the room name is free to claim with a new password."""
        wait_for_server(SERVER_URL)
        room_id = f"test-{random_id()}"
        password1 = f"secret-{random_id()}"
        password2 = f"different-{random_id()}"

        # Create room with password1
        assert broadcast_message(SERVER_URL, room_id, password1, "Hello") == 200

        # Delete room
        status, _, _ = make_request(
            'DELETE', f'/r/{room_id}',
            headers={"Authorization": password1}
        )
        assert status == 202, f"Failed to delete room: {status}"

        # Reclaim with a different password
        assert broadcast_message(SERVER_URL, room_id, password2, "Hello") == 200, \
            "Room should be claimable with a new password after delete"

    def test_delete_clears_room_messages(self):
        """After DELETE, late joiners should not receive old messages."""
        wait_for_server(SERVER_URL)
        room_id = f"test-{random_id()}"
        password = f"secret-{random_id()}"

        # Create room with a message
        assert broadcast_message(SERVER_URL, room_id, password, "this-should-be-deleted") == 200

        # Delete room
        status, _, _ = make_request(
            'DELETE', f'/r/{room_id}',
            headers={"Authorization": password}
        )
        assert status == 202, f"DELETE failed: {status}"

        # A late joiner must not see the deleted history
        late = SocketListener(room_id)
        late.connect()
        try:
            msg = late.wait_for_message(timeout=2, containing="this-should-be-deleted")
            assert msg is None, "Deleted room history leaked to a late joiner"
        finally:
            late.disconnect()


class TestStaticFiles:
    """Tests for static file serving."""

    def test_script_exe_exists(self):
        """GET /bin/shellshare should return 200."""
        wait_for_server(SERVER_URL)
        status, headers, body = make_request('GET', '/bin/shellshare')
        assert status == 200, f"Expected 200, got {status}"
        assert 'application/octet-stream' in headers.get('Content-Type', ''), \
            f"Expected octet-stream, got {headers.get('Content-Type')}"

    def test_static_files_have_cache_headers(self):
        """Static files should have cache headers."""
        wait_for_server(SERVER_URL)
        status, headers, body = make_request('GET', '/bin/shellshare')
        assert 'Cache-Control' in headers, "Expected Cache-Control header"


class TestMessageDelivery:
    """Tests that broadcast content actually reaches viewers."""

    def test_special_characters_in_message(self, unique_room, unique_password, socket_listener):
        """Messages with special characters and unicode should survive the pipeline."""
        special_message = "Hello! @#$%^&*() 日本語 émoji 🎉\n\t\r"

        status = broadcast_message(SERVER_URL, unique_room, unique_password, special_message)
        assert status == 200, f"Expected 200, got {status}"

        ok = wait_for_content(socket_listener, lambda acc: "日本語 émoji 🎉" in acc, timeout=5)
        assert ok, "Special characters did not reach the viewer intact"

    def test_large_message_handling(self, unique_room, unique_password, socket_listener):
        """Server should handle reasonably large messages."""
        # A 10KB message with a marker at the end
        large_message = "X" * 10000 + "END-MARKER;"

        status = broadcast_message(SERVER_URL, unique_room, unique_password, large_message)
        assert status == 200, f"Expected 200, got {status}"

        ok = wait_for_content(
            socket_listener,
            lambda acc: "END-MARKER;" in acc and acc.count("X") >= 10000,
            timeout=10,
        )
        assert ok, "Large message did not reach the viewer in full"


class TestMessageAccumulation:
    """Tests for message accumulation behavior."""

    def test_messages_accumulate_in_order(self):
        """Multiple broadcasts should accumulate in order for late joiners."""
        wait_for_server(SERVER_URL)
        room_id = f"test-{random_id()}"
        password = f"secret-{random_id()}"

        messages = ["AAA;", "BBB;", "CCC;"]
        for msg in messages:
            status = broadcast_message(SERVER_URL, room_id, password, msg)
            assert status == 200, f"Broadcast failed: {status}"

        late = SocketListener(room_id)
        late.connect()
        try:
            ok = wait_for_content(late, lambda acc: "CCC;" in acc, timeout=5)
            assert ok, "History never arrived"
            accumulated = late.get_accumulated_messages()
            assert accumulated.index("AAA;") < accumulated.index("BBB;") < accumulated.index("CCC;"), \
                f"Messages out of order: {accumulated!r}"
        finally:
            late.disconnect()


class TestConcurrentBroadcasts:
    """Tests for concurrent broadcast handling."""

    def test_rapid_broadcasts_same_room(self):
        """Rapid broadcasts to same room with same password should all succeed."""
        wait_for_server(SERVER_URL)
        room_id = f"test-{random_id()}"
        password = f"secret-{random_id()}"

        results = []
        for i in range(10):
            status = broadcast_message(SERVER_URL, room_id, password, f"Message {i}")
            results.append(status)

        # All should succeed
        assert all(s == 200 for s in results), f"Not all broadcasts succeeded: {results}"

    def test_concurrent_broadcasts_different_rooms(self):
        """Concurrent broadcasts to different rooms should all succeed independently."""
        wait_for_server(SERVER_URL)
        rooms = [
            (f"test-{random_id()}", f"secret-{random_id()}")
            for _ in range(5)
        ]

        def claim_and_broadcast(room_password):
            room_id, password = room_password
            return broadcast_message(SERVER_URL, room_id, password, f"hello from {room_id}")

        with ThreadPoolExecutor(max_workers=5) as pool:
            results = list(pool.map(claim_and_broadcast, rooms))

        assert all(s == 200 for s in results), f"Not all broadcasts succeeded: {results}"


class TestEdgeCases:
    """Tests for edge cases and error handling."""

    def test_room_names_are_case_sensitive(self):
        """Room names should be case sensitive."""
        wait_for_server(SERVER_URL)
        room_lower = f"test-{random_id()}"
        room_upper = room_lower.upper()
        password = f"secret-{random_id()}"

        # Claim lowercase room
        status1 = broadcast_message(SERVER_URL, room_lower, password, "Test")
        assert status1 == 200, "Failed to claim lowercase room"

        # Uppercase room should be different (can claim with any password)
        status2 = broadcast_message(SERVER_URL, room_upper, "different", "Test")
        assert status2 == 200, "Uppercase room should be independent"


class TestUnsupportedMethods:
    """Tests for unsupported HTTP methods on /r/:room.

    POST is not in this class: the route still exists (it returns
    410 Gone, covered by TestLegacyPostRetired), so it is not an
    unsupported method.
    """

    def test_head_room(self):
        """HEAD /r/:room should work like GET but with no body."""
        wait_for_server(SERVER_URL)
        room_id = f"test-{random_id()}"

        status, headers, resp_body = make_request('HEAD', f'/r/{room_id}')

        # HEAD should return same status as GET
        assert status == 200, f"Expected 200 for HEAD, got {status}"
        # HEAD should not have a body
        assert resp_body == "", f"HEAD response should have no body, got: {resp_body[:100]}"


class TestRoomNameEdgeCases:
    """Tests for edge cases in room names."""

    def test_room_with_dots(self):
        """Room names can contain dots."""
        wait_for_server(SERVER_URL)
        room_id = f"test.room.name.{random_id()}"

        status, headers, body = make_request('GET', f'/r/{room_id}')
        assert status == 200, f"Expected 200 for room with dots, got {status}"

    def test_room_with_path_traversal_attempt(self):
        """Room names with .. should be handled safely."""
        wait_for_server(SERVER_URL)

        # Try path traversal - should not escape /r/ route
        status, headers, body = make_request('GET', '/r/../')
        # This would navigate to / which returns the home page
        assert status in [200, 301, 302, 404], f"Got unexpected status {status}"

    def test_room_with_url_encoded_characters(self):
        """Room names with URL-encoded characters."""
        wait_for_server(SERVER_URL)
        room_id = f"test%20room%2F{random_id()}"

        status, headers, body = make_request('GET', f'/r/{room_id}')
        assert status in [200, 400, 404], f"Got unexpected status {status}"

        # Claiming such a room over WS should behave consistently
        claim_status = broadcast_message(SERVER_URL, room_id, "secret", "Test")
        assert claim_status in [200, 400, 404], \
            f"Got unexpected WS claim status {claim_status}"

    def test_room_with_unicode(self):
        """Room names with unicode characters."""
        wait_for_server(SERVER_URL)
        room_id = f"テスト-{random_id()}"
        # URL-encode the unicode characters for HTTP request
        encoded_room_id = urllib.parse.quote(room_id, safe='')

        status, headers, body = make_request('GET', f'/r/{encoded_room_id}')
        assert status == 200, f"Expected 200 for unicode room name, got {status}"

        # Unicode rooms can be claimed over WS too
        claim_status = broadcast_message(SERVER_URL, encoded_room_id, "secret", "Test")
        assert claim_status == 200, f"Expected 200 for WS claim, got {claim_status}"

    def test_room_with_very_long_name(self):
        """Room names that are very long (>1000 chars)."""
        wait_for_server(SERVER_URL)
        room_id = "x" * 1000

        status, headers, body = make_request('GET', f'/r/{room_id}')
        # Document actual behavior - might succeed or return 414 URI Too Long
        assert status in [200, 414], f"Got unexpected status {status} for long room name"

    def test_empty_room_name(self):
        """GET /r/ with no room name."""
        wait_for_server(SERVER_URL)

        status, headers, body = make_request('GET', '/r/')
        # Might return 404 or redirect or serve something
        assert status in [200, 301, 302, 404], f"Got unexpected status {status} for empty room"

    def test_room_with_only_numbers(self):
        """Room names with only numbers."""
        wait_for_server(SERVER_URL)
        room_id = "12345678901234567890"

        status, headers, body = make_request('GET', f'/r/{room_id}')
        assert status == 200, f"Expected 200 for numeric room name, got {status}"

    def test_room_with_special_chars(self):
        """Room names with special characters."""
        wait_for_server(SERVER_URL)
        room_id = f"test-room_name.123-{random_id()}"

        # Viewer page works...
        status1, _, _ = make_request('GET', f'/r/{room_id}')
        assert status1 == 200, f"Expected 200 for GET, got {status1}"

        # ... and the room can be claimed and broadcast to over WS
        status2 = broadcast_message(SERVER_URL, room_id, "secret", "Test")
        assert status2 == 200, f"Expected 200 for WS broadcast, got {status2}"


class TestRoomNameEdgeCasesAdvanced:
    """Additional room name edge cases."""

    def test_room_with_double_slash(self):
        """Room name starting with slash like //foo."""
        wait_for_server(SERVER_URL)
        # This creates path /r//foo
        room_id = "/test-room"

        status, headers, body = make_request('GET', f'/r/{room_id}')
        # Document behavior - might be 200 or 404
        assert status in [200, 301, 404], f"Got unexpected status {status}"

    def test_room_with_semicolon(self):
        """Room name with semicolon."""
        wait_for_server(SERVER_URL)
        room_id = f"test;room;{random_id()}"

        status, headers, body = make_request('GET', f'/r/{room_id}')
        assert status in [200, 400], f"Got unexpected status {status}"


class TestSizeFieldBehavior:
    """Tests for size control-frame leniency.

    Exact passthrough of a size with extra fields and the null-size case
    are already covered by test_viewer_ws.py (size passthrough tests);
    only the missing-cols/rows case is exercised here.
    """

    def test_size_missing_cols_and_rows_produces_no_size_event(
        self, unique_room, unique_password, socket_listener
    ):
        """A size object without both cols and rows is forwarded but
        produces no size event for viewers."""
        status = broadcast_message(
            SERVER_URL, unique_room, unique_password,
            "after-partial-size", size={"rows": 24}
        )
        assert status == 200, f"Expected 200, got {status}"

        # The message itself still arrives...
        msg = socket_listener.wait_for_message(timeout=5, containing="after-partial-size")
        assert msg is not None, "Message did not arrive"
        # ... but no size event is emitted for the partial size
        assert socket_listener.get_last_size() is None, \
            f"Expected no size event, got {socket_listener.get_last_size()}"


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
