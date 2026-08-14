"""
E2E Tests for the herdr plugin (herdr-plugin.toml + herdr-plugin/share.sh)

The suite never talks to a real herdr: a stub `herdr` binary serves the
handful of calls share.sh makes (pane layout/read, session list, api
snapshot, notification show), driven by files the test controls. The
shellshare side is real - broadcasts run against a dedicated local
server with encryption ON, and assertions read the viewer WebSocket,
so the whole pipeline (poller -> stream mode -> server -> viewer) is
exercised, not just the script.

Test categories:
- Manifest <-> share.sh lockstep (routing, dot-free ids)
- Pane share: live frames reach a viewer; graceful stop cleans state
- Pane share: a group signal (what a pane close delivers) still leaves
  a complete last frame, and `sweep` collects the stale record
- Session share: the mirror runs with HERDR_ENV unset and stdin closed
- Failure paths: unreachable server; unresolvable session
"""

import json
import os
import signal
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest

from conftest import (
    CLI_PATH,
    SocketListener,
    parse_share_key,
    random_id,
)

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="the plugin scripts are bash"
)

REPO_ROOT = Path(__file__).parent.parent
SHARE_SH = REPO_ROOT / "herdr-plugin" / "share.sh"
MANIFEST = REPO_ROOT / "herdr-plugin.toml"

FAKE_SOCKET = "/tmp/herdr-plugin-e2e.sock"

# One stub for every herdr call share.sh makes. Reads/writes files under
# $STUB_DIR so tests can both drive it (frame file) and observe it
# (notification log, attach env dump).
STUB_HERDR = textwrap.dedent("""\
    #!/bin/bash
    case "$1 $2" in
    "pane layout")
        printf '{"id":"x","result":{"layout":{"panes":[{"pane_id":"w1:p7","rect":{"height":20,"width":60,"x":0,"y":1}}]}}}\\n'
        ;;
    "pane read")
        [ -f "$STUB_DIR/frame" ] || exit 1
        cat "$STUB_DIR/frame"
        ;;
    "session list")
        printf '{"sessions":[{"default":true,"name":"e2e-session","running":true,"socket_path":"%s"}]}\\n' "$FAKE_SOCKET"
        ;;
    "api snapshot")
        printf '{"id":"x","result":{"snapshot":{"focused_tab_id":"w1:t1","layouts":[{"tab_id":"w1:t1","area":{"height":23,"width":90,"x":10,"y":1}}]}}}\\n'
        ;;
    "notification show")
        printf '%s | %s\\n' "$3" "$5" >> "$STUB_DIR/notifications.log"
        ;;
    "session attach")
        # The mirror client: record the environment the wrapper promised
        # (HERDR_ENV unset; no bytes arrive on the PTY stdin - shellshare's
        # own stdin is /dev/null, so nothing gets forwarded or injected)
        # and emit a marker into the PTY.
        {
            printf 'HERDR_ENV=%s\\n' "${HERDR_ENV:-UNSET}"
            if read -r -t 1 _; then printf 'STDIN=received-bytes\\n'; else printf 'STDIN=quiet\\n'; fi
        } > "$STUB_DIR/attach-env"
        printf 'MIRROR-MARKER-%s\\n' "$3"
        exec sleep 600
        ;;
    "plugin pane")
        printf '%s\\n' "$*" >> "$STUB_DIR/pane-calls.log"
        ;;
    *)
        printf 'stub herdr: unhandled: %s\\n' "$*" >&2
        exit 1
        ;;
    esac
""")


@pytest.fixture
def plugin_env(tmp_path, dedicated_server):
    """A stubbed herdr world plus a real dedicated shellshare server."""
    server = dedicated_server()
    stub_dir = tmp_path / "stub"
    state = tmp_path / "state"
    config_dir = tmp_path / "config"
    for d in (stub_dir, state, config_dir):
        d.mkdir()

    herdr = stub_dir / "herdr"
    herdr.write_text(STUB_HERDR)
    herdr.chmod(0o755)

    (config_dir / "config").write_text(f"server={server.url}\n")
    (stub_dir / "frame").write_text("frame-one\nhello viewers\n")

    env = os.environ.copy()
    env.update(
        HERDR_BIN_PATH=str(herdr),
        HERDR_SOCKET_PATH=FAKE_SOCKET,
        HERDR_PLUGIN_STATE_DIR=str(state),
        HERDR_PLUGIN_CONFIG_DIR=str(config_dir),
        HERDR_PLUGIN_ID="shellshare",
        HERDR_PANE_ID="w1:p99",
        SHELLSHARE_BIN=str(CLI_PATH),
        STUB_DIR=str(stub_dir),
        FAKE_SOCKET=FAKE_SOCKET,
    )
    env.pop("HERDR_ENV", None)
    return type(
        "PluginEnv",
        (),
        dict(server=server, stub_dir=stub_dir, state=state, env=env),
    )


