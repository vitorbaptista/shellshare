# Reading a shellshare broadcast as an agent

Reference decoders for an AI agent (or any non-browser consumer) that
wants to **watch** a shellshare broadcast read-only, without installing
the shellshare CLI. Both use only Node's built-ins (`crypto`,
`WebSocket`) — no `npm install`.

You are given a share link like `http://<host>/r/<room>#<64-hex-key>`.
Broadcasts are end-to-end encrypted: the server stores only ciphertext
and never has the key. The key is the hex after `#` (a URL fragment,
which browsers and `fetch()` never send to the server), so **you**
decrypt locally.

## Snapshot — "what does it show right now?"

```bash
node decrypt.mjs 'http://<host>/r/<room>#<key>'
```

Fetches `GET /r/<room>.bin` (the ciphertext history), decrypts, prints
the terminal text, exits. Poll again later for a fresh snapshot.

## Follow — "show me new output live"

```bash
node follow.mjs 'http://<host>/r/<room>#<key>' [--seconds N] [--idle N] [--until REGEX]
```

Connects to the viewer WebSocket `ws://<host>/ws/v/r/<room>` and decodes
records as they arrive. Needs Node >= 22 (built-in `WebSocket`). It
always stops on its own so an agent's call returns:

- `--seconds N` — stop after N seconds (`N=0` = run until killed)
- `--idle N` — stop after N seconds with no new output
- `--until RE` — stop once the decoded output matches regex `RE`
- (no flag) — defaults to `--seconds 30`

Background-tail pattern (don't block your turn; re-read across turns):

```bash
nohup node follow.mjs URL --seconds 0 > /tmp/ss.log 2>&1 &
tail -n 50 /tmp/ss.log
```

## Wire format

Both the `.bin` body and the WS binary frames are a sequence of
self-delimiting records:

```
[u32 BE N][12-byte nonce][ciphertext || 16-byte GCM tag]   # N = 12 + len(ciphertext||tag)
```

AES-256-GCM; key = the 32 bytes given as 64 hex chars in the link's `#`
fragment. Each record decrypts independently; concatenate the plaintexts
to rebuild the terminal byte stream. Strip ANSI escapes for a clean text
view, and buffer across frames/reads — a record can span two. On the WS,
**text** frames are control JSON (`size`/`broadcasting`/`usersCount`)
you can ignore; the first **binary** frame is the catch-up history.
