"""Tests for the page/asset caching behavior.

The server renders the HTML pages once at startup (inlining local
stylesheets and content-hash-versioning asset URLs) and serves
everything with cache headers tuned for a CDN in front:

- HTML: ``no-cache`` + ETag, so deploys take effect immediately and
  repeat loads are cheap 304s
- assets requested with the current ``?v=<hash>``: immutable for a year
- assets at stable URLs (favicon, robots.txt, ...): one day
"""

import re

from conftest import SERVER_URL, wait_for_server
from test_api import make_request


def get_with_etag_replay(path):
    """GET path, then replay it with If-None-Match from the response."""
    status, headers, body = make_request('GET', path)
    assert status == 200, f"Expected 200 for {path}, got {status}"
    etag = headers.get('ETag')
    assert etag, f"{path} must send an ETag"
    replay = make_request('GET', path, headers={'If-None-Match': etag})
    return (status, headers, body), replay


class TestPageCaching:
    """The HTML pages revalidate on every load."""

    def test_pages_revalidate_with_etag(self):
        wait_for_server(SERVER_URL)
        for path in ['/', '/r/some-room']:
            (_, headers, _), (replay_status, replay_headers, replay_body) = \
                get_with_etag_replay(path)
            assert headers.get('Cache-Control') == 'public, no-cache', \
                f"{path} must always revalidate so deploys show up immediately"
            assert replay_status == 304, f"Expected 304 for {path} replay"
            assert replay_body == '', "304 must not carry a body"
            assert replay_headers.get('ETag') == headers.get('ETag')
            assert 'Cache-Control' in replay_headers

    def test_pages_accept_weak_etags(self):
        """A CDN that compresses our response may weaken the ETag; the
        browser then sends it back with a W/ prefix."""
        wait_for_server(SERVER_URL)
        _, headers, _ = make_request('GET', '/')
        weak = 'W/' + headers.get('ETag')
        status, _, _ = make_request('GET', '/', headers={'If-None-Match': weak})
        assert status == 304, "Weak If-None-Match must still revalidate to 304"

    def test_pages_inline_their_stylesheets(self):
        """First paint must not depend on extra stylesheet requests."""
        wait_for_server(SERVER_URL)
        for path in ['/', '/r/some-room']:
            _, _, body = make_request('GET', path)
            assert '<style>' in body, f"{path} must inline its CSS"
            assert not re.search(r'<link[^>]*rel="stylesheet"', body), \
                f"{path} must not load stylesheets over the network"
            assert '@import' not in body, \
                f"{path} inlined CSS must have @imports resolved"

    def test_room_page_announces_preloads(self):
        """The Link header lets the CDN emit 103 Early Hints for the
        scripts and font the page is about to request."""
        wait_for_server(SERVER_URL)
        _, headers, body = make_request('GET', '/r/some-room')
        link = headers.get('Link', '')
        for asset in ['xterm.js', 'xterm-addon-webgl.js',
                      'xterm-addon-unicode11.js',
                      'Inconsolata.woff2']:
            assert asset in link, f"Link preload header must announce {asset}"
            assert re.search(rf'{re.escape(asset)}\?v=[0-9a-f]+', body), \
                f"{asset} must be referenced with a content-hash version"


class TestAssetCaching:
    """Hash-versioned assets cache forever; stable URLs for a day."""

    def test_versioned_asset_is_immutable(self):
        wait_for_server(SERVER_URL)
        # Take a versioned URL straight from the rendered room page so
        # the test can't drift from what browsers actually request
        _, _, body = make_request('GET', '/r/some-room')
        match = re.search(r'src="(/javascript/[^"]+\?v=[0-9a-f]+)"', body)
        assert match, "Room page must reference a versioned script"
        (_, headers, _), (replay_status, _, _) = get_with_etag_replay(match.group(1))
        assert headers.get('Cache-Control') == 'public, max-age=31536000, immutable'
        assert replay_status == 304

    def test_stale_version_is_not_immutable(self):
        """A pre-deploy URL may serve different bytes after a deploy,
        so it must not be cached forever."""
        wait_for_server(SERVER_URL)
        status, headers, _ = make_request(
            'GET', '/javascript/vendor/xterm.js?v=0000000000000000')
        assert status == 200
        assert headers.get('Cache-Control') == 'public, max-age=86400'

    def test_unversioned_asset_caches_for_a_day(self):
        wait_for_server(SERVER_URL)
        (_, headers, _), (replay_status, _, _) = get_with_etag_replay('/favicon.png')
        assert headers.get('Cache-Control') == 'public, max-age=86400'
        assert replay_status == 304


class TestSelfContainedPages:
    """Everything the pages load comes from our own server."""

    def test_no_third_party_resources(self):
        wait_for_server(SERVER_URL)
        for path in ['/', '/r/some-room']:
            _, _, body = make_request('GET', path)
            tags = re.findall(r'<(?:script|link)[^>]*(?:src|href)="(?:https?:)?//[^"]*"[^>]*>',
                              body)
            loading = [t for t in tags if 'stylesheet' in t or '<script' in t]
            assert not loading, \
                f"{path} must not load scripts/styles from other origins: {loading}"
