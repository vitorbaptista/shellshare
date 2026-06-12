"""
Fan-out benchmark for shellshare: one broadcaster, N viewers.

Measures what benchmark.py cannot: how the server behaves while
broadcasting to dozens or hundreds of simultaneous viewers. Not a
pytest test: run it directly against a dedicated server spawned from
the release binary.

    cd e2e && uv run python benchmark_fanout.py
    cd e2e && uv run python benchmark_fanout.py --viewers 50 200 500 --out fanout.json
    cd e2e && uv run python benchmark_fanout.py --capacity   # max sustainable viewers

Per viewer count, three phases against a fresh room:

- idle:     viewers connected, broadcaster silent. Cost of just holding
            N Socket.IO connections.
- paced:    frames at a fixed rate (default 30/s of 256 B - a busy TUI).
            Reports broadcaster-send -> viewer-receive latency pooled
            across every viewer and frame, plus frame loss and server CPU.
- firehose: ~4 MB of 4 KB frames sent flat out (`cat bigfile`).
            Reports ingest throughput (ack-complete), delivery
            throughput (last viewer sees the end marker), frame loss,
            and server CPU.

Viewers are spread over worker subprocesses (FANOUT_VIEWERS_PER_WORKER,
default 50) so the harness's Python GIL does not inflate the measured
latencies - and it still does, a little: 50 viewers per process adds
~10 ms to the latency percentiles; 10 per process adds ~2 ms. Use
fewer viewers per worker when latency is the number under study, more
when connection scale is. Loss counts are immune to this. Each
worker speaks minimal Socket.IO over a raw WebSocket - no
python-socketio client threads - and answers engine.io pings.

Frame loss is real signal, not harness noise: delivery to each viewer
is ordered, so a viewer that saw the end marker but counted fewer
frames than were sent genuinely lost them (e.g. a full per-socket
buffer on the server).
"""

import argparse
import json
import os
import statistics
import subprocess
import sys
import threading
import time

import websocket

from conftest import (
    CLI_PATH,
    _free_port,
    _spawn_server,
    random_id,
    wait_for_server,
    ws_connect_room,
)

VIEWERS_PER_WORKER = int(os.environ.get("FANOUT_VIEWERS_PER_WORKER", "50"))
CLK_TCK = os.sysconf("SC_CLK_TCK")


# --------------------------------------------------------------------
# Worker: a batch of viewers in their own process
# --------------------------------------------------------------------

