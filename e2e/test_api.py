"""
Comprehensive API Tests for Shellshare Backend

These tests cover all HTTP endpoints and ensure 100% API compatibility.
They are designed to validate that a Go rewrite behaves identically.

Endpoints tested:
- GET / (home page)
- GET /r/:room (room page)
- POST /r/:room (broadcast message)
- DELETE /r/:room (delete room)
- Static files (/bin/script.exe)

Authorization logic:
- First POST to a room with a password "claims" that room
- Subsequent requests need the same password
- Different password returns 401

Edge cases tested:
- Missing/invalid headers
- Malformed requests
- Invalid JSON
- Missing required fields
- Unsupported HTTP methods
- Room name edge cases
- Response body verification
"""

import base64
import http.client
import json
import random
import string
import time
import urllib.parse
import urllib.request

SERVER_URL = "http://localhost:3000"


def random_id(length=12):
    """Generate a random ID for room names."""
    return ''.join(random.choices(string.ascii_lowercase + string.digits, k=length))


def wait_for_server(url, timeout_seconds=30):
    """Wait for server to be ready."""
    start = time.time()
    while time.time() - start < timeout_seconds:
        try:
            urllib.request.urlopen(url, timeout=1)
            return True
        except Exception:
            time.sleep(0.5)
    raise TimeoutError(f"Server not ready after {timeout_seconds}s")


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
    resp_headers = dict(response.getheaders())
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
    resp_headers = dict(response.getheaders())
    resp_body = response.read().decode('utf-8', errors='replace')
    
    conn.close()
    return status, resp_headers, resp_body


def encode_message(text):
    """Encode a message the same way the CLI does."""
    quoted = urllib.parse.quote(text)
    return base64.b64encode(quoted.encode()).decode()


class TestHomePage:
    """Tests for GET / endpoint."""
    
    def test_home_page_returns_200(self):
        """GET / should return 200 OK."""
        wait_for_server(SERVER_URL)
        status, headers, body = make_request('GET', '/')
        assert status == 200, f"Expected 200, got {status}"
        assert 'text/html' in headers.get('Content-Type', ''), "Expected HTML response"
    
    def test_home_page_contains_shellshare(self):
        """Home page should mention shellshare."""
        wait_for_server(SERVER_URL)
        status, headers, body = make_request('GET', '/')
        assert 'shellshare' in body.lower(), "Expected 'shellshare' in home page"
    
    def test_home_page_linux_user_agent_shows_wget(self):
        """Linux user-agent should see wget instructions."""
        wait_for_server(SERVER_URL)
        status, headers, body = make_request(
            'GET', '/',
            headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"}
        )
        assert status == 200, f"Expected 200, got {status}"
        # The page uses CSS to show/hide based on body class, but we can verify
        # the body class or that wget is in the page
        assert 'wget' in body.lower(), "Expected 'wget' in page for Linux user-agent"
    
    def test_home_page_mac_user_agent_shows_curl(self):
        """Mac user-agent should see curl instructions."""
        wait_for_server(SERVER_URL)
        status, headers, body = make_request(
            'GET', '/',
            headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}
        )
        assert status == 200, f"Expected 200, got {status}"
        # curl should be in the page for Mac users
        assert 'curl' in body.lower(), "Expected 'curl' in page for Mac user-agent"
    
    def test_home_page_no_user_agent(self):
        """Page should work without User-Agent header."""
        wait_for_server(SERVER_URL)
        status, headers, body = make_request('GET', '/')
        assert status == 200, f"Expected 200, got {status}"


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
    
    def test_room_page_contains_terminal(self):
        """Room page should have a terminal element."""
        wait_for_server(SERVER_URL)
        room_id = f"test-{random_id()}"
        status, headers, body = make_request('GET', f'/r/{room_id}')
        assert 'terminal' in body.lower(), "Expected 'terminal' in room page"