def start_entrypoint(plugin_env, subcommand, extra_env):
    env = dict(plugin_env.env)
    env.update(
        SHELLSHARE_STATE_KEY=f"s1-{random_id(6)}",
        SHELLSHARE_SHARE_TOKEN=f"tok-{random_id(8)}",
        # The documented direct-open opt-in: without a lock from a share
        # action, entrypoints refuse to start (the restore-respawn guard)
        SHELLSHARE_DIRECT="1",
        **extra_env,
    )
    proc = subprocess.Popen(
        ["bash", str(SHARE_SH), subcommand],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
        start_new_session=True,  # its own process group, like a pane
        cwd=str(REPO_ROOT),
    )
    return proc, env["SHELLSHARE_STATE_KEY"]


def wait_for_url(proc, timeout=45):
    """The banner is the URL's only home - read it from pane stdout.

    Reading happens on a thread: readline on the live pipe would block
    the test (and its deadline check) whenever the entrypoint is slow to
    print, hanging the CI worker instead of failing. The timeout leaves
    headroom over share.sh's own 30s start ceiling so its error message
    wins the race and shows up in the failure."""
    lines = []
    reader = threading.Thread(
        target=lambda: lines.extend(iter(proc.stdout.readline, "")), daemon=True
    )
    reader.start()

    def scan():
        for line in list(lines):
            if "/r/" in line:
                return line.strip()
        return None

    deadline = time.time() + timeout
    while time.time() < deadline:
        found = scan()
        if found:
            return found
        if proc.poll() is not None and not reader.is_alive():
            break
        time.sleep(0.1)
    # One last scan: the reader thread may have appended the URL between
    # the loop's snapshot and its exit condition.
    found = scan()
    if found:
        return found
    pytest.fail(
        f"no share URL in entrypoint output; stdout={''.join(lines)!r} "
        f"stderr={proc.stderr.read() if proc.poll() is not None else '<still running>'}"
    )


def stop_and_reap(proc, sig=signal.SIGTERM, to_group=False):
    if proc.poll() is None:
        os.killpg(proc.pid, sig) if to_group else proc.send_signal(sig)
    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        proc.wait(timeout=5)


class TestManifestLockstep:
    """The manifest and share.sh must not drift apart."""

    def test_manifest_routes_to_existing_subcommands(self):
        try:
            import tomllib
        except ImportError:  # Python 3.10
            import tomli as tomllib

        manifest = tomllib.loads(MANIFEST.read_text())
        script = SHARE_SH.read_text()

        assert manifest["id"] == "shellshare"
        assert manifest["platforms"] == ["linux", "macos"]

        commands = [a["command"] for a in manifest["actions"]]
        commands += [p["command"] for p in manifest["panes"]]
        commands += [s["command"] for s in manifest["startup"]]
        for cmd in commands:
            assert cmd[0] == "bash" and cmd[1] == "herdr-plugin/share.sh", cmd
            # Every routed subcommand must have a dispatch arm.
            assert f"{cmd[2]})" in script, f"share.sh has no arm for {cmd[2]}"

        # The other direction: share.sh hardcodes the entrypoint ids it
        # passes to `plugin pane open --entrypoint`, which herdr resolves
        # against [[panes]] - renaming one side must fail here.
        pane_ids = {p["id"] for p in manifest["panes"]}
        assert pane_ids == {"pane-broadcast", "session-broadcast"}
        for pane_id in pane_ids:
            assert f"open_share_pane {pane_id} " in script or \
                f'open_share_pane {pane_id} "' in script, \
                f"share.sh never opens entrypoint {pane_id}"

        # Local ids are dot-free: dots are reserved for the qualified
        # form shellshare.<id> used by keybindings and action invoke.
        for section in ("actions", "panes"):
            for item in manifest[section]:
                assert "." not in item["id"], item["id"]


