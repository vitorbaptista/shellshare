//! Script mode for shellshare CLI
//!
//! Spawns a PTY and streams output to the server, similar to the `script` command.
//! Output is displayed locally AND sent to the server for remote viewing.

#![allow(unsafe_code)] // PTY handling requires unsafe for terminal control

use crate::cli::get_terminal_size;
use crate::cli::ws;
use crate::protocol::TermSize;

use portable_pty::{native_pty_system, CommandBuilder, PtySize};
use std::io::{Read, Write};
use std::sync::atomic::{AtomicBool, AtomicU16, Ordering};
use std::sync::mpsc;
use std::sync::Arc;
use std::thread;
use std::time::{Duration, Instant};

#[cfg(unix)]
use std::os::fd::{AsRawFd, FromRawFd, OwnedFd};

#[cfg(unix)]
use signal_hook::consts::SIGWINCH;
#[cfg(unix)]
use signal_hook::iterator::{Signals, SignalsInfo};

/// Bounds teardown, not throughput: the wait ends the moment the terminal
/// has bytes, so it costs no output latency.
#[cfg(unix)]
const POLL_TIMEOUT_MS: libc::c_int = 100;

#[cfg(unix)]
enum Wait {
    Readable,
    Again,
    Failed,
}

/// Hang-up counts as readable and must be read: the last output of a short
/// command arrives with `POLLHUP` already set beside `POLLIN`, so treating
/// it as the end would truncate the tail of every `exec -- echo hi`. The
/// read that follows reports the real end.
///
/// A signal means "wait again" - `SA_RESTART` covers `read` but not
/// `poll`, so a terminal resize interrupts this routinely. Anything else
/// means we can no longer tell when the terminal has something to say:
/// end the session, as a failing read always did, rather than stall on a
/// wait that will keep failing or fall through to a read nothing can
/// interrupt.
#[cfg(unix)]
fn wait_readable(fd: &OwnedFd, timeout_ms: libc::c_int) -> Wait {
    let mut pollfd = libc::pollfd {
        fd: fd.as_raw_fd(),
        events: libc::POLLIN,
        revents: 0,
    };
    // SAFETY: one valid, initialized entry, and `fd` outlives the call.
    let ready = unsafe { libc::poll(&raw mut pollfd, 1, timeout_ms) };
    match ready {
        0 => Wait::Again,
        n if n > 0 => Wait::Readable,
        _ if std::io::Error::last_os_error().kind() == std::io::ErrorKind::Interrupted => {
            Wait::Again
        }
        _ => Wait::Failed,
    }
}

/// RAII guard to enable/restore terminal raw mode.
///
/// `portable_pty` handles the PTY (slave side), but the **user's terminal**
/// (master side) also needs raw mode for character-by-character input and
/// escape sequence handling - which is what interactive apps like vim need.
#[cfg(unix)]
struct RawModeGuard {
    original: Option<libc::termios>,
}

#[cfg(unix)]
impl RawModeGuard {
    fn new() -> Self {
        unsafe {
            if libc::isatty(libc::STDIN_FILENO) == 0 {
                return Self { original: None };
            }

            let mut original: libc::termios = std::mem::zeroed();
            if libc::tcgetattr(libc::STDIN_FILENO, &raw mut original) != 0 {
                return Self { original: None };
            }

            let mut raw_mode = original;
            libc::cfmakeraw(&raw mut raw_mode);

            if libc::tcsetattr(libc::STDIN_FILENO, libc::TCSANOW, &raw const raw_mode) != 0 {
                return Self { original: None };
            }

            Self {
                original: Some(original),
            }
        }
    }
}

#[cfg(unix)]
impl Drop for RawModeGuard {
    fn drop(&mut self) {
        if let Some(ref original) = self.original {
            unsafe {
                libc::tcsetattr(libc::STDIN_FILENO, libc::TCSANOW, original);
            }
        }
    }
}

// Windows: ConPTY handles raw mode automatically
#[cfg(windows)]
struct RawModeGuard;

