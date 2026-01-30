"""
E2E tests against PRODUCTION (https://shellshare.net).

These tests verify that the one-liner command changes correctly when
selecting different operating systems via the radio buttons on the 
live production site.

This file is STANDALONE and doesn't use conftest.py fixtures.

Run with: uv run pytest test_production.py -v
"""

import pytest
from playwright.sync_api import sync_playwright

# Test against production!
PRODUCTION_URL = "https://shellshare.net"


# Override the conftest fixture to not wait for localhost
@pytest.fixture(scope="session", autouse=True)
def ensure_server_running():
    """No-op for production tests - production is always running."""
    pass


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


class TestProductionOneliner:
    """Tests for the one-liner command on the PRODUCTION site.
    
    Each OS should show ONLY its command:
    - macOS: curl -sLo (NOT wget, NOT iwr)
    - Linux: wget -qO (NOT curl, NOT iwr)
    - Windows: iwr (NOT curl, NOT wget)
    """

    def test_macos_shows_only_curl(self, page):
        """macOS should show curl, and NOT show wget or iwr."""
        page.goto(PRODUCTION_URL)
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
        page.goto(PRODUCTION_URL)
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
        page.goto(PRODUCTION_URL)
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