class Viewer:
    """Minimal Socket.IO viewer over a raw WebSocket.

    Joins the room, then records every binary frame it receives:
    paced frames (`T:<seq>:<t_send>:...`) yield a latency sample,
    firehose frames (`F:<seq>:...`) a byte count. The end marker
    (`END:<paced_total>:<fire_total>`) stops the viewer: delivery is
    ordered per connection, so nothing arrives after it.
    """

    def __init__(self, server_url, room):
        self.server_url = server_url
        self.room = room
        self.latencies = []
        self.paced_count = 0
        self.fire_count = 0
        self.fire_bytes = 0
        self.saw_end = False
        self.disconnected = False
        self.end_at = None
        self.last_activity = time.time()
        self._ws = None
        self._thread = None

    def connect(self, timeout=15):
        ws_url = self.server_url.replace("http://", "ws://")
        self._ws = websocket.create_connection(
            f"{ws_url}/socket.io/?EIO=4&transport=websocket", timeout=timeout
        )
        opening = self._ws.recv()  # engine.io open packet
        assert opening.startswith("0"), f"unexpected engine.io opening: {opening!r}"
        self._ws.send("40")  # socket.io connect to the default namespace
        deadline = time.time() + timeout
        joined = False
        join = json.dumps(["join", f"/r/{self.room}"])
        while time.time() < deadline:
            frame = self._ws.recv()
            if isinstance(frame, str):
                if frame.startswith("40"):  # connect ack: now join
                    self._ws.send(f"42{join}")
                elif frame.startswith('42["usersCount"'):  # join confirmed
                    joined = True
                    break
                elif frame == "2":
                    self._ws.send("3")
        assert joined, "join never confirmed by usersCount"

    def start(self, stop_event):
        self._thread = threading.Thread(
            target=self._run, args=(stop_event,), daemon=True
        )
        self._thread.start()

    def _run(self, stop_event):
        self._ws.settimeout(0.5)
        while not stop_event.is_set() and not self.saw_end:
            try:
                frame = self._ws.recv()
            except websocket.WebSocketTimeoutException:
                continue
            except (websocket.WebSocketException, OSError):
                self.disconnected = True
                break
            self.last_activity = time.time()
            if isinstance(frame, str):
                if frame == "2":  # engine.io ping
                    try:
                        self._ws.send("3")
                    except (websocket.WebSocketException, OSError):
                        self.disconnected = True
                        break
                continue
            self._on_payload(bytes(frame))
        try:
            self._ws.close()
        except (websocket.WebSocketException, OSError):
            pass

    def _on_payload(self, data):
        # The server may coalesce queued chunks into one frame (the
        # client's sender thread does the same), so count embedded
        # chunk markers, not frames. Chunk payloads are x/y padding,
        # so the markers cannot occur mid-chunk.
        now = time.time()
        if b"T:" in data:
            for chunk in data.split(b"T:")[1:]:
                _seq, t_send, _ = chunk.split(b":", 2)
                self.latencies.append((now - float(t_send)) * 1000)
                self.paced_count += 1
        fire_chunks = data.count(b"F:")
        if fire_chunks:
            self.fire_count += fire_chunks
            self.fire_bytes += len(data)
        if b"END:" in data:
            self.saw_end = True
            self.end_at = now

    def join(self, timeout):
        self._thread.join(timeout)

    def result(self):
        return {
            "paced_count": self.paced_count,
            "fire_count": self.fire_count,
            "fire_bytes": self.fire_bytes,
            "saw_end": self.saw_end,
            "end_at": self.end_at,
            "disconnected": self.disconnected,
        }


def run_worker(config):
    """Worker process body: connect viewers, listen until the end
    marker (or the deadline), report one JSON line on stdout."""
    viewers = []
    for _ in range(config["count"]):
        v = Viewer(config["server_url"], config["room"])
        v.connect()
        viewers.append(v)
    stop = threading.Event()
    for v in viewers:
        v.start(stop)
    print(f"READY {len(viewers)}", flush=True)

    # A connect storm leaves a backlog of usersCount chatter queued for
    # every viewer; measuring before it drains charges the join phase
    # to the broadcast phase. Report quiet once every viewer has gone
    # 1.5s without receiving anything.
    quiet_deadline = time.time() + 120
    while time.time() < quiet_deadline:
        now = time.time()
        if all(now - v.last_activity > 1.5 for v in viewers):
            break
        time.sleep(0.2)
    print("QUIET", flush=True)

    deadline = time.time() + config["deadline_s"]
    for v in viewers:
        v.join(max(0.1, deadline - time.time()))
    stop.set()
    for v in viewers:
        v.join(2)

    latencies = []
    for v in viewers:
        latencies.extend(v.latencies)
    print(json.dumps({
        "viewers": [v.result() for v in viewers],
        "latencies": latencies,
    }), flush=True)


# --------------------------------------------------------------------
# Orchestrator
# --------------------------------------------------------------------

