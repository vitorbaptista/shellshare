# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Shellshare is a live terminal broadcasting tool that allows users to share their terminal session via web links. The project is a complete Rust rewrite providing both client and server in a single binary.

## Build & Development Commands

```bash
# Build
make build                     # Same as above

# Lint (pedantic clippy lints are enabled)
make lint                      # Both clippy + check

# Run locally
cargo run -- server            # Start server on 0.0.0.0:3000
cargo run -- server --port 8080 --host 127.0.0.1
cargo run -- --server http://localhost:3000  # Run client
cargo run -- serve             # Share via a local server on localhost:3000

# E2E tests (Python + Playwright)
# Requires a release binary: the suite starts its own servers from it
cargo build --release
cd e2e && uv sync && uv run pytest -n 10
```

## Architecture

**Dual-mode binary**: `shellshare` operates as client (default) or server (`shellshare server`). `shellshare serve` combines both: it boots the embedded server on a background thread (default `localhost:3000`, configurable via `--host`/`--port`) and runs the client against it, sharing the terminal with no external server. Both `serve` and `server` accept `--tunnel` (`src/tunnel.rs`): it spawns the user's pre-installed `cloudflared` against the local server, waits for the `https://*.trycloudflare.com` URL from its stderr banner, and uses it as the share link (the broadcaster still talks to localhost); missing cloudflared is a fatal error pointing at the install docs, and the tunnel process dies with shellshare.

**Scripting surface**: `shellshare exec -- <cmd>` runs one command in the PTY (instead of a shell), broadcasts it, and exits with the command's exit code. The global `--json` flag switches stdout to newline-delimited JSON events: first one with event `sharing` (parse its `url` field), last `{"event":"end","exit_code":N}` (errors stay on stderr as `ERROR: ...`). This contract is documented in `AGENTS.md` and `templates/llms.txt` and covered by `e2e/test_agents.py` - the three must stay in lockstep.

### Client (`src/cli/`)
Multi-threaded design ensures network latency never blocks terminal display:
- **PTY reader thread**: Captures shell output, displays locally, sends to the sender thread
- **Sender thread**: Owns the WebSocket transport; coalesces whatever is queued and sends immediately (no pacing - frames are cheap)
- **Stdin forwarder thread**: Routes user input to PTY
- **Signal handler** (Unix): Handles SIGWINCH for terminal resize

Key files:
- `mod.rs`: Entry point, room ID generation, server URL handling, terminal size. Connecting the transport claims the room, so auth failures surface before the shell spawns. In stream mode Ctrl+C starts a drain rather than an exit: SIGINT reaches the whole pipeline, so shellshare keeps relaying until the producer closes stdin - leaving first would hand the producer EPIPE and lose its shutdown output. A second Ctrl+C leaves immediately. SIGTERM/SIGHUP still exit at once: SIGTERM arrives alone, so there is nobody to wait for and waiting would only lose the flush to the supervisor's follow-up SIGKILL, while SIGHUP means the terminal is gone - nobody is watching the link, and the second press that escapes a drain could never be typed
- `script.rs`: PTY lifecycle, raw terminal mode, shell spawning
- `ws.rs`: WebSocket transport. Binary frames carry raw terminal bytes; JSON text frames carry control messages (`size`, `reset`). Reliability: output stays in a bounded replay buffer until the server acks it (`{"ack": n}`, cumulative per-connection bytes); on failure the client reconnects with backoff and replays everything unacked (at-least-once delivery). Only authorization errors are fatal. A session ends by flushing and closing, not by deleting: the room and its history outlive the process until the server's TTL evicts it, so a short command (`dmesg | shellshare`) still leaves a working link. The first connection - never a reconnect, where it would destroy exactly what replay is rebuilding - sends `reset` so a reused room name starts clean
- `crypto.rs`: end-to-end encryption, on by default (opt out with `--disable-encryption`). Every output chunk is sealed into a self-delimiting AES-256-GCM record (`[u32 BE len][nonce][ciphertext+tag]`) as it enters the replay buffer, so acks/replay operate on ciphertext and the server stays an opaque relay (zero server changes - it never knew about encryption). The key is HKDF-derived from this machine's id and the room name (so a named room keeps one reusable share link across restarts; nothing is written to disk) and rides only in the link's `#fragment`. The `size` message carries `encrypted: true` so the viewer knows whether to decrypt (via WebCrypto, needs https or localhost) or render plaintext, and shows an explanatory notice when an encrypted link's fragment is missing/invalid/wrong or the context is insecure. `--disable-encryption` broadcasts plaintext for viewers on plain HTTP (a classroom LAN), where browsers have no WebCrypto. Record format must stay in lockstep with `templates/room.html`. Threat model (honest-but-curious server serving the unmodified page; key secrecy rests on the high-entropy machine id; metadata/timing still visible) is documented in `crypto.rs`

