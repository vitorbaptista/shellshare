"""
E2E Tests for the herdr plugin (herdr-plugin.toml + herdr-plugin/share.sh)

The suite never talks to a real herdr: a stub `herdr` binary serves the
handful of calls share.sh makes (pane layout/read, session list, api
snapshot, report-metadata, notification show, plugin pane open), driven
by files the test controls and logging what the plugin asked herdr to
do. The shellshare side is real - broadcasts run against a dedicated
local server with encryption ON, and assertions read the viewer
WebSocket, so the whole pipeline (poller -> stream mode -> server ->
viewer) is exercised, not just the script.

The plugin's surfaces are herdr-native: the broadcast runs detached
(no pane), it announces itself through herdr display metadata (a
sidebar token), and the link is shown by an overlay entrypoint that
reads it from the daemon's fifo. The tests drive those same surfaces.

Test categories:
- Manifest <-> share.sh lockstep (routing, dot-free ids, entrypoint)
- Pane share: live frames reach a viewer; badge set and cleared; the
  link is served to the overlay and never written to disk
- Uncatchable kill: link keeps serving, sweep collects the leftovers
- Session share: the mirror runs with HERDR_ENV unset and stdin closed
- Actions: fast hand-off to a detached daemon, lock, stop
- Failure paths: unreachable server; unresolvable session
"""