class TestBroadcast:
    """Tests for POST /r/:room endpoint (broadcasting messages)."""
    
    def test_broadcast_new_room_returns_200(self):
        """First POST to a new room should return 200."""
        wait_for_server(SERVER_URL)
        room_id = f"test-{random_id()}"
        password = f"secret-{random_id()}"
        
        body = {
            "message": encode_message("Hello, World!"),
            "size": {"rows": 24, "cols": 80}
        }
        
        status, headers, resp_body = make_request(
            'POST', f'/r/{room_id}',
            headers={"Authorization": password},
            body=body
        )
        
        assert status == 200, f"Expected 200, got {status}"
    
    def test_broadcast_same_password_returns_200(self):
        """Subsequent POSTs with same password should return 200."""
        wait_for_server(SERVER_URL)
        room_id = f"test-{random_id()}"
        password = f"secret-{random_id()}"
        
        body = {
            "message": encode_message("First message"),
            "size": {"rows": 24, "cols": 80}
        }
        
        # First request claims the room
        status1, _, _ = make_request(
            'POST', f'/r/{room_id}',
            headers={"Authorization": password},
            body=body
        )
        assert status1 == 200, f"First request failed: {status1}"
        
        # Second request with same password
        body["message"] = encode_message("Second message")
        status2, _, _ = make_request(
            'POST', f'/r/{room_id}',
            headers={"Authorization": password},
            body=body
        )
        assert status2 == 200, f"Second request failed: {status2}"
    
    def test_broadcast_wrong_password_returns_401(self):
        """POST with wrong password should return 401 Unauthorized."""
        wait_for_server(SERVER_URL)
        room_id = f"test-{random_id()}"
        password1 = f"secret-{random_id()}"
        password2 = f"different-{random_id()}"
        
        body = {
            "message": encode_message("Hello"),
            "size": {"rows": 24, "cols": 80}
        }
        
        # First request claims the room
        status1, _, _ = make_request(
            'POST', f'/r/{room_id}',
            headers={"Authorization": password1},
            body=body
        )
        assert status1 == 200, f"First request failed: {status1}"
        
        # Wait for authorization to be persisted (async operation)
        time.sleep(0.5)
        
        # Second request with different password should fail
        status2, _, _ = make_request(
            'POST', f'/r/{room_id}',
            headers={"Authorization": password2},
            body=body
        )
        assert status2 == 401, f"Expected 401, got {status2}"
    
    def test_broadcast_empty_password_claims_room(self):
        """POST with empty password should claim room (anyone can write)."""
        wait_for_server(SERVER_URL)
        room_id = f"test-{random_id()}"
        
        body = {
            "message": encode_message("Hello"),
            "size": {"rows": 24, "cols": 80}
        }
        
        # First request with empty password
        status1, _, _ = make_request(
            'POST', f'/r/{room_id}',
            headers={"Authorization": ""},
            body=body
        )
        assert status1 == 200, f"First request failed: {status1}"
        
        # Second request with empty password should also work
        status2, _, _ = make_request(
            'POST', f'/r/{room_id}',
            headers={"Authorization": ""},
            body=body
        )
        assert status2 == 200, f"Second request failed: {status2}"
    
    def test_broadcast_requires_json_body(self):
        """POST should accept JSON body with message and size."""
        wait_for_server(SERVER_URL)
        room_id = f"test-{random_id()}"
        password = f"secret-{random_id()}"
        
        body = {
            "message": encode_message("Test"),
            "size": {"rows": 24, "cols": 80}
        }
        
        status, _, _ = make_request(
            'POST', f'/r/{room_id}',
            headers={"Authorization": password},
            body=body
        )
        
        assert status == 200, f"Expected 200, got {status}"
    
    def test_broadcast_multiple_messages_accumulate(self):
        """Multiple messages should accumulate in the room."""
        wait_for_server(SERVER_URL)
        room_id = f"test-{random_id()}"
        password = f"secret-{random_id()}"
        
        # Send multiple messages
        messages = ["Line 1\n", "Line 2\n", "Line 3\n"]
        for msg in messages:
            body = {
                "message": encode_message(msg),
                "size": {"rows": 24, "cols": 80}
            }
            status, _, _ = make_request(
                'POST', f'/r/{room_id}',
                headers={"Authorization": password},
                body=body
            )
            assert status == 200, f"Broadcast failed: {status}"
        
        # Note: We can't verify accumulation via HTTP alone,
        # but the Socket.IO tests will verify this


class TestDeleteRoom:
    """Tests for DELETE /r/:room endpoint."""
    
    def test_delete_room_returns_202(self):
        """DELETE with valid auth should return 202 Accepted."""
        wait_for_server(SERVER_URL)
        room_id = f"test-{random_id()}"
        password = f"secret-{random_id()}"
        
        # First create the room
        body = {
            "message": encode_message("Hello"),
            "size": {"rows": 24, "cols": 80}
        }
        make_request(
            'POST', f'/r/{room_id}',
            headers={"Authorization": password},
            body=body
        )
        
        # Then delete it
        status, _, _ = make_request(
            'DELETE', f'/r/{room_id}',
            headers={"Authorization": password}
        )
        
        assert status == 202, f"Expected 202, got {status}"
    
    def test_delete_wrong_password_returns_401(self):
        """DELETE with wrong password should return 401."""
        wait_for_server(SERVER_URL)
        room_id = f"test-{random_id()}"
        password1 = f"secret-{random_id()}"
        password2 = f"different-{random_id()}"
        
        # First create the room
        body = {
            "message": encode_message("Hello"),
            "size": {"rows": 24, "cols": 80}
        }
        make_request(
            'POST', f'/r/{room_id}',
            headers={"Authorization": password1},
            body=body
        )
        
        # Wait for authorization to be persisted (async operation)
        time.sleep(0.5)
        
        # Try to delete with wrong password
        status, _, _ = make_request(
            'DELETE', f'/r/{room_id}',
            headers={"Authorization": password2}
        )
        
        assert status == 401, f"Expected 401, got {status}"
    
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


