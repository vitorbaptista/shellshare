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
# last tab goes, so the space exists exactly as long as the broadcast
# does. A labelled row that appears in the spaces sidebar while you are
# live, and vanishes when you stop, is the status indicator; it needs no
# sidebar configuration and is visible from wherever you are working.
#
# herdr is also the only place live-share state is kept. The toggle asks
# it whether a space with our label exists rather than keeping a pid
# file: a file would go stale across a crash or a reboot, and acting on
# a stale herdr id (they are small per-server counters, reused after a
# restart) means closing somebody else's space. Asking cannot go stale.
#
# bash 3.2 (macOS) compatible; needs jq.

set -u

HERDR="${HERDR_BIN_PATH:-herdr}"
STATE_ROOT="${HERDR_PLUGIN_STATE_DIR:-}"
CONFIG_FILE="${HERDR_PLUGIN_CONFIG_DIR:-}/config"

# The label of the space that exists while a share is live, and of the
# tab inside it. Short: a spaces sidebar row is narrow. The label is
# also how the toggle recognises its own space, so it must stay
# distinctive.
SPACE_LABEL="◉ shellshare"
TAB_LABEL="◉ live"
# What the space is renamed to when a broadcast dies on its own. The
# pane stays open holding the error, but the label must stop saying
# "live" - it is the only thing that answers "am I sharing?".
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
# Parking here keeps the pane - and therefore its space - alive, and the
# space's label is the only thing that answers "am I sharing?". So every
# way of failing, before or after the link exists, has to correct the
# label first: otherwise the sidebar claims a live share that is not
# running, and the next keypress stops that corpse instead of starting
# a share.
die_pane() {
    [ -n "${HERDR_WORKSPACE_ID:-}" ] &&
        "$HERDR" workspace rename "$HERDR_WORKSPACE_ID" "$DEAD_LABEL" >/dev/null 2>&1
    # Closing this pane must not take the diagnostics with it: the
    # message about to be printed points at that file.
    trap 'rm -f "${fifo:-}"; exit 1' HUP INT TERM
    printf '\n  \033[1;31mshellshare\033[0m %s\n\n  Press Enter to close.\n' "$*" >&2
    "$HERDR" notification show "Shellshare" --body "$*" >/dev/null 2>&1 || true
    read -r _ 2>/dev/null
    exit 1
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

# The workspace ids of the live share's spaces (normally one, and
# usually none), one per line. This is the plugin's entire notion of
# "am I sharing?" - herdr is asked, never told.
#
# Returns non-zero when herdr could not be asked at all. That has to be
# distinguishable from "no share is running": treating a failed lookup
# as "not sharing" would turn a stop into a second broadcast.
share_spaces() {
    local out
    out=$("$HERDR" workspace list 2>/dev/null) || return 1
    # An empty answer is not "no spaces" - jq would read it as one and
    # print nothing, which reads as "not sharing".
    [ -n "$out" ] || return 1
    printf '%s' "$out" | jq -r --arg l "$SPACE_LABEL" '
        (.result.workspaces // error("no workspace list"))
        | map(select(.label == $l) | .workspace_id) | .[]' 2>/dev/null
}

# --------------------------------------------------------------------

action_toggle() {
    need_env

    # Stop: closing the space takes the pane with it, and shellshare
    # exits on the pane's SIGHUP and flushes what it has. Only ever a
    # space this plugin labelled - never one the user made.
    local spaces ws stopped=0
    spaces=$(share_spaces) ||
        die "could not ask herdr which spaces exist, so refusing to guess whether you are sharing"
    if [ -n "$spaces" ]; then
        # Close every match, not just the first: two would mean two live
        # broadcasts, and stopping has to mean stopped.
        for ws in $spaces; do
            "$HERDR" workspace close "$ws" >/dev/null 2>&1 &&
                stopped=$((stopped + 1))
        done
        [ "$stopped" -gt 0 ] || die "could not close the shellshare space ($spaces)"
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

    if ! "$HERDR" plugin pane open --plugin "${HERDR_PLUGIN_ID:-shellshare}" \
        --entrypoint live --placement tab --no-focus --workspace "$ws" >/dev/null; then
        "$HERDR" workspace close "$ws" >/dev/null 2>&1
        die "could not open the shellshare pane (see: herdr plugin log list --plugin shellshare)"
    fi

    # The space came with a shell tab; the share does not need it.
    # (Order matters: herdr closes a space when its last tab goes.)
    [ -n "$root_tab" ] && "$HERDR" tab close "$root_tab" >/dev/null 2>&1
    "$HERDR" workspace focus "$ws" >/dev/null 2>&1 || true
}

# --------------------------------------------------------------------
# The pane: this process is the share.

pane_live() {
    [ -n "$STATE_ROOT" ] || die_pane "is not running under herdr"
    command -v jq >/dev/null 2>&1 || die_pane "needs jq (https://jqlang.org)"
    mkdir -p "$STATE_ROOT"
    printf '\n  \033[2mshellshare: starting the broadcast...\033[0m\n'

    local ss
    ss=$(cfg shellshare_bin)
    if [ -n "$ss" ]; then
        [ -x "$ss" ] || die_pane "cannot execute $ss (shellshare_bin in $CONFIG_FILE)"
    elif command -v shellshare >/dev/null 2>&1; then
        ss=shellshare
    else
        die_pane "could not find the shellshare binary.
  Install it (https://shellshare.net - npx -y shellshare, or a static
  binary) or set shellshare_bin= in $CONFIG_FILE"
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
    # $ss_pid is empty until the broadcaster starts; by the time a
    # signal can arrive it is set, and killing it explicitly means a
    # signal aimed at this pane alone cannot orphan a live broadcast.
    trap 'kill "${ss_pid:-0}" 2>/dev/null; rm -f "$fifo" "$err"; exit 0' HUP INT TERM

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
    printf '\n\033[2mCtrl+C, or close this space, to stop sharing.\033[0m\n'
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
