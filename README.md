# shellshare

[![E2E Tests](https://github.com/vitorbaptista/shellshare/actions/workflows/e2e.yml/badge.svg)](https://github.com/vitorbaptista/shellshare/actions/workflows/e2e.yml)
[![Release](https://github.com/vitorbaptista/shellshare/actions/workflows/release.yml/badge.svg)](https://github.com/vitorbaptista/shellshare/actions/workflows/release.yml)

Broadcast your terminal live to anyone with a link — read-only, one command,
viewers just need a browser.

## Quick start

```bash
npx shellshare
```

Or download the binary directly:

```bash
curl -sLo shellshare https://get.shellshare.net/ && chmod +x shellshare && ./shellshare
```

You'll see a line saying `Sharing terminal in
https://shellshare.net/r/h2Uont4F8bvZ8VDjHb` (your link will be different).
Anyone that opens this link will be able to see what you're doing in your
terminal. When you're done, type `exit` or hit CTRL+D.

## Why shellshare

- **Read-only by design** — viewers can never type into your terminal
- **Viewers only need a browser** — no install, no account; broadcasters run one command
- **No signups, no configuration** — one command in, one URL out
- **Single binary contains client _and_ server** — self-host with
  `shellshare serve`, or go public without shellshare.net via `--tunnel`
- **Free and open source** — Apache-2.0

## Use cases

- **Teach a class or run a workshop**: students follow your terminal on
  their own screens instead of squinting at a projector
- **Live demos and conference talks**: attendees open a URL and watch in
  real time
- **Get or give help**: show a colleague a weird error as it happens,
  instead of ping-ponging commands and pasted output
- **Stream a long-running job**: let teammates (or an AI agent's user)
  watch a build, deploy, or migration as it runs

## How it compares

| You want to... | Use |
|---|---|
| Watch together, live | **shellshare** |
| Let viewers type (pair programming, remote rescue) | [tmate](https://tmate.io), [upterm](https://upterm.dev), [sshx](https://sshx.io) |
| Record now, replay later | [asciinema](https://asciinema.org) or [other terminal recorders](https://github.com/topics/terminal-recording) |
| Full two-way terminal in a web page | [ttyd](https://github.com/tsl0922/ttyd), [gotty](https://github.com/sorenisanerd/gotty) |

## Features

- Read-only, one-to-many live broadcasting to the browser
- Named rooms with passwords (`--room MY-ROOM --password MY-PASS`)
- Viewer color themes (`--theme dracula` — same themes as asciinema)
- Late joiners see recent history, not a blank page
- Network drops are handled: output is buffered and replayed on reconnect
- Linux, macOS (Intel and Apple Silicon), and Windows binaries
- Machine-readable mode for scripts and AI agents (`--json`, `exec`)

### Scripting & AI agents

shellshare is built to be driven by scripts and AI agents — for example, an
agent sharing a live view of a long build with its user. Add `--json` for a
machine-readable contract: the first line on stdout is
`{"event":"sharing","url":"https://shellshare.net/r/..."}` (parse `url` and
hand it to your user), and a final `{"event":"end","exit_code":N}` line is
printed when the broadcast finishes. Errors go to stderr as `ERROR: ...`
with a non-zero exit.

```bash
# Share a single command live; exits with the command's exit code
shellshare exec --json -- npm test

# Stream a log or any pipe (reads stdin until EOF)
tail -f build.log | shellshare --stdin --json
```

See [AGENTS.md](AGENTS.md) (or https://shellshare.net/llms.txt) for the full
agent-facing documentation and recipes.

## Self-hosting

The same `shellshare` binary also includes the server code, allowing you to broadcast your terminal to a server you control.

To do so you just need to run `shellshare server` in one terminal and access [http://localhost:3000](http://localhost:3000). You can broadcast to this server using `shellshare --server http://localhost:3000`.

`shellshare serve` does both at once: it starts a local server in the background and broadcasts your terminal to it.

### Sharing a public link without shellshare.net

Add `--tunnel` to `serve` (or `server`) to expose the local server through a [Cloudflare quick tunnel](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/do-more-with-tunnels/trycloudflare/):

```bash
shellshare serve --tunnel
```

The share link becomes a public `https://*.trycloudflare.com` URL that anyone can open, while your terminal never leaves your machine except through that tunnel. It requires [cloudflared](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/) to be installed (`brew install cloudflared` on macOS) - no Cloudflare account needed. The tunnel closes when shellshare exits.

### Building from source

Requires [Rust](https://rustup.rs/):

```bash
cargo build --release
./target/release/shellshare server
```

This will run the server on [localhost:3000](http://localhost:3000). To
broadcast to this instance, use the `--server` option:

```bash
./target/release/shellshare --server http://localhost:3000
```

## Security model

Data flows one way: from your terminal to the server to the viewers.
Viewers cannot send input. Share links are unguessable (18 random
alphanumerics) but public — anyone with the link can watch, so don't
broadcast secrets. Broadcasts are not recorded: rooms are deleted when
the broadcast ends or after 6 hours of inactivity (server default,
configurable with `--room-ttl`). If you don't want your
bytes to touch shellshare.net at all, self-host (`shellshare serve`,
optionally with `--tunnel`).

## Deploying shellshare.net, analytics, releasing

Maintainer documentation lives in [docs/OPERATIONS.md](docs/OPERATIONS.md).

# License

Copyright 2015 Vitor Baptista

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

  http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
