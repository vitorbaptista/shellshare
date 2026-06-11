# Stage 1: Build the Rust binary
FROM rust:1-slim-bookworm AS builder

WORKDIR /build

# Copy entire project
COPY . .

# Build release binary
RUN cargo build --release

# Stage 2: Runtime image
FROM debian:bookworm-slim

WORKDIR /app

# Copy the compiled binary
COPY --from=builder /build/target/release/shellshare .

ENV PORT=3000
EXPOSE $PORT

CMD ["sh", "-c", "./shellshare server --host 0.0.0.0 --port $PORT"]
