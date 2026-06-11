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

This package is a thin launcher around the prebuilt `shellshare` binary for your platform (Linux x64, macOS x64/arm64, Windows x64), fetched automatically through npm's `optionalDependencies` — no postinstall scripts.

Docs, FAQ and other install methods: [shellshare.net](https://shellshare.net) · Source: [github.com/vitorbaptista/shellshare](https://github.com/vitorbaptista/shellshare)
