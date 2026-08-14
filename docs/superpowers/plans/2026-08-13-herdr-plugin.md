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
- `status` (contexts `["workspace"]`) - re-focuses live status panes

Pane entrypoints (the long-running processes): `pane-broadcast` and
`session-broadcast`, placement `tab` (opening one never resizes the pane
being shared), opened by the actions with `--focus`: the user just asked
for a link; showing it is the expected response, and the entrypoint
prints the URL as its first visible output. Notifications are a
secondary channel and never carry the URL (herdr truncates titles at 80
and bodies at 240 chars - a truncated key fragment yields a
"wrong key" viewer page that looks like a plugin bug - and toast
delivery can route to the OS notification center, persisting the key in
notification history). Notification copy is "Sharing pane - link in the
shellshare tab", sent best-effort (the herdr CLI does not surface the
delivery reason); the focused status pane is the delivery guarantee.

A `[[startup]]` hook runs `share.sh sweep` (see State) - it is the
documented restore point and re-runs on live handoff, both idempotent.

### Single pane: snapshot mirror

`run-pane-share` (inside the plugin pane), all herdr calls via
`$HERDR_BIN_PATH` against the inherited `$HERDR_SOCKET_PATH`:

1. Target = `$SHELLSHARE_TARGET_PANE` (set by the action from the
   invocation context's `focused_pane_id`; also the documented override
   for agents/scripts, since `plugin action invoke` cannot pass a pane).
   Absent target is a hard, actionable error printed in the pane - never
   a guess. The action errors out before opening a pane when the context
   has no focused pane and no override was given.
2. Geometry from `pane layout` rect (verified: exact cell width/height).
3. Poll `pane read <target> --source visible --format ansi` at ~4 Hz
   (configurable); compare with the previous snapshot as a shell string
   (no hash subprocess); emit a full-frame repaint (cursor hidden,
   `ESC[H`, per-line erase, `ESC[0J`) only on change.
4. Pipe into `shellshare --json --cols C --rows R ...` through a fifo -
   the fifo IS shellshare's stdin (only the session-share mirror gets
   `</dev/null`; giving the pane pipeline /dev/null would starve the
   broadcast). Stream-mode stdout carries only the two JSON events.
5. Start protocol: read shellshare's first stdout line with a timeout
   while checking the child is alive. No line or early exit = fatal:
   kill both pipeline halves (a connect blocked past the timeout must
   not linger and claim the room after failure was reported), print
   shellshare's captured stderr in the pane, notify failure, write no
   state, exit non-zero. On success, print the URL in the pane, write
   state, notify (without the URL - and without relying on the
   notification's delivery reason, which the herdr CLI does not
   surface).
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
  proper TTY via shellshare's PTY. The status pane copy never says
  "press Ctrl+C" - stopping is the `stop` action or closing the pane.
- stdout: first line (`sharing` event - exec emits it before spawning
  the command) is consumed by the same start protocol as above; the
  rest (the mirror's PTY bytes) drains to /dev/null without ever
  closing stdout (a closed stdout stops shellshare's relay) and without
  unbounded logs.
- Mirror size: try to derive from the user's client viewport (via
  `herdr api snapshot`: the focused tab's layout extent - x+width by
  y+height - reproduces the client size exactly, verified live); fall
  back to config (default 120x36) and print the chosen size in the
  status pane.

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
  mode, target pane id, status-pane id, room name, broadcaster PID, and
  a per-share random token. The URL lives in the entrypoint's memory and
  its status pane text; `status` re-focuses that pane rather than
  re-printing URLs.
- **Locking:** the action takes an atomic `mkdir` lock per state key
  before `plugin pane open` (double-press/agent-double-invoke otherwise
  lands two broadcasts in the start window); the entrypoint converts the
  lock into the state record once the URL exists.
- **Liveness / self-heal:** liveness = recorded PID alive AND its
  environment carries the per-share token (Linux reads
  /proc/<pid>/environ; macOS has no /proc, so it falls back to the
  weaker `ps -o command=` match on share.sh's subcommand - stated
  honestly rather than pretending the token can ride in a fixed argv).
  `share.sh sweep` (startup hook + before every action) deletes records
  and locks that fail it - traps cannot run on SIGKILL/OOM/server stop,
  so cleanup must not depend on them. Entrypoints refuse to start
  without a live action lock or an explicit `SHELLSHARE_DIRECT=1`: a
  restore that respawned the pane with its env intact would otherwise
  silently resume broadcasting after a reboot, and one that dropped the
  env would crash-loop.
- **`stop` order:** SIGTERM the recorded broadcaster PID first, wait
  for exit, then best-effort `plugin pane close`, then clear state.
  For a pane share the trap stops the poller and shellshare drains on
  stdin EOF; for a session share the trap TERMs shellshare directly,
  which force-flushes and tears down the mirror with its PTY. Never
  clear state while the PID is still alive (a failed pane close - e.g.
  the user moved the status pane, changing its id - must not orphan a
  running broadcast).

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
  unencrypted share prefixes its status pane and notification title with
  "PLAINTEXT". (Security section, not just README.)

### Security considerations

- Encryption on by default; the server relays ciphertext. The URL (with
  key fragment) appears only in the owning status pane - not in
  notifications, not in state files, not in action stdout (plugin
  command logs persist stdout).
- A session share broadcasts everything visible in the UI - including,
  if focused, the status panes of OTHER live shares, whose URLs are
  displayed there. The session status pane warns when other shares are
  live, and the README states the rule plainly.
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
