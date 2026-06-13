---
name: release
description: >-
  Cut a new shellshare release: pick the right semver version, run `make
  release`, watch CI, and write the narrative GitHub release notes. Use this
  whenever the user wants to release, ship a new version, cut a release, tag a
  release, bump the version, or publish — even if they just say "release this"
  or "ship it". The hard part is choosing major/minor/patch under shellshare's
  project-specific rule (major only when old CLI binaries break), so reach for
  this skill instead of guessing a version or running `make release` blind.
---

# Releasing shellshare

A release is one command — `make release` — but the command is the easy part.
The two things that actually need judgment, and the reason this skill exists,
are **picking the right version number** and **writing the release notes**. Get
those right and everything else is automation.

## How a release works here

`make release` runs `scripts/release.sh`, which bumps the version in
`Cargo.toml`, refreshes `Cargo.lock`, commits `chore: release vX.Y.Z`, tags
`vX.Y.Z`, and pushes both atomically. The tag push triggers
`.github/workflows/release.yml`, which runs the full e2e suite, builds binaries
for all four platforms, builds the Docker image, publishes to npm, and creates
the GitHub release. You do not build or upload anything by hand.

The script refuses to run unless you're on `main` with a clean working tree, and
it pulls `--ff-only` first. So the release itself is safe by construction — the
risk isn't a broken command, it's shipping the *wrong version number* or
*thin release notes*, both of which are hard to walk back once npm and the
GitHub release are public.

## Step 1 — Confirm the ground is solid

Run these and read them before anything else:

```bash
git rev-parse --abbrev-ref HEAD        # must be main
git status --porcelain                 # must be empty
git fetch --tags origin && git log --oneline @{u}..HEAD   # must be empty (you're not ahead)
git describe --tags --abbrev=0         # the last released tag
```

If the branch isn't `main` or the tree is dirty, stop and surface that — don't
try to work around `release.sh`'s guards, they exist for good reason.

## Step 2 — Choose the version (this is the real work)

Look at everything since the last tag:

```bash
LAST=$(git describe --tags --abbrev=0)
git log --oneline "$LAST"..HEAD
```

shellshare does **not** follow textbook semver. The rule is specific and you
must apply *this* one, not the generic one:

- **MAJOR** (e.g. `3.3.0 → 4.0.0`) — reserved for changes that **break old
  shellshare CLI binaries** talking to the ingest endpoint `/ws/r/:room`. That
  means a breaking change to `src/cli/`, the ingest handshake, the ack
  semantics, or the wire frame format in `src/protocol.rs`. Verify with:
  ```bash
  git diff "$LAST"..HEAD -- src/cli/ src/protocol.rs
  ```
  Breaking the *viewer* WebSocket protocol (`/ws/v/r/:room`), templates, or any
  server internals does **NOT** justify a major bump. Browsers always load the
  viewer page from the same server, so page and protocol ship in lockstep —
  there is no "old viewer" in the wild to break. A commit literally labeled
  `BREAKING` can still be a minor release if old CLIs keep working (this is
  exactly why the Socket.IO→raw-WebSocket viewer rework shipped as `3.3.0`, not
  `4.0.0`).

- **MINOR** (e.g. `3.2.0 → 3.3.0`) — any **new user-facing capability** that
  doesn't break old CLIs: a `feat:` commit, a new flag, a new subcommand, a new
  endpoint. Additive changes to the CLI count here too, even substantial ones
  (e2e encryption shipped as a minor because old binaries still broadcast fine
  with `--disable-encryption` semantics intact).

- **PATCH** (e.g. `3.3.0 → 3.3.1`) — the release contains **only** fixes, perf
  work, refactors, style, tests, docs, or chores. No new capability. This is
  what bare `make release` produces by default.

When in doubt between minor and patch, ask: "could a user *do something new*
after upgrading?" Yes → minor. No → patch. When in doubt between major and
minor, ask: "would someone's already-installed `shellshare` binary stop
broadcasting against the new server?" Only a clear yes → major.

