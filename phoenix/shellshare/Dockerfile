# Build stage
FROM elixir:1.17-alpine AS builder

# Install build dependencies
RUN apk add --no-cache build-base git nodejs npm

WORKDIR /app

# Install hex + rebar
RUN mix local.hex --force && \
    mix local.rebar --force

# Set build environment
ENV MIX_ENV=prod

# Install mix dependencies
COPY mix.exs mix.lock ./
RUN mix deps.get --only $MIX_ENV
RUN mkdir config

# Copy compile-time config files
COPY config/config.exs config/${MIX_ENV}.exs config/
RUN mix deps.compile

# Copy assets
COPY priv priv
COPY assets assets

# Install npm dependencies and build assets
RUN cd assets && npm install
RUN mix assets.deploy

# Compile and build release
COPY lib lib
RUN mix compile
COPY config/runtime.exs config/
RUN mix release

# Runtime stage
FROM alpine:3.18 AS app

RUN apk add --no-cache libstdc++ openssl ncurses-libs

WORKDIR /app

# Create non-root user
RUN addgroup -g 1000 shellshare && \
    adduser -u 1000 -G shellshare -s /bin/sh -D shellshare

# Copy release from builder
COPY --from=builder --chown=shellshare:shellshare /app/_build/prod/rel/shellshare ./

USER shellshare

ENV HOME=/app
ENV PORT=4000

EXPOSE 4000

CMD ["bin/shellshare", "start"]
