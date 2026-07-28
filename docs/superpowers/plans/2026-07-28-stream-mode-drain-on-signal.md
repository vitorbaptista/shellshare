# Stream-Mode Drain-On-Signal Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `producer | shellshare` stay at the end of the pipe after Ctrl+C until the producer closes stdin, so a terminal interrupt no longer hands the producer a `BrokenPipeError`.

**Architecture:** Ctrl+C in stream mode currently flips one `AtomicBool` that both the read loop and the reader thread treat as "exit now", closing the pipe while the producer is still writing its shutdown lines. Only SIGINT is delivered to the whole foreground process group, so only SIGINT justifies waiting: the handler is split so SIGINT flips `running` on the first press and `forced` on the second, while SIGTERM/SIGHUP flip both at once and keep today's prompt, flushed exit. Stream mode's loop then breaks on stdin EOF or `forced`, never on `running` alone.

**Tech Stack:** Rust (`signal-hook` on Unix, `ctrlc` on Windows, `std::sync::atomic`), Python pytest e2e suite.

## Global Constraints

- No Rust unit tests. E2E tests in `e2e/` are the only safety net (CLAUDE.md).
- `make lint` (clippy pedantic + check) must pass, and the tree must be warning-free at every commit.
- New e2e tests are POSIX-only: `@pytest.mark.skipif(platform.system() == "Windows", ...)`, matching `e2e/test_cli_stdin.py:173`.
- Behaviour change is confined to stream mode (non-TTY stdin, no `exec`). Script/PTY mode must be untouched: `e2e/test_cli_tty.py::TestTtySignals` is the guard.
- Run e2e against a freshly built release binary: `cargo build --release`.

## Decisions taken in review (do not relitigate)

1. **Only SIGINT drains.** SIGTERM/SIGHUP set `running=false` *and* `forced=true`. Rationale differs per signal: SIGTERM arrives alone, so there is nobody to wait for and an unbounded drain would only lose today's clean flush to the supervisor's follow-up SIGKILL. SIGHUP *is* group-delivered like SIGINT (a corrected claim - an earlier draft of this plan said otherwise), but it means the terminal is gone: nobody is watching the link, and the second press that escapes a drain could never be typed, so a drain there risks pinning the process forever. This also means `AGENTS.md` and `public/llms.txt` need no change - an agent's `kill` still stops shellshare immediately.
2. **No timeout.** The escape hatch is a second Ctrl+C, so a slow-but-honest shutdown (honcho waits up to 5s on its children) is never truncated.
3. **Keep the 1s stderr hint,** but with no test of its own - the force-quit test asserts it in passing. A drain with a silent producer is indistinguishable from a hang, and the hint is the only thing that tells the user a second press exists.
4. **The reader thread must survive EINTR.** `signal-hook-registry` installs handlers with `SA_RESTART` (verified in `signal-hook-registry-1.4.8/src/lib.rs:187`), but Rust's `Stdin::read` surfaces `ErrorKind::Interrupted` rather than retrying, and the current `while let Ok(..)` would treat it as EOF - ending the stream on the very signal that starts the drain.

## File Structure

- `src/cli/mod.rs` - the only production file that changes: a new `install_signal_handlers` (cfg-split), the `stream_stdin` call site, and `stream_stdin` itself (lines 362-449).
- `e2e/test_cli_stdin.py` - a new `TestStreamSignals` class plus a module-level pipeline helper.
- `CLAUDE.md` - one sentence in the client section.

---

### Task 1: Split the signal handlers and drain stdin after Ctrl+C

**Files:**
- Modify: `src/cli/mod.rs` (handler at 259-265, call site at 291, `stream_stdin` at 362-449)
- Test: `e2e/test_cli_stdin.py` (new `TestStreamSignals`, two tests)

**Interfaces:**
- Produces: `fn install_signal_handlers(running: &Arc<AtomicBool>, forced: &Arc<AtomicBool>) -> Result<(), Box<dyn std::error::Error>>` and `fn stream_stdin(transport: ws::Transport, running: &AtomicBool, forced: &AtomicBool, echo_stdout: bool)`.

- [ ] **Step 1: Write the failing tests**

Append to `e2e/test_cli_stdin.py`. Add `import os`, `import signal`, `import sys`, `import time` to the existing import block, `import requests`, and add `poll_until` to the `from conftest import (...)` list.