import json
import os
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from conftest import (
    CLI_PATH,
    SocketListener,
    parse_share_key,
    poll_until,
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
# (metadata/notification/pane-open logs, attach env dump).
STUB_HERDR = textwrap.dedent("""\
    #!/bin/bash
    case "$1 $2" in
    "pane layout")
        printf '{"id":"x","result":{"layout":{"zoomed":false,"panes":[{"pane_id":"w1:p7","rect":{"height":20,"width":60,"x":0,"y":1}}]}}}\\n'
        ;;
    "pane read")
        [ -f "$STUB_DIR/frame" ] || exit 1
        cat "$STUB_DIR/frame"
        ;;
    "pane report-metadata"|"workspace report-metadata")
        printf '%s\\n' "$*" >> "$STUB_DIR/metadata.log"
        ;;
    "workspace list")
        printf '{"id":"x","result":{"workspaces":[{"workspace_id":"w1"}]}}\\n'
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
        HERDR_PANE_ID="w1:p7",
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


def run_script(plugin_env, subcommand, extra_env=None, timeout=60):
    """Run a share.sh subcommand to completion."""
    env = dict(plugin_env.env)
    env.update(extra_env or {})
    return subprocess.run(
        ["bash", str(SHARE_SH), subcommand],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(REPO_ROOT),
        timeout=timeout,
    )


def start_daemon(plugin_env, subcommand, extra_env):
    """Start a broadcast daemon the way an action does, and wait for it
    to publish its state record."""
    key = f"s1-{random_id(6)}"
    env = dict(plugin_env.env)
    env.update(
        SHELLSHARE_STATE_KEY=key,
        SHELLSHARE_SHARE_TOKEN=f"tok-{random_id(8)}",
        **extra_env,
    )
    (plugin_env.state / "locks" / key).mkdir(parents=True)
    proc = subprocess.Popen(
        ["bash", str(SHARE_SH), subcommand],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
        start_new_session=True,
        cwd=str(REPO_ROOT),
    )
    state_file = plugin_env.state / "shares" / f"{key}.json"
    if not poll_until(state_file.exists, timeout=45):
        stop_and_reap(proc, sig=signal.SIGKILL, to_group=True)
        last_error = plugin_env.state / "last-error.txt"
        pytest.fail(
            "daemon never published its share; last error: "
            f"{last_error.read_text() if last_error.exists() else '<none>'}"
        )
    return proc, key


def share_url(plugin_env, key):
    """The link, read the way the overlay reads it: from the daemon's
    fifo. It exists nowhere else - not on disk, not in any log."""
    proc = run_script(plugin_env, "show-link", {"SHELLSHARE_STATE_KEY": key},
                      timeout=30)
    for line in proc.stdout.splitlines():
        if "/r/" in line:
            return line.strip()
    pytest.fail(f"overlay showed no link: {proc.stdout!r} {proc.stderr!r}")


def stop_and_reap(proc, sig=signal.SIGTERM, to_group=False):
    if proc.poll() is None:
        if to_group:
            os.killpg(proc.pid, sig)
        else:
            proc.send_signal(sig)
    try:
        proc.wait(timeout=20)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        proc.wait(timeout=5)


def listener_for(plugin_env, url):
    room = url.split("/r/")[1].split("#")[0]
    listener = SocketListener(room, server_url=plugin_env.server.url)
    listener.set_key(parse_share_key(url))
    listener.connect()
    return listener


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

        # The daemons are spawned by share.sh itself rather than routed
        # by the manifest, so their subcommands must exist too.
        for sub in ("daemon-pane-share", "daemon-session-share"):
            assert f"{sub})" in script and f'"$sub"' in script or sub in script

        # The one pane entrypoint is the transient link overlay, and
        # share.sh opens it by that exact id.
        assert {p["id"] for p in manifest["panes"]} == {"link"}
        assert manifest["panes"][0]["placement"] == "overlay"
        assert "--entrypoint link" in script

        # Local ids are dot-free: dots are reserved for the qualified
        # form shellshare.<id> used by keybindings and action invoke.
        for section in ("actions", "panes"):
            for item in manifest[section]:
                assert "." not in item["id"], item["id"]


class TestPaneShare:
    def test_frames_reach_viewer_badge_is_reported_and_stop_cleans_up(
        self, plugin_env
    ):
        proc, key = start_daemon(
            plugin_env, "daemon-pane-share", {"SHELLSHARE_TARGET_PANE": "w1:p7"}
        )
        try:
            url = share_url(plugin_env, key)
            listener = listener_for(plugin_env, url)
            try:
                assert listener.wait_for_message(timeout=15, containing="hello viewers")

                # Change the pane content; the poller must pick it up.
                (plugin_env.stub_dir / "frame").write_text("frame-two\nCHANGED-CONTENT\n")
                assert listener.wait_for_message(timeout=15, containing="CHANGED-CONTENT")
            finally:
                listener.disconnect()

            # The live share announces itself through herdr's own display
            # metadata, on the pane being shared, with a TTL so a killed
            # daemon's badge expires by itself.
            metadata = (plugin_env.stub_dir / "metadata.log").read_text()
            assert "pane report-metadata w1:p7" in metadata
            assert "--token shellshare=" in metadata
            assert "--ttl-ms" in metadata

            state_file = plugin_env.state / "shares" / f"{key}.json"
            state = json.loads(state_file.read_text())
            assert state["target"] == "w1:p7" and state["mode"] == "pane"
            # The URL (whose #fragment is the key) must never be on disk:
            # not in the state record, and the run dir's stdout capture
            # is unlinked as soon as the URL is parsed.
            assert "url" not in state and "#" not in state_file.read_text()
            assert (state_file.stat().st_mode & 0o777) == 0o600
            assert not (plugin_env.state / f"run-{key}" / "out").exists()
            # Notifications never carry the URL either - herdr truncates
            # them and may route them to the OS notification center.
            notifications = (plugin_env.stub_dir / "notifications.log").read_text()
            assert "Shellshare" in notifications
            assert "/r/" not in notifications and "#" not in notifications

            # Graceful stop: TERM to the daemon (the stop action's path).
            stop_and_reap(proc)
            assert not state_file.exists(), "state must be cleared on stop"
            assert "--clear-token shellshare" in \
                (plugin_env.stub_dir / "metadata.log").read_text(), \
                "the badge must be cleared when the share ends"
        finally:
            stop_and_reap(proc, sig=signal.SIGKILL, to_group=True)

    def test_uncatchable_kill_leaves_link_readable_and_sweep_collects(self, plugin_env):
        """SIGKILL to the whole group is the worst case: no traps, no
        drains, no badge clearing (the badge's TTL covers that). The
        link must keep serving what it already delivered, and `sweep` -
        the startup hook - must collect the stale record and run dir."""
        proc, key = start_daemon(
            plugin_env, "daemon-pane-share", {"SHELLSHARE_TARGET_PANE": "w1:p7"}
        )
        try:
            url = share_url(plugin_env, key)
            listener = listener_for(plugin_env, url)
            try:
                assert listener.wait_for_message(timeout=15, containing="hello viewers")
            finally:
                listener.disconnect()

            state_file = plugin_env.state / "shares" / f"{key}.json"
            assert state_file.exists()
            stop_and_reap(proc, sig=signal.SIGKILL, to_group=True)

            # Rooms outlive their broadcaster: the link still serves.
            listener = listener_for(plugin_env, url)
            try:
                assert listener.wait_for_message(timeout=15, containing="hello viewers")
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
            run_script(plugin_env, "sweep", timeout=30)
            assert not state_file.exists(), "sweep must collect stale state"
            assert not run_dir.exists(), "sweep must collect the orphaned run dir"
        finally:
            stop_and_reap(proc, sig=signal.SIGKILL, to_group=True)

    def test_unreachable_server_fails_loudly_without_state(self, plugin_env):
        (plugin_env.stub_dir.parent / "config" / "config").write_text(
            "server=http://127.0.0.1:1\n"
        )
        key = f"s1-{random_id(6)}"
        (plugin_env.state / "locks" / key).mkdir(parents=True)
        proc = run_script(
            plugin_env,
            "daemon-pane-share",
            {
                "SHELLSHARE_STATE_KEY": key,
                "SHELLSHARE_SHARE_TOKEN": "tok",
                "SHELLSHARE_TARGET_PANE": "w1:p7",
            },
        )
        assert proc.returncode != 0
        assert "could not start the broadcast" in proc.stderr
        # The message must carry shellshare's actual diagnostics - the
        # fallback text would mean the stderr capture was destroyed
        # before being shown.
        assert "no error output" not in proc.stderr, proc.stderr
        assert not (plugin_env.state / "shares" / f"{key}.json").exists()
        assert not (plugin_env.state / "locks" / key).exists(), \
            "a failed start must release its lock so a retry can proceed"
        # A detached daemon has no pane to print into: the failure has to
        # survive somewhere the user (and `herdr plugin log`) can find.
        last_error = plugin_env.state / "last-error.txt"
        assert last_error.exists() and "broadcast" in last_error.read_text()
        assert "/r/" not in last_error.read_text()


class TestActions:
    """The action layer: fast hand-off to a detached daemon, then stop."""

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

    def test_share_pane_action_hands_off_to_a_daemon_and_returns(self, plugin_env):
        """The action must not become the broadcaster: herdr documents
        actions as one-shot, and a share that lived inside one would die
        with it (and sit 'running' in the plugin log forever)."""
        started = time.time()
        proc = self.run_action(plugin_env, "action-share-pane")
        assert proc.returncode == 0, proc.stderr
        assert time.time() - started < 10, "the action must hand off, not broadcast"

        shares = plugin_env.state / "shares"
        try:
            assert poll_until(lambda: any(shares.glob("*.json")), timeout=45), \
                "the detached daemon never published its share"
            key = next(shares.glob("*.json")).stem
            assert key.endswith("pane-w1-p7")
            # No pane was opened for the broadcast itself; the only pane
            # this plugin opens is the transient link overlay (the
            # daemon opens it just after publishing its state).
            pane_log = plugin_env.stub_dir / "pane-calls.log"
            assert poll_until(
                lambda: pane_log.exists() and "--entrypoint link" in pane_log.read_text(),
                timeout=15,
            ), "the daemon never opened the link overlay"
            calls = pane_log.read_text()
            assert "broadcast" not in calls

            # A second invocation while it is live re-shows the link
            # instead of starting a second broadcast.
            before = calls.count("--entrypoint link")
            proc = self.run_action(plugin_env, "action-share-pane")
            assert proc.returncode == 0, proc.stderr
            calls = (plugin_env.stub_dir / "pane-calls.log").read_text()
            assert calls.count("--entrypoint link") == before + 1
            assert len(list(shares.glob("*.json"))) == 1
        finally:
            self.run_action(plugin_env, "action-stop")

    def test_stop_sees_direct_shares_and_clears_them(self, plugin_env):
        """stop/status match shares by session socket, not state-key
        shape - a directly started share carries a caller-chosen key."""
        keeper = subprocess.Popen(
            ["bash", "-c", f'exec -a "{SHARE_SH} daemon-fake" sleep 60'],
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
                "socket": FAKE_SOCKET,
                "token": "tok-manual",
                "pid": keeper.pid,
            }))
            proc = self.run_action(plugin_env, "action-stop")
            assert proc.returncode == 0, proc.stderr
            assert not (shares / "manual-w1-p7.json").exists(), \
                "stop must clear shares in this session whatever their key"
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
        proc, key = start_daemon(
            plugin_env,
            "daemon-session-share",
            {"SHELLSHARE_SESSION_NAME": "e2e-session", "HERDR_ENV": "1"},
        )
        try:
            url = share_url(plugin_env, key)
            listener = listener_for(plugin_env, url)
            try:
                # The stub mirror's PTY output reaches viewers...
                assert listener.wait_for_message(
                    timeout=15, containing="MIRROR-MARKER-e2e-session"
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

            # A session share badges the workspaces, not a single pane.
            metadata = (plugin_env.stub_dir / "metadata.log").read_text()
            assert "workspace report-metadata w1" in metadata

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
