# shellshare

Live terminal broadcasting. Share your terminal session via a web link with a single command.

```bash
npx shellshare
```

This prints a link like `https://shellshare.net/r/EPbZJ7VZNakS9vwUlS`. Everything you type is broadcast live to anyone watching that page, read-only, until you exit the shell (Ctrl+D).

Or install it globally:

```bash
npm install -g shellshare
shellshare
```

## Scripting & AI agents

Add `--json` for a machine-readable contract: the first line on stdout is
`{"event":"sharing", ..., "url":"https://shellshare.net/r/..."}` (parse `url`
and share it), and a final `{"event":"end","exit_code":N}` is printed when the
broadcast finishes. Errors go to stderr as `ERROR: ...` with a non-zero exit.

```bash
# Share a single command live; exits with the command's exit code
npx shellshare exec --json -- npm test

# Stream a log or any pipe (a non-TTY stdin auto-detects this, reads until EOF)
tail -f build.log | npx shellshare --json
```

Full agent-facing docs: [AGENTS.md](https://github.com/vitorbaptista/shellshare/blob/main/AGENTS.md) · [shellshare.net/llms.txt](https://shellshare.net/llms.txt)

## About this package

This package is a thin launcher around the prebuilt `shellshare` binary for your platform (Linux x64, macOS x64/arm64, Windows x64), fetched automatically through npm's `optionalDependencies` — no postinstall scripts.

Docs, FAQ and other install methods: [shellshare.net](https://shellshare.net) · Source: [github.com/vitorbaptista/shellshare](https://github.com/vitorbaptista/shellshare)
