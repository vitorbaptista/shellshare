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

**Scripting surface**: `shellshare exec -- <cmd>` runs one command in the PTY (instead of a shell), broadcasts it, and exits with the command's exit code. The global `--json` flag switches stdout to newline-delimited JSON events: first one with event `sharing` (parse its `url` field), last `{"event":"end","exit_code":N}` (errors stay on stderr as `ERROR: ...`). This contract is documented in `AGENTS.md` and `public/llms.txt` and covered by `e2e/test_agents.py` - the three must stay in lockstep.

### Client (`src/cli/`)
Multi-threaded design ensures network latency never blocks terminal display:
- **PTY reader thread**: Captures shell output, displays locally, sends to the sender thread
- **Sender thread**: Owns the WebSocket transport; coalesces whatever is queued and sends immediately (no pacing - frames are cheap)
- **Stdin forwarder thread**: Routes user input to PTY
- **Signal handler** (Unix): Handles SIGWINCH for terminal resize

Key files:
- `mod.rs`: Entry point, room ID generation, server URL handling, terminal size. Connecting the transport claims the room, so auth failures surface before the shell spawns
- `script.rs`: PTY lifecycle, raw terminal mode, shell spawning
- `ws.rs`: WebSocket transport. Binary frames carry raw terminal bytes; JSON text frames carry control messages (`size`, `reset`). Reliability: output stays in a bounded replay buffer until the server acks it (`{"ack": n}`, cumulative per-connection bytes); on failure the client reconnects with backoff and replays everything unacked (at-least-once delivery). Only authorization errors are fatal. A session ends by flushing and closing, not by deleting: the room and its history outlive the process until the server's TTL evicts it, so a short command (`dmesg | shellshare`) still leaves a working link. The first connection - never a reconnect, where it would destroy exactly what replay is rebuilding - sends `reset` so a reused room name starts clean
- `crypto.rs`: end-to-end encryption, on by default (opt out with `--disable-encryption`). Every output chunk is sealed into a self-delimiting AES-256-GCM record (`[u32 BE len][nonce][ciphertext+tag]`) as it enters the replay buffer, so acks/replay operate on ciphertext and the server stays an opaque relay (zero server changes - it never knew about encryption). The key is HKDF-derived from this machine's id and the room name (so a named room keeps one reusable share link across restarts; nothing is written to disk) and rides only in the link's `#fragment`. The `size` message carries `encrypted: true` so the viewer knows whether to decrypt (via WebCrypto, needs https or localhost) or render plaintext, and shows an explanatory notice when an encrypted link's fragment is missing/invalid/wrong or the context is insecure. `--disable-encryption` broadcasts plaintext for viewers on plain HTTP (a classroom LAN), where browsers have no WebCrypto. Record format must stay in lockstep with `templates/room.html`. Threat model (honest-but-curious server serving the unmodified page; key secrecy rests on the high-entropy machine id; metadata/timing still visible) is documented in `crypto.rs`

### Server (`src/server/`)
Async Tokio + Axum web server. Viewers connect over a raw WebSocket (`/ws/v/r/:room`) that mirrors the ingest protocol: binary frames are terminal bytes, JSON text frames are control events (`size`, `usersCount`, `broadcasting`). The room is the URL, so there is no join handshake; the server pushes the room snapshot (size, history, broadcasting, usersCount - in that order, usersCount always last) on every connect, making reconnects a clean resync. A viewer that falls hopelessly behind its bounded send queue is disconnected on purpose (the page reconnects and resyncs) instead of silently losing frames. Socket.IO was removed: its per-message double frame (announce + attachment) doubled fan-out work, and its engine.io layer had an unfixable header/attachment interleave race.
- `mod.rs`: Router, WebSocket handlers, and the fan-out/usercount tasks - thin translators that delegate to the modules below
- `viewers.rs`: the raw-WebSocket viewer registry (who watches which room, bounded per-viewer queues, disconnect-on-overflow)
- `rooms.rs`: All room lifecycle behind one interface - first-caller-wins password claiming, message history (max 200), canonical room names (`RoomId`), activity tracking and TTL eviction. Rooms outlive their broadcaster, so TTL eviction is the only teardown - and only the broadcaster postpones it: `append` (broadcast frames and the client's 30s keepalive pings) refreshes activity, while reads (`snapshot`, serving both viewer joins and `/r/:room.bin`) deliberately do not, or a forgotten browser tab or a polling agent would pin a finished broadcast forever. One entry, one lock (a sharded `DashMap`): authorization and mutation are a single critical section on their room, so concurrent broadcasters never serialize on a shared lock
- `pages.rs`: Home page (install options: npx by default, plus per-OS binary downloads), viewer page, embedded static assets
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

## Cross-Platform Notes

- **Unix**: PTY via portable-pty, raw terminal mode via libc tcgetattr/tcsetattr, SIGWINCH handling
- **Windows**: ConPTY, shell from $COMSPEC, no raw mode needed

## Testing

E2E tests are the single source of truth - there are no Rust unit tests by design. The implementation is free to change as long as the e2e suite stays green.

The suite in `e2e/` uses Python pytest + Playwright and manages its own servers: a shared one started automatically on a free port (pin it with `SHELLSHARE_E2E_PORT` to reuse a pre-started server) plus per-test dedicated servers with custom flags (e.g. short `--room-ttl` for eviction tests). Coverage spans the HTTP API, the viewer WebSocket protocol, full CLI-to-browser integration, real-TTY CLI sessions (`test_cli_tty.py`: resize/SIGWINCH, Ctrl+C handling, cleanup on exit), room TTL eviction, and binary downloads.

### What to test (and what not to)

The suite is the whole safety net, but every test also costs CI minutes and maintenance. Keep it lean: one strong test per behavior beats five overlapping ones. Before adding a test, ask whether it guards a real Shellshare failure mode that nothing else already covers.

**Worth a test:**
- **Happy paths** for every public surface: a CLI session broadcasts and a viewer sees it; the home/viewer pages render; binaries download.
- **Lockstep contracts** that span files and break silently if drifted - the wire protocol (`protocol.rs` ↔ `templates/room.html` ↔ `conftest.py`), the encryption record format, the `size` control message (theme, `encrypted` flag, cols/rows omission), and the `--json` event contract (`sharing`/`end`, mirrored in `AGENTS.md` + `public/llms.txt`). Test each contract once, at the layer that owns it.
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

# Additional instructions

- When adding new dependency, always use `cargo add`
- Before committing run `make lint` and fix any issues
- When fixing a lint issue, don't simply disable the check unless we really don't need it
- Run local servers in a random port to avoid conflicts
