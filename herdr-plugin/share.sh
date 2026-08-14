#!/usr/bin/env bash
# Herdr plugin control script for shellshare. One script, many
# subcommands: the manifest (../herdr-plugin.toml) is pure routing.
#
#   action-share-pane / action-share-session   herdr actions (fast, one-shot)
#   action-stop / action-status                herdr actions
#   daemon-pane-share / daemon-session-share   detached broadcasters
#   show-link                                  overlay pane entrypoint
#   sweep                                      startup hook + pre-action GC
#
# Shape follows herdr's grain rather than adding chrome of its own:
#
#   - Broadcasts run detached, not in a plugin pane. A pane would put a
#     permanent tab in the user's tab bar for something that is not a
#     terminal they want to look at. Community plugins do the same
#     (collie's systemd service, mirror's daemon); herdr actions are
#     documented as one-shot, so a daemon belongs outside them.
#   - The live share announces itself through herdr's own display
#     metadata (`report-metadata --token`), which renders inside the
#     user's configured sidebar rows - the documented extension point
#     for exactly this. The token carries a TTL the daemon refreshes,
#     so a hard-killed broadcaster's badge expires on its own.
#   - The link appears in an `overlay` pane: herdr's transient surface,
#     which restores the previous focus and zoom when it closes. It is
#     shown once at share time and re-opened on demand by `status`.
#
# Written for bash 3.2 (macOS ships it): no associative arrays, no
# mapfile, no `setsid` assumption. Requires jq (herdr responses are
# JSON) and a shellshare binary; both are checked loudly, never
# degraded silently.
#
# Security posture (mirrors shellshare's own): the share URL - whose
# #fragment IS the encryption key - is never written to disk, never put
# in a notification (herdr truncates bodies, and toast delivery may be
# routed to the OS notification center), and never printed to action
# stdout (herdr persists action stdout in the plugin command log). The
# daemon holds it in memory and serves it through a fifo, which holds
# no data at rest, to the overlay that displays it.
#
# Helpers that run in $(...) return their error message on stdout with a
# non-zero status - a `fatal` inside command substitution would only
# exit the subshell - and the caller passes it to fatal.

set -u

# ---------------------------------------------------------------------
# Environment / paths

HERDR="${HERDR_BIN_PATH:-herdr}"
STATE_ROOT="${HERDR_PLUGIN_STATE_DIR:-}"
CONFIG_FILE="${HERDR_PLUGIN_CONFIG_DIR:-}/config"
SCRIPT_PATH="$0"

# The metadata source id this plugin reports under; herdr scopes
# tokens per source, so ours never collide with another reporter's.
META_SOURCE="plugin:shellshare"
# Sidebar badge shown while a share is live, and the refresh/TTL pair
# that makes it self-expiring: a daemon killed with SIGKILL cannot
# clear its own badge, so the badge outlives it by at most TTL.
BADGE_VALUE="◉ shared"
BADGE_REFRESH_SECS=30
BADGE_TTL_MS=90000

# Everything this plugin writes is private: state records name rooms
# and PIDs, and fifos carry the plaintext broadcast.
umask 077

notify() {
    # Best-effort: notifications can be disabled, rate-limited, or
    # undeliverable; the overlay and the sidebar badge are the surfaces
    # that actually carry the news.
    "$HERDR" notification show "$1" --body "$2" >/dev/null 2>&1 || true
}

fatal() {
    # Actions run headless; stderr lands in `herdr plugin log`.
    printf 'ERROR: %s\n' "$*" >&2
    notify "Shellshare error" "$*"
    exit 1
}

require_plugin_env() {
    [ -n "$STATE_ROOT" ] && [ -n "${HERDR_BIN_PATH:-}" ] ||
        fatal "not running under herdr: this script is driven by the shellshare herdr plugin (see herdr-plugin/README.md)"
    command -v jq >/dev/null 2>&1 ||
        fatal "jq is required (https://jqlang.org); install it and retry"
    mkdir -p "$SHARES_DIR" "$LOCKS_DIR"
}

# ---------------------------------------------------------------------
# Config: KEY=VALUE lines, values taken literally. Parsed, not sourced -
# a sourced file would execute $(...) from pasted snippets.

cfg() { # cfg <key> <default>
    local v=""
    if [ -f "$CONFIG_FILE" ]; then
        v=$(sed -n "s/^[[:space:]]*$1[[:space:]]*=[[:space:]]*//p" "$CONFIG_FILE" | tail -n 1)
    fi
    printf '%s' "${v:-$2}"
}

# Prints the binary path on success; prints the error message on failure
# (caller: bin=$(shellshare_bin) || fatal "$bin").
shellshare_bin() {
    local bin
    bin="${SHELLSHARE_BIN:-$(cfg shellshare_bin "")}"
    if [ -n "$bin" ]; then
        if [ ! -x "$bin" ]; then
            printf 'shellshare binary not executable: %s' "$bin"
            return 1
        fi
        printf '%s' "$bin"
        return 0
    fi
    if ! command -v shellshare >/dev/null 2>&1; then
        printf 'shellshare not found on PATH. Install it (https://shellshare.net has one-liners: npx -y shellshare, or a static binary) or set shellshare_bin= in %s' "$CONFIG_FILE"
        return 1
    fi
    printf 'shellshare'
}

