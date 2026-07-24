# Room Outlives the Broadcaster — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A shellshare session's room, with its full history, survives the
broadcaster's exit until the server's normal TTL evicts it — so
`dmesg | npx shellshare --json` leaves a working link instead of a dead one.

**Architecture:** The client stops sending `{"delete": true}` on clean exit; it
flushes, waits for outstanding acks, and closes. Because the default password
derives from the machine id, a rerun re-claims the surviving room, so the client
sends a new `{"reset": true}` ingest control frame once — after its **first**
handshake only — and the server clears that room's history while keeping the
claim. And since a polled snapshot would otherwise refresh a dead room's TTL
forever, `/r/:room.bin` stops counting as activity.

**Tech Stack:** Rust (client: `tungstenite` blocking WebSocket; server: Axum +
Tokio + `DashMap`). Tests: Python pytest + Playwright in `e2e/`.

**Spec:** `docs/superpowers/specs/2026-07-24-room-outlives-broadcaster-design.md`

## Global Constraints

- **No Rust unit tests.** The e2e suite in `e2e/` is the entire safety net.
- **Run `make lint` before every commit.** Pedantic + nursery clippy lints are
  enabled. Never silence a lint you can fix.
- **No new dependencies.** This plan needs none.
- **`src/protocol.rs` and `templates/room.html` are NOT touched.** This change
  adds an ingest-side control message only; viewers never see `reset`.
- **Version bump: minor**, handled later by the `release` skill — not here.
- **No user-visible string changes.** `End of transmission.` stays.
- **`--json` contract is frozen.** `sharing` first, `end` last, same fields.

## Decisions taken after plan review

Three reviewers changed the shape of this plan. Recording why, so an
implementer does not "helpfully" restore the cut pieces:

- **No `Viewers::disconnect_room`.** Dropping viewers on reset would break ~25
  existing tests: the e2e harness's `SocketListener` never reconnects
  (`e2e/conftest.py:507-529`), unlike `templates/room.html`. A viewer attached
  across a rerun simply keeps rendering, as a terminal's scrollback does.
- **`reset` does NOT clear `entry.size`.** Two ingest connections may share a
  room and password (`src/server/rooms.rs:101-103`). Clearing a size the *other*
  connection will not re-send leaves the room sizeless — and `encrypted: true`
  rides inside the size message (`src/cli/ws.rs:257-259`), so a keyless viewer
  would render raw ciphertext instead of the "missing key" notice.
- **`reset` is fenced to the room's sole broadcaster.** Without a fence it has
  no ordering guarantee across connections: a previous session's still-draining
  ingest task can append its tail *after* the clear, and a stalled first
  connection's late reset can wipe history its own reconnect already stored.
  Both need a second attached connection, so the fence closes both, and it fails
  in the safe direction (append, never wipe).
- **No `MessageHistory::clear()`.** `Rooms::reset` rebuilds the history inline,
  keeping the lockstep file `src/protocol.rs` out of the changeset.

## File Structure

| File | Change |
|---|---|
| `src/server/rooms.rs` | Add `Rooms::reset()`; split `snapshot()` so a read can skip the activity refresh |
| `src/server/mod.rs` | Handle `{"reset": true}`; `/r/:room.bin` uses the non-refreshing read |
| `src/cli/ws.rs` | `shutdown_and_delete` → `shutdown`; send `reset` after the first handshake |
| `src/cli/mod.rs`, `src/cli/script.rs` | Call `shutdown()`; fix now-false comments |
| `e2e/test_agent_read.py` | New: room outlives stream mode; rerun starts fresh |
| `e2e/test_cli_tty.py` | Rewrite two delete-on-exit tests |
| `e2e/test_room_cleanup.py` | New: a polled snapshot does not keep a room alive |
| `e2e/test_analytics.py` | Clean-exit test drives the path shipping clients use |
| `e2e/test_broadcast.py`, `e2e/test_encryption.py` | Fix now-false comments |
| `AGENTS.md`, `public/llms.txt`, `CLAUDE.md` | New lifetime |

