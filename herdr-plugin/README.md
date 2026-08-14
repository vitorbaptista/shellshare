# shellshare for herdr

Share your [herdr](https://herdr.dev) terminal with anyone who has a
browser: one action broadcasts a single pane - or your entire herdr
session - as a live [shellshare](https://shellshare.net) link. No
signups; viewers just open the URL.

## Installing

```bash
herdr plugin install vitorbaptista/shellshare
```

You also need, on your PATH:

- `shellshare` **3.12 or newer** (the plugin pins the viewer geometry
  with `--cols`/`--rows`, added in 3.12) - one-liners at
  https://shellshare.net (`npx -y shellshare` works, but a real install
  is better for a long-running broadcast), or point the plugin at a
  binary with `shellshare_bin=` in the config below
- `jq`

For development, link a checkout instead:

```bash
git clone https://github.com/vitorbaptista/shellshare
herdr plugin link ./shellshare
```

## Share something right now

From a terminal inside herdr (so the command targets the session you are
looking at):

```bash
herdr plugin action invoke shellshare.share-session
```

A "Shellshare" tab opens with the link. Anyone opening it watches your
whole herdr UI live, read-only. `shellshare.share-pane` shares only the
focused pane instead. Stop with:

```bash
herdr plugin action invoke shellshare.stop
```

Stopping ends the broadcast, but the link keeps showing the final state
until the server's room TTL evicts it (~6 hours on shellshare.net).

## Keybindings

Add to `~/.config/herdr/config.toml`, then run
`herdr server reload-config`:

```toml
[[keys.command]]
key = "prefix+s"
type = "plugin_action"
command = "shellshare.share-pane"
description = "share this pane"

[[keys.command]]
key = "prefix+shift+s"
type = "plugin_action"
command = "shellshare.share-session"
description = "share the whole session"
```

`herdr plugin action list --plugin shellshare` shows everything the
plugin can do (`share-pane`, `share-session`, `stop`, `status`).

Agents and scripts can share a specific pane without a keybinding
(`SHELLSHARE_DIRECT=1` opts out of the guard that stops respawned panes
from silently resuming a broadcast):

```bash
herdr plugin pane open --plugin shellshare --entrypoint pane-broadcast \
  --placement tab --env SHELLSHARE_TARGET_PANE=<pane-id> \
  --env SHELLSHARE_STATE_KEY=manual-<pane-id> \
  --env SHELLSHARE_SHARE_TOKEN=$(od -An -N16 -tx1 /dev/urandom | tr -d ' \n') \
  --env SHELLSHARE_DIRECT=1
```

## Configuration

Optional, at the path printed by `herdr plugin config-dir shellshare`,
in a file named `config`. Plain `key=value` lines - the file is parsed,
not executed:

```
# Where to broadcast (default: https://shellshare.net). Self-hosting is
# one command: shellshare server
server=https://shellshare.example.com

# Viewer color theme (see: shellshare --help)
theme=dracula

# Stable links: rooms become <prefix>-session and <prefix>-pane-<id>,
# so the same share keeps the same URL across restarts. Read the
# security notes below before using this on a shared machine.
room_prefix=myname

# Pane share poll interval in seconds (default 0.25). Each tick asks
# herdr for the pane's rendered viewport - raise it if the sustained
# background load bothers your laptop
poll_interval=0.5

# Session mirror size when your client size can't be detected
session_cols=120
session_rows=36

# Broadcast pane shares in plaintext (for viewers on plain HTTP, e.g. a
# classroom LAN, where browsers can't decrypt)
pane_plaintext=true

# Session shares only go plaintext with this exact spelling - it
# downgrades your ENTIRE herdr UI to plaintext on the wire
session_plaintext=yes-i-know

# Explicit path to the shellshare binary (default: found on PATH)
shellshare_bin=/usr/local/bin/shellshare
```

## What viewers see

- **Pane share** mirrors the pane's rendered viewport a few times per
  second: live, but no scrollback and no cursor, output that scrolls
  through between samples is skipped, and the geometry is fixed when
  the share starts - resizing the pane, or moving it to another
  workspace (herdr pane ids change there), ends the share with a
  notification. When you want a full-fidelity broadcast of one
  terminal's byte stream, run `shellshare` directly inside that pane
  instead - the plugin's pane share is for sharing a pane you can't or
  don't want to restart.
- **Session share** attaches a second, invisible herdr client and
  broadcasts its full UI - sidebar, tabs, every pane - sized to your
  own client when detectable. It is exactly what it sounds like:
  **everything visible anywhere in your session goes out**, including
  the status tabs of other live shares (which display their links) if
  you focus them while sharing.
- Viewers are always read-only; shellshare has no input channel.

## Security notes

- Broadcasts are end-to-end encrypted by default; the server relays
  ciphertext. The key is the `#fragment` of the link - browsers never
  send fragments, so the server can't read your terminal. The link
  appears only in the status tab: not in notifications, not on disk.
- Anyone with the link can watch. Don't broadcast secrets.
- `room_prefix` trades privacy for a stable URL: the room name rides in
  the process argv, so **on a multi-user machine another local user can
  read it (`ps`) and derive the encryption key** from the world-readable
  machine id - stick to the default random rooms there. A leaked stable
  link also stays valid for every future broadcast of that room, and
  after the room idles out (~6h) someone else can claim the name and
  serve their own content at your URL.
- The plugin keeps a small state file per live share (room name, PIDs -
  never the link or key), mode 600, removed when the share ends. The
  config file is yours: if you set `room_prefix` on a shared machine,
  keep the file private (`chmod 600`) - the prefix is enough to derive
  room names.

## Limitations

- Linux and macOS only for now (the plugin is bash; shellshare itself
  runs on Windows).
- One share per pane and one session share at a time; re-invoking an
  active share focuses its status tab instead of double-broadcasting.
