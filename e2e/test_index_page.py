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

# Server URL for the tests
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
    """Tests for the one-liner command on the index page."""

    def test_macos_shows_curl_command(self, page):
        """When macOS is selected, should show curl command and hide iwr."""
        page.goto(SERVER_URL)
        
        # Click macOS radio to ensure it's selected
        page.click("label[for='os-macos']")
        
        # Get the download code block
        download_code = page.locator(".download code")
        
        # Should show curl
        curl_span = download_code.locator("span.macos").first
        expect(curl_span).to_be_visible()
        expect(curl_span).to_contain_text("curl -sLo")
        
        # Should NOT show iwr (Windows command)
        windows_span = download_code.locator("span.windows")
        expect(windows_span).to_be_hidden()

    def test_linux_shows_wget_command(self, page):
        """When Linux is selected, should show wget command and hide iwr."""
        page.goto(SERVER_URL)
        
        # Click Linux radio
        page.click("label[for='os-linux']")
        
        # Get the download code block
        download_code = page.locator(".download code")
        
        # Should show wget
        linux_span = download_code.locator("span.linux").first
        expect(linux_span).to_be_visible()
        expect(linux_span).to_contain_text("wget -qO")
        
        # Should NOT show iwr (Windows command)
        windows_span = download_code.locator("span.windows")
        expect(windows_span).to_be_hidden()

    def test_windows_shows_iwr_command(self, page):
        """When Windows is selected, should show iwr command."""
        page.goto(SERVER_URL)
        
        # Click Windows radio
        page.click("label[for='os-windows']")
        
        # Get the download code block
        download_code = page.locator(".download code")
        
        # Should show iwr
        windows_span = download_code.locator("span.windows")
        expect(windows_span).to_be_visible()
        expect(windows_span).to_contain_text("iwr")
        
        # Should NOT show curl or wget (macOS/Linux commands)
        macos_only_span = download_code.locator("span.macos:not(.linux)")
        linux_only_span = download_code.locator("span.linux:not(.macos)")
        expect(macos_only_span).to_be_hidden()
        expect(linux_only_span).to_be_hidden()

    def test_switching_from_windows_to_macos_hides_iwr(self, page):
        """Switching from Windows to macOS should hide iwr command."""
        page.goto(SERVER_URL)
        
        # First select Windows
        page.click("label[for='os-windows']")
        
        download_code = page.locator(".download code")
        windows_span = download_code.locator("span.windows")
        expect(windows_span).to_be_visible()
        
        # Now switch to macOS
        page.click("label[for='os-macos']")
        
        # iwr should now be hidden
        expect(windows_span).to_be_hidden()
        
        # curl should be visible
        curl_span = download_code.locator("span.macos").first
        expect(curl_span).to_be_visible()

    def test_switching_from_windows_to_linux_hides_iwr(self, page):
        """Switching from Windows to Linux should hide iwr command."""
        page.goto(SERVER_URL)
        
        # First select Windows
        page.click("label[for='os-windows']")
        
        download_code = page.locator(".download code")
        windows_span = download_code.locator("span.windows")
        expect(windows_span).to_be_visible()
        
        # Now switch to Linux
        page.click("label[for='os-linux']")
        
        # iwr should now be hidden
        expect(windows_span).to_be_hidden()
        
        # wget should be visible
        linux_span = download_code.locator("span.linux").first
        expect(linux_span).to_be_visible()

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
        
        # Back to macOS
        page.click("label[for='os-macos']")
        expect(page.locator("body")).to_have_class("instructions-macos")

    def test_oneliner_text_content_for_each_os(self, page):
        """Verify the visible text content of the one-liner for each OS."""
        page.goto(SERVER_URL)
        
        download_code = page.locator(".download code")
        
        # macOS - should show curl, not iwr
        page.click("label[for='os-macos']")
        visible_text = download_code.inner_text()
        assert "curl -sLo" in visible_text, f"macOS should show 'curl -sLo', got: {visible_text}"
        assert "iwr" not in visible_text, f"macOS should NOT show 'iwr', got: {visible_text}"
        
        # Linux - should show wget, not iwr
        page.click("label[for='os-linux']")
        visible_text = download_code.inner_text()
        assert "wget -qO" in visible_text, f"Linux should show 'wget -qO', got: {visible_text}"
        assert "iwr" not in visible_text, f"Linux should NOT show 'iwr', got: {visible_text}"
        
        # Windows - should show iwr, not curl or wget
        page.click("label[for='os-windows']")
        visible_text = download_code.inner_text()
        assert "iwr" in visible_text, f"Windows should show 'iwr', got: {visible_text}"
        assert "curl" not in visible_text, f"Windows should NOT show 'curl', got: {visible_text}"
        assert "wget" not in visible_text, f"Windows should NOT show 'wget', got: {visible_text}"

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
