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

# E2E tests (Python + Playwright)
cd e2e && uv sync && uv run pytest -v
```

## Architecture

**Dual-mode binary**: `shellshare` operates as client (default) or server (`shellshare server`).

### Client (`src/cli/`)
Multi-threaded design ensures network latency never blocks terminal display:
- **PTY reader thread**: Captures shell output, displays locally, sends to HTTP sender
- **HTTP sender thread**: Batches and rate-limits uploads (100ms intervals, 4KB buffer)
- **Stdin forwarder thread**: Routes user input to PTY
- **Signal handler** (Unix): Handles SIGWINCH for terminal resize

Key files:
- `mod.rs`: Entry point, room ID generation, server URL handling
- `script.rs`: PTY lifecycle, raw terminal mode, shell spawning
- `http.rs`: HTTP client with retry logic
- `encoding.rs`: Python-compatible URL+Base64 encoding

### Server (`src/server/mod.rs`)
Async Tokio + Axum web server with Socket.IO for real-time updates:
- In-memory room state with RwLock concurrency
- Per-room message history (max 100 messages)
- Static assets embedded at compile time (public/, templates/)
- Self-serving binary at `/bin/shellshare`

Routes:
- `GET /` - Home page with OS detection for install command
- `GET /r/:room` - Viewer page
- `POST /r/:room` - Broadcast message (first POST claims room with password)
- `DELETE /r/:room` - Cleanup room

### Encoding Protocol
Messages are encoded matching the original Python CLI: URL-encode (Python urllib.parse.quote style) then Base64. This ensures compatibility.

## Cross-Platform Notes

- **Unix**: PTY via portable-pty, raw terminal mode via libc tcgetattr/tcsetattr, SIGWINCH handling
- **Windows**: ConPTY, shell from $COMSPEC, no raw mode needed

## Testing

E2E tests in `e2e/` use Python pytest + Playwright for browser automation. Tests cover HTTP API, Socket.IO events, and full CLI-to-browser integration.

# Additional instructions

- When adding new dependency, always use `cargo add`
- Before committing run `make lint` and fix any issues
- When fixing a lint issue, don't simply disable the check unless we really don't need it
