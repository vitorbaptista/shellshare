# Herdr plugin: share your herdr session via shellshare

Date: 2026-08-13
Status: Final - describes what shipped

## Goal

Let a herdr user share their whole session - read-only, live, in a
browser - with one keypress. The plugin lives in this repo, so the two
tools stay in lockstep and `herdr plugin install vitorbaptista/shellshare`
works as typed (the v1 marketplace index does not surface
subdirectories, so the manifest sits at the repo root).

## What this deliberately does NOT do

There is no "share this pane" mode. Three earlier drafts had one - a
~4 Hz mirror of `herdr pane read --format ansi` piped into shellshare
stream mode - and it was cut after the maintainer put the obvious
question: what does it gain over typing `shellshare` in that pane?
Nothing. Typing the command gives a full-fidelity byte stream with
scrollback, a real cursor and keystroke latency; a rendered-viewport
mirror drops all three and skips output that scrolls between samples. A
plugin should not ship a worse copy of its own CLI. The README says so
in one line and points at the command.

A design panel (five independent proposals, three judges) also looked at
having the plugin *inject* the real command into the pane
(`herdr pane run <id> "shellshare"`, verified working: full fidelity,
`ctrl+d` stops it, the pane survives). It was cut too: on an idle pane
it saves one keystroke over typing, and on a busy pane - the only case
where the user is stuck - it cannot run at all. Documentation beats code
there.

## What the plugin earns

All four verified against herdr 0.8.0; each is a way a hand-typed
session share goes wrong:

- **Nesting is refused.** `herdr session attach` inside a herdr pane
  aborts: HERDR_ENV gates it. The mirror needs `env -u HERDR_ENV`.
- **You cannot name your own session.** HERDR_SESSION is not exported,
  so a hand-typed attach has to guess - and a wrong guess silently
  *starts* and broadcasts the DEFAULT session instead of yours. The
  plugin resolves it by matching `HERDR_SOCKET_PATH` against
  `session list --json`, and refuses rather than guessing.
- **Geometry collapses.** Without `--cols/--rows` the mirror attaches at
  shellshare's headless 80x24 fallback, and herdr's smallest-client-wins
  sizing drags the real session down to it. The plugin derives the size
  from the focused tab's `api snapshot` layout extent (x+width by
  y+height reproduces the client size exactly).
- **The mirror renders itself.** `shellshare exec` echoes the mirror's
  PTY bytes on its stdout, so run by hand in a pane the broadcast shows
  the pane showing the broadcast. The plugin swallows that stdout.

## Design

```
herdr-plugin.toml     # manifest at repo root: 1 action, 1 pane
herdr-plugin/
  share.sh            # toggle + live; the whole plugin
  README.md
```

**The share is a pane, not a daemon.** The `live` pane entrypoint runs
the broadcaster, so the process's lifetime is the share's lifetime.
herdr already owns pane lifetime - which means no supervision, no state
machine, no locks, no liveness tokens, no sweep, and no way to leave an
orphan behind.

**That pane gets a space of its own.** The action creates one
(`workspace create --label "◉ shellshare"`), opens the pane there as a
tab, and closes the shell tab herdr creates with it. Two reasons: what
is being shared is the whole session, so parking it inside one project's
space misfiles it; and herdr closes a space when its last tab goes,
which means the space exists for exactly as long as the broadcast. That
makes it the indicator - a labelled row in the spaces sidebar, visible
from wherever you are working, needing no `[ui.sidebar.*]` config and no
badge-refresh loop. Ctrl+C, closing the space, and toggling the action
are all the same stop, and none of them can leave the indicator lying.

**One action, and no state of its own.** `share` toggles: if a space
carrying the plugin's label exists, close it; otherwise create one and
start. "Am I sharing?" is a question for herdr (`workspace list`), never
a pid file - a file outlives crashes and reboots, and herdr ids are
per-server counters that get reused, so acting on a stale one means
closing somebody else's space. There is no fallback that puts the share
in the caller's space either: stopping closes a space, and closing the
wrong one takes the user's tabs with it, so the plugin owns its space or
refuses to start.

**Start protocol.** shellshare's stdout goes to a fifo; the main shell
reads the first line (the `sharing` event), which keeps failure handling
in a context where `exit` actually exits - through a pipe it would only
leave a subshell, and the pane would report success after failing. On
success the banner prints the link, and a background `cat >/dev/null`
drains the rest. On failure the pane shows shellshare's stderr and waits
so the message can be read.

**The banner is static** on purpose: this pane is inside the broadcast,
so anything that repaints costs every viewer bandwidth for the life of
the share.

**shellshare presents its own link.** The banner does not print the URL
itself - it runs `SHELLSHARE_URL=<url> shellshare status`, the existing
command whose entire job is to present a share link, which brings the
phone-scannable QR code with it. So the QR needs neither a `qrencode`
dependency nor a new shellshare subcommand: the plugin hands back the
URL it just parsed out of the `--json` event and lets shellshare do the
presenting. (A `shellshare qr <url>` subcommand was written first and
then reverted - reusing `status` says the same thing with no new CLI
surface. The one trap: `status` gates the QR on `stdout.is_terminal()`,
so that call must not be piped.)

**Config** is two keys: `shellshare_bin` and `shellshare_args` (passed
verbatim, covering `--server`, `--theme`, `--room`, and
`--disable-encryption` - typed by the user, which is its own explicit
opt-in). Parsed, not sourced.

## Rust changes (both motivated here, both useful on their own)

- `--cols/--rows` pin each dimension at every size read (initial connect,
  SIGWINCH, stream-mode re-reads). Headless runs previously got 80x24
  with no recourse.
- `exec` with a null-device stdin no longer injects anything: the stdin
  forwarder used to drop its PTY writer at EOF, and portable-pty encodes
  that as newline+VEOF - a stray Enter and Ctrl+D typed into every
  headless child. Pipes, even empty ones, keep full EOF semantics.

## Tests

`e2e/test_herdr_plugin.py` stubs `herdr` (session list, api snapshot,
session attach, plugin pane open/close) but runs a real encrypted
broadcast against a dedicated local server:

1. Manifest ↔ share.sh lockstep: routing, dot-free ids, the `live`
   entrypoint id, `tab` placement.
2. The pane broadcasts the session and prints its link; a viewer reads
   the mirrored output back.
3. The four earned properties in one test: HERDR_ENV cleared, the right
   session named (the stub offers a decoy), the PTY sized to the focused
   tab's extent, stdin quiet.
   (2 also carries the anti-recursion contract: a viewer has provably
   received the mirror's bytes, so their absence from the pane's own
   screen is an assertion rather than a race.)
4. The share gets a space of its own: created, labelled, pane opened
   there, the space's own shell tab dropped, then focused.
5. Toggle stops it by closing that space - and with no labelled space
   present it starts one instead of closing anything, even when the
   creation fails. Closing the wrong space would destroy the user's
   tabs, so this is the test that matters most.
6. Unreachable server fails visibly, with shellshare's own diagnostics.
7. An unresolvable session refuses instead of attaching to a guess.

## Ship checklist

- Cut the shellshare release carrying `--cols/--rows` (3.12) before
  announcing the plugin: the pane preflights for it and tells older
  binaries to upgrade.
- Add the `herdr-plugin` GitHub topic to the repo (the marketplace
  index's only signal) and mention the plugin in the repo description.

## Out of scope

- Windows (the script is bash; shellshare itself runs there).
- Sharing a pane (run `shellshare` in it).
- Snapshot/"pin" links for panes you cannot type into - the panel's
  runner-up idea, deferred as a separate decision.
