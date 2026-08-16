# Using shellshare from scripts and AI agents

> Contributing to this repository? See [CLAUDE.md](CLAUDE.md) for build,
> lint, and test instructions. This file documents how to *use* shellshare
> programmatically — e.g. an AI agent sharing a live terminal with its user.

shellshare broadcasts a terminal session to a web link in one command. No
signup, no configuration: run it, parse the link, hand the link to a human.
Viewers see the terminal live in their browser, read-only.

## Install

```bash
npx shellshare --help                    # no install (Node.js)
curl -sLo shellshare https://get.shellshare.net/ && chmod +x shellshare   # static binary (auto-detects OS)
```

Binaries exist for Linux x64/arm64, macOS x64/arm64, and Windows x64
(`https://get.shellshare.net/?os=linux|linux-arm|mac|mac-arm|windows`).

## The machine-readable contract: `--json`

With `--json`, shellshare prints newline-delimited JSON events to stdout:

- First line, before any terminal output:
  `{"event":"sharing","room":"<room>","server":"https://shellshare.net","url":"https://shellshare.net/r/<room>"}`
- Last line: `{"event":"end","exit_code":0}`

Errors are printed to stderr as `ERROR: ...` and the process exits non-zero.
Parse the `url` field from the first stdout line — that is the link to give
to your user.

## Recipes

**Share one command and exit when it finishes** (the usual agent case —
e.g. let the user watch a long build, test run, or migration live):

```bash
shellshare exec --json -- npm test
```

`exec` runs the command in a PTY, streams it live, and exits with the
command's exit code (a signal-killed command reports exit code 1). Note
the `--` separator before the command.

This works for commands that finish immediately, too — `dmesg | shellshare
--json` leaves a working link after `dmesg` exits. You do not need to hold
the process open with a `sleep`.

**Stream a log or pipe** (no PTY; a non-TTY stdin auto-detects this and reads
stdin until EOF):

```bash
tail -f build.log | shellshare --json
```

**Background it and capture the URL while you keep working:**

```bash
shellshare exec --json -- ./long-task.sh </dev/null >/tmp/ss.out 2>/tmp/ss.err &
until URL=$(head -1 /tmp/ss.out | jq -re .url) 2>/dev/null; do sleep 0.2; done
echo "Watch live: $URL"
```

(The `</dev/null` matters when backgrounding from an interactive shell:
without it the broadcast keeps the TTY as stdin and job control suspends
it with SIGTTIN.)

**Stable room name across restarts** (same link every time):

```bash
shellshare exec --json -r my-room -W my-password -- make deploy
```

Without `-W`, the machine's MAC address is the password, so the same room
is only reclaimable from the same machine.

**Fully local / private** — `shellshare serve` runs the broadcast through an
embedded server on localhost (nothing leaves the machine); add `--tunnel` to
get a public `https://*.trycloudflare.com` link without using shellshare.net
(requires `cloudflared` installed).

**Recover the link from inside a session** — `shellshare status` re-prints
the current broadcast's link (and QR code). With `--json` it prints a single
object `{"url":"...","room":"..."}`; outside a live session it prints
`ERROR: ...` to stderr and exits non-zero. It reads the link from the
`SHELLSHARE_URL` environment variable the broadcaster exports into the shell
it spawns, so it only works from within that shell.

## Reading a broadcast (read-only consumer)

The inverse of broadcasting: an agent can *watch* a shellshare broadcast
read-only — e.g. tail a build or dev server you started, or watch a
human's terminal — without the shellshare CLI. Given a share link
`https://shellshare.net/r/<room>#<key>`, the key after `#` is a URL
fragment the server never sees, so you decrypt locally (Node's built-in
`crypto`/`WebSocket`, no install):

- **Snapshot** (current state): `GET /r/<room>.bin` returns the room's
  accumulated history as raw bytes — opaque ciphertext, since the server
  is an end-to-end-encryption-blind relay. Decrypt with the `#` key.
