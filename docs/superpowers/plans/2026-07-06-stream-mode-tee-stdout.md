# Stream-Mode Tee Passthrough Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When bare `shellshare` auto-detects piped stdin (e.g. `journalctl --follow | npx shellshare`), echo the stdin bytes to stdout as they are broadcast — `tee`-style — so the local pipeline keeps working and the user still sees their output.

**Architecture:** The only behavioral gap is in `stream_stdin` (`src/cli/mod.rs`), which today reads stdin → sends to the transport but writes nothing to stdout. We add a plaintext echo of each chunk to stdout, gated off when `--json` is active (that mode reserves stdout for the newline-delimited event protocol). One focused change to one function plus its one caller.

**Tech Stack:** Rust, std `io` (`Write`, `stdout`).

## Global Constraints

- Add deps only via `cargo add` (none needed here).
- `make lint` (pedantic clippy) must pass; never disable a lint to dodge it.
- E2E suite (`e2e/`, pytest + Playwright) is the only safety net — a release binary is required (`cargo build --release`), suite starts its own servers.
- The `--json` contract is a lockstep invariant (`AGENTS.md`, `public/llms.txt`, `e2e/test_agents.py`): stdout carries only `sharing` … `end` JSON events; errors on stderr. Teeing must NOT violate it.

---

### Task 1: Tee stdin to stdout in stream mode

**Files:**
- Modify: `src/cli/mod.rs` — `stream_stdin` (currently ~340-369) and its single call site (~288)
- Test: `e2e/test_cli_stdin.py`

**Interfaces:**
- Consumes: `ws::Transport`, `Arc<AtomicBool>` (unchanged signature inputs)
- Produces: `fn stream_stdin(transport: ws::Transport, running: &Arc<AtomicBool>, echo_stdout: bool)` — new third param. Caller passes `!args.json`.

**Design decisions (locked):**
- **stdout, not stderr.** Classic `tee` writes the passthrough to stdout; the sharing prose already goes to stderr in stream mode, so stdout is otherwise clean. Writing to both would double output when both are a TTY.
- **Plaintext echo.** `buffer[..n]` is the raw stdin bytes (plaintext). `transport.send` seals for E2EE internally; echoing `buffer` gives the local viewer plaintext, which is the whole point. Order: read → echo → send.
- **Gate on `!args.json`.** In `--json` stream mode stdout carries the event protocol; echoing log bytes there would corrupt it. So echo only when not JSON.
- **Broken-pipe tolerance.** If stdout write fails (downstream closed, e.g. `| head`), stop echoing but keep broadcasting — the broadcast is the reason the command was run. Swallow the error, flip a local flag off, continue the loop.
- **Flush per chunk** so `--follow` streams live (a partial line without `\n` would otherwise sit in the LineWriter buffer).

- [ ] **Step 1: Write the failing test**

Add to `e2e/test_cli_stdin.py` in `TestBasicFunctionality`:

```python
    def test_stdin_is_teed_to_stdout(self, unique_room, unique_password, socket_listener):
        """Stream mode echoes stdin to stdout (tee), so the local pipeline
        keeps seeing output while it is broadcast."""
        marker = f"TEE-{random_id(6)}"
        returncode, stdout, stderr = run_cli_stdin(
            marker + "\n", unique_room, unique_password
        )
        assert returncode == 0, f"CLI failed: {stderr}"
        # Echoed locally...
        assert marker in stdout, f"stdin was not teed to stdout; stdout={stdout!r}"
        # ...and still broadcast.
        socket_listener.set_key(parse_share_key(stderr))
        received = socket_listener.wait_for_message(timeout=5, containing=marker)
        assert received is not None and marker in received
```

Add to `TestArgumentParsing` (or a JSON-focused class) — proves the contract is not corrupted:

