# shellshare

[![E2E Tests](https://github.com/vitorbaptista/shellshare/actions/workflows/e2e.yml/badge.svg)](https://github.com/vitorbaptista/shellshare/actions/workflows/e2e.yml)
[![Release](https://github.com/vitorbaptista/shellshare/actions/workflows/release.yml/badge.svg)](https://github.com/vitorbaptista/shellshare/actions/workflows/release.yml)

Live broadcast of terminal sessions.

## Why?

Ever wanted to quickly show what you're doing to some friends? Maybe you're seeing a weird error and would like some help. Or the other way around: some friend of yours is asking for help on something, then you start to ping-pong: you tell a command, he pastes the output, then you tell another, and so on...

The objective of [shellshare.net](https://shellshare.net) is to provide an easy way to broadcast your terminal live. No signups, no configurations, anything: simply run a command and you're good to go.

## Using

Copy and paste the following line in your terminal:

```bash
curl -sLo shellshare https://get.shellshare.net/ && chmod +x shellshare && ./shellshare
```

If you have Node.js, you can also run it with no manual download on Linux, macOS, or Windows:

```bash
npx shellshare
```

You'll see a line saying `Sharing session in
https://shellshare.net/r/h2Uont4F8bvZ8VDjHb` (your link will be different).
Anyone that opens this link will be able to see what you're doing in your
terminal. When you're done, type `exit` or hit CTRL+D.

Broadcasts are end-to-end encrypted. The decryption key travels in the part
of the link after the `#`, which your browser never sends to the server, so
only the people you hand the full link to can watch, not the server and not
your network. The key is derived from your computer and the room name, so
re-broadcasting to a named room from the same machine reproduces the same
link, and a link you've already shared keeps working.

Lost the link after it scrolled off screen? From inside the shared shell,
run `shellshare status` to re-print the link and its QR code. It reads the
link from the environment of the session you're in, so it only works from
within a live broadcast — and nothing is written to disk.

You can also pipe a command or log straight into shellshare instead of
sharing a shell — a non-TTY stdin auto-detects this and reads until EOF. It
behaves like `tee`: the stream is broadcast *and* echoed to your own stdout,
so you still see it locally (or pass it on to the next command in the pipe).

```bash
journalctl --follow | shellshare
```

### Scripting & AI agents

shellshare is built to be driven by scripts and AI agents — for example, an
agent sharing a live view of a long build with its user. Add `--json` for a
machine-readable contract: the first line on stdout is
`{"event":"sharing", ..., "url":"https://shellshare.net/r/..."}` (parse `url`
and hand it to your user), and a final `{"event":"end","exit_code":N}` line is
printed when the broadcast finishes. Errors go to stderr as `ERROR: ...`
with a non-zero exit.

```bash
# Share a single command live; exits with the command's exit code
shellshare exec --json -- npm test

# Stream a log or any pipe (a non-TTY stdin auto-detects this, reads until EOF)
tail -f build.log | shellshare --json
```

See [AGENTS.md](AGENTS.md) (or https://shellshare.net/llms.txt) for the full
agent-facing documentation and recipes.

### Sharing from herdr

If you run your terminals in [herdr](https://herdr.dev), this repo doubles
as a herdr plugin: `herdr plugin install vitorbaptista/shellshare` adds one
action that broadcasts your whole herdr session, read-only, as a shellshare
link — viewers see whichever tab you are working in. (To share a single
pane, just run `shellshare` in it.) See
[herdr-plugin/README.md](herdr-plugin/README.md).

### Hosting a server

The same `shellshare` binary also includes the server code, allowing you to broadcast your terminal to a server you control.

To do so you just need to run `shellshare server` in one terminal and access [http://localhost:3000](http://localhost:3000). You can broadcast to this server using `shellshare --server http://localhost:3000`.

`shellshare serve` does both at once: it starts a local server in the background and broadcasts your terminal to it.

End-to-end encryption only works over HTTPS or localhost. If your viewers reach a self-hosted server over plain HTTP, for example a teacher sharing their terminal with students on a lab's local network, broadcast with `--disable-encryption` to send in plaintext instead.

### Sharing a public link without shellshare.net

Add `--tunnel` to `serve` (or `server`) to expose the local server through a [Cloudflare quick tunnel](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/do-more-with-tunnels/trycloudflare/):

```bash
shellshare serve --tunnel
```

The share link becomes a public `https://*.trycloudflare.com` URL that anyone can open, while your terminal never leaves your machine except through that tunnel. It requires [cloudflared](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/) to be installed (`brew install cloudflared` on macOS) - no Cloudflare account needed. The tunnel closes when shellshare exits.

## Installing

Requires [Rust](https://rustup.rs/) to build from source:

```bash
cargo build --release
./target/release/shellshare server
```

This will run the server on [localhost:3000](http://localhost:3000). To
broadcast to this instance, use the `--server` option:

```bash
./target/release/shellshare --server http://localhost:3000
```

## Deploy

To deploy with [Dokku](https://dokku.com/), let it build the image from
source on each push using the project's `Dockerfile`:

```bash
# Create the app
dokku apps:create shellshare

# Build from the source Dockerfile (this is also Dokku's default)
dokku builder-dockerfile:set shellshare dockerfile-path Dockerfile

# Deploy: pushes the current commit; Dokku builds and releases it
make deploy
```

Each `make deploy` builds the pushed commit on the Dokku host, so the
deployed code always matches what you pushed — there is no separate image
tag to bump.

## Analytics (optional, off by default)

The server can send anonymous usage events (rooms created, broadcast
durations, viewer counts) to [PostHog](https://posthog.com). Nothing is
collected unless you opt in by setting both variables:

```bash
SHELLSHARE_POSTHOG_KEY=phc_yourprojectkey \
SHELLSHARE_POSTHOG_SALT=some-long-random-secret \
shellshare server
```

(Set `SHELLSHARE_POSTHOG_HOST` for self-hosted PostHog. The equivalent
`--posthog-*` flags also exist, but prefer the environment variables:
the salt is a secret, and command-line arguments are visible to other
local users.)

No IP addresses or passwords are sent. Broadcasters are identified only
by `HMAC-SHA256(salt, password)`, which lets the operator count
returning users without being able to identify anyone. Room names are
sent as-is (they are share-link slugs, visible to anyone with the
link). Keep the salt stable across restarts and servers so returning
users stay recognizable; rotating it resets all identities. Events are fire-and-forget and never
block or slow down broadcasting.

## Releasing

```bash
make release                # patch bump, e.g. 2.0.6 -> 2.0.7
make release VERSION=2.1.0  # explicit version
```

This bumps Cargo.toml, commits, tags, and pushes. CI then runs the e2e tests, builds all platforms, creates the GitHub release with binaries, and publishes the [npm packages](https://www.npmjs.com/package/shellshare).

Working in Claude Code? The `release` skill (`.claude/skills/release/SKILL.md`) walks the whole flow — picking the semver bump (major only when old CLI binaries break), running `make release`, and writing the GitHub release notes.

## Limitations

This project is intended for live broadcasts only. If you'd like to record your terminal, check [asciinema.org](https://asciinema.org)
or [other terminal recording tools](https://github.com/topics/terminal-recording).

# License

Copyright 2026 Vitor Baptista

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

  http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
