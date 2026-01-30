# E2E Black-Box Tests

Comprehensive test suite for the Shellshare backend. These tests treat Shellshare as a black box,
interacting only through public interfaces to ensure 100% API compatibility.

## Purpose

This test suite is designed to:
1. **Validate API compatibility** - Ensure any rewrite (e.g., Go) behaves identically
2. **Test all endpoints** - Cover every HTTP endpoint and Socket.IO event
3. **Verify edge cases** - Test authorization, error handling, and special cases

## Test Files

| File | Description |
|------|-------------|
| `test_api.py` | HTTP endpoint tests (GET, POST, DELETE) |
| `test_socketio.py` | Real-time WebSocket tests |
| `test_broadcast.py` | End-to-end CLI to browser test |

## What's Tested

### HTTP API (`test_api.py`)

- **Home Page** (`GET /`)
  - Returns 200 OK
  - Contains HTML content

- **Room Page** (`GET /r/:room`)
  - Returns 200 OK for any room name
  - Handles special characters in room names

- **Broadcast** (`POST /r/:room`)
  - First request claims room with password
  - Same password succeeds on subsequent requests
  - Wrong password returns 401 Unauthorized
  - Empty password creates open room
  - Messages accumulate in room

- **Delete Room** (`DELETE /r/:room`)
  - Valid auth returns 202 Accepted
  - Wrong auth returns 401 Unauthorized
  - Idempotent (deleting unclaimed room succeeds)

- **Static Files**
  - `/bin/script.exe` accessible
  - JavaScript files accessible
  - Cache headers present

- **Authorization Logic**
  - First request claims room
  - Each room has independent auth
  - Case-sensitive room names

- **Edge Cases**
  - Large messages (10KB+)
  - Special characters (unicode, emoji, escapes)
  - 404 for nonexistent files

### Socket.IO (`test_socketio.py`)

- **Connection**
  - Can establish Socket.IO connection
  - Can join rooms

- **Real-time Messages**
  - Receive messages broadcast after joining
  - Receive terminal size updates
  - Get existing room data on join

- **User Count**
  - Receive user count on join
  - Count increases with more clients
  - Count decreases on disconnect

- **Room Isolation**
  - Messages only go to clients in same room

- **Message Accumulation**
  - Multiple messages concatenate in order

### Browser Integration (`test_broadcast.py`)

- CLI broadcasts message → appears in browser terminal

## Running Locally

1. **Install uv** (Python package manager):
   ```bash
   curl -LsSf https://astral.sh/uv/install.sh | sh
   ```

2. **Start MongoDB**:
   ```bash
   docker run -d -p 27017:27017 mongo
   ```

3. **Start the server**:
   ```bash
   npm install
   MONGO_URL=mongodb://localhost:27017/shellshare npm start
   ```

4. **Run tests**:
   ```bash
   cd e2e
   uv sync
   uv run playwright install chromium
   uv run pytest
   ```

## Running Specific Tests

```bash
# Run only API tests
uv run pytest test_api.py

# Run only Socket.IO tests
uv run pytest test_socketio.py

# Run only browser tests
uv run pytest test_broadcast.py

# Run a specific test
uv run pytest test_api.py::TestBroadcast::test_broadcast_wrong_password_returns_401
```

## CI

Tests run automatically on GitHub Actions:
- **Linux (Docker)**: Uses docker-compose for MongoDB
- **Windows**: Installs MongoDB via Chocolatey

Both environments run the full test suite to ensure cross-platform compatibility.

## Compatibility Guarantee

If all tests pass on a new implementation:
- All HTTP endpoints behave identically
- Socket.IO events work the same way
- Authorization logic is preserved
- Message storage and retrieval is compatible
- The CLI will work without modification