### Server (`src/server/`)
Async Tokio + Axum web server. Viewers connect over a raw WebSocket (`/ws/v/r/:room`) that mirrors the ingest protocol: binary frames are terminal bytes, JSON text frames are control events (`size`, `usersCount`, `broadcasting`). The room is the URL, so there is no join handshake; the server pushes the room snapshot (size, history, broadcasting, usersCount - in that order, usersCount always last) on every connect, making reconnects a clean resync. A viewer that falls hopelessly behind its bounded send queue is disconnected on purpose (the page reconnects and resyncs) instead of silently losing frames. Socket.IO was removed: its per-message double frame (announce + attachment) doubled fan-out work, and its engine.io layer had an unfixable header/attachment interleave race.
- `mod.rs`: Router and WebSocket handlers - thin translators that delegate to the modules below
- `viewers.rs`: Complete raw-WebSocket viewer delivery: registry, sharded fan-out and coalescing, user-count convergence, snapshot replay, socket liveness, and disconnect-on-overflow
- `rooms.rs`: All room lifecycle behind one interface - first-caller-wins password claiming, message history (max 200), canonical room names (`RoomId`), activity tracking and TTL eviction. Rooms outlive their broadcaster, so TTL eviction is the only teardown - and only the broadcaster postpones it: `append` (broadcast frames and the client's 30s keepalive pings) refreshes activity, while reads (`snapshot`, serving both viewer joins and `/r/:room.bin`) deliberately do not, or a forgotten browser tab or a polling agent would pin a finished broadcast forever. One entry, one lock (a sharded `DashMap`): authorization and mutation are a single critical section on their room, so concurrent broadcasters never serialize on a shared lock
- `pages.rs`: Home page (install options: npx by default, plus per-OS binary downloads), viewer page, embedded static assets. The viewer page is a JavaScript app, so an agent handed a share link would fetch it and learn nothing. `templates/agent.mjs` is the one reader an agent runs against a link (snapshot, or `--follow`, which ends when the broadcaster leaves - two modes and no other flags, because `timeout`/`grep` compose better than options would). It is inlined via `{{AGENT_DECODER}}` into both `/llms.txt` (a template for this reason, hence rendered rather than static) and every room page, so neither costs a second request and the two cannot drift - paid for by ~3KB gzipped on every room page, and by a screen reader meeting the brief before the terminal. Deliberately not in `public/`, which would serve it as a third copy at its own URL - whoever is reading already has it. Where the page's copy sits is measured, not guessed: inside `<main>` (extractors like pandoc discard everything outside it), prose rendered with only the bulky reader inside a `<details>` (a collapsed one is absent from a browser's `innerText`, so an agent reading rendered text still gets the prose), and never `aria-hidden`/`hidden` (Readability drops those subtrees). A lost placeholder or gutted reader panics `warm()`, which `bind()` runs before taking a port so `serve` cannot report itself ready and then die. Rooms stay out of search indexes via `X-Robots-Tag: noindex` on the room page and on `/r/:room.bin` (not HTML, so it cannot carry the meta tag) rather than a `robots.txt` `Disallow`: `noindex` only takes effect on a page a crawler may fetch, so disallowing it blocks the one directive that says what we mean and leaves a leaked URL listable by URL alone - and `Disallow` being per-agent would mean naming every assistant that opens a link a human pasted, forever
- `binaries.rs`: Platform detection and binary downloads at `/bin/shellshare`

