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
the `--` separator before the command. `exec` cannot be combined with
`--stdin`.

**Stream a log or pipe** (no PTY, reads stdin until EOF):

```bash
tail -f build.log | shellshare --stdin --json
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
