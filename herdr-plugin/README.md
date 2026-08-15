# shellshare for herdr

Share your [herdr](https://herdr.dev) session with anyone who has a
browser: one keypress broadcasts it as a live, read-only
[shellshare](https://shellshare.net) link. Viewers see whichever tab
you are working in - every pane, live. No signups.

**To share a single pane, don't use this plugin: run `shellshare` in
that pane.** That gives a full-fidelity byte stream with scrollback and
keystroke latency, which is strictly better than anything a plugin can
mirror from outside. The plugin exists for the thing you cannot type:
your whole session, already running.

## Installing

```bash
herdr plugin install vitorbaptista/shellshare
```

You also need `shellshare` **3.12 or newer** on your PATH (the plugin
pins the viewer geometry with `--cols`/`--rows`, added in 3.12) —
one-liners at https://shellshare.net — plus `jq`.

For development, link a checkout instead:

```bash
git clone https://github.com/vitorbaptista/shellshare
herdr plugin link ./shellshare
```

## Use it

From a terminal inside herdr:

```bash
herdr plugin action invoke shellshare.share
```

A space called **◉ shellshare** appears with your link in it, and a QR
code to scan it onto a phone. Copy the link, switch back to your own
space, and carry on — viewers follow whatever tab you are looking at.

That space is the indicator: it exists for exactly as long as you are
broadcasting, and you can see it from wherever you are working. What is
being shared is the whole session, so it lives at the session level
rather than inside one of your projects.

To stop: run the action again, close the ◉ shellshare space, or press
Ctrl+C in it — all three do the same thing. The link keeps showing the
final frame until the server's room TTL evicts it (~6 hours on
shellshare.net).

One keybinding is the whole interface. Add to
`~/.config/herdr/config.toml`, then `herdr server reload-config`:

```toml
[[keys.command]]
key = "prefix+s"
type = "plugin_action"
command = "shellshare.share"
description = "share this session (read-only)"
```

## Configuration

Optional, at the path printed by `herdr plugin config-dir shellshare`,
in a file named `config`. Two keys, `key=value`, parsed not executed:

```
# Explicit path to the shellshare binary (default: found on PATH)
shellshare_bin=/usr/local/bin/shellshare

# Extra flags passed to shellshare verbatim. Anything the CLI accepts:
#   --server https://shellshare.example.com   (self-hosted: shellshare server)
#   --theme dracula
#   --room my-room                            (a stable link; see below)
#   --disable-encryption                      (plaintext - you own the consequences)
shellshare_args=--server https://shellshare.example.com --theme dracula
```

## What viewers see, and what they don't

- Everything on your **focused tab**: every pane in it, live, read-only.
  Switching tabs switches what they see; `herdr pane zoom` narrows it to
  one pane. herdr's own controls are the privacy controls.
- **Everything means everything** — the `.env` you left open, the
  credential you paste. Zoom or switch away before doing anything you
  would not put on a call.
- The mirror is a second herdr client, so it renders at your client's
  size, pinned for the life of the share. Resize your terminal and the
  broadcast keeps the old geometry until you stop and start again.
- Viewers can never type: shellshare has no input channel.

## Security notes

- Encrypted end to end by default; the server relays ciphertext. The key
  is the `#fragment` of the link, which browsers never send, so the
  server cannot read your terminal.
- Anyone with the link can watch, so treat it as the secret it is. The
  link is displayed in the share tab — that grants nothing to viewers
  (they already have it), but it does mean the tab is worth switching
  away from if you are screen-sharing to a wider audience.
- With `--room` in `shellshare_args` the link is stable across restarts,
  which also means a leaked link stays valid for every future broadcast
  of that room, and the room name is visible to other users on a
  multi-user machine (`ps`). The default random room avoids both.
- The plugin writes one file: a `live-*` record holding the pane's PID
  and the space it is in, removed when the share ends. No URLs, no keys,
  ever.
- The ◉ shellshare space is a space like any other — if you focus it,
  viewers see it, link and all. They already have that link, so nothing
  leaks; it just isn't very interesting to watch.

## Troubleshooting

The share tab shows any startup error and waits for you to read it.
`shellshare`'s own stderr is kept at
`~/.local/state/herdr/plugins/shellshare/last-error.txt`, and
`herdr plugin log list --plugin shellshare` shows what herdr ran.

## Limitations

- Linux and macOS (the plugin is bash; shellshare itself runs on
  Windows).
- One share per herdr session.
- herdr's right-click menus are built-in only, so the action lives on a
  keybinding and `plugin action invoke`. The manifest already declares
  its contexts, so it would appear automatically if herdr ever surfaces
  plugin actions in menus.
