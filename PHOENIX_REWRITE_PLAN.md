# Phoenix LiveView Backend Rewrite Plan

## Current Architecture (Node.js)

```
┌─────────────────┐     HTTP POST      ┌─────────────────┐
│  Python Client  │ ──────────────────▶│   Express.js    │
│  (broadcaster)  │   /r/:room         │    Server       │
└─────────────────┘                    └────────┬────────┘
                                                │
                                    ┌───────────┴───────────┐
                                    │                       │
                                    ▼                       ▼
                            ┌───────────────┐      ┌───────────────┐
                            │   MongoDB     │      │   Socket.IO   │
                            │  (storage)    │      │  (broadcast)  │
                            └───────────────┘      └───────┬───────┘
                                                          │
                                                          ▼
                                                  ┌───────────────┐
                                                  │   Browsers    │
                                                  │  (viewers)    │
                                                  └───────────────┘
```

### Key Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/r/:room` | Render viewer page |
| POST | `/r/:room` | Receive terminal data (from Python client) |
| DELETE | `/r/:room` | Cleanup room |
| WebSocket | `/socket.io` | Real-time updates to viewers |

### Data Model

- **Room**: Stores terminal output (base64), terminal size
- **Authorization**: Room + secret mapping (TTL-based)

---

## Phoenix Architecture

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
                            │     ETS       │      │   PubSub +    │
                            │   (state)     │      │   LiveView    │
                            └───────────────┘      └───────┬───────┘
                                                          │
                                                          ▼
                                                  ┌───────────────┐
                                                  │   Browsers    │
                                                  │  (viewers)    │
                                                  └───────────────┘
```

### Key Components

1. **RoomController** - HTTP API for Python client
   - `POST /r/:room` - receive terminal data
   - `DELETE /r/:room` - cleanup room

2. **RoomLive** - LiveView for viewers
   - `GET /r/:room` - renders terminal viewer
   - Subscribes to PubSub for real-time updates

3. **RoomServer** (GenServer) - Room state management
   - Stores terminal buffer, size, authorization
   - Auto-cleanup after TTL

4. **RoomRegistry** - Dynamic supervisor for rooms

### Data Storage

Using **ETS** instead of MongoDB:
- Simpler, no external dependency
- Fast in-memory storage
- TTL can be implemented with periodic cleanup

Alternatively, **PostgreSQL** could be used for persistence.

---

## Implementation Plan

### Phase 1: Project Setup
- [ ] Create Phoenix project: `mix phx.new shellshare_phoenix --no-ecto --no-mailer --no-dashboard`
- [ ] Configure for LiveView
- [ ] Set up test framework

### Phase 2: Core Logic (TDD)
- [ ] `Shellshare.Room` - GenServer for room state
  - [ ] Test: create room with secret
  - [ ] Test: push message updates buffer
  - [ ] Test: unauthorized push rejected
  - [ ] Test: room auto-terminates after TTL
  
- [ ] `Shellshare.RoomSupervisor` - DynamicSupervisor
  - [ ] Test: start room on demand
  - [ ] Test: find existing room
  - [ ] Test: room cleanup

### Phase 3: HTTP API
- [ ] `ShellshareWeb.RoomController`
  - [ ] Test: POST creates room if not exists
  - [ ] Test: POST with valid auth succeeds
  - [ ] Test: POST with invalid auth returns 401
  - [ ] Test: DELETE cleans up room

### Phase 4: LiveView
- [ ] `ShellshareWeb.RoomLive`
  - [ ] Test: renders terminal container
  - [ ] Test: subscribes to room updates
  - [ ] Test: updates terminal on message
  - [ ] Test: shows user count

### Phase 5: Integration
- [ ] Test with actual Python client
- [ ] Verify real-time streaming works

---

## API Compatibility

The Python client expects:

```http
POST /r/{room}
Authorization: {secret}
Content-Type: application/json

{"message": "{base64}", "size": {"cols": 80, "rows": 24}}
```

Response: `200 OK` or `401 Unauthorized`

```http
DELETE /r/{room}
Authorization: {secret}
```

Response: `202 Accepted`

---

## File Structure

```
lib/
├── shellshare/
│   ├── application.ex
│   ├── room.ex              # GenServer for room state
│   ├── room_supervisor.ex   # DynamicSupervisor
│   └── room_registry.ex     # Registry for room lookup
├── shellshare_web/
│   ├── controllers/
│   │   └── room_controller.ex
│   ├── live/
│   │   └── room_live.ex
│   ├── components/
│   │   └── terminal.ex
│   └── router.ex
test/
├── shellshare/
│   ├── room_test.exs
│   └── room_supervisor_test.exs
├── shellshare_web/
│   ├── controllers/
│   │   └── room_controller_test.exs
│   └── live/
│       └── room_live_test.exs
```

---

## Dependencies

```elixir
# mix.exs
defp deps do
  [
    {:phoenix, "~> 1.7"},
    {:phoenix_live_view, "~> 0.20"},
    {:phoenix_html, "~> 4.0"},
    {:jason, "~> 1.4"},
    {:plug_cowboy, "~> 2.7"}
  ]
end
```

---

## Migration Notes

1. **No MongoDB** - Using ETS for simplicity
2. **No Socket.IO** - Using Phoenix Channels/LiveView
3. **Same HTTP API** - Python client works unchanged
4. **LiveView for viewers** - More efficient, less JS