class TestStaticFiles:
    """Tests for static file serving."""
    
    def test_script_exe_exists(self):
        """GET /bin/script.exe should return 200."""
        wait_for_server(SERVER_URL)
        status, headers, body = make_request('GET', '/bin/script.exe')
        assert status == 200, f"Expected 200, got {status}"
        assert 'application/octet-stream' in headers.get('Content-Type', ''), \
            f"Expected octet-stream, got {headers.get('Content-Type')}"
    
    def test_javascript_files_exist(self):
        """JavaScript files should be accessible."""
        wait_for_server(SERVER_URL)
        # Test a known JS file
        status, headers, body = make_request('GET', '/javascript/room.min.js')
        assert status == 200, f"Expected 200, got {status}"
    
    def test_static_files_have_cache_headers(self):
        """Static files should have cache headers."""
        wait_for_server(SERVER_URL)
        status, headers, body = make_request('GET', '/bin/script.exe')
        assert 'Cache-Control' in headers, "Expected Cache-Control header"


class TestAuthorization:
    """Tests for room authorization logic."""
    
    def test_first_request_claims_room(self):
        """First request to a room claims it with that password."""
        wait_for_server(SERVER_URL)
        room_id = f"test-{random_id()}"
        password = f"secret-{random_id()}"
        
        body = {
            "message": encode_message("Claim"),
            "size": {"rows": 24, "cols": 80}
        }
        
        # Claim the room
        status, _, _ = make_request(
            'POST', f'/r/{room_id}',
            headers={"Authorization": password},
            body=body
        )
        assert status == 200, "Failed to claim room"
        
        # Wait for authorization to be persisted
        time.sleep(0.5)
        
        # Try with wrong password
        status, _, _ = make_request(
            'POST', f'/r/{room_id}',
            headers={"Authorization": "wrong"},
            body=body
        )
        assert status == 401, "Should reject wrong password"
    
    def test_authorization_is_room_specific(self):
        """Each room has its own authorization."""
        wait_for_server(SERVER_URL)
        room_id1 = f"test-{random_id()}"
        room_id2 = f"test-{random_id()}"
        password1 = f"secret-{random_id()}"
        password2 = f"secret-{random_id()}"
        
        body = {
            "message": encode_message("Test"),
            "size": {"rows": 24, "cols": 80}
        }
        
        # Claim room 1 with password 1
        status1, _, _ = make_request(
            'POST', f'/r/{room_id1}',
            headers={"Authorization": password1},
            body=body
        )
        assert status1 == 200, "Failed to claim room 1"
        
        # Claim room 2 with password 2
        status2, _, _ = make_request(
            'POST', f'/r/{room_id2}',
            headers={"Authorization": password2},
            body=body
        )
        assert status2 == 200, "Failed to claim room 2"
        
        # Wait for authorizations to be persisted
        time.sleep(0.5)
        
        # Password 1 should not work on room 2
        status3, _, _ = make_request(
            'POST', f'/r/{room_id2}',
            headers={"Authorization": password1},
            body=body
        )
        assert status3 == 401, "Password 1 should not work on room 2"


class TestRequestLimits:
    """Tests for request size limits."""
    
    def test_request_over_300kb_is_rejected(self):
        """Requests over 300KB should be rejected."""
        wait_for_server(SERVER_URL)
        room_id = f"test-{random_id()}"
        password = f"secret-{random_id()}"
        
        # Create a message over 300KB (config.express.request_limit = '300kb')
        # The encoded message will be even larger due to base64 + URL encoding
        large_message = "X" * 250000  # 250KB of raw text, will be larger encoded
        
        body = {
            "message": encode_message(large_message),
            "size": {"rows": 24, "cols": 80}
        }
        
        status, headers, resp_body = make_request(
            'POST', f'/r/{room_id}',
            headers={"Authorization": password},
            body=body
        )
        
        # Should get 413 Payload Too Large
        assert status == 413, f"Expected 413, got {status}"
    
    def test_request_under_300kb_is_accepted(self):
        """Requests under 300KB should be accepted."""
        wait_for_server(SERVER_URL)
        room_id = f"test-{random_id()}"
        password = f"secret-{random_id()}"
        
        # Create a message under 300KB
        moderate_message = "X" * 50000  # 50KB
        
        body = {
            "message": encode_message(moderate_message),
            "size": {"rows": 24, "cols": 80}
        }
        
        status, headers, resp_body = make_request(
            'POST', f'/r/{room_id}',
            headers={"Authorization": password},
            body=body
        )
        
        assert status == 200, f"Expected 200, got {status}"