---

### Task 1: The room survives the broadcaster's exit

The bug itself.

**Produces:** `Transport::shutdown(&mut self)` — replaces
`Transport::shutdown_and_delete`. Same flush and ack-drain, no `delete` frame.

**Files:**
- Modify: `src/cli/ws.rs:1-13` (module doc), `src/cli/ws.rs:215-230`
- Modify: `src/cli/mod.rs:403-412`, `src/cli/script.rs:256-268`
- Modify: `e2e/test_cli_tty.py` (module docstring, two tests)
- Modify: `e2e/test_broadcast.py:354`, `e2e/test_encryption.py:70`,
  `e2e/test_analytics.py:99,243`
- Test: `e2e/test_agent_read.py`

- [ ] **Step 1: Write the failing test**

Append to `e2e/test_agent_read.py`:

```python
def test_stream_mode_room_outlives_the_process(unique_room, unique_password):
    """A short command's link keeps working after shellshare exits.

    This is the whole point: `dmesg | shellshare --json` must not need a
    `sleep` to keep its room alive long enough to be opened.
    """
    marker = f"STREAM-{random_id()}"
    proc = subprocess.run(
        CLI_COMMAND
        + ["--json", "-s", SERVER_URL, "-r", unique_room, "-W", unique_password],
        input=marker + "\n",
        capture_output=True,
        text=True,
        timeout=CLI_SESSION_TIMEOUT,
    )
    assert proc.returncode == 0
    key = parse_share_key(json.loads(proc.stdout.splitlines()[0])["url"])

    # The process is gone; the link still resolves to the output
    status, body = _get_bin(unique_room)
    assert status == 200, "the room died with the broadcaster"
    assert marker.encode() in open_records(key, body)
```

Extend `e2e/test_agent_read.py`'s imports (it currently imports only
`os`, `urllib.*`, and from `conftest`: `SERVER_URL`, `broadcast_message`,
`open_records`, `random_id`) with:

```python
import json
import subprocess

from conftest import CLI_COMMAND, CLI_SESSION_TIMEOUT, parse_share_key
```

`CLI_SESSION_TIMEOUT` does not exist in `conftest.py` yet — it is currently a
module constant in `e2e/test_agents.py:41` with a load-bearing comment about
wall-clock timeouts under `-n 10`. Move that constant **and its comment**
verbatim into `e2e/conftest.py`, and in `e2e/test_agents.py` replace the
definition with an import from `conftest`. Do not duplicate the number.

There is deliberately no separate `exec`-mode test: `exec` and the interactive
shell share one call site (`run_script_mode`, `src/cli/script.rs`), which
`e2e/test_cli_tty.py`'s rewritten test covers below.

- [ ] **Step 2: Run the test to verify it fails**

```bash
cargo build --release
cd e2e && uv run pytest test_agent_read.py -k outlives -v
```

Expected: FAIL on `assert status == 200` — the CLI deleted the room, so
`/r/<room>.bin` returns 404.

- [ ] **Step 3: Replace `shutdown_and_delete` with `shutdown`**

In `src/cli/ws.rs`, replace lines 215-230:

```rust
    /// Flush and close at the end of a session, leaving the room behind.
    ///
    /// Pending output is written and its acknowledgments awaited,
    /// briefly, because nothing will retransmit it: the room outlives
    /// this process (until the server's inactivity TTL evicts it), so
    /// the tail of the output has to land before the socket closes.
    pub fn shutdown(&mut self) {
        let _ = self.flush();
        let deadline = Instant::now() + SHUTDOWN_DRAIN;
        while self.socket.is_some() && !self.unacked.is_empty() && Instant::now() < deadline {
            std::thread::sleep(Duration::from_millis(20));
            self.drain_acks();
        }
        if let Some(socket) = self.socket.as_mut() {
            let _ = socket.close(None);
        }
        self.socket = None;
    }
```

