#!/usr/bin/env bash
# Herdr plugin control script for shellshare. One script, many
# subcommands: the manifest (../herdr-plugin.toml) is pure routing.
#
#   action-share-pane / action-share-session   short-lived herdr actions
#   action-stop / action-status                short-lived herdr actions
#   run-pane-share / run-session-share         long-running pane entrypoints
#   sweep                                      startup hook + pre-action GC
#
# Written for bash 3.2 (macOS ships it): no associative arrays, no
# mapfile. Requires jq (herdr responses are JSON) and a shellshare
# binary; both are checked loudly, never degraded silently.
#
# Security posture (mirrors shellshare's own): the share URL - whose
# #fragment IS the encryption key - is never written to disk, never put
# in a notification (herdr truncates bodies, and toast delivery may be
# routed to the OS notification center), and never printed to action
# stdout (herdr persists action stdout in the plugin command log). Its
# only home is the status pane the user is looking at.
#
# Helpers that run in $(...) return their error message on stdout with a
# non-zero status - a `fatal` inside command substitution would only
# exit the subshell - and the caller passes it to fatal/fatal_pane.

set -u

# ---------------------------------------------------------------------
# Environment / paths

HERDR="${HERDR_BIN_PATH:-herdr}"
STATE_ROOT="${HERDR_PLUGIN_STATE_DIR:-}"
CONFIG_FILE="${HERDR_PLUGIN_CONFIG_DIR:-}/config"
SCRIPT_PATH="$0"

# Everything this plugin writes is private: state records name rooms
# and PIDs, and fifos carry the plaintext broadcast.
umask 077

notify() {
    # Best-effort: notifications can be disabled, rate-limited, or
    # undeliverable; the status pane is the delivery guarantee.
    "$HERDR" notification show "$1" --body "$2" >/dev/null 2>&1 || true
}

fatal() {
    # Actions run headless; stderr lands in `herdr plugin log`.
    printf 'ERROR: %s\n' "$*" >&2
    notify "Shellshare error" "$*"
    exit 1
}

# Entrypoints run inside a visible pane: keep the message on screen so
# the user can actually read it before the pane goes away. Also release
# this share's start lock - an entrypoint that dies holding it would
# make retries claim "already starting" until the sweep GC fires.
fatal_pane() {
    if [ -n "${SHELLSHARE_STATE_KEY:-}" ]; then
        rm -rf "$LOCKS_DIR/$SHELLSHARE_STATE_KEY"
    fi
    printf '\nERROR: %s\n' "$*" >&2
    notify "Shellshare error" "$*"
    if [ -t 0 ]; then
        printf '\nPress Enter to close this pane.\n' >&2
        read -r _ || true
    fi
    exit 1
}