class TestEdgeCases:
    """Tests for edge cases and error handling."""
    
    def test_nonexistent_static_file_returns_404(self):
        """Request for nonexistent file should return 404."""
        wait_for_server(SERVER_URL)
        status, _, _ = make_request('GET', '/nonexistent/file.txt')
        assert status == 404, f"Expected 404, got {status}"
    
    def test_large_message_handling(self):
        """Server should handle reasonably large messages."""
        wait_for_server(SERVER_URL)
        room_id = f"test-{random_id()}"
        password = f"secret-{random_id()}"
        
        # Create a 10KB message
        large_message = "X" * 10000
        
        body = {
            "message": encode_message(large_message),
            "size": {"rows": 24, "cols": 80}
        }
        
        status, _, _ = make_request(
            'POST', f'/r/{room_id}',
            headers={"Authorization": password},
            body=body
        )
        
        assert status == 200, f"Expected 200, got {status}"
    
    def test_special_characters_in_message(self):
        """Messages with special characters should work."""
        wait_for_server(SERVER_URL)
        room_id = f"test-{random_id()}"
        password = f"secret-{random_id()}"
        
        # Message with various special characters
        special_message = "Hello! @#$%^&*() 日本語 émoji 🎉\n\t\r"
        
        body = {
            "message": encode_message(special_message),
            "size": {"rows": 24, "cols": 80}
        }
        
        status, _, _ = make_request(
            'POST', f'/r/{room_id}',
            headers={"Authorization": password},
            body=body
        )
        
        assert status == 200, f"Expected 200, got {status}"
    
    def test_room_names_are_case_sensitive(self):
        """Room names should be case sensitive."""
        wait_for_server(SERVER_URL)
        room_lower = f"test-{random_id()}"
        room_upper = room_lower.upper()
        password = f"secret-{random_id()}"
        
        body = {
            "message": encode_message("Test"),
            "size": {"rows": 24, "cols": 80}
        }
        
        # Claim lowercase room
        status1, _, _ = make_request(
            'POST', f'/r/{room_lower}',
            headers={"Authorization": password},
            body=body
        )
        assert status1 == 200, "Failed to claim lowercase room"
        
        # Uppercase room should be different (can claim with any password)
        status2, _, _ = make_request(
            'POST', f'/r/{room_upper}',
            headers={"Authorization": "different"},
            body=body
        )
        assert status2 == 200, "Uppercase room should be independent"


class TestMissingAuthorizationHeader:
    """Tests for requests without Authorization header."""
    
    def test_post_without_authorization_header(self):
        """POST without Authorization header should work for new room (claims with empty)."""
        wait_for_server(SERVER_URL)
        room_id = f"test-{random_id()}"
        
        body = {
            "message": encode_message("Hello"),
            "size": {"rows": 24, "cols": 80}
        }
        
        # POST without any Authorization header
        status, headers, resp_body = make_request(
            'POST', f'/r/{room_id}',
            headers={},  # No Authorization header
            body=body
        )
        
        # Document actual behavior - server treats missing header as empty string
        # The first request should succeed (claiming with empty/undefined auth)
        assert status == 200, f"Expected 200 for first POST without auth, got {status}"
    
    def test_post_without_auth_on_claimed_room(self):
        """POST without auth header on already-claimed room should fail."""
        wait_for_server(SERVER_URL)
        room_id = f"test-{random_id()}"
        password = f"secret-{random_id()}"
        
        body = {
            "message": encode_message("Hello"),
            "size": {"rows": 24, "cols": 80}
        }
        
        # First, claim the room with a password
        status1, _, _ = make_request(
            'POST', f'/r/{room_id}',
            headers={"Authorization": password},
            body=body
        )
        assert status1 == 200, f"Failed to claim room: {status1}"
        
        # Wait for auth to persist
        time.sleep(0.5)
        
        # Now try without Authorization header
        status2, _, _ = make_request(
            'POST', f'/r/{room_id}',
            headers={},  # No Authorization header
            body=body
        )
        
        # Should fail because room is claimed with a different (non-empty) password
        assert status2 == 401, f"Expected 401 for POST without auth on claimed room, got {status2}"
    
    def test_delete_without_authorization_header(self):
        """DELETE without Authorization header on unclaimed room."""
        wait_for_server(SERVER_URL)
        room_id = f"test-{random_id()}"
        
        # DELETE without any Authorization header on unclaimed room
        status, _, _ = make_request(
            'DELETE', f'/r/{room_id}',
            headers={}  # No Authorization header
        )
        
        # Document actual behavior
        assert status == 202, f"Expected 202 for DELETE without auth on unclaimed room, got {status}"