Replace the module doc's control-message sentence (`src/cli/ws.rs:3-4`):

```rust
//! Terminal output travels as binary frames of raw bytes; control
//! messages (`size`, `reset`) travel as JSON text frames. A session ends
//! by flushing and closing, not by deleting: the room and its history
//! stay on the server until it goes idle, so a short command still
//! leaves a working link. (The server still honors the `delete` frame
//! this client no longer sends - older binaries are still out there.)
```

- [ ] **Step 4: Update both call sites and their now-false comments**

`src/cli/mod.rs:412` — replace `transport.shutdown_and_delete();` with
`transport.shutdown();`.

`src/cli/mod.rs:406-407` — the comment explains the skip in terms of deletion,
which `shutdown` no longer does. Replace:

```rust
            // The room belongs to someone else now; it is not ours to
            // delete, so skip the shutdown cleanup
```

with:

```rust
            // The room belongs to someone else now: draining our
            // buffer into it would be someone else's output, so skip
            // the shutdown flush entirely
```

`src/cli/script.rs:266-268` — replace:

```rust
        // Normal end (shell exit or Ctrl+C): flush pending output and
        // delete the room
        transport.shutdown_and_delete();
```

with:

```rust
        // Normal end (shell exit or Ctrl+C): flush pending output, then
        // close - the room stays up so the link keeps working
        transport.shutdown();
```

`src/cli/script.rs:257-258` — replace:

```rust
                // The room belongs to someone else now; it is not ours
                // to delete, so stop without the shutdown cleanup
```

with:

```rust
                // The room belongs to someone else now: our buffered
                // output is not theirs to receive, so stop without the
                // shutdown flush
```

- [ ] **Step 5: Rewrite the two TTY tests that asserted deletion**

In `e2e/test_cli_tty.py`, replace `test_room_is_deleted_on_clean_exit`
(lines 214-238) with:

```python
    def test_room_survives_clean_exit(
        self, unique_room, unique_password, tty_cli
    ):
        cli = tty_cli(unique_room, unique_password)
        assert cli.wait_for_screen("Sharing terminal in")
        cli.send("echo populating-history\n")
        cli.send("exit\n")
        assert cli.wait_exit()

        # The room keeps its claim: another password cannot take the name
        status = broadcast_message(
            SERVER_URL, unique_room, f"other-{unique_password}", "claimed"
        )
        assert status == 401, (
            f"Room name was released on exit; reclaim returned {status}"
        )

        # ...and the link the user already shared still shows the session.
        # Polled: shutdown's ack drain gives up after 1s, so on a loaded
        # runner the last frame can still be in flight when the process
        # is reaped
        assert poll_until(
            lambda: len(requests.get(f"{SERVER_URL}/r/{unique_room}.bin").content) > 0,
            timeout=5,
        ), "the room did not outlive the CLI with its history"
```

Add `import requests` to the file if absent. **Keep** the `poll_until` import —
it is used at `e2e/test_cli_tty.py:137`, `:151`, and `:282`.

Then in the SIGINT test, replace:

```python
        # The Ctrl+C handler deletes the room: it can be claimed anew
        assert poll_until(
            lambda: broadcast_message(
                SERVER_URL, unique_room, f"other-{unique_password}", "x"
            )
            == 200,
            timeout=5,
        ), "Room was not cleaned up on SIGINT"
```

with:

```python
        # Ctrl+C ends the transmission but leaves the room: the link the
        # user already shared keeps working, and the name stays claimed
        assert broadcast_message(
            SERVER_URL, unique_room, f"other-{unique_password}", "x"
        ) == 401, "Room name was released on SIGINT"
```

Finally, update the module docstring at `e2e/test_cli_tty.py:12`, which lists
"room deletion on clean exit" as covered behavior — say "room survival on clean
exit" instead.

- [ ] **Step 6: Make the analytics clean-exit test cover the shipping path**