class ServerStats:
    """CPU and memory of the server process, via /proc."""

    def __init__(self, pid):
        self.pid = pid

    def cpu_seconds(self):
        with open(f"/proc/{self.pid}/stat") as f:
            fields = f.read().rsplit(")", 1)[1].split()
        # utime + stime are fields 14 and 15 (1-based); 11 and 12 here
        return (int(fields[11]) + int(fields[12])) / CLK_TCK

    def sample(self):
        return (self.cpu_seconds(), time.time())

    @staticmethod
    def avg_cores(before, after):
        cpu = after[0] - before[0]
        wall = after[1] - before[1]
        return round(cpu / wall, 3) if wall > 0 else 0.0

    def memory_mb(self):
        rss = hwm = 0
        with open(f"/proc/{self.pid}/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    rss = int(line.split()[1])
                elif line.startswith("VmHWM:"):
                    hwm = int(line.split()[1])
        return {"rss_mb": round(rss / 1024, 1), "peak_mb": round(hwm / 1024, 1)}

    def open_fds(self):
        return len(os.listdir(f"/proc/{self.pid}/fd"))


class Broadcaster:
    """The ingest WebSocket, with a drain thread tracking the
    cumulative ack so sends never block on reads."""

    def __init__(self, url, room, password):
        self.ws = ws_connect_room(url, room, password, timeout=60)
        self.sent_bytes = 0
        self.acked = 0
        self._lock = threading.Condition()
        self._stop = False
        self._drain = threading.Thread(target=self._drain_acks, daemon=True)
        self._drain.start()

    def _drain_acks(self):
        self.ws.settimeout(0.5)
        while not self._stop:
            try:
                frame = self.ws.recv()
            except websocket.WebSocketTimeoutException:
                continue
            except (websocket.WebSocketException, OSError):
                break
            try:
                ack = json.loads(frame).get("ack")
            except (ValueError, TypeError, AttributeError):
                continue  # a dead drain thread turns into opaque timeouts
            if ack is not None:
                with self._lock:
                    self.acked = ack
                    self._lock.notify_all()

    def send(self, payload):
        self.ws.send_binary(payload)
        self.sent_bytes += len(payload)

    def wait_acked(self, timeout=60):
        with self._lock:
            deadline = time.time() + timeout
            while self.acked < self.sent_bytes:
                remaining = deadline - time.time()
                if remaining <= 0:
                    raise TimeoutError(
                        f"ack stalled at {self.acked}/{self.sent_bytes} bytes"
                    )
                self._lock.wait(remaining)

    def close(self):
        self._stop = True
        self.ws.close()


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


def spawn_workers(server_url, room, n_viewers, deadline_s):
    workers = []
    remaining = n_viewers
    while remaining > 0:
        count = min(VIEWERS_PER_WORKER, remaining)
        remaining -= count
        config = json.dumps({
            "server_url": server_url,
            "room": room,
            "count": count,
            "deadline_s": deadline_s,
        })
        workers.append(subprocess.Popen(
            [sys.executable, os.path.abspath(__file__), "--worker", config],
            stdout=subprocess.PIPE,
            stderr=sys.stderr,
            text=True,
            cwd=os.path.dirname(os.path.abspath(__file__)),
        ))
    return workers


def run_trial(url, server_pid, n_viewers, args):
    room, password = f"fanout-{random_id()}", random_id()
    stats = ServerStats(server_pid)

    # The broadcaster claims the room first, like a real session
    caster = Broadcaster(url, room, password)
    # Generous deadline: connect storm + quiet wait + idle + paced +
    # firehose + margin
    deadline_s = 30 + 120 + 3 + args.duration + 60
    workers = spawn_workers(url, room, n_viewers, deadline_s)
    try:
        connected = 0
        for w in workers:
            line = w.stdout.readline().strip()
            assert line.startswith("READY"), f"worker failed: {line!r}"
            connected += int(line.split()[1])
        for w in workers:
            line = w.stdout.readline().strip()
            assert line == "QUIET", f"worker failed: {line!r}"
        print(f"    {connected} viewers connected and quiet", flush=True)

        # Idle: the cost of holding N connections
        idle_before = stats.sample()
        time.sleep(3)
        idle_after = stats.sample()

        # Paced: fixed-rate frames with embedded send timestamps
        pad = b"x" * args.frame_size
        paced_total = args.rate * args.duration
        paced_before = stats.sample()
        t0 = time.time()
        for seq in range(paced_total):
            target = t0 + seq / args.rate
            delay = target - time.time()
            if delay > 0:
                time.sleep(delay)
            header = f"T:{seq}:{time.time():.6f}:".encode()
            caster.send((header + pad)[: args.frame_size])
        caster.wait_acked()
        # The ack confirms storage, not delivery: fan-out may still be
        # draining. Grace before sampling so the CPU window charges the
        # paced phase with its own fan-out work
        time.sleep(1)
        paced_after = stats.sample()

        # Firehose: 4 KB frames as fast as the socket accepts them
        chunk_pad = b"y" * 4096
        fire_total = (args.firehose_mb * 1_000_000) // 4096
        fire_before = stats.sample()
        fire_start = time.time()
        for seq in range(fire_total):
            caster.send((f"F:{seq}:".encode() + chunk_pad)[:4096])
        caster.wait_acked(timeout=120)
        ingest_wall = time.time() - fire_start
        # The end marker releases the viewers; delivery is ordered, so
        # any frame a viewer never counted before END was really lost
        for _ in range(3):
            caster.send(f"END:{paced_total}:{fire_total}".encode())
            time.sleep(0.2)
        caster.wait_acked()
        # Sample before collecting results: a worker stuck on a viewer
        # that lost the end marker blocks for minutes, and folding that
        # idle time into the window would underreport CPU in exactly
        # the lossy trials. Grace first so the fan-out tail is counted
        time.sleep(1)
        fire_after = stats.sample()

        results = []
        for w in workers:
            line = w.stdout.readline()
            results.append(json.loads(line))
            w.wait(timeout=30)

        viewers = [v for r in results for v in r["viewers"]]
        latencies = [ms for r in results for ms in r["latencies"]]
        fire_sent_bytes = fire_total * 4096
        end_times = [v["end_at"] for v in viewers if v["end_at"]]
        delivery_wall = max(end_times) - fire_start if end_times else None
        firehose = None
        if fire_total:
            firehose = {
                "sent_mb": round(fire_sent_bytes / 1e6, 2),
                "ingest_s": round(ingest_wall, 3),
                "ingest_mb_s": round(fire_sent_bytes / 1e6 / ingest_wall, 2),
                "delivery_s": round(delivery_wall, 3) if delivery_wall else None,
                "fanout_mb_s": round(
                    fire_sent_bytes * len(viewers) / 1e6 / delivery_wall, 1
                ) if delivery_wall else None,
                "cpu_avg_cores": ServerStats.avg_cores(fire_before, fire_after),
                "viewers_with_loss": sum(
                    v["fire_count"] < fire_total for v in viewers
                ),
                "frames_lost": sum(
                    fire_total - v["fire_count"] for v in viewers
                ),
            }
        return {
            "viewers_requested": n_viewers,
            "viewers_connected": connected,
            "viewers_disconnected": sum(v["disconnected"] for v in viewers),
            "viewers_missed_end": sum(not v["saw_end"] for v in viewers),
            "idle": {"cpu_avg_cores": ServerStats.avg_cores(idle_before, idle_after)},
            "paced": {
                "rate_fps": args.rate,
                "frame_bytes": args.frame_size,
                "frames_sent": paced_total,
                "latency": percentiles(latencies) if latencies else None,
                "cpu_avg_cores": ServerStats.avg_cores(paced_before, paced_after),
                "viewers_with_loss": sum(
                    v["paced_count"] < paced_total for v in viewers
                ),
                "frames_lost": sum(
                    max(0, paced_total - v["paced_count"]) for v in viewers
                ),
                # Counted more frames than were sent: should never
                # happen; nonzero means delivery (or the harness)
                # duplicated frames and the trial deserves a close look
                "frames_duplicated": sum(
                    max(0, v["paced_count"] - paced_total) for v in viewers
                ),
            },
            "firehose": firehose,
            "server": {**stats.memory_mb(), "open_fds": stats.open_fds()},
        }
    finally:
        caster.close()
        for w in workers:
            if w.poll() is None:
                w.terminate()
        for w in workers:
            try:
                w.wait(timeout=10)
            except subprocess.TimeoutExpired:
                w.kill()
                w.wait()


def evaluate_capacity(trial, max_p99_ms):
    """Pass/fail a trial against the capacity criteria. Loss and
    disconnects are server-side signal; the latency bound is generous
    because at high viewer counts the Python harness inflates it."""
    failures = []
    if trial["viewers_connected"] < trial["viewers_requested"]:
        failures.append(f"only {trial['viewers_connected']} connected")
    if trial["viewers_disconnected"]:
        failures.append(f"{trial['viewers_disconnected']} disconnected")
    if trial["viewers_missed_end"]:
        failures.append(f"{trial['viewers_missed_end']} never saw the end marker")
    if trial["paced"]["frames_lost"]:
        failures.append(f"{trial['paced']['frames_lost']} frames lost")
    if trial["paced"]["frames_duplicated"]:
        failures.append(f"{trial['paced']['frames_duplicated']} frames duplicated")
    latency = trial["paced"]["latency"]
    if latency and latency["p99_ms"] > max_p99_ms:
        failures.append(f"p99 {latency['p99_ms']}ms > {max_p99_ms}ms")
    return failures


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--worker", help=argparse.SUPPRESS)
    parser.add_argument("--viewers", nargs="*", type=int, default=None)
    parser.add_argument("--rate", type=int, default=30, help="paced frames/s")
    parser.add_argument(
        "--frame-size", type=int, default=256,
        help="paced frame bytes (>= 32, or the marker header gets truncated)",
    )
    parser.add_argument("--duration", type=int, default=12, help="paced seconds")
    parser.add_argument("--firehose-mb", type=int, default=4, help="0 skips the phase")
    parser.add_argument(
        "--capacity", action="store_true",
        help="ramp viewer counts until a trial fails the criteria "
        "(connects, no disconnects, no loss, p99 under --max-p99); "
        "reports the largest count that passed. Skips the firehose.",
    )
    parser.add_argument("--max-p99", type=float, default=250.0, help="ms, capacity bound")
    parser.add_argument("--label", default="run")
    parser.add_argument("--out", help="write results JSON to this file")
    args = parser.parse_args()

    if args.worker:
        run_worker(json.loads(args.worker))
        return

    if not CLI_PATH.exists():
        sys.exit(f"binary not found at {CLI_PATH}; run `cargo build --release`")

    if args.capacity:
        args.firehose_mb = 0
        steps = args.viewers or [250, 500, 1000, 1500, 2000, 3000, 4000]
    else:
        steps = args.viewers or [50, 200, 500]

    port = _free_port()
    server = _spawn_server(port)
    url = f"http://127.0.0.1:{port}"
    results = {}
    try:
        wait_for_server(url)
        print(f"Fan-out benchmark against {url} (server pid {server.pid})\n")
        max_passing = None
        for n in steps:
            print(f"  viewers={n}...", flush=True)
            results[n] = run_trial(url, server.pid, n, args)
            if args.capacity:
                failures = evaluate_capacity(results[n], args.max_p99)
                results[n]["capacity_failures"] = failures
                lat = results[n]["paced"]["latency"]
                summary = (
                    f"p50 {lat['p50_ms']}ms p99 {lat['p99_ms']}ms, "
                    f"cpu {results[n]['paced']['cpu_avg_cores']} cores, "
                    f"rss {results[n]['server']['rss_mb']}MB"
                ) if lat else "no latency samples"
                if failures:
                    print(f"    FAIL ({'; '.join(failures)}) - {summary}", flush=True)
                    break
                max_passing = n
                print(f"    PASS - {summary}", flush=True)
            else:
                print(f"    {json.dumps(results[n], indent=2)}", flush=True)
        if args.capacity:
            results["max_viewers"] = max_passing
            print(f"\n  Max sustainable viewers: {max_passing}", flush=True)
    finally:
        server.terminate()
        try:
            server.wait(timeout=5)
        except subprocess.TimeoutExpired:
            server.kill()
            server.wait()

    if args.out:
        with open(args.out, "w") as f:
            json.dump({"label": args.label, "results": results}, f, indent=2)
        print(f"\nWrote {args.out}")


if __name__ == "__main__":
    main()
