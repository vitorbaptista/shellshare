//! Viewer-swarm load generator for `benchmark_fanout.py`.
//!
//! Drop-in replacement for the benchmark's Python viewer workers: the
//! Python harness tops out around 60k frame-deliveries/sec across all
//! its worker processes, which caps measured server capacity well below
//! the server's actual limit. This binary runs thousands of viewers in
//! one async process and speaks the exact same stdout protocol:
//!
//!     READY <connected>      after every viewer joined (or failed)
//!     QUIET                  once no viewer has received anything for 1.5s
//!     {"viewers": [...], "latencies": [...]}     final result JSON
//!
//! Usage: fanout-loadgen <server_url> <room> <count> <deadline_s>
//!
//! Each viewer speaks minimal Socket.IO over a raw WebSocket (engine.io
//! open -> "40" connect -> join -> usersCount confirmation), then counts
//! frames by their embedded chunk markers, mirroring the Python
//! `Viewer._on_payload` exactly (the server may coalesce chunks, so
//! markers are counted inside each frame: `T:<seq>:<t_send>:` paced
//! chunks yield latency samples, `F:` firehose chunks are counted,
//! `END:` stops the viewer).

use futures_util::{SinkExt, StreamExt};
use std::sync::atomic::{AtomicU64, AtomicUsize, Ordering};
use std::sync::Arc;
use std::time::{Duration, SystemTime, UNIX_EPOCH};
use tokio_tungstenite::tungstenite::Message;

fn now() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .expect("clock before epoch")
        .as_secs_f64()
}

/// Wall-clock state shared with the QUIET monitor, as epoch millis.
struct Activity {
    last_ms: AtomicU64,
}

impl Activity {
    fn touch(&self) {
        self.last_ms.store((now() * 1000.0) as u64, Ordering::Relaxed);
    }
    fn idle_for(&self, secs: f64) -> bool {
        let last = self.last_ms.load(Ordering::Relaxed) as f64 / 1000.0;
        now() - last > secs
    }
}

#[derive(Default)]
struct Outcome {
    paced_count: u64,
    fire_count: u64,
    fire_bytes: u64,
    saw_end: bool,
    end_at: Option<f64>,
    disconnected: bool,
    latencies: Vec<f64>,
}

/// Positions of `pat` in `data` (chunk markers never straddle anything:
/// the server coalesces whole frames and padding is x/y bytes).
fn find_all(data: &[u8], pat: &[u8]) -> Vec<usize> {
    let mut out = Vec::new();
    let mut i = 0;
    while i + pat.len() <= data.len() {
        if &data[i..i + pat.len()] == pat {
            out.push(i);
            i += pat.len();
        } else {
            i += 1;
        }
    }
    out
}

fn handle_payload(data: &[u8], outcome: &mut Outcome) {
    let received = now();
    for pos in find_all(data, b"T:") {
        // T:<seq>:<t_send>:  - parse the timestamp between the 2nd and
        // 3rd colon after the marker
        let rest = &data[pos + 2..];
        let mut parts = rest.splitn(3, |&b| b == b':');
        let _seq = parts.next();
        if let Some(ts) = parts.next() {
            if let Ok(t_send) = std::str::from_utf8(ts).unwrap_or("x").parse::<f64>() {
                outcome.latencies.push((received - t_send) * 1000.0);
                outcome.paced_count += 1;
            }
        }
    }
    let fire_chunks = find_all(data, b"F:").len() as u64;
    if fire_chunks > 0 {
        outcome.fire_count += fire_chunks;
        outcome.fire_bytes += data.len() as u64;
    }
    if !find_all(data, b"END:").is_empty() {
        outcome.saw_end = true;
        outcome.end_at = Some(received);
    }
}

type WsStream =
    tokio_tungstenite::WebSocketStream<tokio_tungstenite::MaybeTlsStream<tokio::net::TcpStream>>;