`e2e/test_analytics.py:240-258` asserts exactly one `broadcast_ended` per clean
exit, but drives it with a raw `{"delete": true}` frame that no shipping client
sends any more. The duration now comes from the loop's own detach path
(`src/server/mod.rs:772-782`). Change that test to close the socket **without**
sending `delete`, so it exercises the path the CLI actually takes, and update
its comment at `:243` accordingly. Leave the delete-driven test at `:100-115`
alone — the server still honors that frame for old binaries, and that test is
now its only coverage. Update its comment at `:99-100` to say "as older clients
send on exit" rather than "as the CLI sends on exit".

- [ ] **Step 7: Fix two now-false comments**

`e2e/test_broadcast.py:354-355` — replace:

```python
    # Start a broadcaster that STAYS alive (the CLI deletes the room when
    # it exits, so history only exists while the broadcaster is live)
```

with:

```python
    # Start a broadcaster that STAYS alive: this test needs output
    # delivered to a viewer that joins mid-session
```

`e2e/test_encryption.py:70-73` — replace the docstring's second sentence
("The broadcaster must stay alive while viewers join: the room (and its
history) is deleted when the CLI exits.") with:

```python
    The broadcaster stays alive so viewers join a live session rather
    than reading history.
```

- [ ] **Step 8: Run the tests**

```bash
cargo build --release
cd e2e && uv run pytest test_agent_read.py test_cli_tty.py test_analytics.py -v
```

Expected: PASS. A hang in a TTY test is a real regression in the shutdown path —
do not extend the timeout to hide it.

- [ ] **Step 9: Full suite, lint, commit**

```bash
cd e2e && uv run pytest -n 10
make lint
git add -A
git commit -m "feat(cli): leave the room up on exit so short commands keep a live link"
```

Any failure in the full suite is a test that depended on the room name being
released on exit. Fix the test to match the new lifetime. If one cannot be made
to pass, stop and report it rather than deleting it.

---

### Task 2: A rerun of a named room starts fresh

**Consumes:** nothing. **Produces:** nothing later tasks depend on.

**Files:**
- Modify: `src/server/rooms.rs` (after `Rooms::snapshot`)
- Modify: `src/server/mod.rs:631-637` (doc), `src/server/mod.rs:720-741` (text arm)
- Modify: `src/cli/ws.rs:149-150`
- Test: `e2e/test_agent_read.py`

- [ ] **Step 1: Write the failing test**

Append to `e2e/test_agent_read.py`:

```python
def test_rerunning_a_room_starts_fresh(unique_room, unique_password):
    """A reused room name shows the current run, not the previous one.

    Rooms now outlive their broadcaster, so without this the second run's
    output would stack under the first run's scrollback.
    """
    first = f"RUN1-{random_id()}"
    second = f"RUN2-{random_id()}"
    key = None
    for marker in (first, second):
        proc = subprocess.run(
            CLI_COMMAND
            + ["--json", "-s", SERVER_URL, "-r", unique_room, "-W", unique_password],
            input=marker + "\n",
            capture_output=True,
            text=True,
            timeout=CLI_SESSION_TIMEOUT,
        )
        assert proc.returncode == 0
        # Same room name and password -> same derived key both times
        key = parse_share_key(json.loads(proc.stdout.splitlines()[0])["url"])

    status, body = _get_bin(unique_room)
    assert status == 200
    plaintext = open_records(key, body)
    assert second.encode() in plaintext, "the second run's output is missing"
    assert first.encode() not in plaintext, "the first run's history was not cleared"
```

There is deliberately no separate WebSocket-level reset test: this one proves
both halves (the server clears, and the client sends the frame once at the right
moment), and a raw-WS test would prove a strict subset. A wrong-password reset
needs no test either — the handshake rejects it before the frame is reachable,
which `e2e/test_ws.py:101` already covers.

- [ ] **Step 2: Run the test to verify it fails**

