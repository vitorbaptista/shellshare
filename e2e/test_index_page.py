"""
E2E tests for the index page using Playwright.

These tests verify that the one-liner command changes correctly when
selecting different operating systems via the radio buttons.

Note: The page auto-detects the user's OS via JavaScript, so the default
selection depends on the browser's platform. These tests explicitly click
the radio buttons to test each OS regardless of the default.
"""

import pytest
from playwright.sync_api import sync_playwright, expect

# Server URL for the tests - can be overridden to test against production
SERVER_URL = "http://localhost:3000"


@pytest.fixture(scope="module")
def browser():
    """Create a browser instance for the test module."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        yield browser
        browser.close()


@pytest.fixture
def page(browser):
    """Create a new page for each test."""
    page = browser.new_page()
    yield page
    page.close()


class TestIndexPageOneliner:
    """Tests for the one-liner command on the index page.
    
    Each OS should show ONLY its command:
    - macOS: curl -sLo (NOT wget, NOT iwr)
    - Linux: wget -qO (NOT curl, NOT iwr)
    - Windows: iwr (NOT curl, NOT wget)
    """

    def test_macos_shows_only_curl(self, page):
        """macOS should show curl, and NOT show wget or iwr."""
        page.goto(SERVER_URL)
        page.click("label[for='os-macos']")
        
        download_code = page.locator(".download code")
        visible_text = download_code.inner_text()
        
        # macOS MUST show curl
        assert "curl -sLo" in visible_text, \
            f"macOS should show 'curl -sLo', got: {visible_text}"
        
        # macOS must NOT show wget (Linux command)
        assert "wget" not in visible_text, \
            f"macOS should NOT show 'wget', got: {visible_text}"
        
        # macOS must NOT show iwr (Windows command)
        assert "iwr" not in visible_text, \
            f"macOS should NOT show 'iwr', got: {visible_text}"

    def test_linux_shows_only_wget(self, page):
        """Linux should show wget, and NOT show curl or iwr."""
        page.goto(SERVER_URL)
        page.click("label[for='os-linux']")
        
        download_code = page.locator(".download code")
        visible_text = download_code.inner_text()
        
        # Linux MUST show wget
        assert "wget -qO" in visible_text, \
            f"Linux should show 'wget -qO', got: {visible_text}"
        
        # Linux must NOT show curl (macOS command)
        assert "curl" not in visible_text, \
            f"Linux should NOT show 'curl', got: {visible_text}"
        
        # Linux must NOT show iwr (Windows command)
        assert "iwr" not in visible_text, \
            f"Linux should NOT show 'iwr', got: {visible_text}"

    def test_windows_shows_only_iwr(self, page):
        """Windows should show iwr, and NOT show curl or wget."""
        page.goto(SERVER_URL)
        page.click("label[for='os-windows']")
        
        download_code = page.locator(".download code")
        visible_text = download_code.inner_text()
        
        # Windows MUST show iwr
        assert "iwr" in visible_text, \
            f"Windows should show 'iwr', got: {visible_text}"
        
        # Windows must NOT show curl (macOS command)
        assert "curl" not in visible_text, \
            f"Windows should NOT show 'curl', got: {visible_text}"
        
        # Windows must NOT show wget (Linux command)
        assert "wget" not in visible_text, \
            f"Windows should NOT show 'wget', got: {visible_text}"

    def test_switching_between_all_os(self, page):
        """Verify switching between all OS options works correctly."""
        page.goto(SERVER_URL)
        download_code = page.locator(".download code")
        
        # Test macOS
        page.click("label[for='os-macos']")
        text = download_code.inner_text()
        assert "curl" in text and "wget" not in text and "iwr" not in text, \
            f"macOS failed: {text}"
        
        # Test Linux
        page.click("label[for='os-linux']")
        text = download_code.inner_text()
        assert "wget" in text and "curl" not in text and "iwr" not in text, \
            f"Linux failed: {text}"
        
        # Test Windows
        page.click("label[for='os-windows']")
        text = download_code.inner_text()
        assert "iwr" in text and "curl" not in text and "wget" not in text, \
            f"Windows failed: {text}"
        
        # Back to macOS
        page.click("label[for='os-macos']")
        text = download_code.inner_text()
        assert "curl" in text and "wget" not in text and "iwr" not in text, \
            f"macOS (after switch) failed: {text}"

    def test_body_class_changes_with_os_selection(self, page):
        """Body class should change when selecting different OS."""
        page.goto(SERVER_URL)
        
        # Select macOS explicitly
        page.click("label[for='os-macos']")
        expect(page.locator("body")).to_have_class("instructions-macos")
        
        # Select Linux
        page.click("label[for='os-linux']")
        expect(page.locator("body")).to_have_class("instructions-linux")
        
        # Select Windows
        page.click("label[for='os-windows']")
        expect(page.locator("body")).to_have_class("instructions-windows")

    def test_radio_button_reflects_selection(self, page):
        """Radio buttons should reflect the current selection."""
        page.goto(SERVER_URL)
        
        # Select and verify macOS
        page.click("label[for='os-macos']")
        expect(page.locator("#os-macos")).to_be_checked()
        expect(page.locator("#os-linux")).not_to_be_checked()
        expect(page.locator("#os-windows")).not_to_be_checked()
        
        # Select and verify Linux
        page.click("label[for='os-linux']")
        expect(page.locator("#os-macos")).not_to_be_checked()
        expect(page.locator("#os-linux")).to_be_checked()
        expect(page.locator("#os-windows")).not_to_be_checked()
        
        # Select and verify Windows
        page.click("label[for='os-windows']")
        expect(page.locator("#os-macos")).not_to_be_checked()
        expect(page.locator("#os-linux")).not_to_be_checked()
        expect(page.locator("#os-windows")).to_be_checked()
