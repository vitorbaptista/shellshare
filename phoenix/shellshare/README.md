# Shellshare Phoenix

A Phoenix LiveView implementation of [shellshare](https://shellshare.net) - share your terminal in real-time.

## Features

- **Full API compatibility** with the original Python client
- **Phoenix LiveView** for real-time terminal viewing (no Socket.IO needed)
- **ETS-based storage** (no MongoDB required)
- **Automatic room cleanup** based on TTL

## Requirements

- Elixir 1.14+
- Erlang/OTP 25+

## Getting Started

```bash
# Install dependencies
mix deps.get

# Start the server
mix phx.server
```

The server will start at http://localhost:4000

## API Endpoints

### POST /r/:room

Receive terminal data from the Python client.

**Headers:**
- `Authorization`: Room secret (required)
- `Content-Type`: `application/json`

**Body:**
```json
{
  "message": "<base64 encoded terminal output>",
  "size": {"cols": 80, "rows": 24}
}
```

**Response:** `200 OK` or `401 Unauthorized`

### DELETE /r/:room

Close a room.

**Headers:**
- `Authorization`: Room secret (required)

**Response:** `202 Accepted` or `401 Unauthorized`

### GET /r/:room

View a shared terminal session (LiveView).

## Architecture

```
┌─────────────────┐     HTTP POST      ┌─────────────────┐
│  Python Client  │ ──────────────────▶│     Phoenix     │
│  (broadcaster)  │   /r/:room         │    Server       │
└─────────────────┘                    └────────┬────────┘
                                               │
                                   ┌───────────┴───────────┐
                                   │                       │
                                   ▼                       ▼
                           ┌───────────────┐      ┌───────────────┐
                           │  Room GenServer│     │   PubSub +    │
                           │   (per room)   │     │   LiveView    │
                           └───────────────┘      └───────┬───────┘
                                                          │
                                                          ▼
                                                  ┌───────────────┐
                                                  │   Browsers    │
                                                  │  (viewers)    │
                                                  └───────────────┘
```

### Key Components

- **`Shellshare.Room`** - GenServer managing individual room state
- **`Shellshare.RoomCleaner`** - Periodic cleanup of inactive rooms
- **`ShellshareWeb.RoomController`** - HTTP API for Python client
- **`ShellshareWeb.RoomLive`** - LiveView for terminal viewing

## Configuration

In `config/config.exs`:

```elixir
config :shellshare, :room,
  # Time-to-live for inactive rooms (5 minutes)
  ttl_ms: 5 * 60 * 1000,
  # Maximum buffer size per room (1MB)
  max_buffer_size: 1024 * 1024
```

Environment variables (production):
- `SECRET_KEY_BASE` - Required for session signing
- `PHX_HOST` - Hostname (default: shellshare.net)
- `PORT` - HTTP port (default: 4000)
- `ROOM_TTL_MS` - Room TTL in milliseconds

## Running Tests

```bash
mix test
```

## Deployment

```bash
# Build release
MIX_ENV=prod mix release

# Run
_build/prod/rel/shellshare/bin/shellshare start
```

Or with Docker:

```bash
docker build -t shellshare .
docker run -p 4000:4000 -e SECRET_KEY_BASE=... shellshare
```

## Differences from Node.js Version

| Feature | Node.js | Phoenix |
|---------|---------|---------|
| Storage | MongoDB | In-memory (GenServer) |
| Real-time | Socket.IO | Phoenix LiveView |
| Cleanup | MongoDB TTL index | GenServer timer |
| Sessions | Express sessions | Phoenix sessions |

## License

MIT