# The daemons pass --cols/--rows unconditionally; releases up to 3.11.0
# reject them with a clap error. Preflight so an old binary fails with
# an actionable message instead of raw clap stderr.
check_shellshare_supports_size() { # check_shellshare_supports_size <bin>
    "$1" --help 2>/dev/null | grep -q -- '--cols'
}

# A pane's exact cell rect as "cols rows" (empty when unreadable).
pane_rect() { # pane_rect <pane-id>
    "$HERDR" pane layout --pane "$1" 2>/dev/null |
        jq -r --arg p "$1" \
            '.result.layout.panes[] | select(.pane_id == $p) | "\(.rect.width) \(.rect.height)"' 2>/dev/null
}

# The same rect, but empty while the tab is zoomed. A zoom - including
# the temporary one an `overlay` pane puts on top, i.e. this plugin's
# own link overlay - resizes every rect in the tab without changing the
# pane the user is sharing. Geometry checks must sit that out rather
# than read it as "the pane was resized".
pane_rect_stable() { # pane_rect_stable <pane-id>
    "$HERDR" pane layout --pane "$1" 2>/dev/null |
        jq -r --arg p "$1" \
            'if (.result.layout.zoomed // false) then empty
             else .result.layout.panes[] | select(.pane_id == $p)
                  | "\(.rect.width) \(.rect.height)" end' 2>/dev/null
}

# Append config-driven broadcast flags to the ss_args array.
add_base_args() {
    local server theme
    server=$(cfg server "")
    theme=$(cfg theme "")
    [ -n "$server" ] && ss_args=("${ss_args[@]}" --server "$server")
    [ -n "$theme" ] && ss_args=("${ss_args[@]}" --theme "$theme")
    return 0
}

poll_interval() {
    local v
    v=$(cfg poll_interval "0.25")
    case "$v" in
    '' | *[!0-9.]* | *.*.* | .*) v="0.25" ;; # not a plain decimal
    esac
    case "$v" in
    *[1-9]*) ;;    # any nonzero digit anywhere = a real interval
    *) v="0.25" ;; # every all-zero spelling would busy-loop
    esac
    printf '%s' "$v"
}

# ---------------------------------------------------------------------
# State. Scoped per session: plugin state is global per user, but pane
# ids like w1:p1 repeat in every session - unscoped records would let
# session B's stop clobber session A's share. Key =
# s<cksum of socket path>-<mode>[-<sanitized pane id>].

SHARES_DIR="$STATE_ROOT/shares"
LOCKS_DIR="$STATE_ROOT/locks"

session_scope() {
    printf '%s' "${HERDR_SOCKET_PATH:-}" | cksum | awk '{print $1}'
}

sanitize() {
    printf '%s' "$1" | tr -c 'a-zA-Z0-9' '-' | tr -s '-' | sed 's/-$//'
}

state_file() { printf '%s/%s.json' "$SHARES_DIR" "$1"; }
lock_dir() { printf '%s/%s' "$LOCKS_DIR" "$1"; }
run_dir() { printf '%s/run-%s' "$STATE_ROOT" "$1"; }

# A share is live when its recorded PID is alive AND still runs this
# script (PID reuse guard): the per-share token rides in the process
# environment (Linux: /proc; macOS has no /proc, so fall back to
# matching this script's daemon argv - weaker, but it cannot mistake an
# unrelated process for a broadcaster).
share_alive() { # share_alive <pid> <token>
    local pid="$1" token="$2"
    [ -n "$pid" ] || return 1
    kill -0 "$pid" 2>/dev/null || return 1
    if [ -r "/proc/$pid/environ" ]; then
        tr '\0' '\n' <"/proc/$pid/environ" 2>/dev/null |
            grep -qxF "SHELLSHARE_SHARE_TOKEN=$token"
    else
        ps -o command= -p "$pid" 2>/dev/null | grep -qF "$SCRIPT_PATH daemon-"
    fi
}

state_live() { # state_live <state-file>
    local pid token
    pid=$(jq -r '.pid // empty' "$1" 2>/dev/null)
    token=$(jq -r '.token // empty' "$1" 2>/dev/null)
    share_alive "$pid" "$token"
}

# Does this record belong to the current session? Matched on the
# recorded socket path, not the state key: the documented direct-start
# recipe uses caller-chosen keys, and those shares must still be
# visible to stop/status in their session.
state_in_session() { # state_in_session <state-file>
    [ "$(jq -r '.socket // empty' "$1" 2>/dev/null)" = "${HERDR_SOCKET_PATH:-}" ]
}

