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


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
