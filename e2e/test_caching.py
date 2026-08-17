"""Tests for the page/asset caching behavior.

The server renders the HTML pages once at startup (flattening each
page's local stylesheets into one bundle and content-hash-versioning
asset URLs) and serves everything with cache headers tuned for a CDN
in front:

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

    def test_pages_bundle_their_stylesheets(self):
        """One flattened stylesheet per page, at an immutable URL.

        Every room URL is distinct, so CSS inlined into room HTML is
        re-sent for every broadcast a viewer opens and can never be
        reused; at its own versioned URL it is fetched once.
        """
        wait_for_server(SERVER_URL)
        for path in ['/', '/r/some-room']:
            _, _, body = make_request('GET', path)
            assert '<style>' not in body, f"{path} must not inline its CSS"
            links = re.findall(r'<link[^>]*rel="stylesheet"[^>]*>', body)
            assert len(links) == 1, \
                f"{path} must link exactly one bundle, got {links}"
            href = re.search(r'href="([^"]+)"', links[0]).group(1)
            assert re.fullmatch(r'/stylesheet/\w+\.bundle\.css\?v=[0-9a-f]+', href), \
                f"{path} stylesheet must be a versioned bundle, got {href}"
            status, css_headers, css = make_request('GET', href)
            assert status == 200, f"{href} must be served"
            assert css_headers.get('Cache-Control') == \
                'public, max-age=31536000, immutable'
            assert '@import' not in css, \
                f"{href} must have its @imports resolved"

    def test_room_page_announces_preloads(self):
        """The Link header lets the CDN emit 103 Early Hints for the
        scripts and font the page is about to request."""
        wait_for_server(SERVER_URL)
        _, headers, body = make_request('GET', '/r/some-room')
        link = headers.get('Link', '')
        for asset in ['xterm.js', 'xterm-addon-webgl.js',
                      'xterm-addon-unicode11.js', 'room.js']:
            assert asset in link, f"Link preload header must announce {asset}"
            assert re.search(rf'{re.escape(asset)}\?v=[0-9a-f]+', body), \
                f"{asset} must be referenced with a content-hash version"

    def test_preloads_name_the_urls_actually_requested(self):
        """A preload for a URL nobody requests is worse than none: the
        browser fetches a second copy and warns. Both halves are easy to
        drift, because neither is requested by the HTML - the stylesheet
        is only in the <link>, and the font only inside the stylesheet.
        """
        wait_for_server(SERVER_URL)
        _, headers, body = make_request('GET', '/r/some-room')
        link = headers.get('Link', '')

        href = re.search(r'<link[^>]*rel="stylesheet"[^>]*href="([^"]+)"',
                         body).group(1)
        assert f'<{href}>; rel=preload; as=style' in link, \
            f"Link header must preload the stylesheet the page links ({href})"

        preloaded = re.search(r'<(/font/Inconsolata\.woff2\?v=[0-9a-f]+)>', link)
        assert preloaded, "Link header must preload a versioned Inconsolata"
        _, _, css = make_request('GET', href)
        assert preloaded.group(1) in css, \
            f"{href} must request the preloaded font URL {preloaded.group(1)}"


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

    def test_stale_version_revalidates(self):
        """A version we do not have names bytes we cannot serve, so the
        answer must not be cached at all.

        During a rolling deploy this is how a viewer holding new HTML
        reaches an old replica. Storing that answer would pin stale
        code against fresh markup for as long as the TTL, and no reload
        would clear it: the HTML revalidates, the asset would not.
        """
        wait_for_server(SERVER_URL)
        for path in ['/javascript/room.js', '/stylesheet/room.bundle.css']:
            status, headers, _ = make_request(
                'GET', f'{path}?v=0000000000000000')
            assert status == 200, f"{path} must still serve current bytes"
            assert headers.get('Cache-Control') == 'public, no-cache', \
                f"{path} with a foreign version must revalidate"

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