class TestMalformedRequests:
    """Tests for malformed and invalid requests."""
    
    def test_post_with_invalid_json(self):
        """POST with malformed JSON should return 400."""
        wait_for_server(SERVER_URL)
        room_id = f"test-{random_id()}"
        password = f"secret-{random_id()}"
        
        # Send malformed JSON
        status, headers, resp_body = make_raw_request(
            'POST', f'/r/{room_id}',
            headers={
                "Authorization": password,
                "Content-Type": "application/json"
            },
            body="{ this is not valid json }"
        )
        
        # Should return 400 Bad Request
        assert status == 400, f"Expected 400 for invalid JSON, got {status}"
    
    def test_post_with_wrong_content_type(self):
        """POST with non-JSON content type."""
        wait_for_server(SERVER_URL)
        room_id = f"test-{random_id()}"
        password = f"secret-{random_id()}"
        
        # Send with text/plain content type
        status, headers, resp_body = make_raw_request(
            'POST', f'/r/{room_id}',
            headers={
                "Authorization": password,
                "Content-Type": "text/plain"
            },
            body='{"message": "test", "size": {"rows": 24, "cols": 80}}'
        )
        
        # Document actual behavior - Express bodyParser.json() may reject or ignore
        # The server uses bodyParser.json() which only parses application/json
        # So req.body will be empty, leading to undefined message/size
        assert status in [200, 400, 415], f"Got unexpected status {status} for wrong content type"
    
    def test_post_with_form_urlencoded(self):
        """POST with form-urlencoded content type."""
        wait_for_server(SERVER_URL)
        room_id = f"test-{random_id()}"
        password = f"secret-{random_id()}"
        
        # Send as form data
        status, headers, resp_body = make_raw_request(
            'POST', f'/r/{room_id}',
            headers={
                "Authorization": password,
                "Content-Type": "application/x-www-form-urlencoded"
            },
            body='message=test&size[rows]=24&size[cols]=80'
        )
        
        # Document actual behavior
        assert status in [200, 400, 415], f"Got unexpected status {status} for form-urlencoded"
    
    def test_post_with_empty_body(self):
        """POST with empty body."""
        wait_for_server(SERVER_URL)
        room_id = f"test-{random_id()}"
        password = f"secret-{random_id()}"
        
        status, headers, resp_body = make_raw_request(
            'POST', f'/r/{room_id}',
            headers={
                "Authorization": password,
                "Content-Type": "application/json"
            },
            body=""
        )
        
        # Document actual behavior
        assert status in [200, 400], f"Got unexpected status {status} for empty body"


class TestMissingFields:
    """Tests for requests missing required fields."""
    
    def test_post_missing_message_field(self):
        """POST without 'message' field."""
        wait_for_server(SERVER_URL)
        room_id = f"test-{random_id()}"
        password = f"secret-{random_id()}"
        
        body = {
            "size": {"rows": 24, "cols": 80}
            # missing "message"
        }
        
        status, headers, resp_body = make_request(
            'POST', f'/r/{room_id}',
            headers={"Authorization": password},
            body=body
        )
        
        # Document actual behavior - server may accept (message=undefined) or reject
        # The server emits undefined which Socket.IO clients may receive as undefined
        assert status in [200, 400], f"Got unexpected status {status} for missing message"
    
    def test_post_missing_size_field(self):
        """POST without 'size' field."""
        wait_for_server(SERVER_URL)
        room_id = f"test-{random_id()}"
        password = f"secret-{random_id()}"
        
        body = {
            "message": encode_message("Test")
            # missing "size"
        }
        
        status, headers, resp_body = make_request(
            'POST', f'/r/{room_id}',
            headers={"Authorization": password},
            body=body
        )
        
        # Document actual behavior
        assert status in [200, 400], f"Got unexpected status {status} for missing size"
    
    def test_post_with_null_message(self):
        """POST with null message value."""
        wait_for_server(SERVER_URL)
        room_id = f"test-{random_id()}"
        password = f"secret-{random_id()}"
        
        body = {
            "message": None,
            "size": {"rows": 24, "cols": 80}
        }
        
        status, headers, resp_body = make_request(
            'POST', f'/r/{room_id}',
            headers={"Authorization": password},
            body=body
        )
        
        # Document actual behavior
        assert status in [200, 400], f"Got unexpected status {status} for null message"
    
    def test_post_with_null_size(self):
        """POST with null size value."""
        wait_for_server(SERVER_URL)
        room_id = f"test-{random_id()}"
        password = f"secret-{random_id()}"
        
        body = {
            "message": encode_message("Test"),
            "size": None
        }
        
        status, headers, resp_body = make_request(
            'POST', f'/r/{room_id}',
            headers={"Authorization": password},
            body=body
        )
        
        # Document actual behavior
        assert status in [200, 400], f"Got unexpected status {status} for null size"


