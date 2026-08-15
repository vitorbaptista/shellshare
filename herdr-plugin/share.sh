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
# collect, or resurrect - no locks, no liveness tokens, no sweep. Ctrl+C
# or closing the pane ends the broadcast the way you would expect.
#
# That pane lives in a space of its own, created when the share starts.
# What is being shared is the whole session, so parking it inside one
# project's space would misfile it - and herdr closes a space when its
# last tab goes, so the space exists exactly as long as the broadcast
# does. A labelled row that appears in the spaces sidebar while you are
# live, and vanishes when you stop, is the status indicator; it needs no
# sidebar configuration and is visible from wherever you are working.
#
# There is deliberately NO "share this pane" mode. To share one pane,
# run `shellshare` in it: that is a full-fidelity byte stream with
# scrollback and keystroke latency, and nothing this plugin could build
# on top of pane snapshots would be as good.
#
# What the plugin earns over typing the command yourself, all four
# verified against herdr 0.8.0:
#   - `herdr session attach` from inside a pane is refused (HERDR_ENV
#     gates nested herdr), so the mirror needs `env -u HERDR_ENV`.
#   - HERDR_SESSION is not exported, so a hand-typed attach has to guess
#     the session name - and a wrong guess silently starts and
#     broadcasts the DEFAULT session instead of yours.
#   - Without --cols/--rows the second client attaches at 80x24 and
#     herdr's smallest-client-wins sizing drags the real session down
#     with it.
#   - shellshare exec echoes the mirror's PTY bytes on its stdout, so
#     run by hand the mirror renders the pane it lives in: infinite
#     regress. Swallowing that stdout is what keeps this pane static.
#
# bash 3.2 (macOS) compatible; needs jq.

set -u

HERDR="${HERDR_BIN_PATH:-herdr}"
STATE_ROOT="${HERDR_PLUGIN_STATE_DIR:-}"
CONFIG_FILE="${HERDR_PLUGIN_CONFIG_DIR:-}/config"

# The label of the space that exists while a share is live, and of the
# tab inside it. Short: a spaces sidebar row is narrow.
SPACE_LABEL="◉ shellshare"
TAB_LABEL="◉ live"

umask 077

# --------------------------------------------------------------------
# Small helpers

die() { # for actions: stderr lands in `herdr plugin log`
    printf 'ERROR: %s\n' "$*" >&2
    "$HERDR" notification show "Shellshare" --body "$*" >/dev/null 2>&1 || true
    exit 1
}

# For the pane: the message stays on screen, where it can be read and
# copied, until the user dismisses it. (A pane always has a terminal; the
# sleep is only so an automated run does not lose the message entirely.)
die_pane() {
    printf '\n  \033[1;31mshellshare\033[0m %s\n\n  Press Enter to close.\n' "$*" >&2
    read -r _ 2>/dev/null || sleep 5
    exit 1
}

need_env() {
    [ -n "$STATE_ROOT" ] || die "not running under herdr (see herdr-plugin/README.md)"
    command -v jq >/dev/null 2>&1 || die "jq is required (https://jqlang.org)"
    mkdir -p "$STATE_ROOT"
}

# Config: KEY=VALUE, values taken literally. Parsed, not sourced - a
# sourced file would execute $(...) from a pasted snippet.
cfg() { # cfg <key> <default>
    local v=""
    [ -f "$CONFIG_FILE" ] &&
        v=$(sed -n "s/^[[:space:]]*$1[[:space:]]*=[[:space:]]*//p" "$CONFIG_FILE" | tail -n 1)
    printf '%s' "${v:-$2}"
}

# One live share per herdr session; the state is one line, and a stale
# one self-heals because the next `kill -0` rejects it.
state_file() {
    printf '%s/live-%s' "$STATE_ROOT" \
        "$(printf '%s' "${HERDR_SOCKET_PATH:-}" | cksum | awk '{print $1}')"
}

# --------------------------------------------------------------------

action_toggle() {
    need_env
    local f pid pane space
    f=$(state_file)
    if [ -f "$f" ]; then
        read -r pid pane space <"$f"
        if [ -n "${pid:-}" ] && kill -0 "$pid" 2>/dev/null; then
            # Closing the space takes the pane with it, and shellshare
            # exits on the pane's SIGHUP and flushes what it has.
            { [ -n "${space:-}" ] && "$HERDR" workspace close "$space" >/dev/null 2>&1; } ||
                "$HERDR" plugin pane close "$pane" >/dev/null 2>&1 ||
                kill -TERM "$pid" 2>/dev/null
            rm -f "$f"
            "$HERDR" notification show "Shellshare" \
                --body "Stopped sharing. The link stays readable until the room idles out (~6h)" \
                >/dev/null 2>&1 || true
            exit 0
        fi
        rm -f "$f"
    fi

    # A space of its own for a session-wide share. Created unfocused so
    # the layout does not jump before the pane is in place.
    local created ws root_tab
    created=$("$HERDR" workspace create --label "$SPACE_LABEL" --no-focus 2>/dev/null)
    ws=$(printf '%s' "$created" | jq -r '.result.workspace.workspace_id // empty' 2>/dev/null)
    root_tab=$(printf '%s' "$created" | jq -r '.result.tab.tab_id // empty' 2>/dev/null)

    local open_args
    if [ -n "$ws" ]; then
        open_args="--workspace $ws"
    else
        # herdr would not give us a space; fall back to a tab here
        # rather than refusing to share.
        open_args=""
    fi

    # shellcheck disable=SC2086 # open_args is a fixed internal string
    "$HERDR" plugin pane open --plugin "${HERDR_PLUGIN_ID:-shellshare}" \
        --entrypoint live --placement tab --no-focus $open_args >/dev/null || {
        [ -n "$ws" ] && "$HERDR" workspace close "$ws" >/dev/null 2>&1
        die "could not open the shellshare pane (see: herdr plugin log list --plugin shellshare)"
    }

    if [ -n "$ws" ]; then
        # The space came with a shell tab; the share does not need it.
        # (Order matters: herdr closes a space when its last tab goes.)
        [ -n "$root_tab" ] && "$HERDR" tab close "$root_tab" >/dev/null 2>&1
        "$HERDR" workspace focus "$ws" >/dev/null 2>&1 || true
    fi
}

