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

### Hosting a server

The same `shellshare` binary also includes the server code, allowing you to broadcast your terminal to a server you control.

To do so you just need to run `shellshare server` in one terminal and access [http://localhost:3000](http://localhost:3000). You can broadcast to this server using `shellshare --server http://localhost:3000`.

`shellshare serve` does both at once: it starts a local server in the background and broadcasts your terminal to it.

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

To deploy with [Dokku](https://dokku.com/), configure it to pull from `Dockerfile.production`, which pulls the Docker image from GitHub Container Registry:

```bash
# Create the app
dokku apps:create shellshare

# Configure to pull from Dockerfile.prooduction
dokku builder-dockerfile:set shellshare dockerfile-path Dockerfile.production

# Deploy
make deploy
```

## Limitations

This project is intended for live broadcasts only. If you'd like to record your terminal, check [asciinema.org](https://asciinema.org)
or [other terminal recording tools](https://github.com/topics/terminal-recording).

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