- **Follow** (live): open a WebSocket to `/ws/v/r/<room>`. On connect the
  server sends the catch-up snapshot (the `size` control frame, the room's
  history, then `broadcasting` and `usersCount`), and live frames after
  that. Binary frames are ciphertext records; text frames are control JSON
  you can ignore.

Pick one; do not chain them. Following already begins with the history that
`.bin` would have handed you, so snapshot-then-follow prints it twice. The
same replay happens on every reconnect — deliberately, because it makes a
dropped connection a clean resync rather than a gap, which is why the viewer
page wipes its screen on rejoin — so a reconnect means "discard and start
over", not "continue where I left off". `usersCount` is the last frame of
the replay and marks where live output begins. Delivery is at-least-once,
not exactly-once: a frame broadcast just as you connect can arrive both in
the history and as a live frame.

Both are a sequence of self-delimiting records:
`[u32 BE N][12-byte nonce][ciphertext || 16-byte GCM tag]`, where
`N = 12 + len(ciphertext || tag)`, AES-256-GCM, key = the 64 hex chars
after `#`. Concatenate the decrypted plaintexts to rebuild the terminal
stream; strip ANSI escapes and fold CRLF line endings to LF for a clean
log (the stream is terminal output, so lines end in CRLF - as a PTY
produces - regardless of how the broadcast was fed).

You do not have to implement any of that. One reader (Node, zero
install) does it — [`templates/agent.mjs`](templates/agent.mjs), inlined
into [`/llms.txt`](https://shellshare.net/llms.txt) and into every room
page. Save it as `agent.mjs` and run it:

```bash
node agent.mjs '<url>'            # the history so far, then exit
node agent.mjs '<url>' --follow   # history, then live until it ends
                                  # (--follow needs Node >= 22)
```

It has two modes and no other flags on purpose: it writes plain text on
stdout, and your shell composes the rest better than options would.

```bash
timeout 60 node agent.mjs '<url>' --follow          # bound the wait
node agent.mjs '<url>' --follow | grep -m1 'DONE'   # wait for a marker
node agent.mjs '<url>' | tail -40                   # just the tail
```

`--follow` ends by itself when the broadcaster leaves, which is usually
the thing you were waiting for. One caveat worth knowing: it cannot
notice a closed pipe until it next writes, so a `grep -m1` that matches
just before output goes quiet will not return until the broadcast ends —
pair it with `timeout` when that matters. Exit 0 on success, 1 on a
usage error, an unreachable server, or a key that does not match.

If you have a share link, fetching it is enough on its own — the room
page carries the reader. It lives in a `<pre>`, so `&` and `<` are HTML
entities in the markup; anything that parses the HTML (a fetch tool that
returns text or markdown, a browser) hands you the code already decoded.
Only if you are reading the raw bytes do you need to unescape them — or
take `/llms.txt`, which carries the same file verbatim.

## Behavior worth knowing

- One-way only: viewers cannot send input to the terminal.
- The share link is unguessable (18 random alphanumerics) but public —
  anyone with the link can watch. Don't broadcast secrets.
- The link outlives the command. When shellshare exits, the room and its
  recent history stay on the server until it goes idle (6 hours on
  shellshare.net), so a command that finishes in a second still leaves a
  link the user can open. Rerunning the same room name starts a fresh
  session — the previous run's output is cleared, not appended to.
- The room name stays claimed until the room goes idle, so a name you
  used is yours (and only yours) for that whole window. Another machine
  asking for it gets an authorization error, not a shared room.
- `shellshare serve` and `serve --tunnel` are the exception: they host
  the server inside the same process, so their links do die when the
  command finishes.
- Late joiners see recent history, so the page is not blank if the user
  opens the link mid-run.
- Transient network failures are handled: output is buffered and replayed
  on reconnect. Only authorization errors (room owned by someone else) are
  fatal.
- `--theme <name>` controls the colors viewers see (e.g. `dracula`,
  `solarized-dark`; see `--help` for the full list).

Machine-readable copy of this document: https://shellshare.net/llms.txt
Source: https://github.com/vitorbaptista/shellshare
