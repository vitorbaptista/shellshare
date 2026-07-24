# The room outlives the broadcaster

## Problem

A session's room is destroyed the moment the broadcaster stops. `stream_stdin`
(`src/cli/mod.rs:412`) and `run_script_mode` (`src/cli/script.rs:268`) both end
by calling `Transport::shutdown_and_delete`, which sends `{"delete": true}` on
the ingest socket and the server drops the room.

That makes the most useful agent shape unusable:

```
dmesg | npx shellshare --json
```

`dmesg` exits in milliseconds, shellshare follows it, and the URL it just
printed is dead before anyone can open it. The working alternative is a hack:

```
{ sudo dmesg; sleep 600; } | npx shellshare --json
```

which forces the caller to hold a live process for the entire viewing window,
guess the window's length up front, and manage a background job.

Nothing else forces the deletion. The server already keeps rooms with full
history for 6h by default (`DEFAULT_ROOM_TTL_SECS`, `src/main.rs:27`) and evicts
them on inactivity. The explicit delete is the only reason the link dies early.

## Design

**The room's lifetime stops being tied to the broadcaster's.** A session ends by
flushing, waiting briefly for outstanding acks, and closing. The room and its
history survive on the server until the normal TTL evicts it.

`dmesg | npx shellshare --json` then prints its `sharing` event, streams, prints
`end`, and exits — leaving a working link. No `sleep`, no background process.

This applies to every mode: interactive shell, `exec`, and piped stream. One
rule, nothing to explain. End-to-end encryption is what makes it cheap: the
server holds ciphertext it cannot read, and the key rides only in the link
fragment.

### Room teardown

None beyond the TTL. No `shellshare delete` subcommand, no `--delete-on-exit`
flag. Rooms expire on the server's schedule.

### Fresh start on re-claim

Because the default password derives from the machine id
(`get_default_password`, `src/cli/mod.rs:83`), rerunning `shellshare --room
deploy-log` from the same machine re-claims the surviving room. Without further
work the new session's output would land underneath the previous run's
scrollback.

The client sends a new `{"reset": true}` control frame on the ingest WebSocket,
exactly once, immediately after its **first** handshake — never on a reconnect,
so replay and at-least-once delivery are untouched. The server clears that
room's history under the room's existing lock, keeps the claim, and drops any
attached viewers; the viewer page's reconnect-and-resync path (already
load-bearing for viewers that overflow their send queue) brings them back to the
empty room, and the new session's `size` frame follows immediately.

Deleting the bytes rather than emitting an ANSI clear matters because two paths
read the room as raw bytes, not as a rendered screen: `GET /r/:room.bin`, which
agents use for snapshots, and the 200-frame history budget. A clear sequence
would fix neither.

Random room ids get a new id per run and never hit this path; it exists for
named rooms.

### Not addressed

`shellshare serve` and `--tunnel` host the server in-process, so the link still
dies when the process does. Those keep needing the `{ cmd; sleep N; }` shape.
Out of scope here.

## Implementation

### Client

- `Transport::shutdown_and_delete` (`src/cli/ws.rs:218`) becomes
  `Transport::shutdown`: keep the flush and the bounded `SHUTDOWN_DRAIN` wait
  for outstanding acks — the tail of the output has to land, and nothing will
  retransmit it — then close the socket. Stop sending `{"delete": true}`.
- Update both call sites: `stream_stdin` (`src/cli/mod.rs:412`) and
  `run_script_mode` (`src/cli/script.rs:268`). Ctrl+C already rides these paths.
  The error paths that deliberately skip cleanup (`src/cli/mod.rs:403-409`,
  `src/cli/script.rs:256-263`) are untouched.
- `Transport::connect` sends `{"reset": true}` after the initial handshake
  succeeds. `handshake()` must not send it, so reconnects don't clear history.

### Server

- Handle `{"reset": true}` in the ingest text-frame branch
  (`src/server/mod.rs:720-741`), alongside the existing `delete` and `size`
  cases: clear the room's history, keep the claim and the connection, disconnect
  the room's attached viewers. Unlike `delete`, it does not break the loop.
- Nothing else. In particular, **analytics needs no change**: the detach path at
  `src/server/mod.rs:772-782` already emits `broadcast_ended` with a duration
  whenever `broadcaster_disconnected` reports that the detach ended a still-live
  room's segment. The `delete` branch's own `broadcast_ended` call exists only
  because deletion destroys the room before the detach path can measure it.
  Removing the delete frame hands the reporting back to the path that was always
  there. The resulting shape — one `broadcast_started`/`broadcast_ended` pair per
  live segment, multiple pairs per room, duration derived from first start and
  last end — is what `src/server/analytics.rs:29-35` already documents.

### Compatibility

- Old clients against a new server: unchanged. They still send
  `{"delete": true}` and the server still honors it.
- New clients against an old server: `{"reset": true}` parses as JSON with
  neither `delete` nor `size`, so the existing text branch ignores it. The room
  lifetime change still works; only the fresh-start-on-re-claim degrades to
  appending.
- Version bump: **minor**. No old CLI binary breaks.

### User-visible strings

None change. `End of transmission.` describes the transmission, not the room,
and stays. `--json` keeps its exact contract: `sharing` first, `end` last, same
fields.

### Docs

`AGENTS.md` and `public/llms.txt` need prose describing the new lifetime — the
room outlives the process, so a short command no longer needs a `sleep` — but no
contract change. `CLAUDE.md`'s protocol notes gain the `reset` control message.

## Tests

E2E only, in the existing files. Three additions:

1. **Stream mode outlives the process** — pipe a short input, wait for the
   process to exit, assert `GET /r/:room.bin` still returns the output. This is
   the reported bug; nothing currently covers it.
2. **`exec` mode outlives the process** — same assertion after the command
   exits. A separate call site that can regress independently.
3. **Re-claim starts fresh** — run twice against the same `--room`, assert the
   second run's snapshot contains only the second run's bytes. Covers the
   `reset` frame end to end.

The existing room-TTL eviction test already covers the other half: rooms do
eventually go away.
