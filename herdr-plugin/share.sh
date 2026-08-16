#!/usr/bin/env bash
# Herdr plugin control script for shellshare: share this herdr session,
# read-only, as a live shellshare link.
#
#   toggle   herdr action - starts the share, or stops a running one
#   live     the pane entrypoint that IS the share
#
# The share is a pane, not a daemon. That is the whole design: the pane
# runs `shellshare exec -- herdr session attach <this session>`, so the
# process's lifetime is the share's lifetime. herdr already owns pane
# lifetime, which means there is nothing here to supervise, garbage
# collect, or resurrect. Ctrl+C or closing the pane ends the broadcast
# the way you would expect.
#
# That pane lives in a space of its own, created when the share starts.
# What is being shared is the whole session, so parking it inside one
# project's space would misfile it - and herdr closes a space when its
# last tab goes, so the space is gone as soon as the broadcast is. A
# labelled row that appears in the spaces sidebar while you are live is
# the status indicator; it needs no sidebar configuration and is visible
# from wherever you are working. (The one thing that outlives the
# broadcast is a space whose share FAILED: it is kept, relabelled, to
# hold the error where it can be read.)
#
# herdr is also the only place live-share state is kept. The toggle asks
# it what is running rather than keeping a pid file: a file would go
# stale across a crash or a reboot, and acting on a stale herdr id (they
# are small per-server counters, reused after a restart) means closing
# somebody else's tab. Asking cannot go stale.
#
# What it asks for is the metadata token the live pane puts on ITSELF -
# not the space's label, which is free text a user can type or rename
# into, and not a mark on the space, which would outlive the pane that
# put it there. A pane that has died is not in the snapshot, so the only
# thing that can answer "yes, sharing" is a broadcast that exists.
#
# And what it closes is that pane's TAB. Closing a space would take
# every tab in it, including any the user opened there; closing the
# share's own tab ends the broadcast and lets herdr drop the space when
# its last tab goes. Same outcome in the ordinary case, nothing of the
# user's at risk in any other - so no part of this has to work out whose
# tabs are in the way, or be careful not to be wrong about it.
#
# bash 3.2 (macOS) compatible; needs jq.

set -u

HERDR="${HERDR_BIN_PATH:-herdr}"
PLUGIN_ID="${HERDR_PLUGIN_ID:-shellshare}"
STATE_ROOT="${HERDR_PLUGIN_STATE_DIR:-}"
CONFIG_FILE="${HERDR_PLUGIN_CONFIG_DIR:-}/config"

# The mark the live pane puts on itself, and the only thing that
# authorises stopping it.
OWNER_TOKEN=shellshare_live

# The label of the space that exists while a share is live, and of the
# tab inside it. Short: a spaces sidebar row is narrow. This is what the
# user reads; it decides nothing.
SPACE_LABEL="◉ shellshare"
TAB_LABEL="◉ live"
# What the space is renamed to when the broadcast is over but the space
# is not: a pane parked on an error, or a space the user left a tab in.
# Either way the row must stop saying "live".
DEAD_LABEL="✗ shellshare (stopped)"

umask 077

# --------------------------------------------------------------------

die() { # for actions: stderr lands in `herdr plugin log`
    printf 'ERROR: %s\n' "$*" >&2
    "$HERDR" notification show "Shellshare" --body "$*" >/dev/null 2>&1 || true
    exit 1
}

# For the pane: the message stays on screen, where it can be read and
# copied, until the user dismisses it. A pane always has a terminal, so
# `read` blocks; when it does not (an automated run) the message has
# already gone to stderr, which that caller kept.
#
# Parking here keeps the pane - and therefore its space - alive after
# the broadcast is over, so every way of failing, before or after the
# link exists, has to stop the space claiming to be live: relabel it for
# the user, and drop the token for the toggle.
#
# Strictly in that order, and only dropping the mark if the relabel
# worked. The mark is what lets the next press close this tab; the label
# is what the user sees. Losing the mark first and then failing to
# relabel leaves the worst of both - a row that says "◉ shellshare"
# forever, which no press can clear because nothing recognises it any
# more. Keeping the mark instead costs one wasted press, which closes
# this dead tab and takes the wrong label away with it.
die_pane() {
    if [ -n "${HERDR_WORKSPACE_ID:-}" ] && [ -n "${HERDR_PANE_ID:-}" ] &&
        "$HERDR" workspace rename "$HERDR_WORKSPACE_ID" "$DEAD_LABEL" >/dev/null 2>&1; then
        "$HERDR" pane report-metadata "$HERDR_PANE_ID" \
            --source "$PLUGIN_ID" --clear-token "$OWNER_TOKEN" >/dev/null 2>&1
    fi
    # Closing this pane must not take the diagnostics with it: the
    # message about to be printed points at that file.
    trap 'rm -f "${fifo:-}"; exit 1' HUP INT TERM
    printf '\n  \033[1;31mshellshare\033[0m %s\n\n  Press Enter to close.\n' "$*" >&2
    "$HERDR" notification show "Shellshare" --body "$*" >/dev/null 2>&1 || true
    read -r _ 2>/dev/null
    exit 1
}

