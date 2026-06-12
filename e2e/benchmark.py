"""
Performance benchmark for shellshare.

Measures the full client -> server -> viewer pipeline against a dedicated
server spawned from the release binary. Not a pytest test: run it directly.

    cd e2e && uv run python benchmark.py --label baseline --out bench-baseline.json
    cd e2e && uv run python benchmark.py --label optimized --out bench-optimized.json --compare bench-baseline.json

Scenarios:
- ingest_ack_rtt:     binary frame -> server ack round-trip on one
                      WebSocket connection (the client's send unit)
- broadcast_latency:  frame sent -> viewer WebSocket message received
- cli_latency:        byte written to the CLI's PTY -> received by a viewer
                      (the headline number: the full real pipeline)
- ingest_throughput:  4KB frames streamed on one connection until the
                      final ack confirms everything is stored
- cli_throughput:     ~2MB of shell output through the full pipeline
- join_catchup:       late-joiner join -> full 100-message history received
"""

import argparse
import json
import os
import pty
import statistics
import subprocess
import sys
import threading
import time

from conftest import (
    CLI_PATH,
    SocketListener,
    _free_port,
    _spawn_server,
    decode_message,
    random_id,
    wait_for_content,
    wait_for_server,
    ws_connect_room,
)


def percentiles(samples_ms):
    s = sorted(samples_ms)
    pick = lambda q: s[min(len(s) - 1, int(q * len(s)))]
    return {
        "n": len(s),
        "mean_ms": round(statistics.fmean(s), 3),
        "p50_ms": round(pick(0.50), 3),
        "p95_ms": round(pick(0.95), 3),
        "p99_ms": round(pick(0.99), 3),
        "max_ms": round(s[-1], 3),
    }


def bench_ingest_ack_rtt(url, iters=300):
    room, password = f"bench-{random_id()}", random_id()
    ws = ws_connect_room(url, room, password)
    try:
        samples = []
        for i in range(iters + 20):
            payload = f"rtt-{i} ".encode() * 8
            t0 = time.perf_counter()
            ws.send_binary(payload)
            ws.recv()  # the server's ack: the frame is stored
            dt = (time.perf_counter() - t0) * 1000
            if i >= 20:  # warmup
                samples.append(dt)
        return percentiles(samples)
    finally:
        ws.close()


def bench_broadcast_latency(url, iters=200):
    room, password = f"bench-{random_id()}", random_id()
    ws = ws_connect_room(url, room, password)
    listener = SocketListener(room, server_url=url)
    listener.connect()
    try:
        samples = []
        for i in range(iters + 10):
            marker = f"lat-{i}-{random_id(6)}"
            t0 = time.perf_counter()
            ws.send_binary(marker.encode())
            ok = wait_for_content(listener, lambda acc, m=marker: m in acc, timeout=5)
            dt = (time.perf_counter() - t0) * 1000
            assert ok, f"marker {marker} never arrived"
            ws.recv()  # consume the ack outside the timed window
            if i >= 10:
                samples.append(dt)
        return percentiles(samples)
    finally:
        ws.close()
        listener.disconnect()


class CliSession:
    """A shellshare CLI running in script mode under a real PTY."""

    def __init__(self, url, room, password):
        self.master, slave = pty.openpty()
        env = dict(os.environ, SHELL="/bin/sh", PS1="$ ", TERM="xterm")
        self.proc = subprocess.Popen(
            [str(CLI_PATH), "--server", url, "--room", room, "--password", password],
            stdin=slave,
            stdout=slave,
            stderr=slave,
            env=env,
            close_fds=True,
        )
        os.close(slave)
        # Drain local output so the CLI never blocks writing to the PTY
        self._drain = threading.Thread(target=self._drain_master, daemon=True)
        self._drain.start()

    def _drain_master(self):
        try:
            while True:
                if not os.read(self.master, 65536):
                    break
        except OSError:
            pass

    def write(self, data: bytes):
        os.write(self.master, data)

    def close(self):
        self.proc.terminate()
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait()
        os.close(self.master)


def bench_cli_latency(url, iters=30):
    room, password = f"bench-{random_id()}", random_id()
    listener = SocketListener(room, server_url=url)
    cli = CliSession(url, room, password)
    try:
        listener.connect()
        # Wait for the shell prompt to flow through the pipeline
        assert wait_for_content(listener, lambda acc: len(acc) > 0, timeout=10)

        samples = []
        for i in range(iters + 3):
            # Raw bytes typed at the terminal: the line discipline echoes
            # them, so they traverse PTY -> client batcher -> POST ->
            # the viewer WebSocket without shell command execution noise.
            marker = f"m{i}x{random_id(6)}"
            t0 = time.perf_counter()
            cli.write(marker.encode())
            ok = wait_for_content(listener, lambda acc, m=marker: m in acc, timeout=10)
            dt = (time.perf_counter() - t0) * 1000
            assert ok, f"marker {marker} never arrived"
            cli.write(b"\x15")  # ctrl-u: clear the line for the next round
            if i >= 3:
                samples.append(dt)
        return percentiles(samples)
    finally:
        cli.close()
        listener.disconnect()


