"""
E2E Tests for Shellshare CLI - --tunnel (Cloudflare quick tunnels)

`--tunnel` spawns the user's cloudflared against the local server and
prints the public trycloudflare.com URL as the share link. The suite
never talks to Cloudflare: a stub cloudflared on PATH prints the same
startup banner the real binary does.

Test categories:
- Share link / server log carry the tunnel URL
- cloudflared missing or failing is a loud, actionable error
- cloudflared dies with the shellshare process
"""

import os
import subprocess
import sys
import textwrap
import threading

import pytest

from conftest import CLI_PATH, _free_port, poll_until, random_id, wait_for_server

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="the stub cloudflared is a shell script"
)

STUB_URL = "https://stub-tunnel.trycloudflare.com"

# What the real cloudflared prints on stderr when the quick tunnel is up
STUB_BANNER = textwrap.dedent(f"""\
    #!/bin/sh
    echo $$ > "$(dirname "$0")/stub.pid"
    echo 'INF +--------------------------------------------------------------+' >&2
    echo 'INF |  Your quick Tunnel has been created! Visit it at:            |' >&2
    echo 'INF |  {STUB_URL}                        |' >&2
    echo 'INF +--------------------------------------------------------------+' >&2
    exec sleep 600
""")


@pytest.fixture
def stub_dir(tmp_path):
    """A directory whose cloudflared prints the banner and then idles."""
    stub = tmp_path / "cloudflared"
    stub.write_text(STUB_BANNER)
    stub.chmod(0o755)
    return tmp_path


def env_with_path(*dirs):
    env = os.environ.copy()
    env["PATH"] = ":".join(str(d) for d in dirs)
    return env


def run_serve_tunnel(env, message="hello tunnel", timeout=30, extra_args=()):
    """Run `serve --tunnel` in stdin mode to completion."""
    port = _free_port()
    room = random_id()
    proc = subprocess.run(
        [str(CLI_PATH), "serve", "--port", str(port), "--tunnel", "-r", room, "-W", "pw", *extra_args],
        input=message,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
    )
    return proc, room


def test_serve_tunnel_share_link_uses_tunnel_url(stub_dir):
    proc, room = run_serve_tunnel(env_with_path(stub_dir, os.environ["PATH"]))
    assert proc.returncode == 0, proc.stderr
    # stdin mode prints the share link to stderr
    assert f"Sharing terminal in {STUB_URL}/r/{room}" in proc.stderr
    # The tunnel makes the whole local server public; the user must know
    assert "WARNING" in proc.stderr


def test_serve_tunnel_kills_cloudflared_on_exit(stub_dir):
    proc, _ = run_serve_tunnel(env_with_path(stub_dir, os.environ["PATH"]))
    assert proc.returncode == 0, proc.stderr

    pid = int((stub_dir / "stub.pid").read_text())

    def stub_gone():
        try:
            os.kill(pid, 0)
            return False
        except ProcessLookupError:
            return True

    assert poll_until(stub_gone, timeout=5), "stub cloudflared still running"


def test_serve_tunnel_requires_cloudflared(tmp_path):
    # PATH with no cloudflared in it
    proc, _ = run_serve_tunnel(env_with_path(tmp_path))
    assert proc.returncode != 0
    assert "cloudflared" in proc.stderr
    assert "developers.cloudflare.com" in proc.stderr


def test_serve_tunnel_reports_cloudflared_failure(tmp_path):
    stub = tmp_path / "cloudflared"
    stub.write_text(
        "#!/bin/sh\n"
        "echo 'ERR failed to request quick Tunnel: something broke' >&2\n"
        "exit 1\n"
    )
    stub.chmod(0o755)
    proc, _ = run_serve_tunnel(env_with_path(tmp_path, os.environ["PATH"]))
    assert proc.returncode != 0
    assert "cloudflared exited before reporting a tunnel URL" in proc.stderr
    assert "failed to request quick Tunnel" in proc.stderr


def test_server_tunnel_logs_public_url(stub_dir):
    port = _free_port()
    proc = subprocess.Popen(
        [str(CLI_PATH), "server", "--host", "127.0.0.1", "--port", str(port),
         "--tunnel"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env=env_with_path(stub_dir, os.environ["PATH"]),
    )
    # Drain stdout from a thread: readline in the test body could block
    # forever if the server never logs the URL
    lines = []
    reader = threading.Thread(
        target=lambda: lines.extend(iter(proc.stdout.readline, "")), daemon=True
    )
    reader.start()
    try:
        wait_for_server(f"http://127.0.0.1:{port}")
        assert poll_until(lambda: any(STUB_URL in line for line in lines),
                          timeout=10), "".join(lines)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()

    if sys.platform == "linux":
        # SIGTERM skips Drop, but PR_SET_PDEATHSIG must still reap the
        # tunnel process when the server dies
        pid = int((stub_dir / "stub.pid").read_text())

        def stub_gone():
            try:
                os.kill(pid, 0)
                return False
            except ProcessLookupError:
                return True

        assert poll_until(stub_gone, timeout=5), "stub cloudflared still running"