# --------------------------------------------------------------------
# The pane: this process is the share.

pane_live() {
    [ -n "$STATE_ROOT" ] || die_pane "is not running under herdr"
    command -v jq >/dev/null 2>&1 || die_pane "needs jq (https://jqlang.org)"
    mkdir -p "$STATE_ROOT"

    local ss
    ss="${SHELLSHARE_BIN:-$(cfg shellshare_bin "")}"
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
  Upgrade with: npx -y shellshare@latest"

    # Which session is this? Match the injected socket against the
    # session list - never guess: `herdr session attach <wrong-name>`
    # starts that session and broadcasts it instead of yours.
    local name
    name=$("$HERDR" session list --json 2>/dev/null |
        jq -r --arg s "${HERDR_SOCKET_PATH:-}" \
            '.sessions[] | select(.socket_path == $s) | .name')
    [ -n "$name" ] ||
        die_pane "could not tell which herdr session this is
  (socket ${HERDR_SOCKET_PATH:-unset} is not in \`herdr session list\`)"

    # Size the mirror like the real client: the focused tab's layout
    # extent is the client's terminal size. Pinning it keeps herdr's
    # smallest-client-wins sizing from shrinking the session you are
    # sharing.
    local dims cols rows
    dims=$("$HERDR" api snapshot 2>/dev/null | jq -r '
        .result.snapshot as $s |
        ([$s.layouts[] | select(.tab_id == $s.focused_tab_id)][0].area // empty) |
        "\(.x + .width) \(.y + .height)"' 2>/dev/null)
    cols=${dims%% *}
    rows=${dims##* }
    case "${cols:-x}${rows:-x}" in *[!0-9]*) cols=120 rows=36 ;; esac

    local extra
    extra=$(cfg shellshare_args "")

    local fifo err
    fifo="$STATE_ROOT/mirror.fifo"
    err="$STATE_ROOT/last-error.txt"
    rm -f "$fifo"
    mkfifo "$fifo" || die_pane "could not create $fifo"
    trap 'rm -f "$(state_file)" "$fifo"' EXIT

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
    # shellcheck disable=SC2086 # $extra is user-authored, split on purpose
    "$ss" exec --json --cols "$cols" --rows "$rows" $extra \
        -- env -u HERDR_ENV "$HERDR" session attach "$name" \
        </dev/null >"$fifo" 2>"$err" &
    local ss_pid=$!
    printf '%s %s %s\n' "$$" "${HERDR_PANE_ID:-}" "${HERDR_WORKSPACE_ID:-}" >"$(state_file)"
    # Name the tab for what it is; the space label says the same thing
    # one level up, where other spaces can see it.
    [ -n "${HERDR_TAB_ID:-}" ] &&
        "$HERDR" tab rename "$HERDR_TAB_ID" "$TAB_LABEL" >/dev/null 2>&1

    local first="" url=""
    exec 3<"$fifo"
    IFS= read -r -t 45 first <&3
    url=$(printf '%s' "$first" | jq -r '.url // empty' 2>/dev/null)
    if [ -z "$url" ]; then
        kill "$ss_pid" 2>/dev/null
        rm -f "$(state_file)"
        die_pane "could not start the broadcast:
  $(cat "$err" 2>/dev/null || echo 'no error output')"
    fi

    banner "$ss" "$name" "$url" "$cols" "$rows"
    cat <&3 >/dev/null &
    local drain_pid=$!

    wait "$ss_pid" 2>/dev/null
    kill "$drain_pid" 2>/dev/null
    rm -f "$(state_file)" "$fifo"
    printf '\n  \033[2mShare ended. The link stays readable until the room idles out (~6h).\033[0m\n'
}

# Static on purpose: this pane is inside the broadcast, so anything that
# repaints here costs every viewer bandwidth for as long as the share runs.
#
# The link and its QR code are printed by shellshare itself: `status`
# exists to present a share link, renders the QR whenever its stdout is a
# terminal (this pane always is), and takes the link through the
# documented SHELLSHARE_URL variable. So there is no second QR renderer
# to install, and no shellshare surface to add - the plugin hands back
# the URL it just read and lets shellshare do the presenting.
banner() { # banner <shellshare-bin> <session> <url> <cols> <rows>
    printf '\033[2J\033[H'
    printf '\n\033[1mSHELLSHARE\033[0m  \033[32m● live\033[0m  read-only  %sx%s\n\n' "$4" "$5"
    # No pipe here on purpose: status only draws the QR when its stdout
    # is a terminal, and piping it (to indent, say) would quietly turn
    # the QR off.
    SHELLSHARE_URL="$3" "$1" status
    printf '\nViewers see the herdr session \033[1m%s\033[0m - whichever tab you\n' "$2"
    printf 'are looking at, every pane in it. Switch back to your work space.\n'
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