/// The real CLI sets TCP_NODELAY on its ingest socket (`cli/ws.rs`);
/// without it, per-frame sends stall on Nagle/delayed-ACK timers and
/// the harness measures the kernel's coalescing, not the server.
fn set_nodelay(stream: &WsStream) {
    if let tokio_tungstenite::MaybeTlsStream::Plain(tcp) = stream.get_ref() {
        let _ = tcp.set_nodelay(true);
    }
}

/// Connect and complete the handshake: engine.io open -> socket.io
/// connect ("40") -> join -> usersCount confirmation. Runs under the
/// connect-rate semaphore; the receive loop does not.
async fn connect_and_join(ws_url: &str, room: &str, deadline: f64) -> Result<WsStream, ()> {
    let Ok((mut stream, _)) = tokio_tungstenite::connect_async(ws_url).await else {
        return Err(());
    };
    set_nodelay(&stream);
    let mut connected = false;
    let join = format!("42[\"join\",\"/r/{room}\"]");
    while now() < deadline {
        let frame = match tokio::time::timeout(Duration::from_millis(500), stream.next()).await {
            Err(_) => continue, // poll the deadline
            Ok(Some(Ok(frame))) => frame,
            Ok(_) => return Err(()),
        };
        match frame {
            Message::Text(text) => {
                if text == "2" {
                    stream.send(Message::Text("3".into())).await.map_err(|_| ())?;
                } else if !connected && text.starts_with('0') {
                    stream.send(Message::Text("40".into())).await.map_err(|_| ())?;
                } else if !connected && text.starts_with("40") {
                    connected = true;
                    stream
                        .send(Message::Text(join.clone()))
                        .await
                        .map_err(|_| ())?;
                } else if text.starts_with("42[\"usersCount\"") {
                    return Ok(stream);
                }
            }
            Message::Close(_) => return Err(()),
            _ => {}
        }
    }
    Err(())
}

/// The receive loop: count frames until the end marker or the deadline.
async fn viewer(mut stream: WsStream, activity: Arc<Activity>, deadline: f64) -> Outcome {
    let mut outcome = Outcome::default();
    while now() < deadline && !outcome.saw_end {
        let frame = match tokio::time::timeout(Duration::from_millis(500), stream.next()).await {
            Err(_) => continue, // poll the deadline
            Ok(None) => {
                outcome.disconnected = true;
                break;
            }
            Ok(Some(Err(_))) => {
                outcome.disconnected = true;
                break;
            }
            Ok(Some(Ok(frame))) => frame,
        };
        activity.touch();
        match frame {
            Message::Text(text) => {
                if text == "2" {
                    // engine.io ping
                    if stream.send(Message::Text("3".into())).await.is_err() {
                        outcome.disconnected = true;
                        break;
                    }
                }
            }
            Message::Binary(data) => handle_payload(&data, &mut outcome),
            Message::Ping(_) | Message::Pong(_) | Message::Frame(_) => {}
            Message::Close(_) => {
                outcome.disconnected = true;
                break;
            }
        }
    }
    outcome
}