Routes:
- `GET /` - Home page with install instructions (npx selected by default)
- `GET /r/:room` - Viewer page
- `GET /ws/r/:room` - WebSocket ingest (the only broadcast transport): claimed/verified at the handshake, binary frames are terminal bytes, text frames are control messages (`size`; `reset` clears a reused room's history at the start of a new session, honored only for the room's sole broadcaster; `delete` is the retired exit path older clients still send), every stored frame is acked. Each open connection counts as an attached broadcaster: viewers get a `broadcasting` control event (current state on connect, plus every transition) driving the online/offline indicator in the viewer page; a connection silent past 90s (clients ping every 30s) is treated as dead
- `GET /ws/v/r/:room` - WebSocket viewer endpoint (see above): connect snapshot, then live binary frames and JSON control events; server pings keep the user count free of ghosts
- `POST /r/:room` - Retired: always 410 Gone with an upgrade message, so pre-WebSocket clients fail loudly instead of silently
- `DELETE /r/:room` - Cleanup room

### Wire Protocol (`src/protocol.rs`)
Terminal output is **raw bytes** end to end: binary WebSocket frames from
the CLI, raw bytes in room history, binary WebSocket frames to
viewers, written as bytes into xterm.js, which does its own streaming
UTF-8 decode (the viewer script is inline in `templates/room.html`;
xterm.js and its WebGL/Unicode11 addons are vendored under
`public/javascript/vendor/`). History accumulation for late joiners
lives here too. Must stay in lockstep with `templates/room.html` and
`e2e/conftest.py`.

One viewer-side rewrite happens on the way into xterm.js
(`writeBytes` in `templates/room.html`): colon-form colour parameters
(`38:2:R:G:B`, what herdr and other modern TUIs emit) are converted to
the classic `38;2;R;G;B`. The vendored xterm.js reads the colon form as
`38:2:<colour-space>:R:G:B` and takes R,G,B from the 4th/5th/6th slots,
so the short spelling shifts every channel one place - Catppuccin blue
`#89b4fa` renders as `rgb(180,250,0)`, and a whole mirrored UI comes out
yellow-green. Only 38/48/58 params are touched (`4:3` is a curly
underline, not underline+italic), and an unterminated escape at the end
of a frame is carried to the next one rather than half-rewritten.
Covered by `e2e/test_viewer_sgr.py`.

### Herdr plugin (`herdr-plugin.toml` + `herdr-plugin/`)

