# Herdr plugin: share a pane or the whole session via shellshare

Date: 2026-08-13
Status: Final - describes what shipped (revised after two adversarial
review rounds: 37 + 31 confirmed findings folded in)

## Goal

Let a herdr user share their terminal with one command: either a single
pane or their entire herdr session, as a live shellshare broadcast with a
web link. The plugin lives in this repo, so the two tools stay in
lockstep, and `herdr plugin install vitorbaptista/shellshare` works as
typed - the manifest sits at the repo root precisely so the marketplace
card's obvious install line succeeds (the v1 marketplace index does not
surface subdirectories; a subdir layout would make the natural install
command fail).

## Constraints discovered up front (verified against herdr 0.8.0 and shellshare 3.11.0)

- **No raw pane-output stream exists.** herdr's socket subscriptions
  (`pane.output_matched`, `pane.scroll_changed`, agent status) are
  match/snapshot-based; `herdr pane read` returns rendered snapshots.
  Single-pane sharing is therefore a snapshot mirror, not a byte tap.
- **Multi-client attach renders per-client** (verified empirically): a
  mirror client does not disturb the user's view, and each client keeps
  its own size. `herdr pane layout` exposes exact per-pane cell rects.
- **`herdr session attach` restarts a stopped session** rather than
  failing (verified). That is a hazard: attaching a wrongly-guessed name
  silently broadcasts the wrong (possibly resurrected, empty) session.
- **Nested herdr is refused via `HERDR_ENV`** and unsetting exactly that
  variable bypasses the check (verified). `HERDR_SESSION` is NOT injected
  into panes or plugin commands - session identity must come from
  `HERDR_SOCKET_PATH`, the one injected variable that names the session.
- **shellshare runs headless** (verified): exec with non-TTY stdin works,
  bare shellshare with piped stdin enters stream mode; headless size is
  the 80x24 fallback with no override flag (added below). In exec mode a
  TTY stdin is put in raw mode and forwarded into the PTY - so a mirror
  running in a visible pane must take `</dev/null` or focused-pane
  keystrokes get injected invisibly into the mirrored session.
- **Signals:** shellshare treats SIGINT as "drain until stdin EOF" but
  SIGTERM/SIGHUP as force-exit (still flushing via transport.shutdown();
  at most the in-flight chunk is lost). Closing a herdr pane signals the
  whole process group, so the trap-based graceful path only runs when a
  signal targets share.sh alone; pane close takes shellshare's own
  forced-flush path, which is acceptable because every pane-share frame
  is a full repaint and exec mode keyframes TUIs (~every 60 frames).
- **Plugin actions are short-lived and logged; panes are the persistence
  primitive.** Long-running broadcasts run inside plugin panes
  (server-owned, survive detach, visible and closeable). What signal
  herdr delivers on pane close is undocumented - the design below never
  depends on it (stop goes through PIDs, cleanup self-heals).
- **Rooms outlive the broadcaster** (server TTL, default 6h). Reusing a
  room name resets history and reproduces the same key (HKDF over
  machine id + room name).

## Design

### Repo layout

```
herdr-plugin.toml     # manifest at repo root: install owner/repo works
herdr-plugin/
  share.sh            # single control script (thin manifest, fat script)
  README.md           # install, first run, keybindings, config, limits
```

Manifest commands route as `["bash", "herdr-plugin/share.sh", "<cmd>"]`
(plugin cwd = plugin root = the cloned repo). `platforms = ["linux",
"macos"]` - the script is bash; shellshare itself supports Windows, the
plugin does not yet (stated honestly in the README).
`min_herdr_version = "0.8.0"` (needs `plugin pane open --env`).

### Manifest surface

Local action ids are dot-free (herdr requires this; dots appear only in
the qualified form `shellshare.<id>` used by keybindings and
`herdr plugin action invoke`):

- `share-pane` (contexts `["pane"]`)
- `share-session` (contexts `["workspace"]`)
- `stop` (contexts `["workspace"]`) - stops this session's live shares
- `status` (contexts `["workspace"]`) - re-shows the live links

Actions stay one-shot, as herdr documents them: they validate, take the
start lock, spawn a **detached daemon**, and return in ~0.1s. The
broadcast deliberately does NOT live in a plugin pane. A pane would put
a permanent tab in the user's tab bar for a process that is not a
terminal anyone wants to read - chrome the rest of herdr does not ask
for - and the community precedent (collie's systemd unit, mirror's
daemon) is to keep long-lived processes outside herdr's one-shot hooks.
`setsid` where available, `nohup` on macOS which lacks it.

Three native surfaces replace that tab:

1. **Sidebar badge.** While a share is live the daemon reports herdr
   display metadata (`pane`/`workspace report-metadata --source
   plugin:shellshare --token shellshare=...`), which herdr renders
   wherever the user's `[ui.sidebar.*]` rows mention `$shellshare` -
   the documented extension point for exactly this. The token carries a
   TTL the daemon refreshes every 30s, so a SIGKILLed broadcaster's
   badge expires by itself; nothing has to sweep it.
2. **Link overlay.** The plugin's only pane entrypoint is `link`,
   placement `overlay` - herdr's transient surface, which restores the
   previous focus and zoom when it closes. The daemon opens it once the
   link exists and `status` re-opens it on demand; it closes on any
   key. The daemon holds the URL in memory and serves it one line per
   reader through a fifo (no data at rest), so the overlay can show it
   without it ever touching disk.
3. **Notifications**, best-effort and never carrying the URL (herdr
   truncates titles at 80 and bodies at 240 chars - a truncated key
   fragment yields a "wrong key" viewer page that looks like a plugin
   bug - and toast delivery can route to the OS notification center).

A `[[startup]]` hook runs `share.sh sweep` (see State) - it is the
documented restore point and re-runs on live handoff, both idempotent.

### Single pane: snapshot mirror

`run-pane-share` (inside the plugin pane), all herdr calls via
`$HERDR_BIN_PATH` against the inherited `$HERDR_SOCKET_PATH`:

1. Target = `$SHELLSHARE_TARGET_PANE` (set by the action from the
   invocation context's `focused_pane_id`; also the documented override
   for agents/scripts, since `plugin action invoke` cannot pass a pane).
   Absent target is a hard, actionable error from the action - never a
   guess.
2. Geometry from `pane layout` rect (verified: exact cell width/height).
   Re-checked every ~2s so a resized pane ends the share instead of
   streaming mis-shaped frames forever - but skipped while the tab is
   zoomed, because a zoom (including the temporary one the plugin's own
   link overlay puts on top) resizes every rect without the shared pane
   changing. Verified live: without that guard, opening the overlay
   killed the share it had just started.
3. Poll `pane read <target> --source visible --format ansi` at ~4 Hz
   (configurable); compare with the previous snapshot as a shell string
   (no hash subprocess); emit a full-frame repaint (cursor hidden,
   `ESC[H`, per-line erase, `ESC[0J`, no newline after the last row -
   one at the bottom margin would scroll the viewer) only on change.
4. Pipe into `shellshare --json --cols C --rows R ...` through a fifo -
   the fifo IS shellshare's stdin (only the session-share mirror gets
   `</dev/null`; giving the pane pipeline /dev/null would starve the
   broadcast). Stream-mode stdout carries only the two JSON events.
5. Start protocol: read shellshare's first stdout line with a timeout
   while checking the child is alive, then unlink that capture (its
   first line is the URL). No line or early exit = fatal: kill both
   pipeline halves (a connect blocked past the timeout must not linger
   and claim the room after failure was reported), write no state, and
   report - a detached daemon has no pane to print into, so the reason
   goes to a notification AND to `last-error.txt` in the state dir
   (never a URL, only what went wrong). On success: write state, start
   the link fifo server and the badge refresher, notify (without the
   URL), and open the link overlay.
6. Lifetime: `wait` on shellshare; ANY exit - including 0, because
   stream mode exits 0 even on mid-run authorization loss - ends the
   share: clean state, notify "share ended". Poller write failures
   (SIGPIPE when shellshare dies) funnel into the same path. When pane
   reads start failing (pane closed OR moved across workspaces - herdr
   pane ids change on cross-workspace moves), end the share with an
   explicit "pane closed or moved" notification, never silently; the
   README lists the move limitation.

### Whole session: mirror client in a shellshare PTY

`run-session-share` runs:

```
shellshare exec --json --cols C --rows R <flags> -- \
  env -u HERDR_ENV "$HERDR_BIN_PATH" session attach <name>  </dev/null
```

- `<name>` is resolved by the ACTION, not guessed in the entrypoint:
  match `$HERDR_SOCKET_PATH` against `herdr session list` rows (the
  per-session socket path is listed there; the default session's socket
  is `.../herdr.sock`, named sessions live under `sessions/<name>/`).
  Unresolvable = loud fatal error. There is NO fallback to bare `herdr`,
  which would attach - and possibly resurrect - the default session and
  silently broadcast the wrong content. The resolved name rides into the
  entrypoint via `--env`.
- Only `HERDR_ENV` is unset (the one variable empirically shown to gate
  nesting); `HERDR_SOCKET_PATH` stays - it is the session identity.
- `</dev/null`: exec mode with a TTY stdin raw-modes the pane and
  forwards every keystroke into the fully-privileged mirror client
  (navigation, input into real panes, silent share-kill via double
  Ctrl+C). EOF stdin disarms the forwarder; the mirror still gets a
  proper TTY via shellshare's PTY. Stopping is the `stop` action; the
  overlay says so and never invites a Ctrl+C.