/// Broadcaster mode: drives the ingest WebSocket from Rust so the
/// harness can saturate the server (a Python broadcaster thread caps
/// out far below the server's ingest limit, especially 16 of them
/// sharing a GIL). Commands arrive on stdin, one per line; each prints
/// `DONE <wall_seconds>` when the phase completes (ack-complete for
/// sends):
///
///     PACED <total> <rate> <frame_size>   paced frames with timestamps
///     FIRE <total>                        4KB frames flat out
///     END <paced_total> <fire_total>      3x end markers
///     QUIT
async fn broadcast_main(server_url: &str, room: &str, password: &str) {
    use tokio_tungstenite::tungstenite::client::IntoClientRequest;
    // Plain http only: this harness benchmarks a local server, and
    // set_nodelay is a silent no-op on TLS streams - an https URL here
    // would quietly measure Nagle stalls instead of the server
    assert!(
        server_url.starts_with("http://"),
        "loadgen only supports http:// server URLs"
    );
    let url = format!(
        "{}/ws/r/{}",
        server_url.replace("http://", "ws://"),
        room
    );
    let mut request = url.into_client_request().expect("bad ingest url");
    request.headers_mut().insert(
        "Authorization",
        password.parse().expect("bad password header"),
    );
    let (stream, _) = tokio_tungstenite::connect_async(request)
        .await
        .expect("ingest connect failed");
    set_nodelay(&stream);
    let (mut sink, mut read) = stream.split();

    // Cumulative-ack drain; phases wait on it for durability
    let (ack_tx, mut ack_rx) = tokio::sync::watch::channel(0u64);
    tokio::spawn(async move {
        while let Some(Ok(frame)) = read.next().await {
            if let Message::Text(text) = frame {
                if let Some(ack) = serde_json::from_str::<serde_json::Value>(&text)
                    .ok()
                    .and_then(|v| v.get("ack").and_then(serde_json::Value::as_u64))
                {
                    let _ = ack_tx.send(ack);
                }
            }
        }
    });
    let mut sent: u64 = 0;
    println!("CONNECTED");

    let stdin = tokio::io::stdin();
    let mut lines = tokio::io::BufReader::new(stdin);
    let mut line = String::new();
    loop {
        line.clear();
        use tokio::io::AsyncBufReadExt;
        if lines.read_line(&mut line).await.unwrap_or(0) == 0 {
            break;
        }
        let parts: Vec<&str> = line.split_whitespace().collect();
        let started = std::time::Instant::now();
        match parts.as_slice() {
            ["PACED", total, rate, frame_size] => {
                let (total, rate, frame_size): (u64, u64, usize) = (
                    total.parse().expect("bad total"),
                    rate.parse().expect("bad rate"),
                    frame_size.parse().expect("bad frame size"),
                );
                assert!(rate > 0, "rate must be positive");
                let t0 = tokio::time::Instant::now();
                for seq in 0..total {
                    let target = t0 + Duration::from_secs_f64(seq as f64 / rate as f64);
                    tokio::time::sleep_until(target).await;
                    let mut frame = format!("T:{seq}:{:.6}:", now()).into_bytes();
                    frame.resize(frame_size, b'x');
                    sent += frame.len() as u64;
                    sink.send(Message::Binary(frame)).await.expect("send failed");
                }
            }
            ["FIRE", total] => {
                let total: u64 = total.parse().expect("bad total");
                for seq in 0..total {
                    let mut frame = format!("F:{seq}:").into_bytes();
                    frame.resize(4096, b'y');
                    sent += frame.len() as u64;
                    sink.send(Message::Binary(frame)).await.expect("send failed");
                }
            }
            ["END", paced, fire] => {
                for _ in 0..3 {
                    let frame = format!("END:{paced}:{fire}").into_bytes();
                    sent += frame.len() as u64;
                    sink.send(Message::Binary(frame)).await.expect("send failed");
                    tokio::time::sleep(Duration::from_millis(200)).await;
                }
            }
            ["QUIT"] | [] => break,
            other => panic!("unknown command: {other:?}"),
        }
        // Durability point: the server has stored everything sent.
        // send= separates write-side time from ack latency: send fast
        // but acks slow means the server is slow to process or reply;
        // send itself slow means TCP backpressure from the server's
        // receive side
        let send_done = started.elapsed().as_secs_f64();
        while *ack_rx.borrow() < sent {
            ack_rx.changed().await.expect("ack stream ended early");
        }
        println!(
            "DONE {:.6} send={send_done:.6}",
            started.elapsed().as_secs_f64()
        );
    }
}

fn main() {
    // The harness must not starve the server it measures: a default
    // multi-threaded runtime per process grabs every core, and several
    // loadgen processes (plus broadcasters) once crowded the server's
    // own runtime off the machine, showing up as mysterious ack
    // latency. LOADGEN_THREADS caps the runtime; broadcasters need 1.
    let threads = std::env::var("LOADGEN_THREADS")
        .ok()
        .and_then(|v| v.parse().ok())
        .unwrap_or_else(|| {
            std::thread::available_parallelism().map_or(4, |n| n.get().min(4))
        });
    tokio::runtime::Builder::new_multi_thread()
        .worker_threads(threads)
        .enable_all()
        .build()
        .expect("runtime build failed")
        .block_on(run());
}

