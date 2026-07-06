# Using shellshare from scripts and AI agents

> Contributing to this repository? See [CLAUDE.md](CLAUDE.md) for build,
> lint, and test instructions. This file documents how to *use* shellshare
> programmatically — e.g. an AI agent sharing a live terminal with its user.

shellshare broadcasts a terminal session to a web link in one command. No
signup, no configuration: run it, parse the link, hand the link to a human.
Viewers see the terminal live in their browser, read-only.

## Install

```bash
npx -y shellshare --help                 # no install (Node.js)
curl -sLo shellshare https://get.shellshare.net/ && chmod +x shellshare   # static binary (auto-detects OS)
```

Binaries exist for Linux x64, macOS x64/arm64, and Windows x64
(`https://get.shellshare.net/?os=linux|mac|mac-arm|windows`).

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
  server sends the catch-up snapshot, then live frames. Binary frames are
  ciphertext records; text frames are control JSON you can ignore.

Both are a sequence of self-delimiting records:
`[u32 BE N][12-byte nonce][ciphertext || 16-byte GCM tag]`, where
`N = 12 + len(ciphertext || tag)`, AES-256-GCM, key = the 64 hex chars
after `#`. Concatenate the decrypted plaintexts to rebuild the terminal
stream; strip ANSI escapes and fold CRLF line endings to LF for a clean
log (the stream is terminal output, so lines end in CRLF - as a PTY
produces - regardless of how the broadcast was fed).

Ready-to-run reference decoders (Node, zero install) are in
[`agent/`](agent/): `decrypt.mjs` (snapshot) and `follow.mjs` (live, with
`--seconds`/`--idle`/`--until` bounds and a background-tail mode).

## Behavior worth knowing

- One-way only: viewers cannot send input to the terminal.
- The share link is unguessable (18 random alphanumerics) but public —
  anyone with the link can watch. Don't broadcast secrets.
- Broadcasts are live-only and not recorded; rooms are deleted when the
  broadcast ends (or after a period of inactivity).
- Late joiners see recent history, so the page is not blank if the user
  opens the link mid-run.
- Transient network failures are handled: output is buffered and replayed
  on reconnect. Only authorization errors (room owned by someone else) are
  fatal.
- `--theme <name>` controls the colors viewers see (e.g. `dracula`,
  `solarized-dark`; see `--help` for the full list).

Machine-readable copy of this document: https://shellshare.net/llms.txt
Source: https://github.com/vitorbaptista/shellshare
