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

**Dual-mode binary**: `shellshare` operates as client (default) or server (`shellshare server`). `shellshare serve` combines both: it boots the embedded server on a background thread (default `localhost:3000`, configurable via `--host`/`--port`) and runs the client against it, sharing the terminal with no external server.

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

### Server (`src/server/`)
Async Tokio + Axum web server with Socket.IO for real-time updates:
- `mod.rs`: Router and HTTP/Socket.IO handlers - thin translators that delegate to the modules below
- `rooms.rs`: All room lifecycle behind one interface - first-caller-wins password claiming, message history (max 100), canonical room names (`RoomId`), activity tracking and TTL eviction. One map, one lock: authorization and mutation are a single critical section
- `pages.rs`: Home page (OS detection for the install command), viewer page, embedded static assets
- `binaries.rs`: Platform detection and binary downloads at `/bin/shellshare`

Routes:
- `GET /` - Home page with OS detection for install command
- `GET /r/:room` - Viewer page
- `GET /ws/r/:room` - WebSocket ingest (the only broadcast transport): claimed/verified at the handshake, binary frames are terminal bytes, text frames are control messages, every stored frame is acked
- `POST /r/:room` - Retired: always 410 Gone with an upgrade message, so pre-WebSocket clients fail loudly instead of silently
- `DELETE /r/:room` - Cleanup room

### Wire Protocol (`src/protocol.rs`)
Terminal output is **raw bytes** end to end: binary WebSocket frames from
the CLI, raw bytes in room history, binary Socket.IO attachments to
viewers, decoded in the browser by a streaming `TextDecoder` (the viewer
script is inline in `templates/room.html`). History accumulation for late
joiners lives here too. Must stay in lockstep with `templates/room.html`
and `e2e/conftest.py`.

## Cross-Platform Notes

- **Unix**: PTY via portable-pty, raw terminal mode via libc tcgetattr/tcsetattr, SIGWINCH handling
- **Windows**: ConPTY, shell from $COMSPEC, no raw mode needed

## Testing

E2E tests are the single source of truth - there are no Rust unit tests by design. The implementation is free to change as long as the e2e suite stays green.

The suite in `e2e/` uses Python pytest + Playwright and manages its own servers: a shared one on :3000 (started automatically if none is running) plus per-test dedicated servers with custom flags (e.g. short `--room-ttl` for eviction tests). Coverage spans the HTTP API, Socket.IO events, full CLI-to-browser integration, real-TTY CLI sessions (`test_cli_tty.py`: resize/SIGWINCH, Ctrl+C handling, cleanup on exit), room TTL eviction, and binary downloads.

# Additional instructions

- When adding new dependency, always use `cargo add`
- Before committing run `make lint` and fix any issues
- When fixing a lint issue, don't simply disable the check unless we really don't need it