async fn run() {
    let args: Vec<String> = std::env::args().collect();
    if args.get(1).map(String::as_str) == Some("broadcast") {
        let [_, _, server_url, room, password] = &args[..] else {
            eprintln!("usage: fanout-loadgen broadcast <server_url> <room> <password>");
            std::process::exit(2);
        };
        broadcast_main(server_url, room, password).await;
        return;
    }
    // Viewer-swarm mode: every room's viewers in ONE process, so the
    // harness adds one runtime to the machine, not one per room.
    // roomspec: room1=count1,room2=count2,...
    let [_, server_url, roomspec, deadline_s] = &args[..] else {
        eprintln!("usage: fanout-loadgen <server_url> <room=count,...> <deadline_s>");
        std::process::exit(2);
    };
    let rooms: Vec<(String, usize)> = roomspec
        .split(',')
        .map(|pair| {
            let (room, count) = pair.split_once('=').expect("roomspec: room=count");
            (room.to_string(), count.parse().expect("bad count"))
        })
        .collect();
    let count: usize = rooms.iter().map(|(_, n)| n).sum();
    let deadline = now() + deadline_s.parse::<f64>().expect("deadline must be a number");
    let ws_url = format!(
        "{}/socket.io/?EIO=4&transport=websocket",
        server_url.replace("http://", "ws://")
    );

    let joined = Arc::new(AtomicUsize::new(0));
    let connect_limit = Arc::new(tokio::sync::Semaphore::new(256));
    let mut activities = Vec::with_capacity(count);
    let mut tasks = Vec::with_capacity(count);
    let viewer_rooms = rooms
        .iter()
        .flat_map(|(room, n)| std::iter::repeat_n(room.clone(), *n));
    for room in viewer_rooms {
        let activity = Arc::new(Activity {
            last_ms: AtomicU64::new((now() * 1000.0) as u64),
        });
        activities.push(activity.clone());
        let (ws_url, joined) = (ws_url.clone(), joined.clone());
        let limit = connect_limit.clone();
        tasks.push(tokio::spawn(async move {
            // The permit covers ONLY the handshake, so connects ramp in
            // bounded waves while joined viewers keep receiving
            let stream = {
                let _permit = limit.acquire().await.expect("semaphore closed");
                connect_and_join(&ws_url, &room, deadline).await
            };
            let Ok(stream) = stream else {
                return Outcome {
                    disconnected: true,
                    ..Outcome::default()
                };
            };
            joined.fetch_add(1, Ordering::Relaxed);
            viewer(stream, activity, deadline).await
        }));
    }

    // READY once every viewer has either joined or given up trying:
    // failures show up as connected < requested in the orchestrator
    let ready_deadline = now() + 120.0;
    while now() < ready_deadline {
        let ok = joined.load(Ordering::Relaxed);
        let dead = tasks.iter().filter(|t| t.is_finished()).count();
        if ok + dead >= count {
            break;
        }
        tokio::time::sleep(Duration::from_millis(100)).await;
    }
    println!("READY {}", joined.load(Ordering::Relaxed));

    // QUIET once the join-storm backlog has drained everywhere
    let quiet_deadline = now() + 120.0;
    while now() < quiet_deadline {
        if activities.iter().all(|a| a.idle_for(1.5)) {
            break;
        }
        tokio::time::sleep(Duration::from_millis(200)).await;
    }
    println!("QUIET");

    let mut viewers = Vec::with_capacity(count);
    let mut latencies = Vec::new();
    for task in tasks {
        let outcome = task.await.unwrap_or_default();
        latencies.extend_from_slice(&outcome.latencies);
        viewers.push(serde_json::json!({
            "paced_count": outcome.paced_count,
            "fire_count": outcome.fire_count,
            "fire_bytes": outcome.fire_bytes,
            "saw_end": outcome.saw_end,
            "end_at": outcome.end_at,
            "disconnected": outcome.disconnected,
        }));
    }
    println!(
        "{}",
        serde_json::json!({ "viewers": viewers, "latencies": latencies })
    );
}
