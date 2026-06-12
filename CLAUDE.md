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
- `ws.rs`: WebSocket transport. Binary frames carry raw terminal bytes; JSON text frames carry control messages (`size`, `delete`). Reliability: output stays in a bounded replay buffer until the server acks it (`{"ack": n}`, cumulative per-connection bytes); on failure the client reconnects with backoff and replays everything unacked (at-least-once delivery). Only authorization errors are fatal
- `crypto.rs`: end-to-end encryption, on by default (opt out with `--disable-encryption`). Every output chunk is sealed into a self-delimiting AES-256-GCM record (`[u32 BE len][nonce][ciphertext+tag]`) as it enters the replay buffer, so acks/replay operate on ciphertext and the server stays an opaque relay (zero server changes - it never knew about encryption). The key is HKDF-derived from this machine's id and the room name (so a named room keeps one reusable share link across restarts; nothing is written to disk) and rides only in the link's `#fragment`. The `size` message carries `encrypted: true` so the viewer knows whether to decrypt (via WebCrypto, needs https or localhost) or render plaintext, and shows an explanatory notice when an encrypted link's fragment is missing/invalid/wrong or the context is insecure. `--disable-encryption` broadcasts plaintext for viewers on plain HTTP (a classroom LAN), where browsers have no WebCrypto. Record format must stay in lockstep with `templates/room.html`. Threat model (honest-but-curious server serving the unmodified page; key secrecy rests on the high-entropy machine id; metadata/timing still visible) is documented in `crypto.rs`

### Server (`src/server/`)
Async Tokio + Axum web server with Socket.IO for real-time updates. Socket.IO is WebSocket-only on both ends (server config + viewer page + e2e listeners): the engine.io HTTP long-polling encoder corrupts binary events delivered to a parked long-poll, so polling is rejected outright rather than left as a fallback.
- `mod.rs`: Router and HTTP/Socket.IO handlers - thin translators that delegate to the modules below
- `rooms.rs`: All room lifecycle behind one interface - first-caller-wins password claiming, message history (max 100), canonical room names (`RoomId`), activity tracking and TTL eviction. One entry, one lock (a sharded `DashMap`): authorization and mutation are a single critical section on their room, so concurrent broadcasters never serialize on a shared lock
- `pages.rs`: Home page (install options: npx by default, plus per-OS binary downloads), viewer page, embedded static assets
- `binaries.rs`: Platform detection and binary downloads at `/bin/shellshare`

Routes:
- `GET /` - Home page with install instructions (npx selected by default)
- `GET /r/:room` - Viewer page
- `GET /ws/r/:room` - WebSocket ingest (the only broadcast transport): claimed/verified at the handshake, binary frames are terminal bytes, text frames are control messages, every stored frame is acked. Each open connection counts as an attached broadcaster: viewers get a `broadcasting` Socket.IO event (current state on join, plus every transition) driving the online/offline indicator in the viewer page; a connection silent past 90s (clients ping every 30s) is treated as dead
- `POST /r/:room` - Retired: always 410 Gone with an upgrade message, so pre-WebSocket clients fail loudly instead of silently
- `DELETE /r/:room` - Cleanup room

### Wire Protocol (`src/protocol.rs`)
Terminal output is **raw bytes** end to end: binary WebSocket frames from
the CLI, raw bytes in room history, binary Socket.IO attachments to
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

The suite in `e2e/` uses Python pytest + Playwright and manages its own servers: a shared one started automatically on a free port (pin it with `SHELLSHARE_E2E_PORT` to reuse a pre-started server) plus per-test dedicated servers with custom flags (e.g. short `--room-ttl` for eviction tests). Coverage spans the HTTP API, Socket.IO events, full CLI-to-browser integration, real-TTY CLI sessions (`test_cli_tty.py`: resize/SIGWINCH, Ctrl+C handling, cleanup on exit), room TTL eviction, and binary downloads.

# Additional instructions

- When adding new dependency, always use `cargo add`
- Before committing run `make lint` and fix any issues
- When fixing a lint issue, don't simply disable the check unless we really don't need it
- Run local servers in a random port to avoid conflicts