# A live session share means everything visible is broadcast - a gate
# several callers need before showing another share's link on screen.
session_share_live() {
    local f
    for f in "$SHARES_DIR"/*.json; do
        [ -f "$f" ] || continue
        state_in_session "$f" || continue
        [ "$(jq -r '.mode // empty' "$f" 2>/dev/null)" = "session" ] || continue
        state_live "$f" && return 0
    done
    return 1
}

# Garbage-collect records whose processes died uncleanly (SIGKILL, OOM,
# a reboot - traps never ran), locks whose start never completed, and
# run dirs orphaned by the same. Sidebar badges need no sweeping: they
# carry a TTL and expire themselves.
sweep() {
    mkdir -p "$SHARES_DIR" "$LOCKS_DIR"
    local f d key
    for f in "$SHARES_DIR"/*.json; do
        [ -f "$f" ] || continue
        state_live "$f" || rm -f "$f"
    done
    # A lock older than 2 minutes with no state file is a start that
    # died before the daemon could take over.
    for d in "$LOCKS_DIR"/*; do
        [ -d "$d" ] || continue
        [ -f "$SHARES_DIR/$(basename "$d").json" ] && continue
        if [ -n "$(find "$d" -maxdepth 0 -mmin +2 2>/dev/null)" ]; then
            rm -rf "$d"
        fi
    done
    for d in "$STATE_ROOT"/run-*; do
        [ -d "$d" ] || continue
        key=$(basename "$d")
        key=${key#run-}
        f="$SHARES_DIR/$key.json"
        # Keep while the share is live, still starting (lock held), or
        # simply young - a concurrent sweep must never yank a run dir
        # from under a share that is mid-start.
        { [ -f "$f" ] && state_live "$f"; } && continue
        [ -d "$LOCKS_DIR/$key" ] && continue
        [ -n "$(find "$d" -maxdepth 0 -mmin -2 2>/dev/null)" ] && continue
        rm -rf "$d"
    done
}

# Returns non-zero (keeping the lock) when the record cannot be
# written - a broadcast without a record would be invisible to
# stop/status/sweep and, with a fixed room, invite a second broadcaster
# into the same room.
write_state() { # write_state <key> <mode> <target> <room>
    if jq -n \
        --arg key "$1" --arg mode "$2" --arg target "$3" --arg room "$4" \
        --arg socket "${HERDR_SOCKET_PATH:-}" \
        --arg token "$SHELLSHARE_SHARE_TOKEN" --argjson pid "$$" \
        '{key:$key, mode:$mode, target:$target, room:$room,
          socket:$socket, token:$token, pid:$pid}' \
        >"$(state_file "$1").tmp" &&
        mv "$(state_file "$1").tmp" "$(state_file "$1")"; then
        rm -rf "$(lock_dir "$1")"
        return 0
    fi
    rm -f "$(state_file "$1").tmp"
    return 1
}

# Refuse to start a second broadcaster into a room another live share
# already uses (two broadcasters interleave frames into garbage). On a
# match, prints who owns the room - possibly a DIFFERENT herdr session
# (prefixed room names are per user, pane ids repeat per session), and
# this session's stop action cannot reach that share. The check is
# best-effort: two shares starting in the same instant in different
# sessions hold different locks and can both pass it - a sub-second
# window accepted rather than adding cross-session locking.
room_in_use() { # room_in_use <room> -> prints owner description
    local f
    for f in "$SHARES_DIR"/*.json; do
        [ -f "$f" ] || continue
        if [ "$(jq -r '.room // empty' "$f" 2>/dev/null)" = "$1" ]; then
            if state_in_session "$f"; then
                printf 'a live share of %s in this session (stop it with the shellshare.stop action)' \
                    "$(jq -r '.target // "?"' "$f" 2>/dev/null)"
            else
                printf 'a live share of %s in ANOTHER herdr session (socket %s) - stop it from that session' \
                    "$(jq -r '.target // "?"' "$f" 2>/dev/null)" \
                    "$(jq -r '.socket // "?"' "$f" 2>/dev/null)"
            fi
            return 0
        fi
    done
    return 1
}

# ---------------------------------------------------------------------
# Sidebar badge. herdr renders `$shellshare` wherever the user's
# [ui.sidebar.*] rows mention it; reporting is display-only and cannot
# affect pane lifecycle, so this is safe to do to a pane we do not own.

workspace_ids() {
    "$HERDR" workspace list 2>/dev/null |
        jq -r '.result.workspaces[]? | (.workspace_id // .id // empty)' 2>/dev/null
}

badge_set() { # badge_set <mode> <target-pane>
    local ws
    if [ "$1" = "pane" ]; then
        "$HERDR" pane report-metadata "$2" --source "$META_SOURCE" \
            --token "shellshare=$BADGE_VALUE" --ttl-ms "$BADGE_TTL_MS" >/dev/null 2>&1 || true
        return 0
    fi
    for ws in $(workspace_ids); do
        "$HERDR" workspace report-metadata "$ws" --source "$META_SOURCE" \
            --token "shellshare=$BADGE_VALUE" --ttl-ms "$BADGE_TTL_MS" >/dev/null 2>&1 || true
    done
}

badge_clear() { # badge_clear <mode> <target-pane>
    local ws
    if [ "$1" = "pane" ]; then
        "$HERDR" pane report-metadata "$2" --source "$META_SOURCE" \
            --clear-token shellshare >/dev/null 2>&1 || true
        return 0
    fi
    for ws in $(workspace_ids); do
        "$HERDR" workspace report-metadata "$ws" --source "$META_SOURCE" \
            --clear-token shellshare >/dev/null 2>&1 || true
    done
}

# ---------------------------------------------------------------------
# The link, in memory only. The daemon serves it through a fifo (no
# data at rest); the overlay reads one copy per open.

# One line per reader: each write opens the fifo (blocking until an
# overlay opens the other end), delivers the URL, and closes so the
# reader sees a clean end of line. The brief pause keeps a reader that
# lingers from turning this into a spin.
serve_link() { # serve_link <fifo> <url>
    while :; do
        printf '%s\n' "$2" >"$1" 2>/dev/null || return 0
        sleep 0.2
    done
}

# Reads one line from a share's fifo, with a bounded wait so a dead
# daemon cannot hang the overlay (macOS has no coreutils `timeout`).
read_link() { # read_link <key>
    local fifo out
    fifo="$(run_dir "$1")/link"
    [ -p "$fifo" ] || return 1
    out=$(
        {
            IFS= read -r line <"$fifo" && printf '%s' "$line"
        } &
        reader=$!
        (
            sleep 5
            kill "$reader" 2>/dev/null
        ) >/dev/null 2>&1 &
        guard=$!
        wait "$reader" 2>/dev/null
        kill "$guard" 2>/dev/null
    )
    [ -n "$out" ] || return 1
    printf '%s' "$out"
}

open_link_overlay() { # open_link_overlay [state-key]
    set -- --plugin "${HERDR_PLUGIN_ID:-shellshare}" --entrypoint link \
        --placement overlay --focus ${1:+--env "SHELLSHARE_STATE_KEY=$1"}
    "$HERDR" plugin pane open "$@" >/dev/null 2>&1 || true
}

# ---------------------------------------------------------------------
# Actions - fast and one-shot, the way herdr documents them. They
# validate, take the start lock, hand off to a detached daemon, and
# return; the daemon owns the rest of the lifecycle.

resolve_target_pane() {
    local target="${SHELLSHARE_TARGET_PANE:-}"
    if [ -z "$target" ]; then
        target=$(printf '%s' "${HERDR_PLUGIN_CONTEXT_JSON:-}" |
            jq -r '.focused_pane_id // empty' 2>/dev/null)
    fi
    [ -z "$target" ] && target="${HERDR_PANE_ID:-}"
    if [ -z "$target" ]; then
        printf 'no target pane: invoke share-pane from a keybinding (the focused pane is shared), or name one explicitly with SHELLSHARE_TARGET_PANE=<pane-id> (see herdr-plugin/README.md)'
        return 1
    fi
    printf '%s' "$target"
}

# If the share behind <key> is already live, re-show its link and exit 0
# (idempotent re-invoke); a dead record is cleaned instead.
bail_if_live() { # bail_if_live <key> <what>
    local f
    f=$(state_file "$1")
    [ -f "$f" ] || return 0
    if state_live "$f"; then
        # While a session share is broadcasting, an overlay showing a
        # pane share's link would show that link to every viewer.
        case "$1" in
        *-session) open_link_overlay "$1" ;;
        *) session_share_live || open_link_overlay "$1" ;;
        esac
        notify "Shellshare" "$2 is already being shared"
        exit 0
    fi
    rm -f "$f"
}

start_daemon() { # start_daemon <subcommand> <key> [NAME=VALUE ...]
    local sub="$1" key="$2"
    shift 2
    local lock run token
    lock=$(lock_dir "$key")
    run=$(run_dir "$key")
    token=$(od -An -N16 -tx1 /dev/urandom | tr -d ' \n')
    # mkdir is the atomic test-and-set: a double keypress lands both
    # invocations here, and exactly one proceeds.
    if ! mkdir "$lock" 2>/dev/null; then
        notify "Shellshare" "That share is already starting"
        exit 0
    fi
    rm -rf "$run"
    mkdir -p "$run"

    # Detach: the broadcast must outlive this action (herdr actions are
    # one-shot) without occupying a pane. setsid gives it its own
    # session where available; nohup covers macOS, which has no setsid.
    (
        for kv in "$@"; do export "$kv"; done
        export SHELLSHARE_STATE_KEY="$key" SHELLSHARE_SHARE_TOKEN="$token"
        if command -v setsid >/dev/null 2>&1; then
            exec setsid nohup bash "$SCRIPT_PATH" "$sub" \
                </dev/null >/dev/null 2>"$run/err"
        else
            exec nohup bash "$SCRIPT_PATH" "$sub" \
                </dev/null >/dev/null 2>"$run/err"
        fi
    ) &
    disown 2>/dev/null || true
}

action_share_pane() {
    require_plugin_env
    sweep
    local target key
    target=$(resolve_target_pane) || fatal "$target"
    key="s$(session_scope)-pane-$(sanitize "$target")"
    bail_if_live "$key" "Pane $target"
    start_daemon daemon-pane-share "$key" "SHELLSHARE_TARGET_PANE=$target"
}

action_share_session() {
    require_plugin_env
    sweep
    local name key
    # Resolve the session by matching the injected socket against the
    # session list. Never guess and never fall back to bare `herdr`:
    # attaching a wrongly-guessed name silently broadcasts (and may
    # resurrect) a different session while handing the user a link.
    name=$("$HERDR" session list --json 2>/dev/null |
        jq -r --arg sock "${HERDR_SOCKET_PATH:-}" \
            '.sessions[] | select(.socket_path == $sock) | .name')
    [ -n "$name" ] || fatal "could not resolve which herdr session this is (socket ${HERDR_SOCKET_PATH:-unset} is not in 'herdr session list')"
    key="s$(session_scope)-session"
    bail_if_live "$key" "This session"
    start_daemon daemon-session-share "$key" "SHELLSHARE_SESSION_NAME=$name"
}

action_stop() {
    require_plugin_env
    sweep
    local stopped=0 f pid i
    for f in "$SHARES_DIR"/*.json; do
        [ -f "$f" ] || continue
        state_in_session "$f" || continue
        pid=$(jq -r '.pid // empty' "$f")
        if state_live "$f"; then
            # TERM the daemon: its trap stops the poller / mirror so
            # shellshare drains and flushes, and clears the badge. Only
            # drop the record once the process is really gone.
            kill -TERM "$pid" 2>/dev/null || true
            i=0
            while [ "$i" -lt 100 ] && kill -0 "$pid" 2>/dev/null; do
                sleep 0.1
                i=$((i + 1))
            done
            if kill -0 "$pid" 2>/dev/null; then
                kill -KILL "$pid" 2>/dev/null || true
                sleep 0.2
                # The daemon could not clear its own badge; do it here
                # (it would expire on its own, but not for ~90s).
                badge_clear "$(jq -r '.mode' "$f")" "$(jq -r '.target' "$f")"
            fi
        fi
        rm -f "$f"
        stopped=$((stopped + 1))
    done
    if [ "$stopped" -gt 0 ]; then
        notify "Shellshare" "Stopped $stopped share(s). Links stay readable until the server's room TTL (~6h)"
    else
        notify "Shellshare" "No live shares in this session"
    fi
}

action_status() {
    require_plugin_env
    sweep
    local live=0 f
    for f in "$SHARES_DIR"/*.json; do
        [ -f "$f" ] || continue
        state_in_session "$f" || continue
        live=$((live + 1))
    done
    if [ "$live" -eq 0 ]; then
        notify "Shellshare" "No live shares in this session"
        exit 0
    fi
    open_link_overlay
}

# ---------------------------------------------------------------------
# Overlay: shows the live share links, then restores the user's focus
# and zoom when dismissed. This is the only place a URL is displayed.

show_link() {
    require_plugin_env
    local want="${SHELLSHARE_STATE_KEY:-}" shown=0 hidden=0 f key mode target url
    printf '\033[2J\033[H\n  \033[1mshellshare\033[0m\n'
    local session_live=0
    session_share_live && session_live=1
    for f in "$SHARES_DIR"/*.json; do
        [ -f "$f" ] || continue
        state_in_session "$f" || continue
        state_live "$f" || continue
        key=$(jq -r '.key' "$f")
        mode=$(jq -r '.mode' "$f")
        target=$(jq -r '.target' "$f")
        [ -n "$want" ] && [ "$want" != "$key" ] && continue
        # A session share broadcasts this overlay too: never render
        # another share's key-bearing link into it.
        if [ "$session_live" = "1" ] && [ "$mode" != "session" ]; then
            hidden=$((hidden + 1))
            continue
        fi
        url=$(read_link "$key") || url=""
        shown=$((shown + 1))
        if [ "$mode" = "session" ]; then
            printf '\n  session \033[1m%s\033[0m - the whole herdr UI, read-only\n' "$target"
        else
            printf '\n  pane \033[1m%s\033[0m - live view, read-only\n' "$target"
        fi
        if [ -n "$url" ]; then
            printf '\n    %s\n' "$url"
            if command -v qrencode >/dev/null 2>&1; then
                printf '\n'
                qrencode -t ANSIUTF8 -m 1 "$url" 2>/dev/null | sed 's/^/    /'
            fi
        else
            printf '\n    (link unavailable - the broadcaster may be shutting down)\n'
        fi
    done
    if [ "$shown" -eq 0 ]; then
        printf '\n  No live shares to display.\n'
    fi
    if [ "$hidden" -gt 0 ]; then
        printf '\n  %d pane share(s) hidden: a session share is broadcasting,\n' "$hidden"
        printf '  and their links would be shown to its viewers.\n'
    fi
    printf '\n  \033[2mStop sharing with the shellshare.stop action.\033[0m\n'
    printf '  \033[2mPress any key to close.\033[0m\n'
    read -r -n 1 -s _ 2>/dev/null || read -r _ 2>/dev/null || true
}

# ---------------------------------------------------------------------
# Daemons (detached; no controlling terminal, nothing on screen)

require_daemon_env() {
    [ -n "$STATE_ROOT" ] && [ -n "${HERDR_BIN_PATH:-}" ] || {
        printf 'ERROR: not running under herdr\n' >&2
        exit 1
    }
    [ -n "${SHELLSHARE_STATE_KEY:-}" ] && [ -n "${SHELLSHARE_SHARE_TOKEN:-}" ] || {
        printf 'ERROR: missing share parameters (start shares through the plugin actions)\n' >&2
        exit 1
    }
    export SHELLSHARE_SHARE_TOKEN # liveness checks read it from our environ
    mkdir -p "$SHARES_DIR" "$LOCKS_DIR"
}

# A start that never produced a link: report it where the user will see
# it, drop the lock, and leave no half-share behind. A detached daemon
# has no pane to print into and its stderr dies with the run dir, so
# the message also lands in a last-error file the README points at (it
# never contains a URL - only what went wrong).
start_failed() { # start_failed <key> <message>
    printf 'ERROR: %s\n' "$2" >&2
    printf '%s\n' "$2" >"$STATE_ROOT/last-error.txt" 2>/dev/null || true
    rm -rf "$(lock_dir "$1")" "$(run_dir "$1")"
    notify "Shellshare could not start" "$2"
    exit 1
}

# Wait for shellshare's first stdout line (the `sharing` event). It is
# emitted only after the room is claimed; a connect/auth failure prints
# ERROR to stderr and produces no stdout at all.
wait_for_sharing() { # wait_for_sharing <out-file> <pid> -> sets SHARE_URL
    local out="$1" pid="$2" i=0
    SHARE_URL=""
    while [ "$i" -lt 120 ]; do
        [ -s "$out" ] && break
        kill -0 "$pid" 2>/dev/null || break
        sleep 0.25
        i=$((i + 1))
    done
    # Give the writer a beat to finish the line after the file appears
    [ -s "$out" ] && sleep 0.1
    SHARE_URL=$(head -n 1 "$out" 2>/dev/null | jq -r '.url // empty' 2>/dev/null)
}

# Keep the sidebar badge alive while we are. The TTL is what makes a
# SIGKILLed daemon's badge disappear on its own.
badge_keeper() { # badge_keeper <mode> <target>
    while :; do
        badge_set "$1" "$2"
        sleep "$BADGE_REFRESH_SECS"
    done
}

# The pane poller: full-frame repaints of the target pane's rendered
# viewport. Every frame is self-contained (home + per-line erase +
# erase-below), so a lost final frame on forced shutdown costs nothing.
# No newline after the LAST row: the viewer is exactly rows tall, and a
# newline at the bottom margin would scroll the whole frame up a line.
poll_pane() { # poll_pane <target> <geometry> <interval> <reason-file>
    local target="$1" geometry="$2" interval="$3" reason="$4"
    local prev="" cur fails=0 line first tick=0 rect nap_pid=""
    # An in-flight `sleep` inherits the frames fifo's write end; if it
    # outlived us it would hold the fifo open and delay shellshare's
    # EOF-drain by up to a full interval. Kill it on our way out.
    trap 'kill "$nap_pid" 2>/dev/null; exit 0' TERM INT HUP
    nap() {
        sleep "$interval" &
        nap_pid=$!
        wait "$nap_pid" 2>/dev/null
        nap_pid=""
    }
    printf '\033[2J\033[H\033[?25l'
    while :; do
        if ! cur=$("$HERDR" pane read "$target" --source visible --format ansi 2>/dev/null); then
            fails=$((fails + 1))
            # ~3 consecutive failures = the pane is gone (closed, or
            # moved across workspaces - herdr pane ids change on a
            # cross-workspace move; a limitation the README documents).
            if [ "$fails" -ge 3 ]; then
                printf 'pane %s closed or moved' "$target" >"$reason"
                exit 0
            fi
            nap
            continue
        fi
        fails=0
        # Every ~8th tick, confirm the pane still has the geometry the
        # broadcast was pinned to; a resized pane would otherwise keep
        # streaming mis-shaped frames into the old grid indefinitely.
        tick=$(((tick + 1) % 8))
        if [ "$tick" = "0" ]; then
            rect=$(pane_rect_stable "$target")
            if [ -n "$rect" ] && [ "$rect" != "$geometry" ]; then
                printf 'pane %s resized' "$target" >"$reason"
                exit 0
            fi
        fi
        if [ "$cur" != "$prev" ]; then
            prev="$cur"
            printf '\033[H'
            first=1
            printf '%s\n' "$cur" | while IFS= read -r line; do
                if [ "$first" = "1" ]; then
                    first=0
                else
                    printf '\n'
                fi
                printf '%s\033[K' "$line"
            done
            printf '\033[0J'
        fi
        nap
    done
}

daemon_pane_share() {
    require_daemon_env
    local key="$SHELLSHARE_STATE_KEY" target="${SHELLSHARE_TARGET_PANE:-}" ss
    [ -n "$target" ] || start_failed "$key" "missing SHELLSHARE_TARGET_PANE"
    ss=$(shellshare_bin) || start_failed "$key" "$ss"
    check_shellshare_supports_size "$ss" ||
        start_failed "$key" "this shellshare is too old for the herdr plugin (needs --cols/--rows, shellshare 3.12+). Upgrade: npx -y shellshare@latest"

    local rect cols rows
    rect=$(pane_rect "$target")
    cols=${rect%% *}
    rows=${rect##* }
    case "${cols:-x}${rows:-x}" in *[!0-9]*) start_failed "$key" "cannot read the geometry of pane $target - is it still open?" ;; esac

    # Room: random by default (generated inside shellshare - never in
    # argv). room_prefix gives stable links, scoped per pane so
    # concurrent shares never collide on one room.
    local prefix room="" owner
    local ss_args=(--json --cols "$cols" --rows "$rows")
    prefix=$(cfg room_prefix "")
    if [ -n "$prefix" ]; then
        room="$prefix-pane-$(sanitize "$target")"
        owner=$(room_in_use "$room") && start_failed "$key" "room '$room' is already used by $owner"
        ss_args=("${ss_args[@]}" --room "$room")
    fi
    local plain_flag=0
    if [ "$(cfg pane_plaintext "")" = "true" ]; then
        ss_args=("${ss_args[@]}" --disable-encryption)
        plain_flag=1
    fi
    add_base_args

    local run
    run=$(run_dir "$key")
    mkdir -p "$run"
    mkfifo "$run/frames" "$run/link" 2>/dev/null

    # shellshare in stream mode: stdin = repaint frames, stdout = the
    # two JSON events only. Both ends open concurrently and rendezvous.
    "$ss" "${ss_args[@]}" <"$run/frames" >"$run/out" 2>"$run/err" &
    local ss_pid=$!
    poll_pane "$target" "$rect" "$(poll_interval)" "$run/reason" >"$run/frames" &
    local poller_pid=$!

    trap 'kill "$poller_pid" 2>/dev/null' TERM INT HUP

    wait_for_sharing "$run/out" "$ss_pid"
    # The out file's first line is the sharing event - the URL with its
    # key fragment. Parsed, it has no business surviving on disk.
    rm -f "$run/out"
    if [ -z "$SHARE_URL" ]; then
        local err_text
        err_text=$(cat "$run/err" 2>/dev/null || true)
        kill "$poller_pid" "$ss_pid" 2>/dev/null
        start_failed "$key" "could not start the broadcast: ${err_text:-no error output}"
    fi

    write_state "$key" pane "$target" "$room" || {
        kill "$poller_pid" "$ss_pid" 2>/dev/null
        start_failed "$key" "could not record share state under $SHARES_DIR"
    }

    serve_link "$run/link" "$SHARE_URL" &
    local link_pid=$!
    badge_keeper pane "$target" &
    local badge_pid=$!

    notify "Shellshare$([ "$plain_flag" = 1 ] && printf ' (PLAINTEXT)')" \
        "Sharing pane $target read-only"
    open_link_overlay "$key"

    while kill -0 "$ss_pid" 2>/dev/null; do
        wait "$ss_pid" 2>/dev/null || true
    done
    kill "$poller_pid" "$link_pid" "$badge_pid" 2>/dev/null || true
    badge_clear pane "$target"
    rm -f "$(state_file "$key")"
    local reason=""
    [ -f "$run/reason" ] && reason=$(cat "$run/reason")
    rm -rf "$run"
    notify "Shellshare" "Share of pane $target ended${reason:+ ($reason)}"
}

daemon_session_share() {
    require_daemon_env
    local key="$SHELLSHARE_STATE_KEY" name="${SHELLSHARE_SESSION_NAME:-}" ss
    [ -n "$name" ] || start_failed "$key" "missing SHELLSHARE_SESSION_NAME"
    ss=$(shellshare_bin) || start_failed "$key" "$ss"
    check_shellshare_supports_size "$ss" ||
        start_failed "$key" "this shellshare is too old for the herdr plugin (needs --cols/--rows, shellshare 3.12+). Upgrade: npx -y shellshare@latest"

    # Mirror size: the user's own render extent when available (viewers
    # then see roughly what the user sees), else config, else 120x36.
    local cols rows dims
    dims=$("$HERDR" api snapshot 2>/dev/null | jq -r '
        .result.snapshot as $s |
        ([$s.layouts[] | select(.tab_id == $s.focused_tab_id)][0].area // empty) |
        "\(.x + .width) \(.y + .height)"' 2>/dev/null)
    cols=${dims%% *}
    rows=${dims##* }
    case "${cols:-x}${rows:-x}" in *[!0-9]*) cols="" rows="" ;; esac
    [ -n "$cols" ] || cols=$(cfg session_cols "120")
    [ -n "$rows" ] || rows=$(cfg session_rows "36")

    local prefix room="" owner
    local ss_args=(--json --cols "$cols" --rows "$rows")
    prefix=$(cfg room_prefix "")
    if [ -n "$prefix" ]; then
        room="$prefix-session"
        owner=$(room_in_use "$room") && start_failed "$key" "room '$room' is already used by $owner"
        ss_args=("${ss_args[@]}" --room "$room")
    fi

    # Plaintext for a whole-session mirror needs an explicit, spelled-out
    # opt-in: shellshare made --disable-encryption per-invocation and
    # loud on purpose; a sticky config default silently downgrading the
    # entire UI to plaintext would be exactly the accident its warning
    # exists to prevent.
    local plain_flag=0
    case "$(cfg session_plaintext "")" in
    yes-i-know)
        ss_args=("${ss_args[@]}" --disable-encryption)
        plain_flag=1
        ;;
    "") ;;
    *) start_failed "$key" "session_plaintext must be exactly 'yes-i-know' to broadcast the whole session unencrypted" ;;
    esac
    add_base_args

    local run
    run=$(run_dir "$key")
    mkdir -p "$run"
    mkfifo "$run/pty" "$run/link" 2>/dev/null

    # The mirror: a second herdr client attached to this session inside
    # shellshare's PTY. HERDR_ENV gates nested herdr - unset exactly
    # that. </dev/null is load-bearing: with a TTY stdin, exec mode
    # raw-modes the terminal and forwards every keystroke invisibly into
    # the fully-privileged mirror client; EOF disarms the forwarder
    # while the mirror still gets a real TTY from shellshare's PTY.
    "$ss" exec "${ss_args[@]}" \
        -- env -u HERDR_ENV "$HERDR" session attach "$name" \
        </dev/null >"$run/pty" 2>"$run/err" &
    local ss_pid=$!
    # exec mode interleaves the mirror's PTY bytes on stdout after the
    # first JSON line: keep the line, drain the rest. One continuous
    # open - closing the read end mid-run would SIGPIPE the broadcast,
    # and a log file would grow without bound.
    (
        exec <"$run/pty"
        IFS= read -r first
        printf '%s\n' "$first" >"$run/out"
        exec cat >/dev/null
    ) &
    local drain_pid=$!

    trap 'kill -TERM "$ss_pid" 2>/dev/null' TERM INT HUP

    wait_for_sharing "$run/out" "$ss_pid"
    rm -f "$run/out"
    if [ -z "$SHARE_URL" ]; then
        local err_text
        err_text=$(cat "$run/err" 2>/dev/null || true)
        kill "$ss_pid" "$drain_pid" 2>/dev/null
        start_failed "$key" "could not start the broadcast: ${err_text:-no error output}"
    fi

    write_state "$key" session "$name" "$room" || {
        kill "$ss_pid" "$drain_pid" 2>/dev/null
        start_failed "$key" "could not record share state under $SHARES_DIR"
    }

    serve_link "$run/link" "$SHARE_URL" &
    local link_pid=$!
    badge_keeper session "" &
    local badge_pid=$!

    notify "Shellshare$([ "$plain_flag" = 1 ] && printf ' (PLAINTEXT)')" \
        "Sharing session $name read-only - everything visible in the UI"
    open_link_overlay "$key"

    while kill -0 "$ss_pid" 2>/dev/null; do
        wait "$ss_pid" 2>/dev/null || true
    done
    kill "$drain_pid" "$link_pid" "$badge_pid" 2>/dev/null || true
    badge_clear session ""
    rm -f "$(state_file "$key")"
    rm -rf "$run"
    notify "Shellshare" "Session share ended"
}

# ---------------------------------------------------------------------

case "${1:-}" in
action-share-pane) action_share_pane ;;
action-share-session) action_share_session ;;
action-stop) action_stop ;;
action-status) action_status ;;
daemon-pane-share) daemon_pane_share ;;
daemon-session-share) daemon_session_share ;;
show-link) show_link ;;
sweep)
    require_plugin_env
    sweep
    ;;
*)
    printf 'usage: share.sh {action-share-pane|action-share-session|action-stop|action-status|daemon-pane-share|daemon-session-share|show-link|sweep}\n' >&2
    exit 2
    ;;
esac