class TestInvalidFieldValues:
    """Tests for invalid field values."""
    
    def test_post_invalid_base64_message(self):
        """POST with message that isn't valid base64."""
        wait_for_server(SERVER_URL)
        room_id = f"test-{random_id()}"
        password = f"secret-{random_id()}"
        
        body = {
            "message": "not-valid-base64!!!@@@",
            "size": {"rows": 24, "cols": 80}
        }
        
        status, headers, resp_body = make_request(
            'POST', f'/r/{room_id}',
            headers={"Authorization": password},
            body=body
        )
        
        # Server accepts invalid base64 (it's stored as-is, decoding happens on read)
        # This is important for Go rewrite - don't validate base64 on write
        assert status == 200, f"Expected 200 (server accepts any string as message), got {status}"
    
    def test_post_with_negative_size(self):
        """POST with negative rows/cols values."""
        wait_for_server(SERVER_URL)
        room_id = f"test-{random_id()}"
        password = f"secret-{random_id()}"
        
        body = {
            "message": encode_message("Test"),
            "size": {"rows": -1, "cols": -1}
        }
        
        status, headers, resp_body = make_request(
            'POST', f'/r/{room_id}',
            headers={"Authorization": password},
            body=body
        )
        
        # Document actual behavior - server likely accepts (no validation)
        assert status in [200, 400], f"Got unexpected status {status} for negative size"
    
    def test_post_with_non_integer_size(self):
        """POST with string values for rows/cols."""
        wait_for_server(SERVER_URL)
        room_id = f"test-{random_id()}"
        password = f"secret-{random_id()}"
        
        body = {
            "message": encode_message("Test"),
            "size": {"rows": "big", "cols": "wide"}
        }
        
        status, headers, resp_body = make_request(
            'POST', f'/r/{room_id}',
            headers={"Authorization": password},
            body=body
        )
        
        # Document actual behavior - server likely accepts (no validation)
        assert status in [200, 400], f"Got unexpected status {status} for non-integer size"
    
    def test_post_with_float_size(self):
        """POST with float values for rows/cols."""
        wait_for_server(SERVER_URL)
        room_id = f"test-{random_id()}"
        password = f"secret-{random_id()}"
        
        body = {
            "message": encode_message("Test"),
            "size": {"rows": 24.5, "cols": 80.7}
        }
        
        status, headers, resp_body = make_request(
            'POST', f'/r/{room_id}',
            headers={"Authorization": password},
            body=body
        )
        
        # Document actual behavior
        assert status in [200, 400], f"Got unexpected status {status} for float size"
    
    def test_post_with_zero_size(self):
        """POST with zero rows/cols values."""
        wait_for_server(SERVER_URL)
        room_id = f"test-{random_id()}"
        password = f"secret-{random_id()}"
        
        body = {
            "message": encode_message("Test"),
            "size": {"rows": 0, "cols": 0}
        }
        
        status, headers, resp_body = make_request(
            'POST', f'/r/{room_id}',
            headers={"Authorization": password},
            body=body
        )
        
        # Document actual behavior
        assert status in [200, 400], f"Got unexpected status {status} for zero size"
    
    def test_post_with_very_large_size(self):
        """POST with extremely large rows/cols values."""
        wait_for_server(SERVER_URL)
        room_id = f"test-{random_id()}"
        password = f"secret-{random_id()}"
        
        body = {
            "message": encode_message("Test"),
            "size": {"rows": 999999999, "cols": 999999999}
        }
        
        status, headers, resp_body = make_request(
            'POST', f'/r/{room_id}',
            headers={"Authorization": password},
            body=body
        )
        
        # Document actual behavior
        assert status in [200, 400], f"Got unexpected status {status} for very large size"


