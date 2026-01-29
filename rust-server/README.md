# Shellshare Rust Server

A Rust implementation of the shellshare server using axum and socketioxide.

## Goals

1. **Single binary** - Server with all dependencies (HTML, CSS, JS, images) embedded
2. **CLI interface** - `shellshare server` to run the server
3. **100% API compatible** - Must pass all E2E tests from the Node.js implementation

## Building

```bash
cd rust-server
cargo build --release
```

## Running

```bash
# Start server on default port 3000
./target/release/shellshare server

# Custom host and port
./target/release/shellshare server --host 0.0.0.0 --port 8080
```

## Architecture

The server is implemented as a single `main.rs` file containing:
- CLI entry point using clap
- HTTP routes using axum
- Socket.IO handlers using socketioxide
- In-memory state management

```
rust-server/
├── Cargo.toml           # Dependencies and project config
├── src/
│   └── main.rs          # All server code
└── templates/           # Pre-rendered HTML templates
    ├── index.html       # Home page
    └── room.html        # Room viewer page
```

## Static Assets

Static files from `../public/` are embedded in the binary using `rust-embed`.
Templates from `templates/` are also embedded.

## Dependencies

- **axum** - Web framework
- **socketioxide** - Socket.IO server implementation  
- **rust-embed** - Embed files in binary
- **clap** - CLI argument parsing
- **tokio** - Async runtime

## Current Status

- [x] HTTP endpoints (GET /, GET /r/:room, POST /r/:room, DELETE /r/:room)
- [x] Static file serving
- [x] Socket.IO support
- [x] In-memory storage (no MongoDB required)
- [x] Authorization logic
- [x] Message accumulation
- [x] Pass all E2E tests

## Running E2E Tests

The GitHub Action `rust-e2e.yml` runs the E2E test suite against this server.

```bash
# Local testing
cd rust-server
cargo run --release -- server &
cd ../e2e
uv run pytest -v
```