# Take the live label off on the way out of the pane. Usually there is
# nothing to take it off: this pane is the space's last tab, so the
# space goes when the pane does and the rename quietly fails. It matters
# when the user left a tab of their own in there - then the space
# survives this share, and a row still reading "◉ shellshare" would
# claim a broadcast that has ended. The mark needs no such care: herdr
# takes it away with the pane.
retire_space() {
    [ -n "${HERDR_WORKSPACE_ID:-}" ] || return 0
    "$HERDR" workspace rename "$HERDR_WORKSPACE_ID" "$DEAD_LABEL" >/dev/null 2>&1 || true
}

need_env() {
    # The toggle writes nothing and reads no state; all it needs is a
    # herdr to talk to and jq to read the answer.
    [ -n "${HERDR_SOCKET_PATH:-}" ] || die "not running under herdr (see herdr-plugin/README.md)"
    command -v jq >/dev/null 2>&1 || die "jq is required (https://jqlang.org)"
}

# Config: KEY=VALUE, one per line, values taken literally. Parsed, not
# sourced - a sourced file would execute $(...) from a pasted snippet.
cfg() { # cfg <key>
    [ -f "$CONFIG_FILE" ] || return 0
    sed -n "s/^[[:space:]]*$1[[:space:]]*=[[:space:]]*//p" "$CONFIG_FILE" |
        tail -n 1 | tr -d '\r' | sed 's/[[:space:]]*$//'
}