The repo doubles as a [herdr](https://herdr.dev) plugin: one action that
broadcasts the whole herdr session, read-only, as a shellshare link. The
manifest sits at the repo ROOT (not in `herdr-plugin/`) so
`herdr plugin install vitorbaptista/shellshare` works as typed - the
marketplace card does not surface subdirectories.

**The share is a pane, not a daemon.** The manifest declares one action
(`share`, a toggle) and one pane (`live`), and the pane runs
`shellshare exec -- env -u HERDR_ENV herdr session attach <name>`: the
process's lifetime IS the share's lifetime. herdr already owns pane
lifetime, so there is nothing to supervise, garbage collect or sweep. An
earlier design used a detached daemon plus a sidebar badge, a link fifo
and an overlay; it was ~800 lines more and bought nothing herdr was not
already doing.

That pane lives in a **space of its own**, created by the action
(`workspace create --label "◉ shellshare"`, then the pane opens there as
a tab and the space's own shell tab is closed). A session-wide share
parked inside one project's space is misfiled, and herdr closes a space
when its last tab goes - so the space is there while the broadcast is
and gone when it ends, which makes it the status indicator: a labelled
row in the spaces sidebar, visible from wherever the user is working,
needing no `[ui.sidebar.*]` configuration. (A share that FAILED is the
exception: its space is kept, relabelled `✗ shellshare (stopped)`, to
hold the error.) Ctrl+C, closing the space, and toggling the action are
all the same stop - and any of them that leaves the space standing,
because the user put a tab of their own in it, relabels it on the way
out so the row stops claiming a broadcast that has ended. The pane also
renames its own tab (`tab rename $HERDR_TAB_ID`), since a manifest pane
`title` does not become the tab label.

**herdr holds the state; the plugin holds none.** "Am I sharing?" is
answered by asking `api snapshot` for panes carrying the plugin's
metadata token, which the live pane puts on *itself*
(`pane report-metadata`) before it starts broadcasting. Never a pid
file: a file outlives crashes and reboots, and herdr ids are small
per-server counters that get reused, so acting on a stale one means
closing somebody else's tab. Marking the pane rather than the space is
what makes the answer unfakeable *and* self-cleaning - a label is free
text the user can type or rename into, a mark on the space would outlive
the pane that made it, but a dead pane is simply absent from the
snapshot. A pane that cannot mark itself refuses to broadcast, since the
mark is the only handle on it afterwards.

**Stopping closes that pane's tab, never a workspace.** Closing a space
takes every tab in it, so a share parked in a space the user has also
put a tab in could destroy their work; closing the share's own tab ends
the broadcast and lets herdr drop the space when its last tab goes. The
ordinary case is identical and the awkward one costs nothing, so no code
here has to work out whose tabs are in the way - the one exception is
`abort_start`, which closes a space created milliseconds earlier by the
lines above it, and reports loudly if that close fails (by then the pane
is open, so the session is already being broadcast). A stop that cannot
close every share is a hard error, because "stopped sharing" while a
link is still fed bytes is the one lie the action must never tell.
Likewise there is no fallback that opens the share in the caller's
space, the space's own shell tab is a hard requirement to close (a tab
left in it outlives the broadcast and keeps the indicator up), and a
pane that exits leaves the label corrected behind it. Nothing is guessed
anywhere else either: an unresolvable session name or an unreadable
client size is a hard error, since a wrong guess silently broadcasts the
wrong session or shrinks the real one.

**There is deliberately no pane-sharing mode.** To share one pane you
run `shellshare` in it - a full-fidelity byte stream, better than any
snapshot mirror a plugin could build. The plugin only does what you
cannot type, and all four reasons are verified against herdr 0.8.0:
`herdr session attach` from inside a pane is refused (HERDR_ENV gates
nesting); `HERDR_SESSION` is not exported, so a hand-typed attach guesses
the name and a wrong guess silently starts and broadcasts the DEFAULT
session; without `--cols/--rows` (derived from the focused tab's
`api snapshot` layout extent) the mirror attaches at 80x24 and herdr's
smallest-client-wins sizing shrinks the real session; and `shellshare
exec` echoes the mirror's PTY bytes on stdout, so run by hand the mirror
renders the pane it lives in - infinite regress. share.sh reads the
`sharing` line off a fifo in the main shell (not through a pipe, where
`exit` would only leave a subshell) and hands the rest to a background
`cat >/dev/null`. Lockstep contract: `herdr-plugin.toml` action/pane ids
and commands ↔ `share.sh` dispatch arms ↔ `e2e/test_herdr_plugin.py`. The banner does not print the link
itself: it runs `SHELLSHARE_URL=<url> shellshare status`, which is
shellshare's own link-presentation path and therefore brings the QR code
with it on a terminal - no second QR renderer, and no new CLI surface.
Do not pipe that call (to indent it, say): `status` gates the QR on
`stdout.is_terminal()`. Test split: the plugin test asserts the
delegation happened, `e2e/test_cli_tty.py` owns the QR-on-a-TTY
behavior. The plugin's e2e test stubs `herdr` but runs a real encrypted
broadcast against a real local server.

## Cross-Platform Notes

- **Unix**: PTY via portable-pty, raw terminal mode via libc tcgetattr/tcsetattr, SIGWINCH handling
- **Windows**: ConPTY, shell from $COMSPEC, no raw mode needed

## Testing

E2E tests are the single source of truth - there are no Rust unit tests by design. The implementation is free to change as long as the e2e suite stays green.

The suite in `e2e/` uses Python pytest + Playwright and manages its own servers: a shared one started automatically on a free port (pin it with `SHELLSHARE_E2E_PORT` to reuse a pre-started server) plus per-test dedicated servers with custom flags (e.g. short `--room-ttl` for eviction tests). Coverage spans the HTTP API, the viewer WebSocket protocol, full CLI-to-browser integration, real-TTY CLI sessions (`test_cli_tty.py`: resize/SIGWINCH, Ctrl+C handling, room survival on exit), room TTL eviction, and binary downloads.

### What to test (and what not to)

The suite is the whole safety net, but every test also costs CI minutes and maintenance. Keep it lean: one strong test per behavior beats five overlapping ones. Before adding a test, ask whether it guards a real Shellshare failure mode that nothing else already covers.

**Worth a test:**
- **Happy paths** for every public surface: a CLI session broadcasts and a viewer sees it; the home/viewer pages render; binaries download.
- **Lockstep contracts** that span files and break silently if drifted - the wire protocol (`protocol.rs` ↔ `templates/room.html` ↔ `conftest.py`), the encryption record format, the `size` control message (theme, `encrypted` flag, cols/rows omission), and the `--json` event contract (`sharing`/`end`, mirrored in `AGENTS.md` + `templates/llms.txt`). Test each contract once, at the layer that owns it.
- **Auth and isolation**: password claiming/rejection, per-room message isolation, room TTL eviction.
- **Reliability paths**: reconnect/replay, late-joiner history, user-count convergence, ordering.
- **Platform-specific code** only reachable one way - e.g. real-TTY signal handling in `test_cli_tty.py` (a pipe can't deliver SIGWINCH/SIGINT).
- **The same behavior at genuinely different layers** when each layer can fail independently (e.g. UTF-8 survival across raw history, viewer WS, frame-split, and xterm render are NOT redundant).

**Not worth a test - don't add these, and prefer deleting them:**
- **Skipped tests** (`@pytest.mark.skip`) for known unfixed bugs - they provide zero coverage and only noise. Delete, or unskip with a fix.
- **Assertion-less or tautological tests** - no `assert`, or assertions like `rc == 0 or "error" in stderr` that accept every outcome. They guard nothing.
- **Trivial substring/status checks** already implied by a stronger test (e.g. "page contains the word X" when another test already asserts the page renders).
- **Framework-level behavior**, not ours: standard 404 bodies, unsupported-method status codes, query-string/fragment handling, baseline HTTP semantics.
- **Redundant duplicates within a file** - if two tests exercise the same path with cosmetic differences, merge into one (parametrize when the only difference is input/expected value).

When trimming, fold any unique assertion from the test being removed into the one being kept, then verify the keeper still covers it.

## Agent skills

### Issue tracker

Issues are tracked on GitHub Issues (`vitorbaptista/shellshare`) via the `gh` CLI. See `docs/agents/issue-tracker.md`.

### Triage labels

The five canonical triage labels, used as-is: `needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, `wontfix`. See `docs/agents/triage-labels.md`.

### Domain docs

Single-context: one `CONTEXT.md` and `docs/adr/` at the repo root. See `docs/agents/domain.md`.

# Additional instructions

- When adding new dependency, always use `cargo add`
- Before committing run `make lint` and fix any issues
- When fixing a lint issue, don't simply disable the check unless we really don't need it
- Run local servers in a random port to avoid conflicts
