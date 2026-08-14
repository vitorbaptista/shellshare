# shellshare for herdr

Share your [herdr](https://herdr.dev) terminal with anyone who has a
browser: one action broadcasts a single pane - or your entire herdr
session - as a live, read-only [shellshare](https://shellshare.net)
link. No signups; viewers just open the URL.

The broadcast runs in the background: it takes no pane, no tab, and no
space in your layout. While a share is live it says so in herdr's own
sidebar, and the link lives in an overlay you dismiss with any key.

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

An overlay shows the link (plus a QR code if you have `qrencode`);
press any key and herdr puts you back exactly where you were.
`shellshare.share-pane` shares only the focused pane instead.

```bash
herdr plugin action invoke shellshare.status   # show the links again
herdr plugin action invoke shellshare.stop     # stop this session's shares
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

## Show live shares in the sidebar (recommended)

While a share is live the plugin reports a `shellshare` display token
on the shared pane (or, for a session share, on every space). herdr
renders tokens only where your sidebar layout asks for them, so add
`$shellshare` to the rows you want it in:

```toml
[ui.sidebar.agents]
rows = [
  ["state_icon", "workspace", "tab", "$shellshare"],
  ["agent"],
]

[ui.sidebar.spaces]
rows = [
  ["state_icon", "workspace", "$shellshare"],
  ["branch", "git_status"],
]
```

A live share then shows `◉ shared` in the sidebar for as long as it
runs. The token carries a TTL the broadcaster refreshes, so it clears
itself within ~90 seconds even if the broadcaster is killed outright.

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
  the link overlay itself if you open it while sharing.
- Viewers are always read-only; shellshare has no input channel.

Sharing a pane follows the pane, not your attention: switching focus,
or moving the shared pane to a background tab, changes nothing for
viewers. Invoke `share-pane` again on another pane to run a second,
independent share.

## Security notes

- Broadcasts are end-to-end encrypted by default; the server relays
  ciphertext. The key is the `#fragment` of the link - browsers never
  send fragments, so the server can't read your terminal. The link is
  shown only in the overlay: not in notifications, not on disk, not in
  herdr's plugin logs.
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
- While a session share is live, the overlay refuses to display other
  shares' links: it is on screen, so it is being broadcast too.

## Troubleshooting

A share that cannot start has no pane to complain in, so it reports
through a notification and writes the reason to `last-error.txt` in the
plugin's state directory (`~/.local/state/herdr/plugins/shellshare` on
Linux by default):

```bash
cat ~/.local/state/herdr/plugins/shellshare/last-error.txt
herdr plugin log list --plugin shellshare   # what herdr ran, and when
```

## Limitations

- Linux and macOS only for now (the plugin is bash; shellshare itself
  runs on Windows).
- One share per pane and one session share at a time; re-invoking an
  active share shows its link again instead of double-broadcasting.
- herdr's right-click menus are built-in only, so the actions live on
  keybindings and `plugin action invoke` (the manifest already declares
  their `pane`/`workspace` contexts, so they would appear automatically
  if herdr ever surfaces plugin actions in menus).