```python
# A producer that prints one line, then - on SIGINT - takes a moment to
# shut down and prints a final line, exactly like honcho/foreman reporting
# "process stopped (rc=)". The delay is the whole bug: an instant print
# lands in the 64K pipe buffer before shellshare can close it, so even the
# unfixed binary relays it and the test would prove nothing. 0.5s is well
# above the unfixed exit (~50-100ms) and below DRAIN_HINT_DELAY (1s), so
# this test never sees the hint.
DYING_PRODUCER = """
import signal, sys, time

def bye(_sig, _frame):
    time.sleep(0.5)
    print("SHUTDOWN-MARKER", flush=True)
    sys.exit(0)

signal.signal(signal.SIGINT, bye)
print("LIVE-MARKER", flush=True)
while True:
    time.sleep(0.05)
"""

# A producer that cannot be interrupted at all, so the drain would never end.
STUCK_PRODUCER = """
import signal, time
signal.signal(signal.SIGINT, signal.SIG_IGN)
print("LIVE-MARKER", flush=True)
while True:
    time.sleep(0.05)
"""


def room_bytes(room):
    """GET /r/<room>.bin -> raw history bytes (b'' when the room is gone)."""
    resp = requests.get(f"{SERVER_URL}/r/{room}.bin", timeout=5)
    return resp.content if resp.status_code == 200 else b""


def start_pipeline(producer_src, room, password):
    """Start `python -c <producer_src> | shellshare` in one process group.

    A terminal delivers Ctrl+C to the whole foreground process group, so the
    test must too: the producer opens a new group and the CLI joins it,
    which makes `os.killpg` an exact stand-in for the keypress. The group is
    a new group in the *same session* - `start_new_session=True` would
    setsid(), and setpgid(2) refuses to move the CLI into a group living in
    another session (EPERM).

    Encryption is off so the test can read the room over plain HTTP instead
    of scraping the share key out of a stderr stream it only drains at the
    end; the drain path is independent of the cipher.

    Returns (producer, cli, pgid). The caller must kill the group.
    """
    producer = subprocess.Popen(
        [sys.executable, "-u", "-c", producer_src],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        preexec_fn=lambda: os.setpgid(0, 0),
    )
    # setpgid(0, 0) runs before exec and Popen blocks until then, so the
    # group exists and its id is the leader's pid.
    pgid = producer.pid
    try:
        cli = subprocess.Popen(
            CLI_COMMAND
            + ["-s", SERVER_URL, "-r", room, "-W", password, "--disable-encryption"],
            stdin=producer.stdout,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            preexec_fn=lambda: os.setpgid(0, pgid),
        )
    except BaseException:
        producer.kill()
        producer.wait()
        raise
    # The parent must drop the read end. While it holds it, the producer
    # never sees EPIPE when the CLI exits - precisely the failure these
    # tests exist to catch.
    producer.stdout.close()
    producer.stdout = None  # so producer.communicate() handles stderr only
    return producer, cli, pgid


def kill_group(pgid):
    """Best-effort teardown - the group is already gone on a passing run."""
    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        pass


@pytest.mark.skipif(
    platform.system() == "Windows",
    reason="process groups and SIGINT are POSIX-only",
)
class TestStreamSignals:
    """Signals at the end of a pipe, exactly as a terminal delivers them."""

    def test_sigint_drains_producer_shutdown_output(
        self, unique_room, unique_password
    ):
        """Ctrl+C must not close the pipe under a producer that is still
        writing: shellshare keeps reading until EOF, so the producer exits
        cleanly and its dying words are broadcast."""
        producer, cli, pgid = start_pipeline(
            DYING_PRODUCER, unique_room, unique_password
        )
        try:
            assert poll_until(
                lambda: b"LIVE-MARKER" in room_bytes(unique_room), timeout=15
            ), "the producer's first line never reached the room"

            os.killpg(pgid, signal.SIGINT)

            cli.communicate(timeout=15)
            assert cli.returncode == 0, "CLI did not exit cleanly"
            _, producer_err = producer.communicate(timeout=10)
            assert producer.returncode == 0, (
                f"producer exited {producer.returncode}; stderr={producer_err!r}"
            )
            assert "BrokenPipeError" not in producer_err, (
                f"producer hit a broken pipe: {producer_err!r}"
            )
            # shutdown()'s ack drain gives up after 1s, so the last frame
            # can still be in flight when the process is reaped.
            assert poll_until(
                lambda: b"SHUTDOWN-MARKER" in room_bytes(unique_room), timeout=5
            ), "the producer's shutdown output was not broadcast"
        finally:
            kill_group(pgid)
            producer.wait()
            cli.wait()

    def test_second_sigint_force_quits_a_stuck_producer(
        self, unique_room, unique_password
    ):
        """A producer that ignores SIGINT must not pin shellshare forever:
        the first press starts an open-ended drain (explained on stderr),
        the second leaves anyway."""
        producer, cli, pgid = start_pipeline(
            STUCK_PRODUCER, unique_room, unique_password
        )
        try:
            assert poll_until(
                lambda: b"LIVE-MARKER" in room_bytes(unique_room), timeout=15
            ), "the producer's first line never reached the room"

            os.killpg(pgid, signal.SIGINT)
            # Load-bearing in two ways: it outlasts DRAIN_HINT_DELAY (1s) so
            # the hint has fired, and it keeps the two presses from being
            # coalesced - signals are not queued, so back-to-back SIGINTs
            # can collapse into one and `forced` would never be set.
            time.sleep(2)
            assert cli.poll() is None, (
                "CLI exited on the first signal instead of draining"
            )

            os.killpg(pgid, signal.SIGINT)
            _, cli_err = cli.communicate(timeout=10)
            assert cli.returncode == 0, (
                "a second Ctrl+C did not force-quit the CLI"
            )
            assert "Press Ctrl+C again to quit now." in cli_err, (
                "the drain never explained itself on stderr"
            )
        finally:
            kill_group(pgid)
            producer.wait()
            cli.wait()
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
cargo build --release && cd e2e && uv run pytest test_cli_stdin.py::TestStreamSignals -x -v
```

