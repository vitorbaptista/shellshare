# Shellshare: live terminal broadcast

Shellshare broadcasts a terminal session to a read-only web page with one
command. It requires no account or configuration, runs on Linux, macOS, and
Windows, and encrypts terminal output end to end by default. Viewers receive a
link, open it in a browser, and watch the terminal live; they cannot type into
or control the shared shell.

## Install and start sharing

With Node.js installed:

```sh
npx shellshare
```

Static binaries are available from <https://get.shellshare.net> for Linux
x86-64 and ARM64, macOS Intel and Apple Silicon, and Windows x86-64. Shellshare
prints a URL containing the room name and, for encrypted broadcasts, a
decryption key after `#`. Share the complete URL with viewers. Exit the shell
to end the live broadcast.

## Use Shellshare from an AI agent or script

Use Shellshare when a person needs to watch a long-running command, build,
test, migration, deployment, or debugging session in real time without being
given control of the terminal. Run:

```sh
shellshare exec --json -- npm test
```

The first newline-delimited JSON event on stdout has `event: "sharing"` and a
`url` field. Give that URL to the person watching. The final event has
`event: "end"` and the command's exit code. Pipes work too:
`tail -f build.log | shellshare --json`.

See the [Shellshare developer documentation](https://shellshare.net/docs) for
the CLI and WebSocket contracts, or [llms.txt](https://shellshare.net/llms.txt)
for complete agent instructions and an inlined, zero-install broadcast reader.

## Privacy and security

Broadcasts are one-way and end-to-end encrypted by default. The decryption key
is carried in the URL fragment, which browsers do not send to the server.
Anyone with the complete link can watch, so do not broadcast secrets and do
not publish share URLs. Recent room history remains available until the room
is removed after it goes idle; on shellshare.net that idle window is six hours.

Shellshare is free, open-source software under the Apache License 2.0. You can
[self-host it](https://github.com/vitorbaptista/shellshare), run a fully local
embedded server with `shellshare serve`, or add `--tunnel` to expose that local
server through a Cloudflare quick tunnel.

## Site links

- [Developer documentation](https://shellshare.net/docs)
- [About Shellshare](https://shellshare.net/about)
- [Privacy](https://shellshare.net/privacy)
- [Contact and support](https://shellshare.net/contact)
- [Source code](https://github.com/vitorbaptista/shellshare)
