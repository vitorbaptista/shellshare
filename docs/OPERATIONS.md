# Operations

Maintainer-facing documentation: deploying shellshare.net, server analytics,
and cutting releases. If you just want to use or self-host shellshare, see
the [README](../README.md).

## Deploy

To deploy with [Dokku](https://dokku.com/), let it build the image from
source on each push using the project's `Dockerfile`:

```bash
# Create the app
dokku apps:create shellshare

# Build from the source Dockerfile (this is also Dokku's default)
dokku builder-dockerfile:set shellshare dockerfile-path Dockerfile

# Deploy: pushes the current commit; Dokku builds and releases it
make deploy
```

Each `make deploy` builds the pushed commit on the Dokku host, so the
deployed code always matches what you pushed — there is no separate image
tag to bump.

## Analytics (optional, off by default)

The server can send anonymous usage events (rooms created, broadcast
durations, viewer counts) to [PostHog](https://posthog.com). Nothing is
collected unless you opt in by setting both variables:

```bash
SHELLSHARE_POSTHOG_KEY=phc_yourprojectkey \
SHELLSHARE_POSTHOG_SALT=some-long-random-secret \
shellshare server
```

(Set `SHELLSHARE_POSTHOG_HOST` for self-hosted PostHog. The equivalent
`--posthog-*` flags also exist, but prefer the environment variables:
the salt is a secret, and command-line arguments are visible to other
local users.)

No personal data is sent: no IP addresses, no room names, no passwords.
Broadcasters are identified only by `HMAC-SHA256(salt, password)` and
rooms by `HMAC-SHA256(salt, room_name)`, which lets the operator count
returning users without being able to identify anyone. Keep the salt
stable across restarts and servers so returning users stay recognizable;
rotating it resets all identities. Events are fire-and-forget and never
block or slow down broadcasting.

## Releasing

```bash
make release                # patch bump, e.g. 2.0.6 -> 2.0.7
make release VERSION=2.1.0  # explicit version
```

This bumps Cargo.toml, commits, tags, and pushes. CI then runs the e2e tests, builds all platforms, creates the GitHub release with binaries, and publishes the [npm packages](https://www.npmjs.com/package/shellshare).
