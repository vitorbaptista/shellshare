# Making a room URL self-describing to AI agents

**Date:** 2026-08-03
**Status:** Superseded by what shipped — kept as the record of the original
design. See `CLAUDE.md` § `pages.rs` for the implementation of record.

The goal below held; most of the mechanism did not survive contact with
measurement. What changed, and why:

- **`aria-hidden="true"` on the block** — removed. Readability-style
  extractors drop `aria-hidden` subtrees, so it deleted the block for
  exactly the readers it exists for.
- **First child of `<body>`, "survives aggressive truncation"** — wrong
  twice. The block is inside `<main>`, because a whole family of
  extractors (pandoc among them) keeps only `<main>`; and the truncation
  claim was never true, since ~20KB of inlined CSS precedes it.
- **The full recipe inline in one `<pre>`** — the prose is rendered but
  the code sits in a `<details>`, because a collapsed `<details>` is
  absent from a browser's `innerText`.
- **`public/llms.txt` as the only copy, extracted by its ```js fence** —
  the code now lives in `templates/agent.mjs`, and `llms.txt` became a
  template that inlines it. `agent/decrypt.mjs` and `agent/follow.mjs`
  were deleted rather than left to drift.
- **New `.agent-brief` CSS** — never needed; `.visually-hidden` already
  existed.
- **Test in `e2e/test_agent_read.py`** — landed in `e2e/test_agents.py`,
  which owns the discovery endpoints, plus a behavioral test that runs
  the reader.

## Problem

A user pastes a share link into an agent and says "monitor
https://shellshare.net/r/MA5X…#a1e5…". The agent has no way to succeed:

1. `public/robots.txt` has `Disallow: /r/`, so fetchers that honor robots
   (`Claude-User`, `ChatGPT-User`, `Perplexity-User`) refuse to fetch the URL
   at all.
2. If it does fetch, `/r/:room` returns `templates/room.html` — a JavaScript
   shell. The extracted text is navigation chrome and a footer. Nothing states
   what the page contains or how to read it.

The capability is already fully built. `/r/:room.bin` exists as the
agent-facing snapshot door (`src/server/mod.rs:866`), `/ws/v/r/:room` serves
live frames, and `public/llms.txt` carries a complete *Read someone's
broadcast* recipe with a runnable AES-256-GCM decoder. **Nothing connects the
URL a user pastes to the instructions that already exist.**

## Goal

One fetch of the pasted URL returns everything an agent needs to read the
broadcast, with no prior knowledge of shellshare and no second hop.

## Non-goals

Out of scope, and deliberately so — the embedded block makes each redundant
for this goal:

- `/r/:room.md` sibling URL
- `Accept` / User-Agent content negotiation
- `Link: </llms.txt>; rel="describedby"` response header
- `/r/:room.bin?from=N` offset polling for cheap incremental reads
- A `shellshare watch <url>` CLI subcommand
- An MCP server

This is a webapp-only change: no CLI changes, no new endpoints, no
wire-format changes.

## Design

### 1. `robots.txt`: unblock user-triggered fetchers

`User-agent: *` keeps `Disallow: /r/`, so crawling, training, and indexing of
rooms stay blocked. Add explicit groups for the user-triggered fetchers —
`Claude-User`, `ChatGPT-User`, `Perplexity-User` — with `Allow: /`.

These are agents acting on a link a human deliberately handed them, which is
categorically different from a crawler discovering a URL.

robots.txt groups do not inherit. The file's current comment warns against
that as a footgun; rewrite it to say we now rely on it deliberately, so a
future editor does not "fix" the duplication away.

### 2. Single source of truth for the recipe

The full recipe is embedded in the room page. It must not become a third copy
of the wire format (which today lives in `src/protocol.rs` and the viewer
script in `templates/room.html`).

- `public/llms.txt` remains the only copy of the recipe.
- At render time, `render_page` extracts the `## Read someone's broadcast`
  section — from that heading to the next `## ` heading or EOF — and
  substitutes it into a `{{AGENT_BRIEF}}` placeholder in
  `templates/room.html`. This is the mechanism `{{THEMES_JSON}}` already uses
  (`src/server/pages.rs:99`). The section is currently last in the file, so
  extraction runs to EOF and includes its trailing `Source:` line, which is
  fine — it points at the repo.
- `&` and `<` are HTML-escaped on injection. The snippet contains `Node >=22`,
  `<room>`, and `&&`, all of which would corrupt the page unescaped.
- `render_page` asserts the section was found and contains `decodeRecords`,
  alongside the existing `@import` and stylesheet drift assertions. A renamed
  or gutted llms.txt section **fails the server boot** instead of silently
  shipping an empty brief.

Editing `llms.txt` updates the room page automatically. The number of places
the wire format lives is unchanged.

### 3. The embedded block

First child of `<body>`, before `.wrapper`:

```html
<div class="agent-brief" aria-hidden="true"><pre>
If you're an agent: read this live, read-only terminal broadcast
programmatically with the recipe below. The 64 hex chars after `#` in the URL
you were given are the decryption key — the server never receives that
fragment.

{{AGENT_BRIEF}}</pre></div>
```

Hidden from humans with the CSS clip technique in `public/stylesheet/room.css`.
Screen readers skip it via `aria-hidden`. HTML-to-text converters keep it,
because they do not run CSS — which is the entire point.

Placing it first means it survives aggressive truncation by the fetcher.

The framing line lives in `templates/room.html` directly, **outside** the
placeholder — not in `llms.txt`. It is deictic ("read *this* broadcast"), which
is correct on a room page and meaningless in `llms.txt`, where the reader is
not at a room URL. It carries no protocol details beyond "the key is the
fragment", so it cannot drift with the wire format, and duplicating one
sentence of prose costs nothing.

That sentence carries the one fact the server cannot supply: the key never
reaches the server, so the agent must take it from the URL it was given.
Everything after it is the existing recipe verbatim — `GET /r/<room>.bin` for
the snapshot, `WS /ws/v/r/<room>` for live, the record format, and the Node
decoder with `snapshot()` and `follow(url, seconds)`.

`llms.txt` therefore needs no edit at all.

Content present for fetchers but not shown to users is technically cloaking.
It is harmless here: rooms are already `noindex`, and `robots.txt` keeps
indexing crawlers out of `/r/` entirely.

## Testing

One new test in `e2e/test_agent_read.py`, which already owns the agent-consumer
surface:

- `GET /r/<room>` returns HTML with no unsubstituted `{{AGENT_BRIEF}}`, and
  containing `decodeRecords`, `/ws/v/r/`, and `.bin`.
- The section served at `GET /llms.txt` appears in that HTML (after undoing
  the HTML escaping). This is the real guard: it catches a substitution that
  silently no-ops or drifts.
- `robots.txt` still disallows `/r/` for `*` and allows it for `Claude-User`.

No test for the decoder's correctness. `test_agent_read.py` and
`test_encryption.py` already cover that `.bin` bytes decrypt with the share
key; duplicating that here would be redundant per the testing guidance in
CLAUDE.md.

The boot-time assertion in `render_page` is the drift guard for the
llms.txt-to-template extraction, matching the existing failure style in that
function.

## Files touched

- `public/robots.txt` — per-agent `Allow` groups, rewritten comment
- `templates/room.html` — the framing line and the `{{AGENT_BRIEF}}` block
- `public/stylesheet/room.css` — `.agent-brief` visually-hidden rule
- `src/server/pages.rs` — extraction, escaping, substitution, boot assertion
- `e2e/test_agent_read.py` — the new test