Expected: FAIL. In the first test the producer exits 1 with `BrokenPipeError` in stderr (the exception escapes its SIGINT handler) and `SHUTDOWN-MARKER` never reaches the room; the second fails at `cli.poll() is None` because today's CLI exits on the first signal.

- [ ] **Step 3: Split the signal handlers**

Replace `src/cli/mod.rs:259-265` with:

```rust
    // A termination signal only flips flags; the loops below own the
    // transport and flush on their way out.
    let running = Arc::new(AtomicBool::new(true));
    let forced = Arc::new(AtomicBool::new(false));
    install_signal_handlers(&running, &forced)?;
```

Add above `stream_stdin`:

```rust
/// Install the process-wide termination handlers.
///
/// Ctrl+C is the odd one out: the terminal delivers SIGINT to the whole
/// foreground process group, so in a pipe the producer is dying of the same
/// signal and its last bytes are still worth relaying - `stream_stdin` keeps
/// draining and only a second press cuts that short. SIGTERM and SIGHUP
/// arrive at us alone (a supervisor, a closed terminal): there is nobody to
/// wait for and no keyboard to escape with, so they stop immediately, which
/// is also what they did before the drain existed.
#[cfg(unix)]
fn install_signal_handlers(
    running: &Arc<AtomicBool>,
    forced: &Arc<AtomicBool>,
) -> Result<(), Box<dyn std::error::Error>> {
    use signal_hook::consts::{SIGHUP, SIGINT, SIGTERM};
    use signal_hook::iterator::Signals;

    let mut signals = Signals::new([SIGINT, SIGTERM, SIGHUP])?;
    let running = running.clone();
    let forced = forced.clone();
    thread::spawn(move || {
        for signal in signals.forever() {
            let already_stopping = !running.swap(false, Ordering::SeqCst);
            if already_stopping || signal != SIGINT {
                forced.store(true, Ordering::SeqCst);
            }
        }
    });
    Ok(())
}

/// Windows delivers Ctrl+C to every process attached to the console, so it
/// gets the same drain-then-force treatment as SIGINT.
#[cfg(not(unix))]
fn install_signal_handlers(
    running: &Arc<AtomicBool>,
    forced: &Arc<AtomicBool>,
) -> Result<(), Box<dyn std::error::Error>> {
    let running = running.clone();
    let forced = forced.clone();
    ctrlc::set_handler(move || {
        if !running.swap(false, Ordering::SeqCst) {
            forced.store(true, Ordering::SeqCst);
        }
    })?;
    Ok(())
}
```

- [ ] **Step 4: Thread `forced` into `stream_stdin`**

Call site (`src/cli/mod.rs:291`):

```rust
        stream_stdin(transport, &running, &forced, !args.json);
```

Signature (`src/cli/mod.rs:380`) - plain `&AtomicBool`, since neither flag is cloned inside any more:

```rust
fn stream_stdin(
    mut transport: ws::Transport,
    running: &AtomicBool,
    forced: &AtomicBool,
    echo_stdout: bool,
) {
```

- [ ] **Step 5: Make the reader thread outlive the signal**

Delete the `reader_running` clone (`src/cli/mod.rs:392`) and replace the read loop body:

```rust
    thread::spawn(move || {
        let mut buffer = [0u8; 4096];
        let mut stdout = io::stdout();
        loop {
            let bytes_read = match io::stdin().read(&mut buffer) {
                Ok(0) => break, // EOF
                Ok(n) => n,
                // signal-hook asks for SA_RESTART, but `Stdin::read`
                // surfaces an interrupted read instead of retrying it, and
                // treating that as EOF would end the stream on the very
                // signal that starts the drain
                Err(e) if e.kind() == io::ErrorKind::Interrupted => continue,
                // Any other read error ends the stream, same as EOF: the
                // sender sees the dropped channel and shuts down cleanly
                Err(_) => break,
            };
            let chunk = &buffer[..bytes_read];

            if echo_stdout {
                // Fire-and-forget: flush so `--follow` streams live; a dead
                // downstream just means the local echo stops, not the broadcast.
                let _ = stdout.write_all(chunk);
                let _ = stdout.flush();
            }

            // Deliberately no `running` check: Ctrl+C starts a drain, and
            // quitting here would discard the very bytes it waits for. A
            // forced exit drops the receiver, which ends this thread.
            if tx.send(chunk.to_vec()).is_err() {
                break;
            }
        }
    });
```

- [ ] **Step 6: Break the loop on `forced`, and explain a long drain**

Add `Instant` to the time import (`src/cli/mod.rs:15`): `use std::time::{Duration, Instant};`

Next to `IDLE_TICK` in `stream_stdin`:

```rust
    /// How long a drain runs before it explains the wait. Long enough that
    /// a producer dying promptly - the normal case - stays silent.
    const DRAIN_HINT_DELAY: Duration = Duration::from_secs(1);
```

Before the loop:

```rust
    let mut signalled_at: Option<Instant> = None;
    let mut hinted = false;
```

Replace the guard at `src/cli/mod.rs:420-423`:

```rust
    loop {
        // Only a forced exit leaves early - a second Ctrl+C, or a SIGTERM /
        // SIGHUP, which never wait. A first Ctrl+C reached the whole
        // pipeline, so the producer is on its way out and its last bytes
        // are still worth relaying; EOF is the normal exit.
        if forced.load(Ordering::SeqCst) {
            break;
        }

        // A drain with a silent producer is indistinguishable from a hang,
        // so say what is happening - once, and only if the wait is long
        // enough to be noticed.
        if !hinted && !running.load(Ordering::SeqCst) {
            let since = *signalled_at.get_or_insert_with(Instant::now);
            if since.elapsed() >= DRAIN_HINT_DELAY {
                hinted = true;
                eprintln!("Waiting for input to finish. Press Ctrl+C again to quit now.");
            }
        }
```

Finally fix the now-inverted sentence in the doc comment at `src/cli/mod.rs:378-379`. Replace:

> It also makes Ctrl+C land promptly rather than waiting for the producer's next line.

with:

> It also keeps a drain responsive: a second Ctrl+C lands within a tick instead of after the producer's next line.

- [ ] **Step 7: Run the tests to verify they pass**

```bash
cargo build --release && cd e2e && uv run pytest test_cli_stdin.py::TestStreamSignals -v
```

Expected: PASS (both).

- [ ] **Step 8: Prove the split - SIGTERM still exits promptly**

Add to `TestStreamSignals`. This is the regression guard for the decision above, and the reason the handler is cfg-split at all:

```python
    def test_sigterm_does_not_wait_for_the_producer(
        self, unique_room, unique_password
    ):
        """Only SIGINT reaches the whole pipeline. A SIGTERM aimed at
        shellshare alone leaves nobody to wait for and no keyboard to
        escape with, so it must still exit at once - a supervisor's
        follow-up SIGKILL would otherwise cut the flush short."""
        producer, cli, pgid = start_pipeline(
            STUCK_PRODUCER, unique_room, unique_password
        )
        try:
            assert poll_until(
                lambda: b"LIVE-MARKER" in room_bytes(unique_room), timeout=15
            ), "the producer's first line never reached the room"

            cli.terminate()  # SIGTERM to the CLI only, not the group
            cli.communicate(timeout=10)
            assert cli.returncode == 0, "SIGTERM did not stop the CLI"
        finally:
            kill_group(pgid)
            producer.wait()
            cli.wait()
```