class TestPaneShare:
    def test_frames_reach_viewer_and_stop_cleans_up(self, plugin_env):
        proc, key = start_entrypoint(
            plugin_env, "run-pane-share", {"SHELLSHARE_TARGET_PANE": "w1:p7"}
        )
        try:
            url = wait_for_url(proc)
            room = url.split("/r/")[1].split("#")[0]

            listener = SocketListener(room, server_url=plugin_env.server.url)
            listener.set_key(parse_share_key(url))
            listener.connect()
            try:
                assert listener.wait_for_message(timeout=10, containing="hello viewers")

                # Change the pane content; the poller must pick it up.
                (plugin_env.stub_dir / "frame").write_text("frame-two\nCHANGED-CONTENT\n")
                assert listener.wait_for_message(timeout=10, containing="CHANGED-CONTENT")
            finally:
                listener.disconnect()

            state_file = plugin_env.state / "shares" / f"{key}.json"
            assert state_file.exists()
            state = json.loads(state_file.read_text())
            assert state["target"] == "w1:p7"
            # The URL (whose #fragment is the key) must never be on disk:
            # not in the state record, and the run dir's shellshare
            # stdout capture is unlinked as soon as the URL is parsed.
            assert "url" not in state and "#" not in state_file.read_text()
            assert (state_file.stat().st_mode & 0o777) == 0o600
            assert not (plugin_env.state / f"run-{key}" / "out").exists(), \
                "the sharing event (URL + key) must not persist on disk"
            # Notifications never carry the URL either - herdr truncates
            # them and may route them to the OS notification center.
            notifications = (plugin_env.stub_dir / "notifications.log").read_text()
            assert "Shellshare" in notifications
            assert "/r/" not in notifications and "#" not in notifications

            # Graceful stop: TERM to share.sh alone (the stop action's path).
            stop_and_reap(proc)
            assert not state_file.exists(), "state must be cleared on stop"
        finally:
            stop_and_reap(proc, sig=signal.SIGKILL, to_group=True)

    def test_uncatchable_kill_leaves_frame_and_sweep_collects(self, plugin_env):
        """SIGKILL to the whole group is the worst case a pane teardown
        can degrade to: no traps, no drains. The link must keep serving
        the already-delivered content afterwards (rooms outlive their
        broadcaster), the state file necessarily survives, and `sweep` -
        the startup hook - must then collect both it and the leftover
        run dir."""
        proc, key = start_entrypoint(
            plugin_env, "run-pane-share", {"SHELLSHARE_TARGET_PANE": "w1:p7"}
        )
        try:
            url = wait_for_url(proc)
            room = url.split("/r/")[1].split("#")[0]
            listener = SocketListener(room, server_url=plugin_env.server.url)
            listener.set_key(parse_share_key(url))
            listener.connect()
            try:
                assert listener.wait_for_message(timeout=10, containing="hello viewers")
            finally:
                listener.disconnect()

            state_file = plugin_env.state / "shares" / f"{key}.json"
            assert state_file.exists()
            stop_and_reap(proc, sig=signal.SIGKILL, to_group=True)

            # The last flushed frame stays readable after the group died.
            listener = SocketListener(room, server_url=plugin_env.server.url)
            listener.set_key(parse_share_key(url))
            listener.connect()
            try:
                assert listener.wait_for_message(timeout=10, containing="hello viewers")
            finally:
                listener.disconnect()

            # SIGKILL cannot run traps: the record is guaranteed stale.
            assert state_file.exists()
            # Run dirs get a startup grace period (mtime-based) so a
            # concurrent sweep never yanks one from under a starting
            # share; age it past the grace to test the GC itself.
            run_dir = plugin_env.state / f"run-{key}"
            old = time.time() - 300
            os.utime(run_dir, (old, old))
            subprocess.run(
                ["bash", str(SHARE_SH), "sweep"],
                env=plugin_env.env,
                cwd=str(REPO_ROOT),
                timeout=30,
                check=True,
            )
            assert not state_file.exists(), "sweep must collect stale state"
            assert not run_dir.exists(), "sweep must collect the orphaned run dir"
        finally:
            stop_and_reap(proc, sig=signal.SIGKILL, to_group=True)

    def test_unreachable_server_fails_loudly_without_state(self, plugin_env):
        (plugin_env.stub_dir.parent / "config" / "config").write_text(
            "server=http://127.0.0.1:1\n"
        )
        proc, key = start_entrypoint(
            plugin_env, "run-pane-share", {"SHELLSHARE_TARGET_PANE": "w1:p7"}
        )
        stdout, stderr = proc.communicate(timeout=60)
        assert proc.returncode != 0
        assert "could not start the broadcast" in stderr
        # The message must carry shellshare's actual diagnostics - the
        # fallback text would mean the stderr capture was destroyed
        # before being shown (a bug this suite once had).
        assert "no error output" not in stderr, stderr
        assert not (plugin_env.state / "shares" / f"{key}.json").exists()