```bash
cargo build --release
cd e2e && uv run pytest test_agent_read.py -k starts_fresh -v
```

Expected: FAIL on `assert first.encode() not in plaintext`.

- [ ] **Step 3: Add `Rooms::reset`**

In `src/server/rooms.rs`, after `snapshot`:

```rust
    /// Clear a room's history while keeping its claim.
    ///
    /// Unlike [`Rooms::delete`], the room, its password, its size, and
    /// its live segment survive: the caller is a broadcaster starting a
    /// fresh session on a name it already owns. A room that does not
    /// exist is already clear, so that succeeds; a password mismatch is
    /// [`Unauthorized`].
    ///
    /// Only the room's SOLE broadcaster may clear it. The frame carries
    /// no ordering guarantee across connections, and both ways that can
    /// go wrong need a second one attached: a previous session's ingest
    /// task still draining its last frames would append them after the
    /// clear, and a stalled first connection's late reset would wipe
    /// what its own reconnect already stored. Skipping the clear
    /// degrades to appending; the alternative destroys history.
    ///
    /// The size is deliberately kept: another connection sharing this
    /// room re-sends its size only on ITS next reconnect, and the size
    /// message is what tells viewers the stream is encrypted.
    pub fn reset(&self, room: &RoomId, secret: &str) -> Result<(), Unauthorized> {
        let Some(mut entry) = self.inner.get_mut(room) else {
            return Ok(());
        };
        if entry.password != secret {
            return Err(Unauthorized);
        }
        if entry.broadcasters > 1 {
            return Ok(());
        }
        entry.messages = MessageHistory::new(MAX_HISTORY_MESSAGES);
        entry.last_activity = Instant::now();
        Ok(())
    }
```

- [ ] **Step 4: Handle the frame in the ingest loop**

In `src/server/mod.rs`, in the `WsMessage::Text(text)` arm (lines 720-741),
insert after the `delete` block and wrap the existing `size` handling in the
`else`:

```rust
                if body.get("reset").and_then(serde_json::Value::as_bool) == Some(true) {
                    // A returning broadcaster's first connection on a room
                    // it still owns: drop the previous session's history so
                    // the reused name starts clean instead of stacking this
                    // run under the last one's scrollback
                    (state.rooms.reset(&room_id, &secret), false)
                } else {
                    let size = body
                        .get("size")
                        .filter(|s| protocol::size_has_dimensions(s));
                    size.map_or((Ok(()), false), |size| {
                        (ingest(&state, &room_id, &secret, Some(size), None), true)
                    })
                }
```

Update the handler doc at `src/server/mod.rs:633-635` to list the new message:

```rust
/// The fast path: binary frames carry raw terminal bytes; text frames
/// carry JSON control messages (`{"size": {...}}` to resize,
/// `{"reset": true}` to clear a reused room's history at the start of a
/// new session, `{"delete": true}` to delete the room - the retired exit
/// path older clients still send). The room is claimed - or the password
/// verified - at upgrade time, so an unauthorized client is rejected with
/// 401 before the connection is established.
```

- [ ] **Step 5: Send `reset` after the first handshake**

In `src/cli/ws.rs`, replace lines 149-150 of `connect`:

```rust
        transport.socket = Some(transport.handshake()?);
        // A room outlives its broadcaster, and the default password is
        // this machine's id, so rerunning a named room re-claims one
        // that still holds the previous session. Clear it here - on the
        // FIRST connection only, never in `handshake`'s reconnect path,
        // where wiping history would destroy exactly what replay is
        // rebuilding. Best effort: a failed write means the connection
        // died, and the reconnect carries on without the reset.
        let _ = transport.write(Message::text(json!({"reset": true}).to_string()));
        Ok(transport)
```

- [ ] **Step 6: Verify reconnect replay cannot send it**

```bash
grep -n "reset" src/cli/ws.rs
```

Expected: matches only inside `connect` and its comment — nothing in
`handshake` or `flush`.

- [ ] **Step 7: Run the tests**

