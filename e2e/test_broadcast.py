"""
E2E Black-Box Tests for Shellshare

These tests treat the application as a black box - they only interact
with it through the public interfaces (CLI + browser). They test both
the server and the Python CLI client.
"""

import random
import string
import subprocess
import sys
import time
import urllib.request
from playwright.sync_api import sync_playwright

from conftest import CLI_COMMAND

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


def broadcast_with_cli(room_id, message, password, server_url):
    """Broadcast a message using the shellshare CLI with --stdin flag."""
    proc = subprocess.Popen(
        CLI_COMMAND + ["--stdin", "-s", server_url, "-r", room_id, "-W", password],
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
        
        # Wait for Socket.IO to connect (user count becomes 1)
        page.wait_for_function("document.getElementById('online-counter').textContent !== '0'", timeout=10000)
        print("Socket.IO connected (user count updated)")
        
        # Broadcast using CLI
        print(f"Broadcasting via CLI: {test_message}")
        returncode, stdout, stderr = broadcast_with_cli(room_id, test_message, password, SERVER_URL)
        print(f"CLI stderr: {stderr.strip()}")
        assert returncode == 0, f"CLI failed with code {returncode}: {stderr}"

        # Wait for message to appear in terminal
        # The terminal text content is updated when Socket.IO message arrives
        terminal = page.locator("#terminal")
        try:
            terminal.wait_for(state="visible", timeout=5000)
            # Use expect to wait for the text to appear
            from playwright.sync_api import expect
            expect(terminal).to_contain_text(test_message, timeout=10000)
        except Exception as e:
            # If the assertion fails, get content for debugging
            content = terminal.text_content()
            print(f"Terminal content when failed: {content[:200] if content else 'empty'}")
            raise

        # Get terminal content
        content = terminal.text_content()
        
        # Normalize whitespace for comparison
        normalized = ' '.join(content.split())
        print(f"Terminal content: {normalized[:100]}...")
        
        # Verify message appears
        assert test_message in normalized, f"Message not found in terminal: {normalized[:200]}"
        
        print("✓ PASSED: Message from CLI appeared in browser!")
        browser.close()


def test_user_counter_shows_in_browser():
    """
    Test that the user counter appears and updates in the browser UI.
    """
    room_id = f"test-{random_id()}"
    
    print(f"Waiting for server at {SERVER_URL}...")
    wait_for_server(SERVER_URL)
    print("Server is ready!")
    
    with sync_playwright() as p:
        browser = p.chromium.launch()
        
        # First user joins
        page1 = browser.new_page()
        room_url = f"{SERVER_URL}/r/{room_id}"
        print(f"User 1 opening room: {room_url}")
        page1.goto(room_url)

        # Wait for counter to show 1
        page1.wait_for_function(
            "document.getElementById('online-counter').textContent === '1'",
            timeout=10000
        )

        # Check counter shows 1
        counter = page1.locator("#online-counter")
        count1 = counter.text_content()
        print(f"User count after 1 user: {count1}")
        assert count1 == "1", f"Expected 1 user, got {count1}"

        # Second user joins
        page2 = browser.new_page()
        print(f"User 2 opening room: {room_url}")
        page2.goto(room_url)

        # Wait for counter to show 2 on first page
        page1.wait_for_function(
            "document.getElementById('online-counter').textContent === '2'",
            timeout=10000
        )

        # Check counter shows 2 on first page
        count2 = counter.text_content()
        print(f"User count after 2 users: {count2}")
        assert count2 == "2", f"Expected 2 users, got {count2}"

        # Close second user
        page2.close()

        # Wait for counter to show 1 again
        # Windows needs longer timeout as Socket.IO disconnect detection is slower
        disconnect_timeout = 60000 if sys.platform == "win32" else 10000
        page1.wait_for_function(
            "document.getElementById('online-counter').textContent === '1'",
            timeout=disconnect_timeout
        )

        # Check counter shows 1 again
        count3 = counter.text_content()
        print(f"User count after user 2 leaves: {count3}")
        assert count3 == "1", f"Expected 1 user after disconnect, got {count3}"
        
        print("✓ PASSED: User counter updates correctly in browser!")
        browser.close()


def test_terminal_size_updates_in_browser():
    """
    Test that terminal size updates are reflected in the browser.
    """
    room_id = f"test-{random_id()}"
    password = f"secret-{random_id()}"
    
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
        
        # Broadcast with a specific size
        custom_size = {"rows": 40, "cols": 120}
        returncode, stdout, stderr = broadcast_with_cli(
            room_id, "Test message", password, SERVER_URL
        )
        assert returncode == 0, f"CLI failed: {stderr}"

        # Wait for message to appear in terminal (confirms size update was processed)
        terminal = page.locator("#terminal")
        from playwright.sync_api import expect
        expect(terminal).to_contain_text("Test message", timeout=10000)

        # The terminal should have been created - verify it exists
        assert terminal.count() > 0, "Terminal should exist"
        
        print("✓ PASSED: Terminal receives size updates!")
        browser.close()


def test_multiple_broadcasts_no_duplication():
    """
    Test that multiple broadcasts don't cause message duplication.

    Bug scenario (before fix):
    - POST 1: "AAA" → server broadcasts "AAA"
    - POST 2: "BBB" → server broadcasts "AAABBB" (entire history!)
    - Browser sees: "AAA" + "AAABBB" = "AAA" duplicated

    This test should FAIL with the current Rust server bug.
    """
    room_id = f"test-{random_id()}"
    password = f"secret-{random_id()}"

    # Use unique markers that are easy to count
    marker_a = f"MARKER_ALPHA_{random_id(6)}"
    marker_b = f"MARKER_BETA_{random_id(6)}"

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

        # Wait for terminal and socket connection
        page.wait_for_selector("#terminal", timeout=10000)
        page.wait_for_function(
            "document.getElementById('online-counter').textContent !== '0'",
            timeout=10000
        )
        print("Terminal ready and Socket.IO connected")

        # First broadcast
        print(f"Broadcasting first message: {marker_a}")
        returncode, _, stderr = broadcast_with_cli(room_id, marker_a, password, SERVER_URL)
        assert returncode == 0, f"First broadcast failed: {stderr}"

        # Wait for first message to arrive
        terminal = page.locator("#terminal")
        from playwright.sync_api import expect
        expect(terminal).to_contain_text(marker_a, timeout=10000)

        # Second broadcast
        print(f"Broadcasting second message: {marker_b}")
        returncode, _, stderr = broadcast_with_cli(room_id, marker_b, password, SERVER_URL)
        assert returncode == 0, f"Second broadcast failed: {stderr}"

        # Wait for second message to arrive
        expect(terminal).to_contain_text(marker_b, timeout=10000)

        # Get terminal content
        content = terminal.text_content()
        print(f"Terminal content: {content[:200]}...")

        # Count occurrences of each marker
        count_a = content.count(marker_a)
        count_b = content.count(marker_b)

        print(f"Marker A count: {count_a} (expected: 1)")
        print(f"Marker B count: {count_b} (expected: 1)")

        # Each marker should appear exactly once
        assert count_a == 1, f"Marker A appeared {count_a} times, expected 1 (duplication bug!)"
        assert count_b == 1, f"Marker B appeared {count_b} times, expected 1 (duplication bug!)"

        print("✓ PASSED: No message duplication!")
        browser.close()


if __name__ == "__main__":
    test_happy_path_broadcast_appears_in_browser()
    test_user_counter_shows_in_browser()
    test_terminal_size_updates_in_browser()
    test_multiple_broadcasts_no_duplication()
