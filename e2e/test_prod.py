"""Quick test against production to verify the test logic."""
from playwright.sync_api import sync_playwright

SERVER_URL = "https://shellshare.net"

with sync_playwright() as p:
    browser = p.chromium.launch()
    page = browser.new_page()
    page.goto(SERVER_URL)
    
    download_code = page.locator(".download code")
    
    # Test macOS (default)
    print("=== macOS (default) ===")
    visible_text = download_code.inner_text()
    print(f"Visible text: {repr(visible_text)}")
    print(f"Has curl: {'curl' in visible_text}")
    print(f"Has iwr: {'iwr' in visible_text}")
    
    # Test Linux
    print("\n=== Linux ===")
    page.click("label[for='os-linux']")
    visible_text = download_code.inner_text()
    print(f"Visible text: {repr(visible_text)}")
    print(f"Has wget: {'wget' in visible_text}")
    print(f"Has iwr: {'iwr' in visible_text}")
    
    # Test Windows
    print("\n=== Windows ===")
    page.click("label[for='os-windows']")
    visible_text = download_code.inner_text()
    print(f"Visible text: {repr(visible_text)}")
    print(f"Has iwr: {'iwr' in visible_text}")
    
    browser.close()
