"""
E2E Tests for the herdr plugin (herdr-plugin.toml + herdr-plugin/share.sh)

The plugin is one pane that runs `shellshare exec -- herdr session
attach <this session>`: the pane IS the share. These tests never talk to
a real herdr - a stub `herdr` serves the four calls share.sh makes
(session list, api snapshot, session attach, plugin pane open/close) -
but the shellshare side is real: the broadcast runs against a dedicated
local server with encryption on, and the assertions read it back through
the viewer WebSocket.

Test categories:
- Manifest <-> share.sh lockstep (routing, dot-free ids, entrypoint)
- The pane broadcasts the session and prints its link
- The four things a hand-typed command gets wrong (nesting gate, session
  identity, pinned geometry, swallowed stdout)
- Toggle stops a live share; a stale record self-heals
- Failure paths: unreachable server, unresolvable session
"""

import os
import re
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
    poll_until,
    random_id,
)

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="the plugin script is bash"
)

REPO_ROOT = Path(__file__).parent.parent
SHARE_SH = REPO_ROOT / "herdr-plugin" / "share.sh"
MANIFEST = REPO_ROOT / "herdr-plugin.toml"

FAKE_SOCKET = "/tmp/herdr-plugin-e2e.sock"

# The stub records what the plugin asked herdr to do, and stands in for
# the mirror client. `session attach` is where the contract lives: it
# reports the environment it was given and then streams a marker.
STUB_HERDR = textwrap.dedent("""\
    #!/bin/bash
    case "$1 $2" in
    "session list")
        printf '{"sessions":[{"default":true,"name":"e2e-session","running":true,"socket_path":"%s"},{"default":false,"name":"other-session","running":true,"socket_path":"/tmp/other.sock"}]}\\n' "$FAKE_SOCKET"
        ;;
    "api snapshot")
        printf '{"id":"x","result":{"snapshot":{"focused_tab_id":"w1:t1","layouts":[{"tab_id":"w1:t1","area":{"height":37,"width":133,"x":10,"y":1}}]}}}\\n'
        ;;
    "session attach")
        {
            printf 'ATTACHED=%s\\n' "$3"
            printf 'HERDR_ENV=%s\\n' "${HERDR_ENV:-UNSET}"
            if read -r -t 1 _; then printf 'STDIN=received-bytes\\n'; else printf 'STDIN=quiet\\n'; fi
            printf 'SIZE=%s\\n' "$(stty size 2>/dev/null | tr ' ' 'x')"
        } > "$STUB_DIR/attach-env"
        printf 'MIRROR-MARKER-%s\\n' "$3"
        # Long enough to outlive any single test, short enough that an
        # orphan (shellshare's PTY child is not in our process group)
        # cannot hold a test hostage.
        exec sleep 30
        ;;
    "workspace create")
        printf '%s\\n' "$*" >> "$STUB_DIR/herdr-calls.log"
        printf '{"id":"x","result":{"workspace":{"workspace_id":"w9"},"tab":{"tab_id":"w9:t1"},"root_pane":{"pane_id":"w9:p1"}}}\\n'
        ;;
    "plugin pane"|"notification show"|"workspace close"|"workspace focus"|"tab close"|"tab rename")
        printf '%s\\n' "$*" >> "$STUB_DIR/herdr-calls.log"
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
    (config_dir / "config").write_text(f"shellshare_args=--server {server.url}\n")

    env = os.environ.copy()
    env.update(
        HERDR_BIN_PATH=str(herdr),
        HERDR_SOCKET_PATH=FAKE_SOCKET,
        HERDR_PLUGIN_STATE_DIR=str(state),
        HERDR_PLUGIN_CONFIG_DIR=str(config_dir),
        HERDR_PLUGIN_ID="shellshare",
        HERDR_PANE_ID="w1:p9",
        HERDR_TAB_ID="w1:t1",
        HERDR_WORKSPACE_ID="w1",
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


def start_pane(plugin_env, extra_env=None):
    """Run the pane entrypoint the way herdr runs it, and wait for the
    banner - the link's only home.

    Returns (proc, url, lines) where `lines` keeps accumulating whatever
    the pane prints, so a test can assert on the pane's screen without
    reading the live pipe itself."""
    env = dict(plugin_env.env)
    env.update(extra_env or {})
    proc = subprocess.Popen(
        ["bash", str(SHARE_SH), "live"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
        start_new_session=True,
        cwd=str(REPO_ROOT),
    )
    lines = []
    threading.Thread(
        target=lambda: lines.extend(iter(proc.stdout.readline, "")), daemon=True
    ).start()

    def link():
        for line in list(lines):
            found = re.search(r"https?://\S+/r/\S+", line)
            if found:
                return found.group(0)
        return None

    deadline = time.time() + 45
    while time.time() < deadline:
        url = link()
        if url:
            return proc, url, lines
        if proc.poll() is not None:
            break
        time.sleep(0.1)
    stop_pane(proc)
    pytest.fail(f"pane never printed a link; stdout={''.join(lines)!r}")


def stop_pane(proc):
    """Kill the pane's whole process group. shellshare's PTY child gets
    its own session, so it can outlive this - hence the short-lived stub
    mirror rather than a wait that could hang the suite."""
    if proc.poll() is None:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    try:
        proc.wait(timeout=10)
    finally:
        for stream in (proc.stdout, proc.stderr):
            try:
                stream.close()
            except Exception:
                pass


def listener_for(plugin_env, url):
    room = url.split("/r/")[1].split("#")[0]
    listener = SocketListener(room, server_url=plugin_env.server.url)
    listener.set_key(parse_share_key(url))
    listener.connect()
    return listener


class TestManifestLockstep:
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
        for cmd in commands:
            assert cmd[0] == "bash" and cmd[1] == "herdr-plugin/share.sh", cmd
            assert f"{cmd[2]})" in script, f"share.sh has no arm for {cmd[2]}"

        # One action, one pane: the share is the pane, and the action
        # toggles it. share.sh opens that pane by its exact id.
        assert [a["id"] for a in manifest["actions"]] == ["share"]
        assert [p["id"] for p in manifest["panes"]] == ["live"]
        assert manifest["panes"][0]["placement"] == "tab"
        assert "--entrypoint live" in script

        # Local ids are dot-free: dots are reserved for the qualified
        # form shellshare.<id> used by keybindings and action invoke.
        for section in ("actions", "panes"):
            for item in manifest[section]:
                assert "." not in item["id"], item["id"]


class TestSessionShare:
    def test_pane_broadcasts_the_session_and_shows_its_link(self, plugin_env):
        proc, url, lines = start_pane(plugin_env)
        try:
            assert parse_share_key(url), "the link must carry the decryption key"
            # The link is presented by shellshare itself (`status`, handed
            # the URL through SHELLSHARE_URL), so the pane inherits its QR
            # code on a terminal without a second QR renderer. This phrase
            # comes from shellshare, not from share.sh - it is the proof
            # the delegation happened. (The QR itself is gated on a TTY
            # and covered by test_cli_tty.py, which owns that behavior.)
            assert any("Sharing this terminal at" in line for line in lines), \
                f"the banner did not delegate to shellshare status: {lines!r}"
            listener = listener_for(plugin_env, url)
            try:
                assert listener.wait_for_message(
                    timeout=15, containing="MIRROR-MARKER-e2e-session"
                ), "the mirrored session never reached a viewer"
            finally:
                listener.disconnect()

            # The pane records itself so the action can toggle it off.
            state = list(plugin_env.state.glob("live-*"))
            assert len(state) == 1
            pid, pane, space = state[0].read_text().split()
            assert int(pid) > 0 and pane == "w1:p9" and space == "w1"
        finally:
            stop_pane(proc)

    def test_mirror_gets_what_a_hand_typed_command_would_get_wrong(self, plugin_env):
        """The four things that make this worth a plugin: the nesting
        gate cleared, the right session named, the geometry pinned to the
        real client size, and a stdin that cannot drive the session."""
        proc, url, _lines = start_pane(plugin_env, {"HERDR_ENV": "1"})
        try:
            # SIZE is the dump's last line, so waiting for it avoids
            # reading the file while the stub is still writing it.
            dump_file = plugin_env.stub_dir / "attach-env"
            assert poll_until(
                lambda: dump_file.exists() and "SIZE=" in dump_file.read_text(),
                timeout=20,
            ), "the mirror never reported its environment"
            env_dump = dump_file.read_text()

            # Nesting gate: herdr refuses to attach with HERDR_ENV set.
            assert "HERDR_ENV=UNSET" in env_dump
            # Identity: resolved from the socket, not guessed. The stub
            # offers a second session precisely so a guess would show up.
            assert "ATTACHED=e2e-session" in env_dump
            # Geometry: the PTY is the focused tab's extent (x+width by
            # y+height = 143x38, reported by stty as rows x cols), not
            # shellshare's 80x24 headless fallback - which herdr's
            # smallest-client-wins sizing would impose on the real
            # session.
            assert "SIZE=38x143" in env_dump, env_dump
            # Stdin: EOF, so no keystroke can reach the mirror client.
            assert "STDIN=quiet" in env_dump
        finally:
            stop_pane(proc)

    def test_pane_screen_never_contains_the_mirror_output(self, plugin_env):
        """shellshare exec echoes the mirror's PTY bytes on stdout. They
        must be swallowed: this pane is inside the broadcast, so echoing
        them would make the mirror render a rendering of itself."""
        proc, url, lines = start_pane(plugin_env)
        try:
            # Give the mirror time to stream; its bytes must never show
            # up on this pane's screen.
            time.sleep(3)
            shown = "".join(lines)
            assert "MIRROR-MARKER" not in shown, \
                f"the mirror's output leaked onto the pane's screen: {shown!r}"
            assert "SHELLSHARE" in shown and "live" in shown
        finally:
            stop_pane(proc)

    def test_toggle_stops_a_live_share_and_stale_state_self_heals(self, plugin_env):
        proc, _url, _lines = start_pane(plugin_env)
        try:
            state = next(iter(plugin_env.state.glob("live-*")))
            result = run_script(plugin_env, "toggle")
            assert result.returncode == 0, result.stderr
            # Stopping closes the pane; the record goes with it.
            calls = (plugin_env.stub_dir / "herdr-calls.log").read_text()
            assert "workspace close w1" in calls
            assert not state.exists()
        finally:
            stop_pane(proc)

        # A record left by a killed pane must not block the next share:
        # the toggle checks the PID and starts a new one instead.
        state.write_text("999999 w1:p9\n")
        result = run_script(plugin_env, "toggle")
        assert result.returncode == 0, result.stderr
        calls = (plugin_env.stub_dir / "herdr-calls.log").read_text()
        assert "--entrypoint live" in calls, "a stale record must not block a new share"

    def test_share_gets_a_space_of_its_own(self, plugin_env):
        """What is shared is the whole session, so the share does not
        squat in a project's space: the action makes one, labelled, and
        drops the shell tab herdr creates with it. herdr closes a space
        with its last tab, so that space exists exactly while the
        broadcast does - the status indicator, visible from anywhere,
        with no sidebar configuration."""
        result = run_script(plugin_env, "toggle")
        assert result.returncode == 0, result.stderr
        calls = (plugin_env.stub_dir / "herdr-calls.log").read_text()

        assert "workspace create --label" in calls
        assert "shellshare" in calls.split("workspace create --label")[1].split("\n")[0]
        # The pane goes into that space, not the caller's.
        pane_open = [c for c in calls.splitlines() if "plugin pane open" in c]
        assert pane_open and "--workspace w9" in pane_open[0], pane_open
        assert "--placement tab" in pane_open[0]
        # The space's own shell tab is closed, and it ends up focused so
        # the link is in front of the user who just asked to share.
        assert "tab close w9:t1" in calls
        assert "workspace focus w9" in calls

    def test_pane_names_its_tab_live(self, plugin_env):
        proc, _url, _lines = start_pane(plugin_env)
        try:
            calls_file = plugin_env.stub_dir / "herdr-calls.log"
            assert poll_until(
                lambda: calls_file.exists() and "tab rename" in calls_file.read_text(),
                timeout=15,
            ), "the pane never labelled its tab"
            rename = [
                c for c in calls_file.read_text().splitlines() if "tab rename" in c
            ][0]
            assert "w1:t1" in rename and "live" in rename
        finally:
            stop_pane(proc)

    def test_unreachable_server_fails_visibly(self, plugin_env):
        (plugin_env.stub_dir.parent / "config" / "config").write_text(
            "shellshare_args=--server http://127.0.0.1:1\n"
        )
        proc = subprocess.Popen(
            ["bash", str(SHARE_SH), "live"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            env=plugin_env.env, cwd=str(REPO_ROOT), stdin=subprocess.DEVNULL,
        )
        _, err = proc.communicate(timeout=90)
        assert proc.returncode != 0
        assert "could not start the broadcast" in err
        # shellshare's own diagnostics must survive into the message.
        assert "no error output" not in err, err
        assert not list(plugin_env.state.glob("live-*"))

    def test_unresolvable_session_refuses_rather_than_guessing(self, plugin_env):
        """A wrong guess would start and broadcast a different session -
        the failure mode that makes resolution non-optional."""
        proc = subprocess.Popen(
            ["bash", str(SHARE_SH), "live"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            env={**plugin_env.env, "HERDR_SOCKET_PATH": "/nonexistent/other.sock"},
            cwd=str(REPO_ROOT), stdin=subprocess.DEVNULL,
        )
        _, err = proc.communicate(timeout=60)
        assert proc.returncode != 0
        assert "could not tell which herdr session" in err
        assert not (plugin_env.stub_dir / "attach-env").exists(), \
            "nothing may be attached when the session cannot be identified"