```python
    def test_json_mode_does_not_tee_to_stdout(self, unique_room, unique_password):
        """--json reserves stdout for the event protocol: stdin is NOT echoed
        there, so every stdout line stays parseable JSON."""
        import json
        marker = f"NOTEE-{random_id(6)}"
        args = CLI_COMMAND + ["-s", SERVER_URL, "-r", unique_room, "-W", unique_password, "--json"]
        proc = subprocess.Popen(
            args, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True,
        )
        stdout, stderr = proc.communicate(input=marker + "\n", timeout=30)
        assert proc.returncode == 0, f"CLI failed: {stderr}"
        # The raw stdin marker must NOT appear on stdout...
        assert marker not in stdout, f"json stdout was polluted by teed stdin: {stdout!r}"
        # ...and every non-empty stdout line is valid JSON.
        for line in stdout.splitlines():
            if line.strip():
                json.loads(line)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cargo build --release && cd e2e && uv run pytest test_cli_stdin.py -k "teed_to_stdout or json_mode_does_not_tee" -v`
Expected: `test_stdin_is_teed_to_stdout` FAILS (marker absent from stdout); `test_json_mode_does_not_tee_to_stdout` PASSES already (json mode never wrote stdin to stdout). The JSON test is a regression guard for Step 3.

- [ ] **Step 3: Implement the tee**

In `src/cli/mod.rs`, change the call site (in the `stream_mode` branch):

```rust
        // Read from stdin, tee to stdout (unless --json owns stdout), stream to server
        stream_stdin(transport, &running, !args.json)?;
```

Rewrite `stream_stdin`:

```rust
/// Stream stdin to the server: relay raw bytes until EOF. When `echo_stdout`
/// is set, also tee each chunk to stdout (plaintext) so the local pipeline
/// keeps working - `journalctl --follow | shellshare` still shows/forwards
/// its output. `--json` clears the flag because that mode reserves stdout
/// for the event protocol.
fn stream_stdin(
    mut transport: ws::Transport,
    running: &Arc<AtomicBool>,
    echo_stdout: bool,
) -> Result<(), Box<dyn std::error::Error>> {
    let mut buffer = [0u8; 4096];
    let mut stdout = io::stdout();
    // Broken downstream pipe (e.g. `| head`) must not abort the broadcast:
    // once a stdout write fails we stop echoing but keep streaming.
    let mut echo = echo_stdout;

    loop {
        if !running.load(Ordering::SeqCst) {
            break;
        }

        let bytes_read = io::stdin().read(&mut buffer)?;
        if bytes_read == 0 {
            // EOF
            break;
        }
        let chunk = &buffer[..bytes_read];

        if echo && (stdout.write_all(chunk).is_err() || stdout.flush().is_err()) {
            echo = false;
        }

        let size = get_terminal_size();

        if let Err(e) = transport.send(chunk, size) {
            eprintln!("\r\nERROR: {e}");
            eprintln!("\rERROR: Exit shellshare and try again later.");
            // The room belongs to someone else now; it is not ours to
            // delete, so skip the shutdown cleanup
            return Ok(());
        }
    }

    transport.shutdown_and_delete();
    Ok(())
}
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cargo build --release && cd e2e && uv run pytest test_cli_stdin.py -k "teed_to_stdout or json_mode_does_not_tee" -v`
Expected: both PASS.

- [ ] **Step 5: Lint**

Run: `make lint`
Expected: clean (no clippy warnings, no errors).

- [ ] **Step 6: Update docs (lockstep contract surfaces)**

`README.md:59`, `AGENTS.md` (~46), `public/llms.txt:24` describe the stream/pipe pattern. Add a short note that non-`--json` stream mode tees stdin to stdout (`tee`-style) so the local pipeline is preserved, and that `--json` does not (stdout is the event channel). Keep wording tight; match each file's existing voice.

- [ ] **Step 7: Full suite + commit**

Run: `cd e2e && uv run pytest -n 10`
Expected: green.

```bash
git add src/cli/mod.rs e2e/test_cli_stdin.py README.md AGENTS.md public/llms.txt
git commit -m "feat: tee piped stdin to stdout in stream mode"
```

---

## Self-Review

- **Spec coverage:** Request = "when piped like `journalctl -f | npx shellshare`, echo stdin to stdout like tee." Task 1 does exactly that. "maybe stderr as well" → resolved to stdout-only (tee convention; avoids doubling on a TTY) — a deliberate deviation, noted for review.
- **Placeholder scan:** none — full code shown.
- **Type consistency:** new signature `stream_stdin(_, _, echo_stdout: bool)` matches the single call site `!args.json`.
- **Invariant at risk:** `--json` stdout contract — guarded by the gate and by `test_json_mode_does_not_tee_to_stdout`.
