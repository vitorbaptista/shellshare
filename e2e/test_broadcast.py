"""
E2E Black-Box Tests for Shellshare

These tests treat the application as a black box - they only interact
with it through the public interfaces (HTTP API + browser). They will work
regardless of implementation language or internal changes.
"""

import base64
import json
import random
import string
import time
import urllib.request
import urllib.parse
from playwright.sync_api import sync_playwright, expect

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


def broadcast_message(room_id, message, password):
    """Broadcast a message to a room using HTTP POST (same as Python CLI)."""
    url = f"{SERVER_URL}/r/{room_id}"
    
    # Encode message same way the CLI does
    encoded = base64.b64encode(urllib.parse.quote(message).encode()).decode()
    
    data = json.dumps({
        "message": encoded,
        "size": {"rows": 24, "cols": 80}
    }).encode()
    
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Content-Type": "application/json",
            "Authorization": password
        },
        method="POST"
    )
    
    with urllib.request.urlopen(req, timeout=10) as response:
        return response.status


def test_happy_path_broadcast_appears_in_browser():
    """
    Happy path test:
    1. Open room in browser
    2. Broadcast a message via HTTP
    3. Verify message appears in the terminal
    """
    room_id = f"test-{random_id()}"
    password = f"secret-{random_id()}"
    test_message = "Hello from E2E test"
    
    print(f"Waiting for server at {SERVER_URL}...")
    wait_for_server(SERVER_URL)
    print("Server is ready!")
    
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        
        # Navigate to room
        room_url = f"{SERVER_URL}/r/{room_id}"
        print(f"Opening room: {room_url}")
        page.goto(room_url)
        
        # Wait for terminal element
        page.wait_for_selector("#terminal", timeout=10000)
        print("Terminal element found")
        
        # Broadcast the message
        print(f"Broadcasting: {test_message}")
        status = broadcast_message(room_id, test_message, password)
        print(f"Broadcast response: {status}")
        assert status == 200, f"Expected 200, got {status}"
        
        # Wait for Socket.io to deliver the message
        page.wait_for_timeout(2000)
        
        # Get terminal content
        terminal = page.locator("#terminal")
        content = terminal.text_content()
        
        # Normalize whitespace for comparison
        normalized = ' '.join(content.split())
        print(f"Terminal content: {normalized[:100]}...")
        
        # Verify message appears
        assert test_message in normalized, f"Message not found in terminal: {normalized[:200]}"
        
        print("✓ PASSED: Message appeared in browser!")
        browser.close()


if __name__ == "__main__":
    test_happy_path_broadcast_appears_in_browser()