- [ ] **Step 9: Run the full guard set**

```bash
make lint
cargo build --release && cd e2e && uv run pytest test_cli_stdin.py test_cli_tty.py test_agents.py -n 6
```

Expected: clean lint with no warnings, all green. `test_cli_tty.py::TestTtySignals` proves script mode still exits on the first SIGINT.

---

### Task 2: Document the rule

**Files:** Modify `CLAUDE.md` (Client section, `mod.rs` bullet)

- [ ] **Step 1: Add the sentence**

> In stream mode Ctrl+C starts a drain instead of an exit: SIGINT reaches the whole pipeline, so shellshare keeps relaying until the producer closes stdin - otherwise the producer's shutdown output dies with a broken pipe. A second Ctrl+C leaves immediately, and SIGTERM/SIGHUP (which arrive alone, with no keyboard to escape with) still exit at once.

- [ ] **Step 2: Confirm no lockstep drift**

`AGENTS.md` and `public/llms.txt` describe a pipe as reading "until EOF" and document no signal behaviour; `kill` on a shellshare process is unchanged by this work, so neither file needs an edit. Re-read both to confirm before closing this task.

- [ ] **Step 3: Full suite**

```bash
cargo build --release && cd e2e && uv run pytest -n 10
```

## Changed during the adversarial pass

The task steps above describe the first implementation. Two reviewers with reproductions changed it; the shipped code is the source of truth, and these are the deltas worth knowing:

- **SIGHUP rationale corrected** (see decision 1). The doc comment and `CLAUDE.md` no longer claim SIGHUP "arrives alone".
- **The tests did not pin the decisions.** Mutation testing showed an implementation that drained *every* signal with a 5s cap passed all three original tests and the full 252-test suite. Fixed by measuring what the decisions actually promise: the force-quit test now proves the drain survives 6s (past honcho's own 5s child grace) instead of sleeping 2s, and the targeted-signal test asserts the exit took under 3s instead of merely completing.
- **SIGHUP gained coverage** by parametrizing the targeted-signal test over `SIGTERM`/`SIGHUP` - it was the untested path with the worst failure mode.
- **`ctrlc` moved to `[target.'cfg(not(unix))'.dependencies]`**, since signal-hook now owns Unix and the crate's `termination` feature was Unix-only, i.e. entirely dead. Verified dropped from the host build via `cargo tree`; the Windows arm is only exercised by CI.
- **The EINTR arm is documented as defensive**, not load-bearing: `SA_RESTART` means it is unreachable on Linux (proven - mutating `continue` to `break` changes no test).

## Known limits (accepted, not bugs to fix here)

- **Two SIGINTs delivered within the same microseconds coalesce** and the force-quit never arms (measured: 19/20 at a 0-second gap, 0/20 at 1ms). A keyboard is always milliseconds apart; a programmatic sender should leave a gap. The alternative - `signal_hook::flag::register_conditional_shutdown` - escalates from inside the handler but skips `transport.shutdown()` and the `--json` `end` event, which is a worse trade.
- **A wedged server can still make the process unkillable.** `ws.rs`'s handshake has no connect or read timeout, so a server that accepts TCP and never answers parks the main loop where no flag is observed. Pre-existing and unchanged by this work (the old `ctrlc` handler swallowed the same signals), but the new hint's promise of "press Ctrl+C again" would be false in that state. Fixing it means adding timeouts in `ws.rs` - a separate change.

## Self-Review

- **Coverage:** SIGINT drain (Task 1 steps 3-6, test 1), second-press escape + hint (step 6, test 2), SIGTERM/SIGHUP unchanged (step 3, test 3), EINTR survival (step 5), docs (Task 2). Complete.
- **Placeholders:** none.
- **Type consistency:** `install_signal_handlers(&Arc<AtomicBool>, &Arc<AtomicBool>) -> Result<(), Box<dyn Error>>` matches `run`'s error type; `stream_stdin`'s `&AtomicBool` parameters accept `&running`/`&forced` by deref coercion from `Arc`; `room_bytes`, `start_pipeline`, `kill_group`, `DYING_PRODUCER`, `STUCK_PRODUCER` are each defined once and reused by name.