#[cfg(windows)]
impl RawModeGuard {
    fn new() -> Self {
        Self
    }
}

/// Run script mode - spawn a shell (or, for `exec`, a single command) in a
/// PTY and stream output to server. Returns the exit code to propagate:
/// the child's status when it can be observed, otherwise 0.
///
/// `session` carries the share URL and room name; they are exported into
/// the spawned process's environment (`SHELLSHARE_URL`, `SHELLSHARE_ROOM`)
/// so `shellshare status`, run from inside the shared shell, can re-print
/// the link and QR code without anything being written to disk.
#[allow(clippy::too_many_lines)] // Complex PTY setup with multiple threads
pub fn run_script_mode(
    transport: ws::Transport,
    running: &Arc<AtomicBool>,
    exec: Option<&[String]>,
    session: &super::SessionEnv<'_>,
) -> Result<i32, Box<dyn std::error::Error>> {
    // Raw mode BEFORE spawning the shell, so the first keystroke already
    // reaches an interactive TUI app character by character
    let _raw_guard = RawModeGuard::new();

    #[cfg(unix)]
    let shell = std::env::var("SHELL").unwrap_or_else(|_| "/bin/sh".to_string());

    #[cfg(windows)]
    let shell = std::env::var("COMSPEC").unwrap_or_else(|_| "cmd.exe".to_string());

    let size = get_terminal_size();
    let pty_size = PtySize {
        rows: size.rows,
        cols: size.cols,
        pixel_width: 0,
        pixel_height: 0,
    };

    let pty_system = native_pty_system();
    let pair = pty_system.openpty(pty_size)?;

    let mut cmd = match exec {
        Some([program, args @ ..]) => {
            let mut cmd = CommandBuilder::new(program);
            cmd.args(args);
            cmd
        }
        _ => CommandBuilder::new(&shell),
    };
    if let Ok(cwd) = std::env::current_dir() {
        cmd.cwd(cwd);
    }
    // Only for an interactive shell: `exec` runs one arbitrary command that
    // can't usefully call `status` and might forward its environment onward
    // (CI logs, crash reporters), so it must not carry the URL's secret
    // #fragment.
    if exec.is_none() {
        cmd.env(super::ENV_URL, session.url);
        cmd.env(super::ENV_ROOM, session.room);
    }

    // A descriptor of our own to wait on. The master's own would do until
    // the drain watchdog closes it, at which point the number could be
    // reissued to one of the sender thread's sockets and the reader would
    // quietly wait on that. Readiness belongs to the terminal, so this and
    // portable-pty's reader see the same bytes - and it is that reader's
    // EIO-to-EOF mapping that ends the loop, where a raw read would report
    // an error.
    //
    // Before the spawn, because failing here is fatal - without it the
    // reader parks in a read nothing can interrupt - and refusing after
    // the spawn would strand a running command.
    #[cfg(unix)]
    let poll_fd = {
        let fd = pair
            .master
            .as_raw_fd()
            .ok_or("this PTY has no descriptor to wait on")?;
        // SAFETY: `fd` is the live master, owned by `pair`. Close-on-exec
        // because plain dup() drops the flag portable-pty set on it and
        // the child is spawned right below.
        let duped = unsafe { libc::fcntl(fd, libc::F_DUPFD_CLOEXEC, 0) };
        if duped < 0 {
            return Err(Box::new(std::io::Error::last_os_error()));
        }
        // SAFETY: a fresh, exclusively-owned descriptor.
        unsafe { OwnedFd::from_raw_fd(duped) }
    };

    let mut child = pair.slave.spawn_command(cmd)?;

    let master = pair.master;

    // Explicitly resize after spawning to ensure correct dimensions
    master.resize(pty_size)?;

    let mut reader = master.try_clone_reader()?;
    let mut writer = master.take_writer()?;

    // Shared terminal size, updated by the SIGWINCH handler
    let current_cols = Arc::new(AtomicU16::new(size.cols));
    let current_rows = Arc::new(AtomicU16::new(size.rows));

    #[cfg(unix)]
    let (resize_tx, resize_rx) = mpsc::channel::<PtySize>();

    // Watch SIGWINCH for dynamic terminal resize.
    //
    // Registered here, on this thread, rather than inside the worker
    // below, because the `Handle` it yields is what stops that worker at
    // the end of the session. Waking it with `raise(SIGWINCH)` instead
    // raced with its own registration: SIGWINCH's default disposition is
    // to be ignored, so a wake-up sent before the handler existed was
    // discarded, the worker stayed parked in `forever()` and the join
    // below never returned - a hang no termination signal could clear,
    // since those only set flags a blocked thread never reads. Registering
    // first closes the window; `close()` then needs no signal at all.
    // `exec` is where it bit: a child like `sh -c 'exit 3'` produces no
    // output, so nothing delays the teardown that races the worker's start.
    #[cfg(unix)]
    let sigwinch = Signals::new([SIGWINCH]).ok();
    #[cfg(unix)]
    let sigwinch_handle = sigwinch.as_ref().map(SignalsInfo::handle);
    #[cfg(unix)]
    let sigwinch_thread = sigwinch.map(|mut signals| {
        let cols = current_cols.clone();
        let rows = current_rows.clone();

        thread::spawn(move || {
            // Ends when the handle is closed, not on a flag: `forever()`
            // blocks, so a flag would only be read after a signal that
            // may never come.
            for _ in signals.forever() {
                let new_size = get_terminal_size();
                cols.store(new_size.cols, Ordering::SeqCst);
                rows.store(new_size.rows, Ordering::SeqCst);
                // The main loop owns the master, so it does the resize
                let _ = resize_tx.send(PtySize {
                    rows: new_size.rows,
                    cols: new_size.cols,
                    pixel_width: 0,
                    pixel_height: 0,
                });
            }
        })
    });

    let (tx, rx) = mpsc::channel::<Vec<u8>>();

    let running_clone = running.clone();
    let running_http = running.clone();

    let http_cols = current_cols;
    let http_rows = current_rows;

    // The sender thread owns the transport and handles all network I/O, so
    // network latency never blocks terminal display
    let http_thread = thread::spawn(move || {
        // Bound on one frame's payload; chunks queued beyond this are
        // simply sent as further frames on the same connection
        const MAX_BATCH: usize = 64 * 1024;

        let mut transport = transport;
        loop {
            if !running_http.load(Ordering::SeqCst) {
                break;
            }
            let size = TermSize {
                cols: http_cols.load(Ordering::SeqCst),
                rows: http_rows.load(Ordering::SeqCst),
            };

            let result = match rx.recv_timeout(Duration::from_millis(50)) {
                Ok(data) => {
                    // Coalesce whatever else is already queued into one
                    // frame, then send immediately - frames are cheap,
                    // so there is no pacing
                    let mut batch = data;
                    while batch.len() < MAX_BATCH {
                        match rx.try_recv() {
                            Ok(more) => batch.extend(more),
                            Err(_) => break,
                        }
                    }
                    transport.send(&batch, size)
                }
                // Idle: let the transport retry buffered data/reconnects
                Err(mpsc::RecvTimeoutError::Timeout) => transport.tick(size),
                Err(mpsc::RecvTimeoutError::Disconnected) => break,
            };

            if let Err(e) = result {
                // The room belongs to someone else now: our buffered
                // output is not theirs to receive, so stop without the
                // shutdown flush
                eprintln!("\r\nERROR: {e}");
                eprintln!("\rERROR: Exit shellshare and try again later.");
                running_http.store(false, Ordering::SeqCst);
                return;
            }
        }

        // Take what the reader still owes us. Only the EOF exit leaves the
        // channel empty: `running` going false breaks the loop wherever it
        // stood, and `shutdown` flushes only what already reached the
        // transport, so the paths that exist to bound the wait would
        // otherwise drop output the terminal had already produced.
        //
        // Waiting, not just draining what is queued, because the reader
        // sees the same flag and may be between its read and its send -
        // measured as a ~50ms window in which output reaches the room with
        // this loop and is lost without it. Its sender dropping is the
        // real end; the deadline only keeps this from becoming the
        // unbounded wait the rest of the teardown just removed, and gates
        // every pass because each send can itself sit on a socket timeout.
        let size = TermSize {
            cols: http_cols.load(Ordering::SeqCst),
            rows: http_rows.load(Ordering::SeqCst),
        };
        let deadline = Instant::now() + Duration::from_millis(500);
        while Instant::now() < deadline {
            match rx.recv_timeout(Duration::from_millis(20)) {
                Ok(data) => {
                    if transport.send(&data, size).is_err() {
                        break;
                    }
                }
                Err(mpsc::RecvTimeoutError::Disconnected) => break,
                Err(mpsc::RecvTimeoutError::Timeout) => {}
            }
        }

        // Normal end (shell exit or Ctrl+C): flush pending output, then
        // close - the room stays up so the link keeps working
        transport.shutdown();
    });

    // Reads from the PTY, displays locally, and hands off to the sender
    // thread. NEVER blocks on network I/O.
    let stream_thread = thread::spawn(move || {
        let mut read_buffer = [0u8; 4096];
        let mut stdout = std::io::stdout();

        loop {
            if !running_clone.load(Ordering::SeqCst) {
                break;
            }

            // Bounded, so the check above stays reachable: the drain
            // watchdog only flips that flag, and a thread parked in a
            // blocking read never gets back to it. Enough while whatever
            // holds the slave open keeps writing, since each write returns
            // from read() - but a silent survivor (`sleep 30 &` under a
            // shell ignoring SIGHUP) gives it nothing to return from.
            #[cfg(unix)]
            match wait_readable(&poll_fd, POLL_TIMEOUT_MS) {
                Wait::Readable => {}
                Wait::Again => continue,
                Wait::Failed => break,
            }

            match reader.read(&mut read_buffer) {
                Ok(0) => {
                    break;
                }
                Ok(n) => {
                    let data = &read_buffer[..n];

                    if stdout.write_all(data).is_err() {
                        break;
                    }
                    let _ = stdout.flush();

                    // A closed channel just drops the data for the server;
                    // the local display already happened
                    let _ = tx.send(data.to_vec());
                }
                Err(e) => {
                    if e.kind() == std::io::ErrorKind::WouldBlock {
                        thread::sleep(Duration::from_millis(10));
                        continue;
                    }
                    // A signal is not the PTY closing. SA_RESTART means the
                    // kernel resumes the read and this shouldn't surface,
                    // but treating it as EOF would end a live session on a
                    // stray signal.
                    if e.kind() == std::io::ErrorKind::Interrupted {
                        continue;
                    }
                    // Likely the PTY closed
                    break;
                }
            }
        }

        // Signals the sender thread to flush and exit
        drop(tx);
    });

    let running_stdin = running.clone();

    // Forwards stdin to the PTY, and detects double Ctrl+C for force-quit
    // (useful when the shell is unresponsive)
    let _stdin_thread = thread::spawn(move || {
        const CTRLC_BYTE: u8 = 0x03;
        const DOUBLE_CTRLC_WINDOW: Duration = Duration::from_millis(500);

        let stdin = std::io::stdin();
        let mut stdin_lock = stdin.lock();
        let mut buffer = [0u8; 1024];
        let mut last_ctrlc: Option<Instant> = None;

        loop {
            if !running_stdin.load(Ordering::SeqCst) {
                break;
            }

            match stdin_lock.read(&mut buffer) {
                Ok(0) => break, // EOF
                Ok(n) => {
                    let data = &buffer[..n];

                    if data.contains(&CTRLC_BYTE) {
                        if let Some(last) = last_ctrlc {
                            if last.elapsed() < DOUBLE_CTRLC_WINDOW {
                                running_stdin.store(false, Ordering::SeqCst);
                                break;
                            }
                        }
                        last_ctrlc = Some(Instant::now());
                    }

                    // A single Ctrl+C is forwarded like any other input
                    if writer.write_all(data).is_err() {
                        break;
                    }
                    if writer.flush().is_err() {
                        break;
                    }
                }
                Err(e) => {
                    if e.kind() == std::io::ErrorKind::WouldBlock {
                        thread::sleep(Duration::from_millis(10));
                        continue;
                    }
                    break;
                }
            }
        }
    });

    // The child's exit status when it could be observed; `exec` forwards
    // it to the caller (interrupted/unobservable children report 0)
    let mut exit_code = 0;

    // Poll rather than block, so Ctrl+C and resize requests get a turn
    loop {
        if !running.load(Ordering::SeqCst) {
            // Kill the child process and reap it (bounded), so the slave
            // is only dropped after the child is actually gone
            let _ = child.kill();
            for _ in 0..40 {
                match child.try_wait() {
                    Ok(None) => thread::sleep(Duration::from_millis(50)),
                    Ok(Some(_)) | Err(_) => break,
                }
            }
            break;
        }

        #[cfg(unix)]
        while let Ok(new_size) = resize_rx.try_recv() {
            let _ = master.resize(new_size);
        }

        match child.try_wait() {
            Ok(Some(status)) => {
                exit_code = i32::try_from(status.exit_code()).unwrap_or(1);
                break;
            }
            Ok(None) => {
                thread::sleep(Duration::from_millis(50));
            }
            // Error checking status: assume the child died
            Err(_) => {
                break;
            }
        }
    }

    // Close our copy of the slave BEFORE joining the reader thread.
    // The child has exited (or been killed and reaped, bounded) in every
    // path that reaches here, so this is safe (portable-pty only requires
    // the slave to outlive the child). Without this, the master never
    // sees EOF and the PTY reader thread can block in read() forever,
    // hanging the join below.
    //
    // Unix only, in effect: on Windows both handles refer to one
    // refcounted ConPTY, so this releases a reference and closes nothing.
    // See the watchdog below.
    drop(pair.slave);

    // Do NOT flip `running` before joining: a short-lived child (e.g.
    // `shellshare exec -- echo hi`) can exit before the reader thread
    // gets scheduled, and the flag would make it quit without draining
    // the output still buffered in the PTY. With the slave closed the
    // reader drains to EOF and exits on its own; dropping its sender
    // then ends the network thread after the channel is emptied. On
    // Ctrl+C `running` is already false, keeping interrupts immediate.
    //
    // The drain must be bounded, though: an orphan that outlived the
    // child (e.g. `ping example.com & exit`) holds the slave open, so EOF
    // never comes. After a grace period the watchdog gives up on each
    // platform's terms. `running` is what stops a Unix reader, which
    // reaches the flag between bounded waits. Dropping the master is the
    // only lever on a Windows one: it is the last reference to the
    // pseudoconsole, and closing that releases the pipe the reader is
    // parked on. The resize loop above was the master's only user, and
    // the reader holds its own descriptors, so this costs Unix nothing.
    // Dropped before the flag, so whatever the close shakes loose is
    // still something the reader will relay.
    let drain_watchdog = {
        let running = running.clone();
        thread::spawn(move || {
            thread::sleep(Duration::from_secs(2));
            drop(master);
            running.store(false, Ordering::SeqCst);
        })
    };
    let _ = stream_thread.join();
    let _ = http_thread.join();
    drop(drain_watchdog);

    // Signal the remaining helper threads (stdin forwarder, SIGWINCH) to stop
    running.store(false, Ordering::SeqCst);

    // Join SIGWINCH handler thread (Unix only). Closing the handle ends
    // its `forever()` loop directly - no signal to deliver, so nothing
    // to lose, and this join always returns.
    #[cfg(unix)]
    if let Some(handle) = sigwinch_thread {
        if let Some(signals) = sigwinch_handle {
            signals.close();
        }
        let _ = handle.join();
    }

    Ok(exit_code)
}