- stdout: first line (`sharing` event - exec emits it before spawning
  the command) is consumed by the same start protocol as above; the
  rest (the mirror's PTY bytes) drains to /dev/null without ever
  closing stdout (a closed stdout stops shellshare's relay) and without
  unbounded logs.
- Mirror size: try to derive from the user's client viewport (via
  `herdr api snapshot`: the focused tab's layout extent - x+width by
  y+height - reproduces the client size exactly, verified live); fall
  back to config (default 120x36).

### shellshare binary resolution

`herdr plugin install` clones sources; it never produces a shellshare
binary, and `[[build]]` would need a Rust toolchain (and is skipped by
`plugin link` anyway) - so the plugin resolves the binary at runtime:
`$SHELLSHARE_BIN` env, then the `shellshare_bin` config key, then PATH,
each failure a loud, actionable error pointing at the install
one-liners. Because the entrypoints pass `--cols/--rows`
unconditionally and every release up to 3.11.0 rejects those flags with
a raw clap error, a preflight checks the resolved binary supports them
(`--help` mentions `--cols`) and otherwise fails with an upgrade
message naming 3.12+. "Lockstep" between the two tools means co-located
source plus this runtime check - the cloned checkout does not control
which binary is on PATH.

### Rust change: `--cols` / `--rows` overrides

Two optional global u16 flags, each independently overriding the
corresponding detected dimension at every `get_terminal_size()` use
(initial size, stream-mode periodic re-read, exec PTY size + SIGWINCH
updates - an overridden dimension stays fixed; fixed viewer geometry is
the point). Headless embedding currently gets 80x24 with no recourse;
this plugin is the second consumer after headless agent runs.
Documented in AGENTS.md's "Behavior worth knowing" and mirrored in
templates/llms.txt if size comes up there; the --json event contract is
unchanged.

### State, locking, stop

All state under `$HERDR_PLUGIN_STATE_DIR` with `umask 077` (dirs 700,
files 600). State is scoped by session: key =
`<session-hash>-<mode>[-<pane>]` where session-hash derives from
`HERDR_SOCKET_PATH` (plugin state is global per user; pane ids like
`w1:p1` repeat across sessions - unscoped state would let session B's
`stop` clobber session A's share).

- **No URLs on disk.** shellshare's documented posture is "nothing is
  written to disk" (crypto.rs); the plugin honors it. State records
  mode, target, room name, broadcaster PID, and a per-share random
  token. The URL lives only in the daemon's memory, served through a
  fifo (which holds nothing at rest) to the overlay that displays it.
- **Locking:** the action takes an atomic `mkdir` lock per state key
  before spawning the daemon (double-press/agent-double-invoke otherwise
  lands two broadcasts in the start window); the daemon converts the
  lock into the state record once the URL exists.
- **Liveness / self-heal:** liveness = recorded PID alive AND its
  environment carries the per-share token (Linux reads
  /proc/<pid>/environ; macOS has no /proc, so it falls back to the
  weaker `ps -o command=` match on share.sh's subcommand - stated
  honestly rather than pretending the token can ride in a fixed argv).
  `share.sh sweep` (startup hook + before every action) deletes records,
  locks and orphaned run dirs that fail it - traps cannot run on
  SIGKILL/OOM/server stop, so cleanup must not depend on them. Badges
  need no sweeping: their TTL expires them. Nothing auto-resumes a
  broadcast either - daemons are started by actions, never respawned by
  herdr, so a restart cannot silently put a share back on the air.
- **`stop` order:** SIGTERM the recorded broadcaster PID first, wait
  for exit, then clear state. For a pane share the trap stops the
  poller and shellshare drains on stdin EOF; for a session share the
  trap TERMs shellshare directly, which force-flushes and tears down
  the mirror with its PTY. The daemon clears its own badge on the way
  out; if it had to be SIGKILLed, `stop` clears the badge instead, and
  the token's TTL is the backstop for cases nobody is around to handle.
  Never clear state while the PID is still alive.

### Config

`$HERDR_PLUGIN_CONFIG_DIR/config`, KEY=VALUE, read with a whitelist
parser (`while IFS='=' read` over known keys, values taken literally) -
NOT sourced: a sourced file executes `$(...)` from pasted snippets; a
parsed file is inert data, matching how herdr's own config.toml treats
values. Keys: server URL, theme, room prefix, poll interval, session
mirror size, and per-mode plaintext switches.

- **Rooms:** default is shellshare's random room (never in argv - safe).
  Optional `room_prefix` gives stable links, scoped per share
  (`<prefix>-session`, `<prefix>-pane-<n>`) so two shares never
  interleave one room; before starting, a share whose resolved room
  matches a live share's room refuses with a clear message. README owns
  the fixed-room trade-offs honestly: the name rides in argv (other
  local users on a multi-user machine can read it and derive the key
  from the world-readable machine id - use random rooms there), a leaked
  stable link stays valid for all future broadcasts, and after ~6h idle
  the name can be claimed by someone else, so a long-lived published
  link can later render an impostor's content.
- **Plaintext:** `--disable-encryption` is deliberately per-invocation
  and loudly warned in shellshare; the plugin must not turn it into a
  silent sticky global. `pane_plaintext=true` covers the classroom-LAN
  case; session shares require `session_plaintext=yes-i-know`, and any
  unencrypted share marks its overlay and its notification title with
  "PLAINTEXT". (Security section, not just README.)

### Security considerations

- Encryption on by default; the server relays ciphertext. The URL (with
  key fragment) appears only in the link overlay - not in
  notifications, not in state files, not in action stdout (plugin
  command logs persist stdout), not in `last-error.txt`.
- A session share broadcasts everything visible in the UI - the link
  overlay included, since it is on screen. So while a session share is
  live the overlay renders only that share's own link and says how many
  pane-share links it withheld; the README states the rule plainly.
- Session mirror stdin is closed (keystroke-injection channel removed);
  the mirror client is view-only in practice because no input reaches
  it.
- State files carry room names and PIDs, never keys or URLs; 600/700
  modes enforced and asserted in e2e.
- Fixed rooms: see Config - argv exposure on multi-user machines,
  leaked-link permanence, post-TTL room claiming, all documented next to
  the feature that creates them.

### Tests (e2e; plugin tests skipped on Windows - bash)

1. `--cols/--rows`: headless exec broadcast; viewer WebSocket asserts
   the `size` control message carries the overrides. One test at the
   layer that owns the contract.
2. Pane share: stub `herdr` (the cloudflared-stub pattern) served via
   `HERDR_BIN_PATH`, canned `pane layout`/`pane read` output that
   changes once; run `share.sh run-pane-share` against a dedicated local
   server with encryption ON; the test reads the URL from the
   entrypoint's captured stdout, feeds `parse_share_key` to
   SocketListener, asserts content arrives; then (a) SIGTERM to share.sh
   alone asserts the graceful drain path, and a separate case delivers
   the signal to the whole process group (what a real pane close does)
   and asserts the room still holds a complete last frame.
3. Session share: stub `herdr` whose `session list` matches the fake
   socket and whose `session attach` prints a marker and asserts stdin
   is EOF and `HERDR_ENV` is unset; assert the marker reaches a viewer
   and the wrong-session guard trips when the socket matches nothing.
4. Manifest lockstep: parse herdr-plugin.toml (tomllib, with `tomli`
   added to e2e deps for Python 3.10), assert action/pane ids are
   dot-free, every command routes to a share.sh subcommand that exists,
   platforms/min-version sanity.
5. Failure path: unreachable server - entrypoint exits non-zero, no
   state file, error text present.

### Docs

- `herdr-plugin/README.md`, ordered for the first ten seconds: install
  one-liner → "share your session right now" via
  `herdr plugin action invoke shellshare.share-session` → keybinding
  block with the `herdr server reload-config` step →
  `herdr plugin action list --plugin shellshare` → config reference →
  limitations (viewport mirror: no scrollback/cursor, fixed geometry
  per share, cross-workspace pane moves end the share) → security notes.
  Repo voice: command-first, honest, no hype.
- Root README: short subsection pointing at the plugin.
- CLAUDE.md: herdr-plugin section (architecture + the
  manifest↔share.sh↔e2e lockstep contract).

## Implementation gates (all verified live against herdr 0.8.0)

- `herdr session attach default` works (restarts a stopped default
  session and attaches; verified, then stopped again).
- `herdr session list --json` rows carry `socket_path` (the session
  resolution mechanism) and `herdr api snapshot` layouts expose the
  client render extent (the mirror-size mechanism).
- Closing a plugin pane delivers a catchable signal: the wrapper's traps
  ran and state was cleaned in the live test. Sweep still covers the
  uncatchable cases.
- The whole pipeline ran for real: plugin linked, actions invoked over
  the socket, broadcasts verified through a local server and viewer
  WebSocket, stop/status/pane-close all exercised.

## Ship checklist (beyond the merge)

- The shellshare release carrying `--cols/--rows` (3.12+) must ship
  before or with the plugin announcement - older binaries fail the
  plugin's preflight with an upgrade message.
- Add the `herdr-plugin` GitHub topic to vitorbaptista/shellshare (the
  marketplace index's only signal) and mention the herdr plugin in the
  repo description (the listing card shows it).

## Out of scope (v1)

- Windows plugin scripts.
- Live geometry tracking on pane resize (restart the share instead).
- Viewer input (shellshare is broadcast-only - a feature here).
- QR rendering / link handlers - later niceties.
