"""
E2E Tests for the herdr plugin (herdr-plugin.toml + herdr-plugin/share.sh)

The plugin is one pane that runs `shellshare exec -- herdr session
attach <this session>`: the pane IS the share. These tests never talk to
a real herdr - a stub `herdr` serves the calls share.sh makes (session
list, api snapshot, session attach, and the workspace/tab/pane calls,
including the metadata token the live pane marks itself with) - but the
shellshare side is real: the broadcast runs against a dedicated local
server with encryption on, and the assertions read it back through the
viewer WebSocket.

Test categories:
- Manifest <-> share.sh lockstep (routing, dot-free ids, entrypoint)
- The pane broadcasts the session, marks itself, and prints its link
- The four things a hand-typed command gets wrong (nesting gate, session
  identity, pinned geometry, swallowed stdout)
- Toggle starts a share in a space of its own and stops it by closing
  that pane - never a tab or a space, which may hold the user's work
- The indicator never lies: a share that dies, or never starts, gives up
  both the label and the mark; a stop that leaves anything live fails
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
        # Only with --json, as real herdr: without it the answer is a
        # human table, and the plugin would resolve no session at all.
        if [ "$3" != "--json" ]; then exit 1; fi
        printf '{"sessions":[{"default":true,"name":"e2e-session","running":true,"socket_path":"%s"},{"default":false,"name":"other-session","running":true,"socket_path":"/tmp/other.sock"}]}\\n' "$FAKE_SOCKET"
        ;;
    "api snapshot")
        # Two jobs: the focused tab's extent is the client size the
        # mirror is pinned to, and the pane list is where a live share
        # announces itself. A pane that has died is simply absent, which
        # is the whole point of marking the pane rather than the space.
        # While a share is live the space around it is deliberately
        # crowded: the marked pane, a split the user made beside it in
        # the SAME tab, and a tab of theirs elsewhere in the space. Every
        # stop assertion therefore runs against windows that must not be
        # touched.
        panes='{"pane_id":"w1:p1","tab_id":"w1:t1","tokens":null}'
        if [ -f "$STUB_DIR/pane-token" ]; then
            panes="$panes"',{"pane_id":"w9:p2","tab_id":"w9:t9","tokens":{"shellshare_live":"1"}}'
            panes="$panes"',{"pane_id":"w9:p3","tab_id":"w9:t9","tokens":null}'
            panes="$panes"',{"pane_id":"w9:p4","tab_id":"w9:t8","tokens":null}'
        fi
        printf '{"id":"x","result":{"snapshot":{"focused_tab_id":"w1:t1","layouts":[{"tab_id":"w1:t1","area":{"height":37,"width":133,"x":10,"y":1}}],"panes":[%s]}}}\\n' "$panes"
        ;;
    "pane report-metadata")
        printf '%s\\n' "$*" >> "$STUB_DIR/herdr-calls.log"
        # Real herdr rejects a mark it cannot place, so the stub must
        # too: accepting a pane id the plugin never had, or a missing
        # --source, would let a broken mark pass the suite and fail on
        # a real herdr with the share already running.
        case "$3" in w[0-9]*:p[0-9]*) ;; *) exit 1 ;; esac
        case "$*" in *--source*) ;; *) exit 1 ;; esac
        case "$*" in
        *--clear-token*) rm -f "$STUB_DIR/pane-token" ;;
        *)
            if [ -f "$STUB_DIR/mark-fails" ]; then exit 1; fi
            : > "$STUB_DIR/pane-token"
            ;;
        esac
        ;;
    "pane close")
        printf '%s\\n' "$*" >> "$STUB_DIR/herdr-calls.log"
        if [ -f "$STUB_DIR/pane-close-fails" ]; then exit 1; fi
        # herdr unwinds the rest: the pane goes, and with it the mark.
        rm -f "$STUB_DIR/pane-token"
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
        printf '{"id":"x","result":{"workspace":{"workspace_id":"w9"},"tab":{"tab_id":"w9:t1"},"root_pane":{"pane_id":"w9:p1"}}}\\n'
        ;;
    "plugin pane")
        printf '%s\\n' "$*" >> "$STUB_DIR/herdr-calls.log"
        # herdr runs the pane, and the pane marks itself. Standing in for
        # that here is what lets a toggle test press the key twice.
        : > "$STUB_DIR/pane-token"
        ;;
    "workspace close")
        printf '%s\\n' "$*" >> "$STUB_DIR/herdr-calls.log"
        rm -f "$STUB_DIR/pane-token"
        ;;
    "tab close")
        printf '%s\\n' "$*" >> "$STUB_DIR/herdr-calls.log"
        if [ -f "$STUB_DIR/tab-close-fails" ]; then exit 1; fi
        ;;
    "workspace rename")
        printf '%s\\n' "$*" >> "$STUB_DIR/herdr-calls.log"
        # Fails ONCE, like a transient herdr hiccup - so a test can
        # check both what the failure costs and that the retry pays.
        if [ -f "$STUB_DIR/rename-fails" ]; then
            rm -f "$STUB_DIR/rename-fails"
            exit 1
        fi
        ;;
    "notification show"|"workspace focus"|"tab rename")
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
        # Not the terminal pytest was run from: die_pane parks on
        # `read`, so an inherited TTY would hang the suite when run
        # with -s instead of failing.
        stdin=subprocess.DEVNULL,
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

            calls = (plugin_env.stub_dir / "herdr-calls.log").read_text()
            # The tab says what it is (a manifest pane `title` does not
            # become the tab label, so the pane sets it).
            rename = [c for c in calls.splitlines() if "tab rename" in c]
            assert rename and "w1:t1" in rename[0] and "live" in rename[0], rename

            # The mark that makes this share stoppable, spelled exactly:
            # this pane's own id, the plugin as the source, the token the
            # toggle looks for. A share herdr rejects the mark for is a
            # share nothing can stop.
            assert [c for c in calls.splitlines() if c.startswith("pane report-metadata")] == [
                "pane report-metadata w1:p9 --source shellshare "
                "--token shellshare_live=1"
            ], calls
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
        The next press stops the share by closing its pane, found by
        asking herdr, not by trusting a file: a pid file would go stale
        across a crash or a reboot, and herdr ids are small per-server
        counters that get reused, so acting on a stale one means closing
        somebody else's window."""
        result = run_script(plugin_env, "toggle")
        assert result.returncode == 0, result.stderr
        calls = (plugin_env.stub_dir / "herdr-calls.log").read_text()

        assert "workspace create --label" in calls
        assert "shellshare" in calls.split("workspace create --label")[1].split("\n")[0]
        # The pane goes into that space, not the caller's - spelled in
        # full, because every part of it is load-bearing and a stub will
        # accept a call real herdr would not: the plugin and entrypoint
        # name the pane to open, --workspace puts it in the new space,
        # and --no-focus keeps the layout still until it is in place.
        assert [c for c in calls.splitlines() if "plugin pane open" in c] == [
            "plugin pane open --plugin shellshare --entrypoint live "
            "--placement tab --no-focus --workspace w9"
        ], calls
        # The space's own shell tab is closed, and it ends up focused so
        # the link is in front of the user who just asked to share.
        assert "tab close w9:t1" in calls
        assert "workspace focus w9" in calls

        # Now that a marked pane exists, the same action stops it - by
        # closing that PANE, the narrowest thing that is unambiguously
        # the share. Never its tab (the stub's share has a split of the
        # user's in it) and never its space (they have a tab there too);
        # herdr unwinds both by itself when nothing else is left in them.
        result = run_script(plugin_env, "toggle")
        assert result.returncode == 0, result.stderr
        calls = (plugin_env.stub_dir / "herdr-calls.log").read_text()
        assert [c for c in calls.splitlines() if c.startswith("pane close")] == \
            ["pane close w9:p2"], calls
        assert "workspace close" not in calls, \
            f"stopping closed a whole space: {calls!r}"
        assert "tab close w9:t9" not in calls, \
            f"stopping closed the share's whole tab: {calls!r}"

    def test_an_interrupt_during_startup_still_corrects_the_label(self, plugin_env):
        """The pane is stoppable from the moment it marks itself, but
        startup keeps going for a while after that - looking up
        shellshare, the session, the client size. A stop landing in that
        window must still leave the label right, or a space the user has
        a tab in keeps claiming a broadcast that never began."""
        slow = plugin_env.stub_dir / "slow-shellshare"
        slow.write_text("#!/bin/bash\nsleep 30\n")
        slow.chmod(0o755)
        (plugin_env.stub_dir.parent / "config" / "config").write_text(
            f"shellshare_bin={slow}\n"
        )
        proc = subprocess.Popen(
            ["bash", str(SHARE_SH), "live"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            env=plugin_env.env, cwd=str(REPO_ROOT), stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
        calls_file = plugin_env.stub_dir / "herdr-calls.log"
        try:
            # Marked, and now stuck in `shellshare --help`.
            assert poll_until(
                lambda: calls_file.exists()
                and "--token shellshare_live=1" in calls_file.read_text(),
                timeout=60,
            ), "the pane never marked itself"
            os.killpg(proc.pid, signal.SIGINT)
            assert poll_until(
                lambda: "workspace rename w1" in calls_file.read_text(), timeout=60
            ), f"an interrupted startup left the space live: {calls_file.read_text()!r}"
        finally:
            stop_pane(proc)

    def test_a_space_that_outlives_the_share_stops_saying_live(self, plugin_env):
        """Normally the space goes when this pane does. When it does not
        - the user left a tab of their own in there - the row is still
        labelled ◉ shellshare, claiming a broadcast that has ended."""
        proc, _url, _lines = start_pane(plugin_env)
        calls_file = plugin_env.stub_dir / "herdr-calls.log"
        try:
            assert poll_until(
                lambda: "tab rename" in calls_file.read_text(), timeout=60
            ), "the share never came up"
            # Ctrl+C, the way the README says to stop.
            os.killpg(proc.pid, signal.SIGINT)
            assert poll_until(
                lambda: "workspace rename w1" in calls_file.read_text(), timeout=60,
            ), f"the space kept its live label: {calls_file.read_text()!r}"
            rename = [
                c for c in calls_file.read_text().splitlines()
                if c.startswith("workspace rename")
            ][0]
            assert "live" not in rename, rename
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
            '  "api snapshot") printf \'{"result":{"snapshot":{"panes":[]}}}\\n\' ;;\n'
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

    def test_a_pane_that_cannot_mark_itself_does_not_broadcast(self, plugin_env):
        """The mark is the only handle on a running share. A pane that
        cannot make one must not go on to broadcast: it would put the
        session on the internet with nothing able to stop it, and the
        mark is made before the broadcast for exactly that reason."""
        (plugin_env.stub_dir / "mark-fails").touch()
        proc = subprocess.Popen(
            ["bash", str(SHARE_SH), "live"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            env=plugin_env.env, cwd=str(REPO_ROOT), stdin=subprocess.DEVNULL,
        )
        _, err = proc.communicate(timeout=60)
        assert proc.returncode != 0
        assert "could not mark this pane" in err, err
        assert not (plugin_env.stub_dir / "attach-env").exists(), \
            "an unstoppable broadcast was started anyway"

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
            '  "api snapshot") printf \'{"result":{"snapshot":{"panes":['
            '{"pane_id":"w8:p1","tab_id":"w8:t1","tokens":{"shellshare_live":"1"}},'
            '{"pane_id":"w9:p1","tab_id":"w9:t1","tokens":{"shellshare_live":"1"}}]}}}\\n\' ;;\n'
            '  "pane close") printf \'%s\\n\' "$*" >> "$STUB_DIR/wontclose-calls.log"\n'
            '     if [ "$3" = "w9:p1" ]; then exit 1; fi ;;\n'
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
        assert "w9:p1" in result.stderr, result.stderr
        calls = (plugin_env.stub_dir / "wontclose-calls.log").read_text()
        # Both were attempted - one failure must not abandon the rest.
        assert "pane close w8:p1" in calls and "pane close w9:p1" in calls, calls
        assert "Stopped sharing" not in calls, \
            f"a partial stop was announced as a stop: {calls!r}"

    def test_toggle_never_closes_a_space_it_did_not_create(self, plugin_env):
        """The dangerous failure mode: closing a space takes every tab in
        it. With no share running, the action must start one rather than
        close whatever the user happens to be looking at - even when
        creating the space fails."""
        # herdr refuses to make the space (stub returns nothing useful).
        broken = dict(plugin_env.env, HERDR_BIN_PATH=str(plugin_env.stub_dir / "broken"))
        (plugin_env.stub_dir / "broken").write_text(
            "#!/bin/bash\n"
            'case "$1 $2" in\n'
            '  "api snapshot") printf \'{"result":{"snapshot":{"panes":[{"pane_id":"w1:p1","tab_id":"w1:t1","tokens":null}]}}}\\n\' ;;\n'
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

    @pytest.mark.parametrize(
        "snapshot",
        [
            pytest.param("exit 1", id="herdr-does-not-answer"),
            # A shape that is not the one we read. `map` over an object
            # succeeds and finds nothing, so without a type check this
            # answers "not sharing" - and starts a second broadcast on
            # top of the live one.
            pytest.param(
                'printf \'{"result":{"snapshot":{"panes":{}}}}\\n\'',
                id="answer-in-the-wrong-shape",
            ),
        ],
    )
    def test_toggle_refuses_when_it_cannot_ask_herdr(self, plugin_env, snapshot):
        """"Not sharing" and "could not ask" must not look the same: if a
        failed lookup read as "not sharing", pressing the key to STOP a
        live share would start a second one instead."""
        broken = dict(
            plugin_env.env, HERDR_BIN_PATH=str(plugin_env.stub_dir / "mute")
        )
        (plugin_env.stub_dir / "mute").write_text(
            "#!/bin/bash\n"
            'case "$1 $2" in\n'
            f'  "api snapshot") {snapshot} ;;\n'
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

            # The label is what the user reads; the mark is what the
            # toggle reads. Both have to stop saying live, or the next
            # keypress "stops" a share that is already over. Wait on the
            # LAST thing die_pane does (the notification), so the two
            # calls before it are certainly logged - and the mark being
            # cleared at all proves the order, since it only happens
            # once the relabel has succeeded.
            assert poll_until(
                lambda: "stopped unexpectedly" in calls(), timeout=60,
            ), f"the dead broadcast never reported itself: {calls()!r}"
            assert "--clear-token shellshare_live" in calls(), \
                f"the dead share still counts as one: {calls()!r}"
            # The whole command, not just "it does not say live": a
            # rename herdr would reject still logs here, and the token
            # is given up on the strength of it succeeding. (There is
            # more than one - die_pane's, then the retry on the way out
            # - and every one has to be well formed.)
            renames = [
                c for c in calls().splitlines()
                if c.startswith("workspace rename")
            ]
            assert renames and set(renames) == {
                "workspace rename w1 ✗ shellshare (stopped)"
            }, renames
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
        order. The token is what lets the next press close this pane;
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

            # And the relabel gets another go on the way out of the
            # parked pane - the stub fails it only once. Without that
            # retry the pane and its mark vanish together, leaving a
            # space the user has a tab in still saying "◉ shellshare",
            # with nothing left to recognise it by.
            assert poll_until(
                lambda: calls_file.read_text().count("workspace rename") > 1,
                timeout=60,
            ), f"the failed relabel was never retried: {calls_file.read_text()!r}"
            last = [
                c for c in calls_file.read_text().splitlines()
                if c.startswith("workspace rename")
            ][-1]
            assert "live" not in last, last
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
