# Stage 1: Build the Rust binary
FROM rust:1.83-slim AS builder

WORKDIR /build

# Copy entire project (rust-embed needs ../public/ relative to rust-server/)
COPY . .

# Build release binary
WORKDIR /build/rust-server
RUN cargo build --release

# Stage 2: Runtime image
FROM debian:bookworm-slim

WORKDIR /app

# Copy the compiled binary
COPY --from=builder /build/rust-server/target/release/shellshare .

EXPOSE 3000

CMD ["./shellshare", "server", "--host", "0.0.0.0"]
