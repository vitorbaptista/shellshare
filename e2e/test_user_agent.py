"""
Test for User-Agent header requirement.

This test verifies that Cloudflare (in front of shellshare.net) blocks
Python's default User-Agent. This reproduces the issue described in:
- Issue #65: Installing dependencies failing (403 Forbidden on Windows)
- PR #64 and #66: Adding User-Agent to fix the 403 error

The shellshare CLI on Windows uses Python's urlretrieve to download script.exe.
Python sends "User-Agent: Python-urllib/3.x" which Cloudflare blocks with 403.
"""

import http.client
import ssl

# Production server (behind Cloudflare)
PRODUCTION_HOST = "shellshare.net"
SCRIPT_PATH = "/bin/script.exe"

# Python's default User-Agent that gets blocked
PYTHON_USER_AGENT = "Python-urllib/3.9"


def make_https_request(host, path, user_agent=None):
    """Make an HTTPS request with optional User-Agent header."""
    context = ssl.create_default_context()
    conn = http.client.HTTPSConnection(host, context=context)
    
    headers = {}
    if user_agent:
        headers["User-Agent"] = user_agent
    
    conn.request("GET", path, headers=headers)
    response = conn.getresponse()
    status = response.status
    conn.close()
    return status


def test_cloudflare_blocks_python_user_agent():
    """
    Test that Cloudflare blocks Python's default User-Agent.
    
    This reproduces the 403 error that Windows users experience when
    the shellshare CLI tries to download script.exe using urlretrieve.
    
    Expected behavior:
    - Python-urllib User-Agent: 403 Forbidden (blocked by Cloudflare)
    - Custom User-Agent: 200 OK
    """
    # Test 1: Python's default User-Agent should get 403
    status_python_ua = make_https_request(
        PRODUCTION_HOST, SCRIPT_PATH, 
        user_agent=PYTHON_USER_AGENT
    )
    print(f"Request with '{PYTHON_USER_AGENT}': {status_python_ua}")
    
    # Test 2: Custom User-Agent should get 200
    status_custom_ua = make_https_request(
        PRODUCTION_HOST, SCRIPT_PATH,
        user_agent="shellshare"
    )
    print(f"Request with 'shellshare': {status_custom_ua}")
    
    # Assert the expected behavior
    assert status_python_ua == 403, (
        f"Expected 403 with Python User-Agent, got {status_python_ua}. "
        "Cloudflare may have changed its bot protection rules."
    )
    
    assert status_custom_ua == 200, (
        f"Expected 200 with custom User-Agent, got {status_custom_ua}. "
        "The script.exe file may not exist on the server."
    )
    
    print("✓ Confirmed: Cloudflare blocks Python-urllib User-Agent")
    print("✓ PRs #64 and #66 are needed to fix this issue")


if __name__ == "__main__":
    test_cloudflare_blocks_python_user_agent()