def bench_ingest_throughput(url, iters=300, chunk_size=4096):
    room, password = f"bench-{random_id()}", random_id()
    chunk = b"x" * chunk_size
    total = iters * chunk_size
    ws = ws_connect_room(url, room, password, timeout=60)
    try:
        t0 = time.perf_counter()
        for _ in range(iters):
            ws.send_binary(chunk)
        # Acks are cumulative bytes; the run is done when the final ack
        # confirms every frame is stored
        while json.loads(ws.recv())["ack"] < total:
            pass
        elapsed = time.perf_counter() - t0
    finally:
        ws.close()
    total_mb = total / 1e6
    return {
        "n": iters,
        "total_mb": round(total_mb, 2),
        "elapsed_s": round(elapsed, 3),
        "mb_per_s": round(total_mb / elapsed, 2),
        "frames_per_s": round(iters / elapsed, 1),
    }


def bench_cli_throughput(url, total_bytes=2_000_000):
    room, password = f"bench-{random_id()}", random_id()
    listener = SocketListener(room, server_url=url)
    cli = CliSession(url, room, password)
    try:
        listener.connect()
        assert wait_for_content(listener, lambda acc: len(acc) > 0, timeout=10)
        listener.clear_messages()

        marker = f"DONE{random_id(8)}"
        # Generate ~total_bytes of output, then the end marker. The marker
        # is split with '' in the command so the echoed input line doesn't
        # contain it - only the actual echo output does.
        cmd = (
            f"head -c {total_bytes} /dev/zero | tr '\\0' 'x'; "
            f"echo; echo {marker[:4]}''{marker[4:]}\n"
        )
        t0 = time.perf_counter()
        cli.write(cmd.encode())
        # Decode each message exactly once (wait_for_content re-decodes
        # everything per event, which would make the harness the bottleneck)
        received = 0
        seen = 0
        tail = ""  # rolling window: the marker may straddle two messages
        deadline = time.time() + 120
        ok = False
        while not ok and time.time() < deadline:
            with listener._condition:
                if seen == len(listener._messages):
                    listener._condition.wait(timeout=0.5)
                new = listener._messages[seen:]
                seen += len(new)
            for raw in new:
                decoded = decode_message(raw)
                received += len(decoded)
                tail = (tail + decoded)[-2 * len(marker):]
                if marker in tail:
                    ok = True
        elapsed = time.perf_counter() - t0
        assert ok, "end marker never arrived"
        return {
            "sent_mb": round(total_bytes / 1e6, 2),
            "received_mb": round(received / 1e6, 2),
            "elapsed_s": round(elapsed, 3),
            "mb_per_s": round(received / 1e6 / elapsed, 2),
        }
    finally:
        cli.close()
        listener.disconnect()


def bench_join_catchup(url, iters=10, history_messages=100, message_size=2048):
    room, password = f"bench-{random_id()}", random_id()
    payload = "h" * (message_size - 4)
    ws = ws_connect_room(url, room, password)
    try:
        for i in range(history_messages):
            ws.send_binary(f"{i:03d}:{payload}".encode())
            ws.recv()  # ack: stored before the joins are timed
    finally:
        ws.close()

    samples = []
    for _ in range(iters):
        listener = SocketListener(room, server_url=url)
        try:
            t0 = time.perf_counter()
            listener.connect()  # connect + join + receive history
            ok = wait_for_content(
                listener, lambda acc: f"{history_messages - 1:03d}:" in acc, timeout=10
            )
            dt = (time.perf_counter() - t0) * 1000
            assert ok, "history never arrived"
            samples.append(dt)
        finally:
            listener.disconnect()
    return percentiles(samples)


SCENARIOS = {
    "ingest_ack_rtt": bench_ingest_ack_rtt,
    "broadcast_latency": bench_broadcast_latency,
    "cli_latency": bench_cli_latency,
    "ingest_throughput": bench_ingest_throughput,
    "cli_throughput": bench_cli_throughput,
    "join_catchup": bench_join_catchup,
}


def print_comparison(results, baseline):
    print(f"\n=== Comparison vs {baseline['label']} ===")
    for name, current in results.items():
        before = baseline["results"].get(name)
        if not before:
            continue
        for key in ("p50_ms", "p95_ms", "p99_ms", "mb_per_s"):
            if key in current and key in before and before[key]:
                change = (current[key] - before[key]) / before[key] * 100
                better = change < 0 if key.endswith("_ms") else change > 0
                arrow = "✓" if better else "✗"
                print(
                    f"  {name}.{key}: {before[key]} -> {current[key]} "
                    f"({change:+.1f}%) {arrow}"
                )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--label", default="run")
    parser.add_argument("--out", help="write results JSON to this file")
    parser.add_argument("--compare", help="baseline JSON to compare against")
    parser.add_argument(
        "--only", nargs="*", choices=sorted(SCENARIOS), help="run only these scenarios"
    )
    args = parser.parse_args()

    if not CLI_PATH.exists():
        sys.exit(f"binary not found at {CLI_PATH}; run `cargo build --release`")

    port = _free_port()
    server = _spawn_server(port)
    url = f"http://127.0.0.1:{port}"
    try:
        wait_for_server(url)
        print(f"Benchmarking {CLI_PATH} against {url}\n")

        results = {}
        for name, fn in SCENARIOS.items():
            if args.only and name not in args.only:
                continue
            print(f"  {name}...", end=" ", flush=True)
            results[name] = fn(url)
            print(json.dumps(results[name]))
    finally:
        server.terminate()
        try:
            server.wait(timeout=5)
        except subprocess.TimeoutExpired:
            server.kill()
            server.wait()

    payload = {"label": args.label, "results": results}
    if args.out:
        with open(args.out, "w") as f:
            json.dump(payload, f, indent=2)
        print(f"\nWrote {args.out}")

    if args.compare:
        with open(args.compare) as f:
            print_comparison(results, json.load(f))


if __name__ == "__main__":
    main()