# The tabs of the live share (normally one, and usually none), one id
# per line. This is the plugin's entire notion of "am I sharing?" -
# herdr is asked, never told.
#
# What it asks for is panes carrying this plugin's metadata token, which
# the live pane puts on itself. That makes the answer the broadcast
# itself rather than a description of it: a pane that has died is not in
# the snapshot, so there is no mark left to go stale, and no user can
# type one by accident the way they can type a label.
#
# The answer is in PANES, and panes are what stopping closes - the
# narrowest thing that is unambiguously the share and nothing else.
# Closing a space would take every tab in it, closing a tab every pane
# in it, and the user can put their own work in either: a tab beside the
# share, or a split inside its tab. herdr unwinds the rest by itself -
# the tab goes when its last pane does, the space when its last tab
# does - so the ordinary case still ends with the space disappearing,
# and no code here has to reason about whose windows are in the way.
#
# Returns non-zero when herdr could not be asked at all. That has to be
# distinguishable from "no share is running": treating a failed lookup
# as "not sharing" would turn a stop into a second broadcast. Hence the
# type check rather than `// error`: an object where the pane list
# should be answers "no panes at all" instead of failing.
share_panes() {
    local out
    out=$("$HERDR" api snapshot 2>/dev/null) || return 1
    # An empty answer is not "nothing running" - jq would read it as one
    # and print nothing, which reads as "not sharing".
    [ -n "$out" ] || return 1
    printf '%s' "$out" | jq -r --arg t "$OWNER_TOKEN" '
        (.result.snapshot.panes | if type == "array" then . else error("no pane list") end)
        | map(select((.tokens // {})[$t] != null) | .pane_id | strings) | unique | .[]' 2>/dev/null
}

# Undo a half-built share and report why it failed. This is the one
# place that closes a space rather than a tab, and it is safe for the
# reason the general case is not: the space was created by the lines
# just above, so everything in it got there in the last few
# milliseconds and none of it is the user's.
#
# A close that fails has to be said out loud rather than swallowed under
# the original error: by the time the last step can fail the pane is
# already open, which means the session is already being broadcast. An
# error about a shell tab, with a live link still running behind it, is
# the wrong thing to leave a user holding.
abort_start() { # abort_start <workspace> <what-failed>
    "$HERDR" workspace close "$1" >/dev/null 2>&1 && die "$2"
    die "$2
  The shellshare space ($1) could not be closed either, so it may still
  be open - and if the share had started, still broadcasting. Close it
  with: herdr workspace close $1"
}

# --------------------------------------------------------------------

action_toggle() {
    need_env

    # Stop: closing the pane ends the broadcast - shellshare exits on the
    # SIGHUP and flushes what it has - and herdr unwinds the tab and the
    # space behind it if nothing else is in them, which is the usual
    # case. Anything else in them is the user's, and stays.
    local panes pane alive=""
    panes=$(share_panes) ||
        die "could not ask herdr what is running, so refusing to guess whether you are sharing"
    if [ -n "$panes" ]; then
        # Close every match, not just the first: two would mean two live
        # broadcasts, and stopping has to mean stopped. For the same
        # reason one failed close fails the whole stop - reporting
        # "stopped sharing" while a link is still fed live bytes is the
        # one lie this action must never tell.
        for pane in $panes; do
            "$HERDR" pane close "$pane" >/dev/null 2>&1 || alive="$alive $pane"
        done
        [ -z "$alive" ] ||
            die "could not stop every shellshare pane - still broadcasting:$alive
  Close them by hand (herdr pane close <id>); the link stays live until you do"
        "$HERDR" notification show "Shellshare" \
            --body "Stopped sharing. The link stays readable until the room idles out (~6h)" \
            >/dev/null 2>&1 || true
        exit 0
    fi

    # Start: a space of its own, created unfocused so the layout does
    # not jump before the pane is in place. No fallback to the caller's
    # space: the share must never live somewhere stopping it would take
    # the user's own tabs with it.
    local created root_tab
    created=$("$HERDR" workspace create --label "$SPACE_LABEL" --no-focus 2>/dev/null)
    ws=$(printf '%s' "$created" | jq -r '.result.workspace.workspace_id // empty' 2>/dev/null)
    root_tab=$(printf '%s' "$created" | jq -r '.result.tab.tab_id // empty' 2>/dev/null)
    [ -n "$ws" ] || die "could not create the shellshare space (see: herdr plugin log list --plugin shellshare)"

    if ! "$HERDR" plugin pane open --plugin "$PLUGIN_ID" \
        --entrypoint live --placement tab --no-focus --workspace "$ws" >/dev/null; then
        abort_start "$ws" "could not open the shellshare pane (see: herdr plugin log list --plugin shellshare)"
    fi

    # The space came with a shell tab; the share does not need it, and
    # must not keep it: a tab that outlives the broadcast holds the space
    # open, so the sidebar would claim a share that has ended - and the
    # user's next shell in that tab would be destroyed by the stop.
    # (Order matters: herdr closes a space when its last tab goes.)
    if [ -z "$root_tab" ] || ! "$HERDR" tab close "$root_tab" >/dev/null 2>&1; then
        abort_start "$ws" "could not close the shellshare space's own shell tab, which would outlive the broadcast and keep claiming to be live"
    fi
    "$HERDR" workspace focus "$ws" >/dev/null 2>&1 || true
}

# --------------------------------------------------------------------
# The pane: this process is the share.

pane_live() {
    [ -n "$STATE_ROOT" ] || die_pane "is not running under herdr"
    command -v jq >/dev/null 2>&1 || die_pane "needs jq (https://jqlang.org)"
    mkdir -p "$STATE_ROOT"
    printf '\n  \033[2mshellshare: starting the broadcast...\033[0m\n'

    # Mark this pane as the share, before the slow work rather than
    # after it: the mark is how the toggle finds this share, so anything
    # done before it is running unstoppable, and a second press during
    # startup would open a second share rather than cancel this one.
    # A mark that cannot be made is fatal for the same reason it is made
    # early - a broadcast nothing can stop is not worth starting.
    [ -n "${HERDR_PANE_ID:-}" ] ||
        die_pane "could not tell herdr which pane this is, so refusing to start a share that could not be stopped"
    "$HERDR" pane report-metadata "$HERDR_PANE_ID" \
        --source "$PLUGIN_ID" --token "$OWNER_TOKEN=1" >/dev/null 2>&1 ||
        die_pane "could not mark this pane as the share, so refusing to start one that could not be stopped"
    # From here on this pane is a share, and everything below is setup
    # that takes a moment. A signal arriving in that moment still has to
    # leave the label right; the fuller trap replaces this one once
    # there is a broadcaster to kill and files to remove.
    trap 'retire_space; exit 0' HUP INT TERM

    local ss
    ss=$(cfg shellshare_bin)
    if [ -n "$ss" ]; then
        [ -x "$ss" ] || die_pane "cannot execute $ss (shellshare_bin in $CONFIG_FILE)"
    elif command -v shellshare >/dev/null 2>&1; then
        ss=shellshare
    else
        # Not `npx -y shellshare`, which shellshare.net leads with: it
        # runs a copy and leaves nothing behind, so the next press would
        # land here again. The plugin needs a binary that stays.
        die_pane "could not find the shellshare binary.
  Put one on your PATH (npm i -g shellshare, or a static binary from
  https://get.shellshare.net), or set shellshare_bin= in $CONFIG_FILE"
    fi
    # --cols/--rows arrived in shellshare 3.12. Without them the mirror
    # attaches at 80x24 and drags the real session down to that size, so
    # this is a hard gate rather than a degraded mode.
    "$ss" --help 2>/dev/null | grep -q -- '--cols' ||
        die_pane "needs shellshare 3.12 or newer (for --cols/--rows).
  Found: $(command -v "$ss" 2>/dev/null || printf '%s' "$ss") ($("$ss" -v 2>/dev/null | head -n 1))
  Upgrade it (https://shellshare.net), or point shellshare_bin= at a
  newer one in $CONFIG_FILE"

    # Which session is this? Match the injected socket against the
    # session list - never guess: `herdr session attach <wrong-name>`
    # starts that session and broadcasts it instead of yours.
    local name
    name=$("$HERDR" session list --json 2>/dev/null |
        jq -r --arg s "${HERDR_SOCKET_PATH:-}" \
            '.sessions[]? | select(.socket_path == $s) | .name' 2>/dev/null)
    [ -n "$name" ] ||
        die_pane "could not tell which herdr session this is
  (socket ${HERDR_SOCKET_PATH:-unset} is not in \`herdr session list\`)"

    # Size the mirror like the real client: the focused tab's layout
    # extent is the client's terminal size. Pinning it keeps herdr's
    # smallest-client-wins sizing from shrinking the session being
    # shared - so a guess here would cause the very bug the pinning
    # exists to prevent, and there is no safe default to fall back to.
    local dims cols rows
    dims=$("$HERDR" api snapshot 2>/dev/null | jq -r '
        .result.snapshot as $s |
        ([$s.layouts[]? | select(.tab_id == $s.focused_tab_id)][0].area // empty) |
        "\(.x + .width) \(.y + .height)"' 2>/dev/null)
    cols=${dims%% *}
    rows=${dims##* }
    case "${cols:-x}${rows:-x}" in
    *[!0-9]*) die_pane "could not read this client's terminal size from \`herdr api snapshot\`" ;;
    esac

    local extra
    extra=$(cfg shellshare_args)

    # One fifo and one stderr file per pane process: the state dir is
    # shared by every herdr session, so fixed names would collide
    # between two sessions sharing at once - and the stderr file is
    # exactly what the user is told to read when a share goes wrong.
    local fifo err
    fifo="$STATE_ROOT/mirror-$$.fifo"
    err="$STATE_ROOT/mirror-$$.err"
    rm -f "$fifo"
    mkfifo "$fifo" || die_pane "could not create $fifo"
    # The fifo is plumbing and always goes; the stderr file outlives a
    # failure on purpose, so there is something to read after the pane
    # is gone. A stop the user asked for is not a failure, so it takes
    # both - and it arrives as a signal (closing the space HUPs this
    # pane, Ctrl+C INTs it), which bash does not run EXIT traps for
    # unless the signal itself is trapped.
    trap 'rm -f "$fifo"' EXIT
    # Killing the broadcaster explicitly means a signal aimed at this
    # pane alone cannot orphan a live broadcast. Guarded because $ss_pid
    # is empty until it starts, and `kill ""` would default to 0 - the
    # whole process group, which is not this pane's to signal.
    trap 'retire_space; [ -n "${ss_pid:-}" ] && kill "$ss_pid" 2>/dev/null; rm -f "$fifo" "$err"; exit 0' HUP INT TERM

    # The mirror: a second herdr client attached to this session, inside
    # shellshare's PTY.
    #   env -u HERDR_ENV : herdr refuses to nest without it
    #   </dev/null       : exec mode would otherwise raw-mode this pane
    #                      and forward its keystrokes into a fully
    #                      privileged client of your own session
    #   stdout to a fifo : shellshare echoes the mirror's PTY bytes
    #                      after the JSON line. They must not reach this
    #                      pane's screen - the pane is inside the
    #                      broadcast, so the mirror would end up
    #                      rendering a rendering of itself. Reading the
    #                      first line here rather than through a pipe
    #                      keeps the control flow (and any failure) in
    #                      this shell, where `exit` actually exits.
    # `set -f` for the split: $extra is meant to be split into words,
    # but not to have `--room my*name` matched against the plugin
    # directory the pane happens to run in.
    set -f
    # shellcheck disable=SC2086 # $extra is user-authored, split on purpose
    "$ss" exec --json --cols "$cols" --rows "$rows" $extra \
        -- env -u HERDR_ENV "$HERDR" session attach "$name" \
        </dev/null >"$fifo" 2>"$err" &
    local ss_pid=$!
    set +f

    local first="" url=""
    exec 3<"$fifo"
    IFS= read -r -t 45 first <&3
    url=$(printf '%s' "$first" | jq -r '.url // empty' 2>/dev/null)
    if [ -z "$url" ]; then
        # Two very different failures reach here, and the fix for one is
        # not the fix for the other: shellshare died (its stderr says
        # why), or it is still running and never announced a link -
        # a server that accepts the connection and stalls. Whether the
        # process is still alive tells them apart; bash 3.2 does not
        # report a `read` timeout distinguishably.
        local detail
        detail=$(cat "$err" 2>/dev/null)
        if kill -0 "$ss_pid" 2>/dev/null; then
            detail="shellshare did not report a link within 45s - is the
  server reachable?${detail:+
  }$detail"
        fi
        kill "$ss_pid" 2>/dev/null
        die_pane "could not start the broadcast:
  ${detail:-shellshare exited without saying why}"
    fi

    banner "$ss" "$name" "$url" "$cols" "$rows"
    # Name the tab for what it is; the space label says the same thing
    # one level up, where other spaces can see it. (A manifest pane
    # `title` names the pane, not the tab.)
    [ -n "${HERDR_TAB_ID:-}" ] &&
        "$HERDR" tab rename "$HERDR_TAB_ID" "$TAB_LABEL" >/dev/null 2>&1
    cat <&3 >/dev/null &
    local drain_pid=$!

    wait "$ss_pid"
    local rc=$?
    kill "$drain_pid" 2>/dev/null
    if [ "$rc" -eq 0 ]; then
        # A clean stop closes this pane (and with it the space), so
        # there is nobody left to read a goodbye - and nothing to
        # diagnose afterwards.
        retire_space
        rm -f "$err"
        exit 0
    fi
    # A broadcast that DIED must not look like one the user stopped;
    # die_pane relabels the space so the sidebar stops claiming it.
    #
    # shellshare's stderr is the only thing captured here: the mirror
    # client's own output went down the PTY, i.e. into the broadcast, so
    # a failure inside herdr itself leaves this file empty. Say what is
    # known either way rather than trailing off after a colon.
    local why
    why=$(cat "$err" 2>/dev/null)
    die_pane "the broadcast stopped unexpectedly (exit $rc)${why:+:
  $why}
  shellshare's output: $err"
}

# Static on purpose: this pane is inside the broadcast, so anything that
# repaints here costs every viewer bandwidth for as long as the share
# runs.
#
# The link and its QR code are printed by shellshare itself: `status`
# exists to present a share link, renders the QR whenever its stdout is
# a terminal (this pane always is), and takes the link through the
# documented SHELLSHARE_URL variable. So there is no second QR renderer
# to install, and no shellshare surface to add. Do not pipe that call -
# a pipe would make stdout not-a-terminal and silently drop the QR.
banner() { # banner <shellshare-bin> <session> <url> <cols> <rows>
    printf '\033[2J\033[H'
    printf '\n\033[1mSHELLSHARE\033[0m  \033[32m● live\033[0m  read-only  %sx%s\n\n' "$4" "$5"
    SHELLSHARE_URL="$3" "$1" status
    printf '\nViewers see the herdr session \033[1m%s\033[0m as you see it: the tab\n' "$2"
    printf 'you are looking at, and herdr chrome - your spaces, tabs and agents.\n'
    printf '\n\033[2mCtrl+C here, or the shellshare action again, to stop sharing.\033[0m\n'
}

# --------------------------------------------------------------------

case "${1:-}" in
toggle) action_toggle ;;
live) pane_live ;;
*)
    printf 'usage: share.sh {toggle|live}\n' >&2
    exit 2
    ;;
esac