```bash
cargo build --release
cd e2e && uv run pytest test_agent_read.py -v
cd e2e && uv run pytest test_broadcast.py test_viewer_ws.py test_encryption.py -v
```

Expected: PASS.

- [ ] **Step 8: Lint and commit**

```bash
make lint
git add src/server/rooms.rs src/server/mod.rs src/cli/ws.rs e2e/test_agent_read.py
git commit -m "feat: clear a reused room's history when a new session starts"
```

---

### Task 3: A polled snapshot must not keep a dead room alive forever

`Rooms::snapshot` refreshes `last_activity` (`src/server/rooms.rs:173-174`) and
`/r/:room.bin` calls it (`src/server/mod.rs:828`). Now that rooms outlive
broadcasters, TTL eviction is the only teardown — and an agent polling the
snapshot would reset the TTL on every poll, so the room would never be evicted.
Without this the "expires after 6h idle" line in Task 4's docs is false.

**Files:**
- Modify: `src/server/rooms.rs` (`snapshot`), `src/server/mod.rs:828`
- Test: `e2e/test_room_cleanup.py`

- [ ] **Step 1: Write the failing test**

Read `e2e/test_room_cleanup.py` first: it already spins a `dedicated_server`
with a short `--room-ttl`. Follow that file's existing fixture and helper
idioms exactly, and add:

```python
def test_polling_the_snapshot_does_not_keep_a_room_alive(dedicated_server):
    """A dead broadcast's room expires even while an agent polls it.

    Rooms outlive their broadcaster now, so TTL eviction is the only
    teardown - a snapshot read must not count as activity, or a polled
    room would live forever.
    """
    server = dedicated_server("--room-ttl", "2", "--cleanup-interval", "1")
    room = random_id()
    assert broadcast_message(server.url, room, "pw", "hello") == 200

    # Poll faster than the TTL for longer than the TTL
    deadline = time.time() + 6
    while time.time() < deadline:
        resp = requests.get(f"{server.url}/r/{room}.bin")
        if resp.status_code == 404:
            return
        time.sleep(0.5)
    pytest.fail("room was never evicted while its snapshot was polled")
```

Verify the exact `dedicated_server` signature and the real flag names
(`--room-ttl`, `--cleanup-interval`) against `e2e/conftest.py` and
`src/main.rs` before writing this; adjust the call to match rather than
assuming.

- [ ] **Step 2: Run the test to verify it fails**

```bash
cargo build --release
cd e2e && uv run pytest test_room_cleanup.py -k polling -v
```

Expected: FAIL via `pytest.fail` — every poll refreshes the room.

- [ ] **Step 3: Split the activity refresh out of `snapshot`**

In `src/server/rooms.rs`, replace `snapshot` with a pair sharing a builder:

```rust
    /// Catch-up data for a joining viewer; refreshes the room's
    /// activity, because a viewer watching is a reason to keep the room.
    /// Returns `None` (and creates nothing) when the room does not exist.
    pub fn snapshot(&self, room: &RoomId) -> Option<RoomSnapshot> {
        let mut entry = self.inner.get_mut(room)?;
        entry.last_activity = Instant::now();
        Some(Self::snapshot_of(&entry))
    }

    /// The same data WITHOUT counting as activity, for one-shot reads.
    ///
    /// A room now outlives its broadcaster, so TTL eviction is the only
    /// teardown left: an agent polling `/r/:room.bin` would otherwise
    /// keep a finished broadcast alive forever. A room that is still
    /// live is kept alive by its broadcaster's pings, and a watching
    /// viewer by its own connect, so nothing that should survive
    /// depends on this read.
    pub fn peek(&self, room: &RoomId) -> Option<RoomSnapshot> {
        self.inner.get(room).map(|entry| Self::snapshot_of(&entry))
    }

    fn snapshot_of(room: &Room) -> RoomSnapshot {
        RoomSnapshot {
            size: room.size.clone(),
            history: room.messages.accumulated(),
            broadcasting: room.broadcasters > 0,
        }
    }
```

