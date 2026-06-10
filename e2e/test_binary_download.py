"""
E2E tests for GET /bin/shellshare - the install-script download endpoint.

The server has two build flavors:
- with the embed-binaries feature: serves the platform-specific binary
- without it (dev builds): always falls back to serving itself

These tests assert the contract that holds in BOTH flavors, and tighten
the platform-specific assertions only when embedded binaries are present.
"""

import re
import urllib.request

from conftest import SERVER_URL


def _fetch_headers(path, headers=None):
    """GET the path but only read the response headers, not the body."""
    req = urllib.request.Request(f"{SERVER_URL}{path}", headers=headers or {})
    resp = urllib.request.urlopen(req, timeout=10)
    try:
        # resp.headers is case-insensitive (the server sends lowercase names)
        return resp.status, resp.headers
    finally:
        resp.close()


def _disposition_filename(headers):
    match = re.search(r'filename="([^"]+)"', headers.get("Content-Disposition", ""))
    return match.group(1) if match else None


def _server_has_embedded_binaries():
    """Probe: an embedded-binaries server names the Windows download .exe."""
    _, headers = _fetch_headers("/bin/shellshare?os=windows")
    return _disposition_filename(headers) == "shellshare.exe"


class TestBinaryDownload:
    def test_download_without_hints_serves_a_binary(self):
        status, headers = _fetch_headers("/bin/shellshare")
        assert status == 200
        assert headers.get("Content-Type") == "application/octet-stream"
        assert "attachment" in headers.get("Content-Disposition", "")
        assert _disposition_filename(headers) in ("shellshare", "shellshare.exe")
        assert headers.get("ETag"), "Download must be cacheable via ETag"
        assert "max-age" in headers.get("Cache-Control", "")

    def test_all_os_query_params_serve_a_binary(self):
        # Every documented ?os= value must yield a download, embedded or not
        for os_param in ["linux", "windows", "win", "mac", "macos", "mac-intel", "mac-arm"]:
            status, headers = _fetch_headers(f"/bin/shellshare?os={os_param}")
            assert status == 200, f"?os={os_param} returned {status}"
            assert "attachment" in headers.get("Content-Disposition", ""), (
                f"?os={os_param} is not a download"
            )

    def test_unknown_os_param_falls_back_to_self(self):
        status, headers = _fetch_headers("/bin/shellshare?os=plan9")
        assert status == 200
        assert _disposition_filename(headers) == "shellshare"

    def test_os_param_is_case_insensitive(self):
        status, headers = _fetch_headers("/bin/shellshare?os=LINUX")
        assert status == 200
        assert "attachment" in headers.get("Content-Disposition", "")

    def test_windows_filename_has_exe_suffix_when_embedded(self):
        if not _server_has_embedded_binaries():
            # Fallback build: everything is served as the running binary
            _, headers = _fetch_headers("/bin/shellshare?os=windows")
            assert _disposition_filename(headers) == "shellshare"
            return

        _, headers = _fetch_headers("/bin/shellshare?os=windows")
        assert _disposition_filename(headers) == "shellshare.exe"
        for os_param in ["linux", "mac", "mac-arm"]:
            _, headers = _fetch_headers(f"/bin/shellshare?os={os_param}")
            assert _disposition_filename(headers) == "shellshare"

    def test_user_agent_detection_serves_a_binary(self):
        # The ?os= param takes priority, but bare User-Agent must also work
        agents = [
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
            "curl/8.0.1 (aarch64-apple-darwin)",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
            "curl/7.81.0 (x86_64-pc-linux-gnu)",
            "",  # no UA at all: fallback to self
        ]
        for ua in agents:
            headers = {"User-Agent": ua} if ua else {}
            status, resp_headers = _fetch_headers("/bin/shellshare", headers)
            assert status == 200, f"UA {ua!r} returned {status}"
            assert "attachment" in resp_headers.get("Content-Disposition", "")

    def test_query_param_overrides_user_agent(self):
        # ?os= wins over a conflicting User-Agent
        status, headers = _fetch_headers(
            "/bin/shellshare?os=windows",
            {"User-Agent": "curl/7.81.0 (x86_64-pc-linux-gnu)"},
        )
        assert status == 200
        expected = (
            "shellshare.exe" if _server_has_embedded_binaries() else "shellshare"
        )
        assert _disposition_filename(headers) == expected
