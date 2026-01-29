# Shellshare Rust Server

A Rust implementation of the shellshare server using actix-web.

## Goals

1. **Single binary** - Server with all dependencies (HTML, CSS, JS, images) embedded
2. **CLI interface** - `shellshare server` to run the server, future `shellshare` for client
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

# Custom port and MongoDB URI
./target/release/shellshare server --port 8080 --mongodb-uri mongodb://localhost:27017/shellshare
```

## Architecture

```
rust-server/
├── Cargo.toml           # Dependencies and project config
├── src/
│   ├── main.rs          # CLI entry point and HTTP server setup
│   └── server/
│       ├── mod.rs       # Module exports
│       ├── handlers.rs  # HTTP request handlers
│       ├── socket.rs    # Socket.IO event handlers
│       ├── state.rs     # Application state (DB, caches)
│       └── models.rs    # MongoDB document models
└── templates/           # Pre-rendered HTML templates
    ├── index.html       # Home page
    └── room.html        # Room viewer page
```

## Static Assets

Static files from `../public/` are embedded in the binary using `rust-embed`.
Templates from `templates/` are also embedded.

## Dependencies

- **actix-web** - Web framework
- **socketioxide** - Socket.IO server implementation  
- **rust-embed** - Embed files in binary
- **clap** - CLI argument parsing
- **mongodb** - Database driver
- **tokio** - Async runtime

## Current Status

🚧 **Work in Progress** 🚧

- [ ] HTTP endpoints (GET /, GET /r/:room, POST /r/:room, DELETE /r/:room)
- [ ] Static file serving
- [ ] Socket.IO support
- [ ] MongoDB integration
- [ ] Authorization logic
- [ ] Message accumulation
- [ ] Pass all E2E tests

## Running E2E Tests

The GitHub Action `rust-e2e.yml` runs the E2E test suite against this server.
Currently many tests will fail - the goal is to make them all pass.

```bash
# Local testing (requires MongoDB running)
cd rust-server
cargo run --release -- server &
cd ../e2e
uv run pytest -v
```