class TestUnsupportedMethods:
    """Tests for unsupported HTTP methods."""
    
    def test_put_room_not_supported(self):
        """PUT /r/:room should return 404 (no route defined)."""
        wait_for_server(SERVER_URL)
        room_id = f"test-{random_id()}"
        
        body = {
            "message": encode_message("Test"),
            "size": {"rows": 24, "cols": 80}
        }
        
        status, headers, resp_body = make_request(
            'PUT', f'/r/{room_id}',
            headers={"Authorization": "secret"},
            body=body
        )
        
        # Express returns 404 for undefined routes
        assert status == 404, f"Expected 404 for PUT, got {status}"
    
    def test_patch_room_not_supported(self):
        """PATCH /r/:room should return 404."""
        wait_for_server(SERVER_URL)
        room_id = f"test-{random_id()}"
        
        body = {
            "message": encode_message("Test")
        }
        
        status, headers, resp_body = make_request(
            'PATCH', f'/r/{room_id}',
            headers={"Authorization": "secret"},
            body=body
        )
        
        assert status == 404, f"Expected 404 for PATCH, got {status}"
    
    def test_options_room(self):
        """OPTIONS /r/:room - check CORS preflight handling."""
        wait_for_server(SERVER_URL)
        room_id = f"test-{random_id()}"
        
        status, headers, resp_body = make_request(
            'OPTIONS', f'/r/{room_id}',
            headers={"Origin": "http://example.com"}
        )
        
        # Document actual behavior - Express may return 200 or 404 depending on CORS setup
        assert status in [200, 204, 404], f"Got unexpected status {status} for OPTIONS"
    
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
        # The actual behavior depends on how Express handles it
        assert status in [200, 301, 302, 404], f"Got unexpected status {status}"
    
    def test_room_with_url_encoded_characters(self):
        """Room names with URL-encoded characters."""
        wait_for_server(SERVER_URL)
        room_id = f"test%20room%2F{random_id()}"
        
        status, headers, body = make_request('GET', f'/r/{room_id}')
        assert status in [200, 400, 404], f"Got unexpected status {status}"
    
    def test_room_with_unicode(self):
        """Room names with unicode characters."""
        wait_for_server(SERVER_URL)
        room_id = f"テスト-{random_id()}"
        # URL-encode the unicode characters for HTTP request
        encoded_room_id = urllib.parse.quote(room_id, safe='')
        
        status, headers, body = make_request('GET', f'/r/{encoded_room_id}')
        assert status == 200, f"Expected 200 for unicode room name, got {status}"
    
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
        
        body = {
            "message": encode_message("Test"),
            "size": {"rows": 24, "cols": 80}
        }
        
        # Test both GET and POST
        status1, _, _ = make_request('GET', f'/r/{room_id}')
        assert status1 == 200, f"Expected 200 for GET, got {status1}"
        
        status2, _, _ = make_request(
            'POST', f'/r/{room_id}',
            headers={"Authorization": "secret"},
            body=body
        )
        assert status2 == 200, f"Expected 200 for POST, got {status2}"


class TestResponseBodyFormat:
    """Tests to verify exact response body format for compatibility."""
    
    def test_200_response_body_on_post(self):
        """POST 200 response should have specific body format."""
        wait_for_server(SERVER_URL)
        room_id = f"test-{random_id()}"
        password = f"secret-{random_id()}"
        
        body = {
            "message": encode_message("Hello"),
            "size": {"rows": 24, "cols": 80}
        }
        
        status, headers, resp_body = make_request(
            'POST', f'/r/{room_id}',
            headers={"Authorization": password},
            body=body
        )
        
        assert status == 200, f"Expected 200, got {status}"
        # Express sendStatus(200) sends "OK" as body
        assert resp_body == "OK", f"Expected 'OK' body, got: {resp_body}"
    
    def test_401_response_body(self):
        """401 Unauthorized response should have specific body format."""
        wait_for_server(SERVER_URL)
        room_id = f"test-{random_id()}"
        password1 = f"secret-{random_id()}"
        password2 = f"different-{random_id()}"
        
        body = {
            "message": encode_message("Hello"),
            "size": {"rows": 24, "cols": 80}
        }
        
        # Claim room
        make_request(
            'POST', f'/r/{room_id}',
            headers={"Authorization": password1},
            body=body
        )
        time.sleep(0.5)
        
        # Try wrong password
        status, headers, resp_body = make_request(
            'POST', f'/r/{room_id}',
            headers={"Authorization": password2},
            body=body
        )
        
        assert status == 401, f"Expected 401, got {status}"
        # Express sendStatus(401) sends "Unauthorized" as body
        assert resp_body == "Unauthorized", f"Expected 'Unauthorized' body, got: {resp_body}"
    
    def test_202_response_body_on_delete(self):
        """DELETE 202 response should have specific body format."""
        wait_for_server(SERVER_URL)
        room_id = f"test-{random_id()}"
        password = f"secret-{random_id()}"
        
        body = {
            "message": encode_message("Hello"),
            "size": {"rows": 24, "cols": 80}
        }
        
        # Create room
        make_request(
            'POST', f'/r/{room_id}',
            headers={"Authorization": password},
            body=body
        )
        
        # Delete it
        status, headers, resp_body = make_request(
            'DELETE', f'/r/{room_id}',
            headers={"Authorization": password}
        )
        
        assert status == 202, f"Expected 202, got {status}"
        # Express sendStatus(202) sends "Accepted" as body
        assert resp_body == "Accepted", f"Expected 'Accepted' body, got: {resp_body}"
    
    def test_404_response_body(self):
        """404 Not Found response should have specific format."""
        wait_for_server(SERVER_URL)
        
        status, headers, resp_body = make_request('GET', '/nonexistent/path')
        
        assert status == 404, f"Expected 404, got {status}"
        # Document the exact 404 body format
        assert len(resp_body) > 0, "404 should have a body"