require_plugin_env() {
    [ -n "$STATE_ROOT" ] && [ -n "${HERDR_BIN_PATH:-}" ] ||
        fatal "not running under herdr: this script is driven by the shellshare herdr plugin (see herdr-plugin/README.md)"
    command -v jq >/dev/null 2>&1 ||
        fatal "jq is required (https://jqlang.org); install it and retry"
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
# (caller: bin=$(shellshare_bin) || fatal[_pane] "$bin").
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

# The entrypoints pass --cols/--rows unconditionally; releases up to
# 3.11.0 reject them with a clap error. Preflight so an old binary
# fails with an actionable message instead of raw clap stderr.
check_shellshare_supports_size() { # check_shellshare_supports_size <bin>
    "$1" --help 2>/dev/null | grep -q -- '--cols'
}

# A pane's exact cell rect as "cols rows" (empty when unreadable).
pane_rect() { # pane_rect <pane-id>
    "$HERDR" pane layout --pane "$1" 2>/dev/null |
        jq -r --arg p "$1" \
            '.result.layout.panes[] | select(.pane_id == $p) | "\(.rect.width) \(.rect.height)"' 2>/dev/null
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
    *[1-9]*) ;; # any nonzero digit anywhere = a real interval
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

# A share is live when its recorded PID is alive AND still runs this
# script (PID reuse guard): the per-share token rides in the process
# environment (Linux: /proc; macOS fallback: the command line still
# names share.sh run-*).
share_alive() { # share_alive <pid> <token>
    local pid="$1" token="$2"
    [ -n "$pid" ] || return 1
    kill -0 "$pid" 2>/dev/null || return 1
    if [ -r "/proc/$pid/environ" ]; then
        tr '\0' '\n' <"/proc/$pid/environ" 2>/dev/null |
            grep -qxF "SHELLSHARE_SHARE_TOKEN=$token"
    else
        # No /proc (macOS): the token is invisible, so match this exact
        # script's entrypoint argv - still weaker than the token check
        # (a reused PID running another share's entrypoint would pass),
        # but tight enough that stop/sweep cannot kill or keep alive an
        # unrelated process.
        ps -o command= -p "$pid" 2>/dev/null | grep -qF "$SCRIPT_PATH run-"
    fi
}

state_live() { # state_live <state-file>
    local pid token
    pid=$(jq -r '.pid // empty' "$1" 2>/dev/null)
    token=$(jq -r '.token // empty' "$1" 2>/dev/null)
    share_alive "$pid" "$token"
}

# Does this record belong to the current session? Matched on the
# recorded socket path, not the state key: the documented direct-open
# recipe uses caller-chosen keys (manual-...), and those shares must
# still be visible to stop/status in their session.
state_in_session() { # state_in_session <state-file>
    [ "$(jq -r '.socket // empty' "$1" 2>/dev/null)" = "${HERDR_SOCKET_PATH:-}" ]
}

# A live session share means everything focused is broadcast - a gate
# several callers need before focusing a pane-share status tab, whose
# text is that share's key-bearing URL.
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

# Garbage-collect records whose processes died uncleanly (SIGKILL,
# server stop - traps never ran), locks whose start never completed,
# and run dirs (fifos plus shellshare's stdout, whose first line is the
# key-bearing sharing event until the entrypoint unlinks it) orphaned
# by those same unclean deaths.
sweep() {
    mkdir -p "$SHARES_DIR" "$LOCKS_DIR"
    local f d key
    for f in "$SHARES_DIR"/*.json; do
        [ -f "$f" ] || continue
        state_live "$f" || rm -f "$f"
    done
    # A lock older than 2 minutes with no state file is a start that
    # died before the entrypoint could take over.
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
        --arg pane "${HERDR_PANE_ID:-}" --arg socket "${HERDR_SOCKET_PATH:-}" \
        --arg token "$SHELLSHARE_SHARE_TOKEN" --argjson pid "$$" \
        '{key:$key, mode:$mode, target:$target, room:$room,
          status_pane:$pane, socket:$socket, token:$token, pid:$pid}' \
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
                printf 'a live share of %s in ANOTHER herdr session (socket %s) - stop it from that session or close its Shellshare pane' \
                    "$(jq -r '.target // "?"' "$f" 2>/dev/null)" \
                    "$(jq -r '.socket // "?"' "$f" 2>/dev/null)"
            fi
            return 0
        fi
    done
    return 1
}

# ---------------------------------------------------------------------
# Actions

# Prints the pane id on success; the error message on failure.
resolve_target_pane() {
    local target="${SHELLSHARE_TARGET_PANE:-}"
    if [ -z "$target" ]; then
        target=$(printf '%s' "${HERDR_PLUGIN_CONTEXT_JSON:-}" |
            jq -r '.focused_pane_id // empty' 2>/dev/null)
    fi
    [ -z "$target" ] && target="${HERDR_PANE_ID:-}"
    if [ -z "$target" ]; then
        printf 'no target pane: invoke share-pane from a keybinding (the focused pane is shared), or open the broadcast directly: herdr plugin pane open --plugin shellshare --entrypoint pane-broadcast --env SHELLSHARE_TARGET_PANE=<pane-id> --env ... (see herdr-plugin/README.md)'
        return 1
    fi
    printf '%s' "$target"
}

# If the share behind <key> is already live, focus its pane, notify, and
# exit 0 (idempotent re-invoke); a dead record is cleaned instead.
# Focusing is skipped while a session share is broadcasting and the
# re-invoked share is a pane share: its status tab shows its key-bearing
# URL, and focusing it would hand that link to every session viewer.
bail_if_live() { # bail_if_live <key> <what>
    local f
    f=$(state_file "$1")
    [ -f "$f" ] || return 0
    if state_live "$f"; then
        case "$1" in
        *-session) "$HERDR" plugin pane focus "$(jq -r '.status_pane' "$f")" >/dev/null 2>&1 || true ;;
        *)
            if ! session_share_live; then
                "$HERDR" plugin pane focus "$(jq -r '.status_pane' "$f")" >/dev/null 2>&1 || true
            fi
            ;;
        esac
        notify "Shellshare" "$2 is already being shared - link in the Shellshare tab"
        exit 0
    fi
    rm -f "$f"
}

open_share_pane() { # open_share_pane <entrypoint> <key> [--env KEY=VAL ...]
    local entrypoint="$1" key="$2"
    shift 2
    local token lock
    token=$(od -An -N16 -tx1 /dev/urandom | tr -d ' \n')
    lock=$(lock_dir "$key")
    # mkdir is the atomic test-and-set: a double keypress lands both
    # invocations here, and exactly one proceeds.
    if ! mkdir "$lock" 2>/dev/null; then
        notify "Shellshare" "This share is already starting or live - see the Shellshare tab"
        exit 0
    fi
    if ! "$HERDR" plugin pane open --plugin "${HERDR_PLUGIN_ID:-shellshare}" \
        --entrypoint "$entrypoint" --placement tab --focus \
        --env "SHELLSHARE_STATE_KEY=$key" --env "SHELLSHARE_SHARE_TOKEN=$token" \
        "$@" >/dev/null; then
        rm -rf "$lock"
        fatal "could not open the broadcast pane (see: herdr plugin log list --plugin shellshare)"
    fi
}

action_share_pane() {
    require_plugin_env
    sweep
    local target key
    target=$(resolve_target_pane) || fatal "$target"
    key="s$(session_scope)-pane-$(sanitize "$target")"
    bail_if_live "$key" "Pane $target"
    open_share_pane pane-broadcast "$key" --env "SHELLSHARE_TARGET_PANE=$target"
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
    open_share_pane session-broadcast "$key" --env "SHELLSHARE_SESSION_NAME=$name"
}

action_stop() {
    require_plugin_env
    sweep
    local stopped=0 f pid pane i
    for f in "$SHARES_DIR"/*.json; do
        [ -f "$f" ] || continue
        # Socket match, not key prefix: direct-opened shares (README's
        # agent recipe) carry caller-chosen keys but the right socket,
        # and their banner promises the stop action works on them.
        state_in_session "$f" || continue
        pid=$(jq -r '.pid // empty' "$f")
        pane=$(jq -r '.status_pane // empty' "$f")
        if state_live "$f"; then
            # TERM the entrypoint first: its trap stops the poller /
            # mirror so shellshare drains and flushes. Only clear state
            # once the process is really gone - a failed pane close must
            # never orphan a running broadcast.
            kill -TERM "$pid" 2>/dev/null || true
            i=0
            while [ "$i" -lt 100 ] && kill -0 "$pid" 2>/dev/null; do
                sleep 0.1
                i=$((i + 1))
            done
            if kill -0 "$pid" 2>/dev/null; then
                kill -KILL "$pid" 2>/dev/null || true
                sleep 0.2
            fi
        fi
        if [ -n "$pane" ]; then
            "$HERDR" plugin pane close "$pane" >/dev/null 2>&1 || true
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
    local live=0 f pane mode session_live=0
    # While a session share is live, everything focused is broadcast -
    # including other shares' status tabs, whose URLs carry their keys.
    # Focusing them would hand every session viewer those links.
    session_share_live && session_live=1
    for f in "$SHARES_DIR"/*.json; do
        [ -f "$f" ] || continue
        state_in_session "$f" || continue
        pane=$(jq -r '.status_pane // empty' "$f")
        mode=$(jq -r '.mode // empty' "$f")
        if [ -n "$pane" ] && { [ "$session_live" = "0" ] || [ "$mode" = "session" ]; }; then
            "$HERDR" plugin pane focus "$pane" >/dev/null 2>&1 || true
        fi
        live=$((live + 1))
    done
    if [ "$live" -gt 0 ]; then
        if [ "$session_live" = "1" ] && [ "$live" -gt 1 ]; then
            notify "Shellshare" "$live live share(s). Not focusing pane-share tabs: a session share is broadcasting, and their links would be shown to its viewers"
        else
            notify "Shellshare" "$live live share(s) - links are in the Shellshare tab(s)"
        fi
    else
        notify "Shellshare" "No live shares in this session"
    fi
}

# ---------------------------------------------------------------------
# Entrypoints (inside the status pane; long-running)

require_entrypoint_env() {
    # A pane respawned without its --env parameters (e.g. by a session
    # restore that does not preserve them) must fail fast and clearly,
    # not crash-loop or share the wrong thing.
    [ -n "$STATE_ROOT" ] && [ -n "${HERDR_BIN_PATH:-}" ] ||
        fatal_pane "not running under herdr (see herdr-plugin/README.md)"
    command -v jq >/dev/null 2>&1 ||
        fatal_pane "jq is required (https://jqlang.org); install it and retry"
    [ -n "${SHELLSHARE_STATE_KEY:-}" ] && [ -n "${SHELLSHARE_SHARE_TOKEN:-}" ] ||
        fatal_pane "missing share parameters - this pane must be opened by the share actions (or 'herdr plugin pane open ... --env', see herdr-plugin/README.md), not restarted directly"
    export SHELLSHARE_SHARE_TOKEN # liveness checks read it from our environ
    mkdir -p "$SHARES_DIR" "$LOCKS_DIR"
    # Broadcasts start only from a fresh action invocation (which holds
    # the lock) or an explicit opt-in. Without this, a session restore
    # that preserves the pane's env would silently resume broadcasting
    # after a reboot - a surprise share is a privacy problem, not a
    # convenience.
    [ -d "$(lock_dir "$SHELLSHARE_STATE_KEY")" ] || [ "${SHELLSHARE_DIRECT:-}" = "1" ] ||
        fatal_pane "refusing to auto-restart a broadcast (this pane was respawned, not opened by a share action). Re-run the share action, or pass --env SHELLSHARE_DIRECT=1 when opening the pane yourself"
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

banner() { # banner <what> <url> <plaintext?> <extra lines...>
    local what="$1" url="$2" plaintext="$3"
    shift 3
    printf '\n'
    if [ "$plaintext" = "1" ]; then
        printf '  PLAINTEXT - this broadcast is NOT end-to-end encrypted.\n\n'
    fi
    printf '  shellshare - %s\n\n' "$what"
    printf '    %s\n\n' "$url"
    while [ "$#" -gt 0 ]; do
        printf '  %s\n' "$1"
        shift
    done
    printf '\n  Stop: run the shellshare.stop action, or close this pane.\n'
    printf '  The link stays readable for a while after stopping (server room TTL).\n\n'
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
            rect=$(pane_rect "$target")
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

run_pane_share() {
    require_entrypoint_env
    local target="${SHELLSHARE_TARGET_PANE:-}"
    [ -n "$target" ] || fatal_pane "missing SHELLSHARE_TARGET_PANE"
    local key="$SHELLSHARE_STATE_KEY" ss
    ss=$(shellshare_bin) || fatal_pane "$ss"
    check_shellshare_supports_size "$ss" ||
        fatal_pane "this shellshare is too old for the herdr plugin (needs --cols/--rows, shellshare 3.12+). Upgrade: npx -y shellshare@latest, or download from https://shellshare.net"

    # Geometry: the pane's exact cell rect.
    local rect cols rows
    rect=$(pane_rect "$target")
    cols=${rect%% *}
    rows=${rect##* }
    case "${cols:-x}${rows:-x}" in *[!0-9]*) fatal_pane "cannot read the geometry of pane $target - is it still open?" ;; esac

    # Room: random by default (generated inside shellshare - never in
    # argv). room_prefix gives stable links, scoped per pane so
    # concurrent shares never collide on one room.
    local prefix room=""
    local ss_args=(--json --cols "$cols" --rows "$rows")
    prefix=$(cfg room_prefix "")
    if [ -n "$prefix" ]; then
        room="$prefix-pane-$(sanitize "$target")"
        local owner
        owner=$(room_in_use "$room") && fatal_pane "room '$room' is already used by $owner"
        ss_args=("${ss_args[@]}" --room "$room")
    fi
    local plain_flag=0
    if [ "$(cfg pane_plaintext "")" = "true" ]; then
        ss_args=("${ss_args[@]}" --disable-encryption)
        plain_flag=1
    fi
    add_base_args

    local run="$STATE_ROOT/run-$key"
    rm -rf "$run"
    mkdir -p "$run"
    mkfifo "$run/frames"

    # shellshare in stream mode: stdin = repaint frames, stdout = the
    # two JSON events only. Both ends open concurrently and rendezvous.
    "$ss" "${ss_args[@]}" <"$run/frames" >"$run/out" 2>"$run/err" &
    local ss_pid=$!
    poll_pane "$target" "$rect" "$(poll_interval)" "$run/reason" >"$run/frames" &
    local poller_pid=$!

    # Graceful stop (the stop action TERMs us): stop the poller so
    # shellshare sees stdin EOF and drains. A pane close signals the
    # whole process group instead; shellshare force-flushes on its own
    # there, which full-frame repaints make harmless.
    trap 'kill "$poller_pid" 2>/dev/null' TERM INT HUP

    wait_for_sharing "$run/out" "$ss_pid"
    # The out file's first line is the sharing event - the URL with its
    # key fragment. Parsed, it has no business surviving on disk.
    rm -f "$run/out"
    if [ -z "$SHARE_URL" ]; then
        # Kill shellshare too: a connect blocked past the timeout must
        # not linger and claim the room after we've reported failure.
        # And capture the diagnostics BEFORE deleting the run dir.
        kill "$poller_pid" "$ss_pid" 2>/dev/null
        local err_text
        err_text=$(cat "$run/err" 2>/dev/null || true)
        rm -rf "$run"
        fatal_pane "could not start the broadcast: ${err_text:-no error output}"
    fi

    write_state "$key" pane "$target" "$room" || {
        kill "$poller_pid" "$ss_pid" 2>/dev/null
        rm -rf "$run"
        fatal_pane "could not record share state under $SHARES_DIR - stopping the broadcast rather than running untracked"
    }
    banner "sharing pane $target (${cols}x${rows})" "$SHARE_URL" "$plain_flag" \
        "Viewers see a live snapshot of the pane (no scrollback, ~4 frames/s)." \
        "Resizing the pane or moving it to another workspace ends the share."
    notify "Shellshare$([ "$plain_flag" = 1 ] && printf ' (PLAINTEXT)')" "Sharing pane $target - link in the Shellshare tab"

    while kill -0 "$ss_pid" 2>/dev/null; do
        wait "$ss_pid" 2>/dev/null || true
    done
    kill "$poller_pid" 2>/dev/null || true
    rm -f "$(state_file "$key")"
    local reason=""
    [ -f "$run/reason" ] && reason=$(cat "$run/reason")
    rm -rf "$run"
    printf '\033[?25h\nShare ended%s. The link stays readable until the room idles out (~6h).\n' \
        "${reason:+ ($reason)}"
    notify "Shellshare" "Share of pane $target ended${reason:+ ($reason)}"
}

run_session_share() {
    require_entrypoint_env
    local name="${SHELLSHARE_SESSION_NAME:-}"
    [ -n "$name" ] || fatal_pane "missing SHELLSHARE_SESSION_NAME"
    local key="$SHELLSHARE_STATE_KEY" ss
    ss=$(shellshare_bin) || fatal_pane "$ss"
    check_shellshare_supports_size "$ss" ||
        fatal_pane "this shellshare is too old for the herdr plugin (needs --cols/--rows, shellshare 3.12+). Upgrade: npx -y shellshare@latest, or download from https://shellshare.net"

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

    local prefix room=""
    local ss_args=(--json --cols "$cols" --rows "$rows")
    prefix=$(cfg room_prefix "")
    if [ -n "$prefix" ]; then
        room="$prefix-session"
        local owner
        owner=$(room_in_use "$room") && fatal_pane "room '$room' is already used by $owner"
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
    *) fatal_pane "session_plaintext must be exactly 'yes-i-know' to broadcast the whole session unencrypted" ;;
    esac
    add_base_args

    local run="$STATE_ROOT/run-$key"
    rm -rf "$run"
    mkdir -p "$run"
    mkfifo "$run/pty"

    # The mirror: a second herdr client attached to this session inside
    # shellshare's PTY. HERDR_ENV gates nested herdr - unset exactly
    # that. </dev/null is load-bearing: with a TTY stdin, exec mode
    # raw-modes this pane and forwards every keystroke invisibly into
    # the fully-privileged mirror client (double Ctrl+C would even kill
    # the share); EOF disarms the forwarder while the mirror still gets
    # a real TTY from shellshare's PTY.
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
    # The out file's first line is the sharing event - the URL with its
    # key fragment. Parsed, it has no business surviving on disk.
    rm -f "$run/out"
    if [ -z "$SHARE_URL" ]; then
        kill "$ss_pid" "$drain_pid" 2>/dev/null
        local err_text
        err_text=$(cat "$run/err" 2>/dev/null || true)
        rm -rf "$run"
        fatal_pane "could not start the broadcast: ${err_text:-no error output}"
    fi

    write_state "$key" session "$name" "$room" || {
        kill "$ss_pid" "$drain_pid" 2>/dev/null
        rm -rf "$run"
        fatal_pane "could not record share state under $SHARES_DIR - stopping the broadcast rather than running untracked"
    }
    # Count other live shares in THIS session - their status tabs, if
    # focused, are visible to these viewers.
    local others=0 f
    for f in "$SHARES_DIR"/*.json; do
        [ -f "$f" ] || continue
        [ "$(basename "$f")" = "$key.json" ] && continue
        state_in_session "$f" || continue
        state_live "$f" && others=$((others + 1))
    done
    set -- "Viewers see the entire herdr UI at ${cols}x${rows} - every pane and tab." \
        "This tab is what they see right now - switch back to your work tab."
    if [ "$others" -gt 0 ]; then
        set -- "$@" "Careful: $others other share(s) are live - focusing their status tabs shows their links to these viewers too."
    fi
    banner "sharing session '$name'" "$SHARE_URL" "$plain_flag" "$@"
    notify "Shellshare$([ "$plain_flag" = 1 ] && printf ' (PLAINTEXT)')" "Sharing session $name - link in the Shellshare tab"

    while kill -0 "$ss_pid" 2>/dev/null; do
        wait "$ss_pid" 2>/dev/null || true
    done
    kill "$drain_pid" 2>/dev/null || true
    rm -f "$(state_file "$key")"
    rm -rf "$run"
    printf '\nShare ended. The link stays readable until the room idles out (~6h).\n'
    notify "Shellshare" "Session share ended"
}

# ---------------------------------------------------------------------

case "${1:-}" in
action-share-pane) action_share_pane ;;
action-share-session) action_share_session ;;
action-stop) action_stop ;;
action-status) action_status ;;
run-pane-share) run_pane_share ;;
run-session-share) run_session_share ;;
sweep)
    require_plugin_env
    sweep
    ;;
*)
    printf 'usage: share.sh {action-share-pane|action-share-session|action-stop|action-status|run-pane-share|run-session-share|sweep}\n' >&2
    exit 2
    ;;
esac
