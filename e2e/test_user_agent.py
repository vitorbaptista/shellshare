"""
Test for User-Agent header requirement.

This test verifies that the server requires a User-Agent header for certain
endpoints. This reproduces the issue described in:
- Issue #65: Installing dependencies failing (403 Forbidden on Windows)
- PR #64 and #66: Adding User-Agent to fix the 403 error

The shellshare CLI on Windows tries to download script.exe without a User-Agent
header, which causes a 403 Forbidden error from the server.
"""

import http.client
import time
import urllib.request
from urllib.parse import urlparse

SERVER_URL = "http://localhost:3000"


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


def make_request_without_user_agent(url):
    """
    Make an HTTP request without a User-Agent header.
    
    This simulates what Python's urlretrieve does by default on Windows,
    which is the root cause of issue #65.
    """
    parsed = urlparse(url)
    conn = http.client.HTTPConnection(parsed.netloc)
    
    # Make request with minimal headers (no User-Agent)
    conn.request("GET", parsed.path, headers={})
    response = conn.getresponse()
    status = response.status
    conn.close()
    return status


def make_request_with_user_agent(url):
    """
    Make an HTTP request with a User-Agent header.
    
    This simulates the fix proposed in PR #64 and #66.
    """
    parsed = urlparse(url)
    conn = http.client.HTTPConnection(parsed.netloc)
    
    # Make request with User-Agent header
    conn.request("GET", parsed.path, headers={"User-Agent": "shellshare"})
    response = conn.getresponse()
    status = response.status
    conn.close()
    return status


def test_server_rejects_requests_without_user_agent():
    """
    Test that the server rejects requests without a User-Agent header.
    
    This reproduces the 403 error that Windows users experience when
    the shellshare CLI tries to download script.exe.
    
    Expected behavior:
    - Request without User-Agent: 403 Forbidden
    - Request with User-Agent: Not 403 (could be 200 or 404)
    """
    wait_for_server(SERVER_URL)
    
    # The endpoint that the Windows CLI tries to download
    script_url = f"{SERVER_URL}/bin/script.exe"
    
    # Test 1: Request without User-Agent should get 403
    status_without_ua = make_request_without_user_agent(script_url)
    print(f"Request without User-Agent: {status_without_ua}")
    
    # Test 2: Request with User-Agent should NOT get 403
    status_with_ua = make_request_with_user_agent(script_url)
    print(f"Request with User-Agent: {status_with_ua}")
    
    # Assert the expected behavior
    assert status_without_ua == 403, (
        f"Expected 403 without User-Agent, got {status_without_ua}. "
        "The server may have been updated to not require User-Agent."
    )
    
    assert status_with_ua != 403, (
        f"Expected non-403 with User-Agent, got {status_with_ua}. "
        "The User-Agent header should fix the 403 error."
    )
    
    print("✓ Confirmed: Server requires User-Agent header for /bin/script.exe")


if __name__ == "__main__":
    test_server_rejects_requests_without_user_agent()