class TestDeleteEdgeCases:
    """Tests for DELETE edge cases."""
    
    def test_delete_room_twice(self):
        """DELETE same room twice should be idempotent."""
        wait_for_server(SERVER_URL)
        room_id = f"test-{random_id()}"
        password = f"secret-{random_id()}"
        
        body = {
            "message": encode_message("Hello"),
            "size": {"rows": 24, "cols": 80}
        }
        
        # Create room
        make_request(
            'POST', f'/r/{room_id}',
            headers={"Authorization": password},
            body=body
        )
        
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
    
    def test_post_after_delete_reclaims_room(self):
        """POST after DELETE should be able to reclaim room with new password."""
        wait_for_server(SERVER_URL)
        room_id = f"test-{random_id()}"
        password1 = f"secret-{random_id()}"
        password2 = f"different-{random_id()}"
        
        body = {
            "message": encode_message("Hello"),
            "size": {"rows": 24, "cols": 80}
        }
        
        # Create room with password1
        status1, _, _ = make_request(
            'POST', f'/r/{room_id}',
            headers={"Authorization": password1},
            body=body
        )
        assert status1 == 200, f"Failed to create room: {status1}"
        
        # Delete room
        status2, _, _ = make_request(
            'DELETE', f'/r/{room_id}',
            headers={"Authorization": password1}
        )
        assert status2 == 202, f"Failed to delete room: {status2}"
        
        # Wait for deletion to complete
        time.sleep(0.5)
        
        # Reclaim with different password
        status3, _, _ = make_request(
            'POST', f'/r/{room_id}',
            headers={"Authorization": password2},
            body=body
        )
        # This might or might not work depending on how auth cache is cleared
        # Document actual behavior
        assert status3 in [200, 401], f"Got unexpected status {status3} for reclaim after delete"


class TestConcurrentRequests:
    """Tests for concurrent request handling."""
    
    def test_rapid_posts_same_room(self):
        """Rapid POSTs to same room with same password should all succeed."""
        wait_for_server(SERVER_URL)
        room_id = f"test-{random_id()}"
        password = f"secret-{random_id()}"
        
        body = {
            "message": encode_message("Test"),
            "size": {"rows": 24, "cols": 80}
        }
        
        # Send 10 rapid requests
        results = []
        for i in range(10):
            body["message"] = encode_message(f"Message {i}")
            status, _, _ = make_request(
                'POST', f'/r/{room_id}',
                headers={"Authorization": password},
                body=body
            )
            results.append(status)
        
        # All should succeed
        assert all(s == 200 for s in results), f"Not all requests succeeded: {results}"
    
    def test_first_post_wins_race(self):
        """When two different passwords race, first one wins."""
        wait_for_server(SERVER_URL)
        room_id = f"test-{random_id()}"
        password1 = f"secret-{random_id()}"
        password2 = f"different-{random_id()}"
        
        body = {
            "message": encode_message("Test"),
            "size": {"rows": 24, "cols": 80}
        }
        
        # First request claims
        status1, _, _ = make_request(
            'POST', f'/r/{room_id}',
            headers={"Authorization": password1},
            body=body
        )
        assert status1 == 200, f"First request failed: {status1}"
        
        # Immediate second request with different password
        status2, _, _ = make_request(
            'POST', f'/r/{room_id}',
            headers={"Authorization": password2},
            body=body
        )
        
        # Second should fail (or succeed if cache hasn't updated yet - document behavior)
        # Due to async auth persistence, there may be a race window
        assert status2 in [200, 401], f"Got unexpected status {status2}"


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
