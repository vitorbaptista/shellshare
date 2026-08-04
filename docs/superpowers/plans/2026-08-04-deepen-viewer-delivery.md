# Deepen Viewer Delivery

## Goal

Concentrate raw-WebSocket viewer delivery behind one deep module without
changing observable behavior or performance characteristics.

## Corrected diagnosis

The existing `Viewers` registry is not a pass-through. It hides viewer-id
allocation, concurrent empty-room cleanup, bounded queues, and removal on
overflow. The architectural friction is that the rest of viewer delivery is
split across `src/server/viewers.rs` and `src/server/mod.rs`: worker startup,
fan-out sharding and coalescing, user-count convergence, snapshot replay,
socket liveness, per-viewer coalescing, and cleanup have no single seam.

The deepened module therefore absorbs the complete raw-WebSocket viewer
delivery lifecycle. It does not absorb room mutation, ingest authorization,
room TTL, broadcaster analytics, routes, pages, or binary downloads.

## Interface

`ViewerDelivery` has four entry points:

1. `start(Rooms, Analytics)` constructs the registry and all background
   workers. It must run inside Tokio.
2. `publish_ingest(&RoomId, size, payload)` publishes a successfully stored
   ingest item. Size and payload stay together so size remains an ordering
   barrier within the room's shard.
3. `publish_control(&RoomId, ViewerControl)` publishes `Reset` or
   `Broadcasting(bool)` immediately, preserving the current ability for these
   controls to overtake queued ingest output.
4. `join(RoomId, WebSocket)` owns registration, snapshot replay, live relay,
   liveness, cleanup, user-count convergence, and viewer analytics.

`join` deliberately couples the interface to Axum's raw WebSocket. There is
one real adapter; a transport-neutral port would be a hypothetical seam.
`Analytics` is an explicit internal dependency because `viewer_joined` must be
recorded only after replay succeeds and only for an existing room.

## Non-negotiable behavior

- Register before snapshot: concurrent output may duplicate but never vanish.
- Replay order remains optional size, optional history, broadcasting, then
  `usersCount` last.
- Store before publish. Unauthorized, malformed, unknown, oversized, ping, and
  pong frames refresh the room where applicable but do not publish.
- One stable shard per room preserves accepted publish order.
- Fan-out remains unbounded and best-effort so viewer work never stalls ingest
  acknowledgments.
- Immediate reset and broadcasting controls remain outside the ingest queue.
- Keep both coalescing stages: 64 KiB before fan-out and 1 MiB per viewer.
- Keep `VIEWER_QUEUE` at 2048, viewer writes/idleness at 75 seconds, and pings
  at 25 seconds.
- Preserve current overflow behavior: remove a full sender from the registry;
  let the socket task drain its existing receiver before it observes closure.
- Worker tasks capture only the registry and their receiver. They never hold a
  `ViewerDelivery` clone or its senders.
- All entry points accept canonical `RoomId` values.
- No Rust unit tests or test-only seams. Existing e2e tests remain the interface
  test surface.

## Implementation sequence

1. Expand `src/server/viewers.rs` with the current fan-out, user-count, viewer
   socket, and relay implementations, moving constants and comments verbatim.
2. Add `ViewerDelivery::start`, `publish_ingest`, `publish_control`, and `join`.
3. Replace `AppState`'s registry and optional queue fields with one mandatory
   `ViewerDelivery`; remove `AppState::default` construction.
4. Route ingest publication, reset/broadcasting controls, and viewer upgrades
   through the new interface in one coherent change. Never run old and new
   delivery paths together.
5. Delete the moved implementation from `src/server/mod.rs`, then format and
   lint.

## Verification

- `make lint`
- `cargo build --release`
- `cd e2e && uv run pytest -n 10 test_viewer_ws.py test_analytics.py test_ws.py`
- `cd e2e && uv run pytest -n 10`
- Bounded fan-out smoke benchmark with 50 viewers, a short steady phase, and a
  1 MiB firehose; require zero loss/disconnects and the final marker at every
  viewer.

The snapshot order and dead-room analytics gate already have dedicated e2e
coverage. This refactor does not add timing-sensitive overflow or overtaking
tests for implementation characteristics that are intentionally unchanged.
