# Shellshare Integration Tests

Automated tests for the shellshare happy path.

## Overview

These tests verify the core functionality:

1. **Room Creation** - Broadcaster can create a room by posting messages
2. **Message Streaming** - Messages are stored and can be sent continuously
3. **Real-time Broadcast** - Connected viewers receive messages via Socket.io
4. **Authorization** - Only the room creator (with correct secret) can write
5. **Late Joiner Support** - New viewers receive message history
6. **Room Cleanup** - Rooms can be deleted by the owner

## Architecture

```
┌──────────────────────────────────────────────────────────────┐
│                        Test Runner                           │
├──────────────────────────────────────────────────────────────┤
│                                                              │
│  ┌─────────────────┐     ┌─────────────────┐                │
│  │ MongoDB Memory  │◄────│  Express Server │                │
│  │    Server       │     │  + Socket.io    │                │
│  └─────────────────┘     └────────┬────────┘                │
│                                   │                          │
│         ┌─────────────────────────┼─────────────────────┐   │
│         │                         │                     │   │
│         ▼                         ▼                     ▼   │
│  ┌─────────────┐          ┌─────────────┐       ┌──────────┐│
│  │ HTTP Client │          │  Socket.io  │       │  Assert  ││
│  │  (POST/DEL) │          │   Client    │       │  Results ││
│  └─────────────┘          └─────────────┘       └──────────┘│
│                                                              │
└──────────────────────────────────────────────────────────────┘
```

## Running Tests

```bash
# Install dependencies (includes mongodb-memory-server)
npm install

# Run tests
npm test
```

## Test Cases

### 1. Happy Path (`testHappyPath`)

The main end-to-end flow:
- Connect a viewer via Socket.io
- Viewer joins a room
- Broadcaster POSTs a message with authorization
- Verify viewer receives the message and terminal size
- Verify unauthorized requests are rejected (401)
- Clean up by DELETEing the room

### 2. Multiple Messages (`testMultipleMessages`)

Tests continuous streaming:
- Send multiple messages in sequence
- Verify all messages are received in order

### 3. Late Joiner (`testLateJoiner`)

Tests history replay:
- Broadcaster sends messages first
- Viewer joins after messages are sent
- Verify viewer receives the message history

## Message Encoding

Messages are encoded the same way as the Python client:
1. URL-encode the text (`encodeURIComponent`)
2. Base64 encode the result

This matches the client code:
```python
urlencoded = urllib_quote(data).encode('utf-8')
encoded_str = base64.b64encode(urlencoded).decode('utf-8')
```

## Dependencies

- `mongodb-memory-server` - In-memory MongoDB for isolated testing
- `socket.io-client` - Already in dependencies, used for viewer simulation
- `assert` - Node.js built-in for assertions

## Notes

- Tests use a simplified version of the server (not the full app.js)
- Each test uses unique room names to avoid conflicts
- Tests run sequentially to ensure clean state
- MongoDB Memory Server downloads MongoDB binaries on first run (~100MB)
