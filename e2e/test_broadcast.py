"""
E2E Black-Box Tests for Shellshare

These tests treat the application as a black box - they only interact
with it through the public interfaces (CLI + browser). They test both
the server and the Python CLI client.
"""

import os
import random
import string
import subprocess
import sys
import time
import urllib.request
from playwright.sync_api import sync_playwright

SERVER_URL = "http://localhost:3000"
CLI_PATH = os.path.join(os.path.dirname(__file__), "..", "public", "bin", "shellshare")


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


def broadcast_with_cli(room_id, message, password, server_url):
    """Broadcast a message using the shellshare CLI with --stdin flag."""
    proc = subprocess.Popen(
        [sys.executable, CLI_PATH, "--stdin", "-s", server_url, "-r", room_id, "-p", password],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True
    )
    stdout, stderr = proc.communicate(input=message, timeout=10)
    return proc.returncode, stdout, stderr


def test_happy_path_broadcast_appears_in_browser():
    """
    Happy path test:
    1. Open room in browser
    2. Broadcast a message via the shellshare CLI
    3. Verify message appears in the terminal
    """
    room_id = f"test-{random_id()}"
    password = f"secret-{random_id()}"
    test_message = "Hello from CLI E2E test"
    
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
        
        # Broadcast using CLI
        print(f"Broadcasting via CLI: {test_message}")
        returncode, stdout, stderr = broadcast_with_cli(room_id, test_message, password, SERVER_URL)
        print(f"CLI stderr: {stderr.strip()}")
        assert returncode == 0, f"CLI failed with code {returncode}: {stderr}"
        
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
        
        print("✓ PASSED: Message from CLI appeared in browser!")
        browser.close()


if __name__ == "__main__":
    test_happy_path_broadcast_appears_in_browser()
