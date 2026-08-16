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

That space is the indicator: it is there while you are broadcasting and
gone when you stop, and you can see it from wherever you are working.
(The exception is a share that *failed* — that space stays, relabelled
**✗ shellshare (stopped)**, to hold the error.) What is being shared is
the whole session, so it lives at the session level rather than inside
one of your projects.

To stop: run the action again, or press Ctrl+C in the ◉ live tab. Either
way only the share's own pane closes — herdr then drops the tab and the
space around it, so normally the whole ◉ shellshare row just disappears.
If you had put anything of your own in that space (another tab, or a
split beside the share), it stays, and the space is renamed
**✗ shellshare (stopped)** because nothing is being broadcast any more.

Closing the ◉ shellshare **space** stops the share too, but it is not
the same thing: that closes every tab in the space, including yours.

The link keeps showing the final frame until the server's room TTL
evicts it (~6 hours on shellshare.net).

One keybinding is the whole interface. Add to
`~/.config/herdr/config.toml`, then `herdr server reload-config`:

```toml
[[keys.command]]
key = "prefix+shift+s"
type = "plugin_action"
command = "shellshare.share"
description = "share this session (read-only)"
```

(`prefix+s` is herdr's own **settings**; `prefix+shift+s` is free in the
default keymap. `herdr --default-config` lists every binding herdr ships
with, if you want a different key.)

## Configuration

Optional, at the path printed by `herdr plugin config-dir shellshare`,
in a file named `config`. Two keys, `key=value`, parsed not executed:

```
# Explicit path to the shellshare binary (default: found on PATH)
shellshare_bin=/usr/local/bin/shellshare

# Extra flags for shellshare, split on whitespace - so a value that
# contains a space cannot be expressed here:
#   --server https://shellshare.example.com   (self-hosted: shellshare server)
#   --theme dracula
#   --room my-room                            (a stable link; see below)
#   --disable-encryption                      (plaintext - you own the consequences)
shellshare_args=--server https://shellshare.example.com --theme dracula
```

`--cols`, `--rows` and `--json` are the plugin's own (they pin the
mirror's size and carry the link back); passing them here makes
shellshare refuse to start, and the pane shows why.

## What viewers see, and what they don't

- Everything on your **focused tab**: every pane in it, live, read-only.
  Switching tabs switches what they see; `herdr pane zoom` narrows it to
  one pane. herdr's own controls are the privacy controls.
- **And your herdr chrome**: the mirror is a full herdr client, so the
  spaces sidebar (every space's name — often client or project names),
  the tab bar, and your agent rows with their statuses are all in shot.
  Zooming a pane hides the panes around it, not the sidebar.
- **Everything means everything** — the `.env` you left open, the
  credential you paste. Zoom or switch away before doing anything you
  would not put on a call.
- The mirror is a second herdr client pinned to the size your client had
  when the share started. That pinning is what stops herdr's
  smallest-client-wins sizing from shrinking your session — but it cuts
  both ways: **while you are sharing, your own session will not grow
  past that size**. Making your terminal bigger mid-share does nothing
  until you stop and start again.
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
- The plugin keeps no record of your share: it asks herdr which panes
  carry the marker the live one puts on itself, and stopping closes
  those panes' tabs. So the only thing it can ever close is the share
  itself — never a space, never a tab of yours — and a share that has
  died leaves nothing behind to act on, because the marker goes with the
  pane. In its state directory it writes only a
  fifo (empty; carries the mirror's output between two processes while
  the share runs, removed when it ends) and a `mirror-<pid>.err` file
  holding shellshare's stderr, kept only when a share fails so there is
  something to read afterwards. No URLs, no keys, ever.
- The ◉ shellshare space is a space like any other — if you focus it,
  viewers see it, link and all. They already have that link, so nothing
  leaks; it just isn't very interesting to watch.

## Troubleshooting

The share tab shows what went wrong and waits for you to read it. If a
broadcast dies on its own, the space is renamed **✗ shellshare
(stopped)** — so the sidebar never claims you are live when you are not
— and the tab stays open with the reason.

`shellshare`'s own stderr is kept alongside:

```bash
cat ~/.local/state/herdr/plugins/shellshare/mirror-*.err
herdr plugin log list --plugin shellshare   # what herdr ran, and when
```

## Limitations

- Linux and macOS (the plugin is bash; shellshare itself runs on
  Windows).
- One share per herdr session.
- herdr's right-click menus are built-in only, so the action lives on a
  keybinding and `plugin action invoke`. The manifest already declares
  its contexts, so it would appear automatically if herdr ever surfaces
  plugin actions in menus.