class TestActions:
    """The action layer: lock handoff, pane open parameters, stop."""

    def run_action(self, plugin_env, action, extra_env=None):
        env = dict(plugin_env.env)
        env["HERDR_PLUGIN_CONTEXT_JSON"] = json.dumps({"focused_pane_id": "w1:p7"})
        env.update(extra_env or {})
        return subprocess.run(
            ["bash", str(SHARE_SH), action],
            capture_output=True,
            text=True,
            env=env,
            cwd=str(REPO_ROOT),
            timeout=30,
        )

    def test_share_pane_action_opens_focused_tab_once(self, plugin_env):
        proc = self.run_action(plugin_env, "action-share-pane")
        assert proc.returncode == 0, proc.stderr
        calls = (plugin_env.stub_dir / "pane-calls.log").read_text()
        assert "open --plugin shellshare --entrypoint pane-broadcast" in calls
        assert "--placement tab --focus" in calls
        assert "SHELLSHARE_TARGET_PANE=w1:p7" in calls
        locks = list((plugin_env.state / "locks").iterdir())
        assert len(locks) == 1, "the action must hold the start lock for the entrypoint"

        # A double invocation inside the start window must not open a
        # second broadcast pane (the lock is the atomic test-and-set).
        proc = self.run_action(plugin_env, "action-share-pane")
        assert proc.returncode == 0, proc.stderr
        calls = (plugin_env.stub_dir / "pane-calls.log").read_text()
        assert calls.count("--entrypoint pane-broadcast") == 1

    def test_stop_sees_direct_shares_and_clears_them(self, plugin_env):
        """stop/status match shares by session socket, not state-key
        shape - the documented direct-open recipe uses caller-chosen
        keys (manual-...), and its banner promises the stop action
        works."""
        # A stand-in broadcaster that passes BOTH liveness checks: the
        # token rides in its environment (Linux reads /proc/pid/environ)
        # and its argv matches the entrypoint pattern (the macOS ps
        # fallback greps for "<share.sh path> run-").
        keeper = subprocess.Popen(
            ["bash", "-c", f'exec -a "{SHARE_SH} run-fake" sleep 60'],
            env={**plugin_env.env, "SHELLSHARE_SHARE_TOKEN": "tok-manual"},
        )
        try:
            shares = plugin_env.state / "shares"
            shares.mkdir(parents=True, exist_ok=True)
            (shares / "manual-w1-p7.json").write_text(json.dumps({
                "key": "manual-w1-p7",
                "mode": "pane",
                "target": "w1:p7",
                "room": "",
                "status_pane": "w1:p42",
                "socket": FAKE_SOCKET,
                "token": "tok-manual",
                "pid": keeper.pid,
            }))
            proc = self.run_action(plugin_env, "action-stop")
            assert proc.returncode == 0, proc.stderr
            assert not (shares / "manual-w1-p7.json").exists(), \
                "stop must clear direct-opened shares in this session"
            assert "close w1:p42" in (plugin_env.stub_dir / "pane-calls.log").read_text()
            if os.path.exists("/proc"):
                # On Linux the liveness token matched, so stop must have
                # terminated the recorded PID before clearing state.
                assert keeper.poll() is not None or keeper.wait(5) is not None
        finally:
            if keeper.poll() is None:
                keeper.kill()
            keeper.wait()


class TestSessionShare:
    def test_mirror_runs_detached_from_herdr_env_and_stdin(self, plugin_env):
        proc, key = start_entrypoint(
            plugin_env,
            "run-session-share",
            {"SHELLSHARE_SESSION_NAME": "e2e-session", "HERDR_ENV": "1"},
        )
        try:
            url = wait_for_url(proc)
            room = url.split("/r/")[1].split("#")[0]

            listener = SocketListener(room, server_url=plugin_env.server.url)
            listener.set_key(parse_share_key(url))
            listener.connect()
            try:
                # The stub mirror's PTY output reaches viewers...
                assert listener.wait_for_message(
                    timeout=10, containing="MIRROR-MARKER-e2e-session"
                )
            finally:
                listener.disconnect()

            # ...and it ran with the nesting gate cleared and a quiet
            # stdin: the mirror gets a PTY, but shellshare's stdin is
            # /dev/null so nothing is forwarded - and the writer-drop EOF
            # encoding (newline+VEOF) must not be injected either, or the
            # mirrored session would receive an Enter and a Ctrl+D.
            env_dump = (plugin_env.stub_dir / "attach-env").read_text()
            assert "HERDR_ENV=UNSET" in env_dump
            assert "STDIN=quiet" in env_dump

            stop_and_reap(proc)
            assert not (plugin_env.state / "shares" / f"{key}.json").exists()
        finally:
            stop_and_reap(proc, sig=signal.SIGKILL, to_group=True)

    def test_share_session_action_requires_resolvable_session(self, plugin_env):
        env = dict(plugin_env.env)
        env["HERDR_SOCKET_PATH"] = "/nonexistent/other.sock"
        proc = subprocess.run(
            ["bash", str(SHARE_SH), "action-share-session"],
            capture_output=True,
            text=True,
            env=env,
            cwd=str(REPO_ROOT),
            timeout=30,
        )
        assert proc.returncode != 0
        assert "could not resolve" in proc.stderr