- [ ] **Step 4: Point `/r/:room.bin` at `peek`**

In `src/server/mod.rs:828`, replace `state.rooms.snapshot(&room_id)` with
`state.rooms.peek(&room_id)`. Leave the viewer-connect call at
`src/server/mod.rs:458` on `snapshot`.

- [ ] **Step 5: Run the tests**

```bash
cargo build --release
cd e2e && uv run pytest test_room_cleanup.py test_agent_read.py -v
```

Expected: PASS.

- [ ] **Step 6: Lint and commit**

```bash
make lint
git add src/server/rooms.rs src/server/mod.rs e2e/test_room_cleanup.py
git commit -m "fix(server): reading a room snapshot no longer refreshes its TTL"
```

---

### Task 4: Documentation

**Files:** `AGENTS.md`, `public/llms.txt`, `CLAUDE.md`

- [ ] **Step 1: Fix the false claim in `AGENTS.md`**

Replace lines 119-120:

```markdown
- Broadcasts are live-only and not recorded; rooms are deleted when the
  broadcast ends (or after a period of inactivity).
```

with:

```markdown
- The link outlives the command. When shellshare exits, the room and its
  recent history stay on the server until it goes idle (6 hours on
  shellshare.net), so a command that finishes in a second still leaves a
  link the user can open. Rerunning the same room name starts a fresh
  session — the previous run's output is cleared, not appended to.
```

- [ ] **Step 2: Add the short-command note to the recipes**

In `AGENTS.md`, after the paragraph ending "Note the `--` separator before the
command.", add:

```markdown
This works for commands that finish immediately, too — `dmesg | shellshare
--json` leaves a working link after `dmesg` exits. You do not need to hold
the process open with a `sleep`.
```

- [ ] **Step 3: Update `public/llms.txt`**

After the `- Errors: ...` bullet (line 21), add:

```
- The link outlives the command: the room and its recent history stay on the
  server until it goes idle (6h), so a short command still leaves a viewable
  link. Rerunning the same room name clears the previous run.
```

- [ ] **Step 4: Update `CLAUDE.md`**

In the `GET /ws/r/:room` route bullet, replace "text frames are control
messages" with:

```markdown
text frames are control messages (`size`; `reset` clears a reused room's
history at the start of a new session, honored only for the room's sole
broadcaster; `delete` is the retired exit path older clients still send)
```

In the `ws.rs` client bullet, append:

```markdown
A session ends by flushing and closing, not by deleting: the room and its
history outlive the process until the server's TTL evicts it, so a short
command still leaves a working link. The first connection (never a
reconnect) sends `reset` so a reused room name starts clean.
```

In the `rooms.rs` server bullet, note that `/r/:room.bin` reads via `peek`,
which does not refresh activity, so polling cannot keep a dead room alive.

- [ ] **Step 5: Verify and commit**

```bash
cd e2e && uv run pytest test_agents.py -v
git add AGENTS.md public/llms.txt CLAUDE.md
git commit -m "docs: the room outlives the broadcaster"
```

---

## Final gates

```bash
make lint
cargo build --release
cd e2e && uv run pytest -n 10
```

All three must be clean.

## Residual risk

- **A room name stays claimed after exit** — until the TTL, not just for the
  session. Intended (squatting protection), and the reason two TTY tests flip
  from asserting 200 to asserting 401.
- **`serve` and `--tunnel` are unchanged.** They host the server in-process, so
  their links still die with the process. Out of scope.
- **`reset` is best-effort.** If the first socket dies before the frame lands,
  or a second broadcaster is attached, the run appends to the previous session
  instead of replacing it. Bounded by the 200-frame history cap.
- **Server memory.** Rooms now live up to the full TTL after their broadcast
  ends, so steady-state memory is higher: roughly rooms-created-per-TTL times
  their history, instead of only live broadcasts.
