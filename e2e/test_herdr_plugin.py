"""
E2E Tests for the herdr plugin (herdr-plugin.toml + herdr-plugin/share.sh)

The plugin is one pane that runs `shellshare exec -- herdr session
attach <this session>`: the pane IS the share. These tests never talk to
a real herdr - a stub `herdr` serves the calls share.sh makes (session
list, api snapshot, session attach, and the workspace/tab/pane calls,
including the metadata token that marks a space as the plugin's) - but
the shellshare side is real: the broadcast runs against a dedicated
local server with encryption on, and the assertions read it back through
the viewer WebSocket.

Test categories:
- Manifest <-> share.sh lockstep (routing, dot-free ids, entrypoint)
- The pane broadcasts the session and prints its link
- The four things a hand-typed command gets wrong (nesting gate, session
  identity, pinned geometry, swallowed stdout)
- Toggle starts a share in a space it claims and stops it by closing
  that space - and never touches a space it did not claim, whatever it
  is called
- The indicator never lies: a share that dies, or never starts, gives up
  both the label and the token; a stop that leaves anything live fails
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
        if [ -f "$STUB_DIR/attach-dies" ]; then
            printf 'the mirror fell over\\n' >&2
            sleep 1
            exit 3
        fi
        # Long enough to outlive any single test, short enough that an
        # orphan (shellshare's PTY child is not in our process group)
        # cannot hold a test hostage.
        exec sleep 30
        ;;
    "workspace create")
        printf '%s\\n' "$*" >> "$STUB_DIR/herdr-calls.log"
        # A created space becomes visible to `workspace list` - that is
        # how the plugin later recognises its own share.
        printf '%s\\n' "$4" > "$STUB_DIR/space-label"
        printf '{"id":"x","result":{"workspace":{"workspace_id":"w9"},"tab":{"tab_id":"w9:t1"},"root_pane":{"pane_id":"w9:p1"}}}\\n'
        ;;
    "workspace report-metadata")
        printf '%s\\n' "$*" >> "$STUB_DIR/herdr-calls.log"
        # Real herdr stores these against the workspace named in $3 and
        # hands them back in `workspace list`; these files are that
        # store. Setting the token is what makes a space this plugin's,
        # clearing it is how a share stops counting as one.
        case "$*" in
        *--clear-token*) rm -f "$STUB_DIR/token-$3" ;;
        *) : > "$STUB_DIR/token-$3" ;;
        esac
        ;;
    "workspace list")
        # w1 is the user's, and in one test it wears the plugin's label
        # to prove the label decides nothing. Only a token does - and
        # tokens are per workspace here, as they are in herdr, so
        # stamping the wrong one cannot pass unnoticed.
        user_label="${STUB_USER_SPACE_LABEL:-work}"
        w1_tokens=null; w9_tokens=null
        [ -f "$STUB_DIR/token-w1" ] && w1_tokens='{"shellshare_live":"1"}'
        [ -f "$STUB_DIR/token-w9" ] && w9_tokens='{"shellshare_live":"1"}'
        if [ -f "$STUB_DIR/space-label" ]; then
            printf '{"id":"x","result":{"workspaces":[{"workspace_id":"w1","label":"%s","tab_count":1,"tokens":%s},{"workspace_id":"w9","label":"%s","tab_count":%s,"tokens":%s}]}}\\n' "$user_label" "$w1_tokens" "$(cat "$STUB_DIR/space-label")" "${STUB_SHARE_TABS:-1}" "$w9_tokens"
        else
            printf '{"id":"x","result":{"workspaces":[{"workspace_id":"w1","label":"%s","tab_count":1,"tokens":%s}]}}\\n' "$user_label" "$w1_tokens"
        fi
        ;;
    "workspace close")
        printf '%s\\n' "$*" >> "$STUB_DIR/herdr-calls.log"
        rm -f "$STUB_DIR/space-label" "$STUB_DIR/token-$3"
        ;;
    "tab close")
        printf '%s\\n' "$*" >> "$STUB_DIR/herdr-calls.log"
        if [ -f "$STUB_DIR/tab-close-fails" ]; then exit 1; fi
        ;;
    "workspace rename")
        printf '%s\\n' "$*" >> "$STUB_DIR/herdr-calls.log"
        if [ -f "$STUB_DIR/rename-fails" ]; then exit 1; fi
        ;;
    "plugin pane"|"notification show"|"workspace focus"|"tab rename")
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
    (config_dir / "config").write_text(
        f"shellshare_bin={CLI_PATH}\nshellshare_args=--server {server.url}\n"
    )

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

        # One action, one pane: the share is the pane, and the action
        # toggles it. Each must route to ITS OWN subcommand - checking
        # only that both arms exist would let the two be swapped, and a
        # swap is silent and bad both ways: the action would broadcast
        # in the caller's space instead of making one, and the pane
        # would toggle workspaces instead of sharing.
        assert [a["command"] for a in manifest["actions"]] == \
            [["bash", "herdr-plugin/share.sh", "toggle"]]
        assert [p["command"] for p in manifest["panes"]] == \
            [["bash", "herdr-plugin/share.sh", "live"]]
        for arm in ("toggle", "live"):
            assert f"{arm})" in script, f"share.sh has no arm for {arm}"

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

            # The mirror has provably streamed (a viewer just read it),
            # so its absence from this pane's own screen is now a real
            # assertion rather than a race: shellshare exec echoes those
            # PTY bytes on stdout, and swallowing them is what stops the
            # mirror from rendering a rendering of itself.
            shown = "".join(lines)
            assert "MIRROR-MARKER" not in shown, \
                f"the mirror's output leaked onto the pane's screen: {shown!r}"
            assert "SHELLSHARE" in shown and "live" in shown

            # The tab says what it is (a manifest pane `title` does not
            # become the tab label, so the pane sets it).
            rename = [
                c for c in (plugin_env.stub_dir / "herdr-calls.log").read_text().splitlines()
                if "tab rename" in c
            ]
            assert rename and "w1:t1" in rename[0] and "live" in rename[0], rename
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

    def test_toggle_starts_a_share_in_its_own_space_and_stops_it(self, plugin_env):
        """One press makes a space, claims it, puts the share in it and
        drops the shell tab herdr created with it (that tab would
        outlive the broadcast and keep the space - the indicator - up).
        The next press stops the share by closing that space, found by
        asking herdr, not by trusting a file: a pid file would go stale
        across a crash or a reboot, and herdr ids are small per-server
        counters that get reused, so acting on a stale one means closing
        somebody else's space."""
        result = run_script(plugin_env, "toggle")
        assert result.returncode == 0, result.stderr
        calls = (plugin_env.stub_dir / "herdr-calls.log").read_text()

        assert "workspace create --label" in calls
        assert "shellshare" in calls.split("workspace create --label")[1].split("\n")[0]
        # Claimed before anything runs in it: the token is what makes the
        # space stoppable, so it has to exist before the share does.
        # On w9, the space it just made - never on w1, the caller's,
        # which the next stop would then close with the user's tabs.
        claim = [c for c in calls.splitlines() if c.startswith("workspace report-metadata")]
        assert claim == [
            "workspace report-metadata w9 --source shellshare "
            "--token shellshare_live=1"
        ], calls
        assert calls.index("report-metadata") < calls.index("plugin pane open"), calls
        # The pane goes into that space, not the caller's.
        pane_open = [c for c in calls.splitlines() if "plugin pane open" in c]
        assert pane_open and "--workspace w9" in pane_open[0], pane_open
        assert "--placement tab" in pane_open[0]
        # The space's own shell tab is closed, and it ends up focused so
        # the link is in front of the user who just asked to share.
        assert "tab close w9:t1" in calls
        assert "workspace focus w9" in calls

        # Now that a claimed space exists, the same action stops it -
        # and closes w9 (the one it made), never w1 (the user's).
        result = run_script(plugin_env, "toggle")
        assert result.returncode == 0, result.stderr
        calls = (plugin_env.stub_dir / "herdr-calls.log").read_text()
        closes = [c for c in calls.splitlines() if c.startswith("workspace close")]
        assert closes == ["workspace close w9"], closes

    def test_a_user_space_wearing_the_label_is_never_closed(self, plugin_env):
        """The label is for the human; it authorises nothing. A user who
        names a space of their own "◉ shellshare" - or renames the share
        - must not have it closed, because closing a space takes every
        tab in it. Only the token this plugin stamps says "mine"."""
        env = dict(plugin_env.env, STUB_USER_SPACE_LABEL="◉ shellshare")
        result = subprocess.run(
            ["bash", str(SHARE_SH), "toggle"],
            capture_output=True, text=True, env=env,
            cwd=str(REPO_ROOT), timeout=30,
        )
        assert result.returncode == 0, result.stderr
        calls = (plugin_env.stub_dir / "herdr-calls.log").read_text()
        assert "workspace close" not in calls, \
            f"a space the plugin never claimed was closed: {calls!r}"
        # It read the label as "no share running" and started one.
        assert "workspace create --label" in calls, calls

    def test_a_space_the_user_moved_into_is_not_closed_by_stopping(self, plugin_env):
        """The action focuses the share when it starts, so opening a tab
        there is an easy thing to do - and stopping closes the whole
        space. Ours to close means ours alone; otherwise one press
        destroys work the user did in a tab they opened."""
        result = run_script(plugin_env, "toggle")
        assert result.returncode == 0, result.stderr

        # The user has since opened a tab of their own in it.
        result = run_script(
            plugin_env, "toggle", extra_env={"STUB_SHARE_TABS": "2"}
        )
        assert result.returncode != 0
        assert "tabs of your own" in result.stderr, result.stderr
        calls = (plugin_env.stub_dir / "herdr-calls.log").read_text()
        assert "workspace close" not in calls, \
            f"stopping closed a space holding the user's own tab: {calls!r}"

    def test_a_pane_that_outlives_its_space_gives_up_the_claim(self, plugin_env):
        """Normally the space goes with this pane. When it does not -
        the user left a tab of their own in there - the token must go
        anyway, or their space stays marked as a live share and the next
        stop closes it."""
        proc, _url, _lines = start_pane(plugin_env)
        calls_file = plugin_env.stub_dir / "herdr-calls.log"
        try:
            assert poll_until(
                lambda: "tab rename" in calls_file.read_text(), timeout=60
            ), "the share never came up"
            # Ctrl+C, the way the README says to stop.
            os.killpg(proc.pid, signal.SIGINT)
            assert poll_until(
                lambda: "--clear-token shellshare_live" in calls_file.read_text(),
                timeout=30,
            ), f"the pane exited still holding its claim: {calls_file.read_text()!r}"
        finally:
            stop_pane(proc)

    def test_a_rollback_that_fails_says_the_share_may_still_be_live(self, plugin_env):
        """By the time the last startup step can fail the pane is open,
        so the session is already being broadcast. If the rollback that
        should close it also fails, an error about a shell tab would
        leave the user holding a live link they were never told about."""
        stub = plugin_env.stub_dir / "stuck"
        stub.write_text(
            "#!/bin/bash\n"
            'printf \'%s\\n\' "$*" >> "$STUB_DIR/stuck-calls.log"\n'
            'case "$1 $2" in\n'
            '  "workspace list") printf \'{"result":{"workspaces":[]}}\\n\' ;;\n'
            '  "workspace create") printf \'{"result":{"workspace":{"workspace_id":"w9"},"tab":{"tab_id":"w9:t1"}}}\\n\' ;;\n'
            '  "tab close") exit 1 ;;\n'
            '  "workspace close") exit 1 ;;\n'
            "esac\n"
        )
        stub.chmod(0o755)

        result = subprocess.run(
            ["bash", str(SHARE_SH), "toggle"],
            capture_output=True, text=True,
            env=dict(plugin_env.env, HERDR_BIN_PATH=str(stub)),
            cwd=str(REPO_ROOT), timeout=30,
        )
        assert result.returncode != 0
        assert "still broadcasting" in result.stderr, result.stderr
        assert "herdr workspace close w9" in result.stderr, result.stderr

    def test_a_share_that_cannot_be_claimed_is_not_started(self, plugin_env):
        """The token is the only handle on the space afterwards. Without
        it the share would broadcast with no way for the toggle to stop
        it, so a failed claim has to take the space down with it."""
        broken = dict(plugin_env.env, HERDR_BIN_PATH=str(plugin_env.stub_dir / "noclaim"))
        (plugin_env.stub_dir / "noclaim").write_text(
            "#!/bin/bash\n"
            'case "$1 $2" in\n'
            '  "workspace list") printf \'{"result":{"workspaces":[]}}\\n\' ;;\n'
            '  "workspace create") printf \'{"result":{"workspace":{"workspace_id":"w9"},"tab":{"tab_id":"w9:t1"}}}\\n\' ;;\n'
            '  "workspace report-metadata") exit 1 ;;\n'
            '  *) printf \'%s\\n\' "$*" >> "$STUB_DIR/noclaim-calls.log" ;;\n'
            "esac\n"
        )
        (plugin_env.stub_dir / "noclaim").chmod(0o755)

        result = subprocess.run(
            ["bash", str(SHARE_SH), "toggle"],
            capture_output=True, text=True, env=broken,
            cwd=str(REPO_ROOT), timeout=30,
        )
        assert result.returncode != 0
        calls = (plugin_env.stub_dir / "noclaim-calls.log").read_text()
        assert "plugin pane open" not in calls, \
            f"an unstoppable share was started anyway: {calls!r}"
        assert "workspace close w9" in calls, \
            f"the unclaimed space was left behind: {calls!r}"

    def test_a_shell_tab_that_survives_takes_the_share_down_with_it(self, plugin_env):
        """herdr closes a space with its last tab - that is what makes
        the space the indicator. A shell tab left in it breaks that: the
        sidebar would keep claiming a share that had ended, and the next
        stop would destroy whatever the user had put in that tab."""
        (plugin_env.stub_dir / "tab-close-fails").touch()
        result = run_script(plugin_env, "toggle")
        assert result.returncode != 0
        assert "shell tab" in result.stderr, result.stderr
        calls = (plugin_env.stub_dir / "herdr-calls.log").read_text()
        assert "workspace close w9" in calls, \
            f"the half-built space was left claiming to be live: {calls!r}"

    def test_a_stop_that_leaves_a_broadcast_live_is_not_reported_as_stopped(
        self, plugin_env
    ):
        """"Stopped sharing" while a link is still fed live bytes is the
        one lie this action must never tell."""
        stub = plugin_env.stub_dir / "wontclose"
        stub.write_text(
            "#!/bin/bash\n"
            'case "$1 $2" in\n'
            '  "workspace list") printf \'{"result":{"workspaces":['
            '{"workspace_id":"w8","label":"a","tokens":{"shellshare_live":"1"}},'
            '{"workspace_id":"w9","label":"b","tokens":{"shellshare_live":"1"}}]}}\\n\' ;;\n'
            '  "workspace close") printf \'%s\\n\' "$*" >> "$STUB_DIR/wontclose-calls.log"\n'
            '     if [ "$3" = "w9" ]; then exit 1; fi ;;\n'
            '  *) printf \'%s\\n\' "$*" >> "$STUB_DIR/wontclose-calls.log" ;;\n'
            "esac\n"
        )
        stub.chmod(0o755)

        result = subprocess.run(
            ["bash", str(SHARE_SH), "toggle"],
            capture_output=True, text=True,
            env=dict(plugin_env.env, HERDR_BIN_PATH=str(stub)),
            cwd=str(REPO_ROOT), timeout=30,
        )
        assert result.returncode != 0
        assert "w9" in result.stderr, result.stderr
        calls = (plugin_env.stub_dir / "wontclose-calls.log").read_text()
        # Both were attempted - one failure must not abandon the rest.
        assert "workspace close w8" in calls and "workspace close w9" in calls, calls
        assert "Stopped sharing" not in calls, \
            f"a partial stop was announced as a stop: {calls!r}"

    def test_toggle_never_closes_a_space_it_did_not_create(self, plugin_env):
        """The dangerous failure mode: closing a space takes every tab in
        it. With no labelled space present, the action must start a share
        rather than close whatever the user happens to be looking at -
        even when creating the space fails."""
        # herdr refuses to make the space (stub returns nothing useful).
        broken = dict(plugin_env.env, HERDR_BIN_PATH=str(plugin_env.stub_dir / "broken"))
        (plugin_env.stub_dir / "broken").write_text(
            "#!/bin/bash\n"
            'case "$1 $2" in\n'
            '  "workspace list") printf \'{"result":{"workspaces":[{"workspace_id":"w1","label":"work"}]}}\\n\' ;;\n'
            '  "workspace create") exit 1 ;;\n'
            '  *) printf \'%s\\n\' "$*" >> "$STUB_DIR/broken-calls.log" ;;\n'
            "esac\n"
        )
        (plugin_env.stub_dir / "broken").chmod(0o755)

        result = subprocess.run(
            ["bash", str(SHARE_SH), "toggle"],
            capture_output=True, text=True, env=broken,
            cwd=str(REPO_ROOT), timeout=30,
        )
        assert result.returncode != 0
        assert "could not create the shellshare space" in result.stderr
        log = plugin_env.stub_dir / "broken-calls.log"
        calls = log.read_text() if log.exists() else ""
        assert "workspace close" not in calls, \
            f"a failed start must never close a space: {calls!r}"
        assert "plugin pane open" not in calls, \
            f"no share may be opened outside its own space: {calls!r}"

    def test_unreachable_server_fails_visibly(self, plugin_env):
        (plugin_env.stub_dir.parent / "config" / "config").write_text(
            f"shellshare_bin={CLI_PATH}\n"
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
        # shellshare's own stderr has to survive into the pane's message
        # - it is the half that says WHY, and the pane is the only place
        # the user will look.
        assert "127.0.0.1:1" in err and "Connection refused" in err, err

    def test_a_share_that_never_starts_stops_claiming_to_be_live(self, plugin_env):
        """The space is created and labelled before the pane runs, so a
        failure BEFORE the link exists leaves the same lie as one after
        it: the sidebar says live, and the next keypress stops a share
        that never started. Every pane failure path must relabel."""
        (plugin_env.stub_dir.parent / "config" / "config").write_text(
            "shellshare_bin=/nonexistent/shellshare\n"
        )
        proc = subprocess.Popen(
            ["bash", str(SHARE_SH), "live"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            env=plugin_env.env, cwd=str(REPO_ROOT), stdin=subprocess.DEVNULL,
        )
        _, err = proc.communicate(timeout=60)
        assert proc.returncode != 0
        assert "cannot execute" in err, err
        calls = (plugin_env.stub_dir / "herdr-calls.log").read_text()
        rename = [c for c in calls.splitlines() if c.startswith("workspace rename")]
        assert rename and "live" not in rename[0], \
            f"a share that never started kept its live label: {calls!r}"

    def test_toggle_refuses_when_it_cannot_ask_herdr(self, plugin_env):
        """"Not sharing" and "could not ask" must not look the same: if a
        failed lookup read as "not sharing", pressing the key to STOP a
        live share would start a second one instead."""
        broken = dict(
            plugin_env.env, HERDR_BIN_PATH=str(plugin_env.stub_dir / "mute")
        )
        (plugin_env.stub_dir / "mute").write_text(
            "#!/bin/bash\n"
            'case "$1 $2" in\n'
            '  "workspace list") exit 1 ;;\n'
            '  *) printf \'%s\\n\' "$*" >> "$STUB_DIR/mute-calls.log" ;;\n'
            "esac\n"
        )
        (plugin_env.stub_dir / "mute").chmod(0o755)

        result = subprocess.run(
            ["bash", str(SHARE_SH), "toggle"],
            capture_output=True, text=True, env=broken,
            cwd=str(REPO_ROOT), timeout=30,
        )
        assert result.returncode != 0
        assert "could not ask herdr" in result.stderr
        log = plugin_env.stub_dir / "mute-calls.log"
        calls = log.read_text() if log.exists() else ""
        assert "workspace create" not in calls, \
            f"a share must not be started on a guess: {calls!r}"

    def test_a_dead_broadcast_stops_claiming_to_be_live(self, plugin_env):
        """When a broadcast dies the pane stays open holding the error,
        so its space outlives it. Both answers to "am I sharing?" have to
        be corrected - the label the user reads, and the token the toggle
        reads - or the sidebar lies and the next keypress stops a corpse
        instead of starting a share."""
        (plugin_env.stub_dir / "attach-dies").touch()
        proc, _url, _lines = start_pane(plugin_env)
        try:
            calls_file = plugin_env.stub_dir / "herdr-calls.log"
            # The pane may not have made a single logged call yet, so an
            # absent log is "not yet", not a failure.
            def calls():
                return calls_file.read_text() if calls_file.exists() else ""

            # The label is what the user reads; the token is what the
            # toggle reads. Both have to stop saying live, or the next
            # keypress "stops" a share that is already over. Waiting on
            # the token - the second of the two - also proves the order:
            # it is only cleared once the relabel has succeeded.
            assert poll_until(
                lambda: "--clear-token shellshare_live" in calls(), timeout=60,
            ), f"the dead share still counts as one: {calls()!r}"
            rename = [
                c for c in calls().splitlines()
                if c.startswith("workspace rename")
            ][0]
            assert "shellshare" in rename and "live" not in rename, rename
            # The pane says what it knows. A mirror that dies inside
            # herdr writes to the PTY (i.e. into the broadcast), not to
            # shellshare's stderr, so the exit code is the diagnosis
            # that always survives - the message must not trail off
            # after a colon.
            notes = [
                c for c in calls().splitlines()
                if "stopped unexpectedly" in c
            ]
            assert notes and "exit 3" in notes[0], notes
        finally:
            stop_pane(proc)

    def test_a_corpse_that_cannot_be_relabelled_stays_stoppable(self, plugin_env):
        """The two ways of saying "not live" have to fail in the right
        order. The token is what lets the next press close this space;
        the label is what the user sees. Dropping the token and then
        failing to relabel would leave a row saying "◉ shellshare"
        forever, which no press can clear because nothing recognises it
        any more - so the token is only given up once the label is."""
        (plugin_env.stub_dir / "attach-dies").touch()
        (plugin_env.stub_dir / "rename-fails").touch()
        proc, _url, _lines = start_pane(plugin_env)
        calls_file = plugin_env.stub_dir / "herdr-calls.log"
        try:
            assert poll_until(
                lambda: "stopped unexpectedly" in calls_file.read_text(), timeout=60
            ), f"the dead broadcast never reported itself: {calls_file.read_text()!r}"
            assert "--clear-token" not in calls_file.read_text(), \
                f"a corpse gave up the only handle on it: {calls_file.read_text()!r}"
        finally:
            stop_pane(proc)

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