**Worked examples from real history:**

| Since tag | What landed | Chosen | Why |
|-----------|-------------|--------|-----|
| v3.2.0 | `feat: ENV analytics label`, `feat: link analytics to rooms`, viewer fan-out perf rework (Socket.IO removed) | **3.3.0** minor | New features; viewer rework breaks no CLI |
| v3.1.0 | `feat: e2e encryption`, `feat: exec + --json`, xterm.js viewer | **3.2.0** minor | Big additive CLI features, old binaries still work |
| v2.0.6 | npm distribution, `serve`, WebSocket pipeline rebuild, master→main | **3.0.0** major | Ingest protocol rebuilt on WebSockets — old CLIs broke |

Decide the bump, then state your reasoning to the user in one or two sentences
and name the proposed version. Because a release is public and effectively
irreversible (npm + GitHub release + git tag), **wait for the user to confirm
the number before running anything.** This is the one place to be sure rather
than fast.

## Step 3 — Cut it

For a patch (the default), bare `make release` is enough:

```bash
make release
```

For a minor or major, pass the version explicitly:

```bash
make release VERSION=3.4.0
```

The script prints a link to the running workflow when it's done pushing.

## Step 4 — Watch CI

The tag triggers the full pipeline; e2e tests run before any binary is built, so
a red suite stops the release cleanly. Follow it:

```bash
gh run watch $(gh run list --workflow=release.yml --limit=1 --json databaseId -q '.[0].databaseId')
```

If e2e fails, the release doesn't publish — fix forward on `main` and cut a new
patch; you can't reuse a tag. Tell the user if it goes red.

## Step 5 — Write the narrative release notes

This is the second half of the job and easy to forget. CI creates the GitHub
release with `generate_release_notes: true`, which produces only the mechanical
`## What's Changed` PR list and a Full Changelog link. Every good shellshare
release has a **hand-written narrative on top of that**, and you add it after CI
publishes.

The voice is for *users*, not contributors. Open with a single sentence naming
the theme of the release, then 2–4 highlights, each led by an emoji and a bold
phrase, each explaining the user-facing value — and folding any migration note
inline rather than in a separate "breaking changes" section.

**The shape (from v3.3.0 and v3.2.0):**

```markdown
<One sentence naming what this release is fundamentally about.>

⚡ **<Punchy lead>.** <What changed, in plain language, and why a user cares.
If there's a migration, say it right here: "point it at `/ws/v/r/:room`.">

🔒 **<Next highlight>.** <Same again. Keep it concrete — name the flag, the
command, the number ("6,000 viewers at 30fps").>

✨ **<A smaller third highlight>.** <Polish, rendering, UX wins go last.>
```

Match the emoji to the substance (⚡ perf/scale, 🔒 privacy/security, 🤖
scripting/agents, 📱 mobile, 📊 analytics, ✨ general polish). Don't pad to a
fixed count — a focused two-highlight release reads better than a padded four.

Publish it by **prepending** your narrative above the auto-generated section,
without clobbering the `## What's Changed` list CI made:

```bash
V=v3.4.0
BODY=$(gh release view "$V" --json body -q .body)
printf '%s\n\n%s' "$NARRATIVE" "$BODY" | gh release edit "$V" --notes-file -
```

Then show the user the final notes (`gh release view $V --web`) so they can
tweak wording. The release is the public face of the version — it's worth the
extra minute.

## Quick reference

| Situation | Version | Command |
|-----------|---------|---------|
| Only fixes/perf/chore since last tag | patch | `make release` |
| New flag / subcommand / feature, old CLIs fine | minor | `make release VERSION=X.Y+1.0` |
| Old CLI binaries break against new ingest protocol | major | `make release VERSION=X+1.0.0` |

Pre-flight: on `main`, clean tree, not ahead of origin. Post-flight: watch
`release.yml`, then write the narrative notes on the published release.
