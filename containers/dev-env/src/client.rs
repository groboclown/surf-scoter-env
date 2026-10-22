//! Command that passes the invoked command to the listening host.
//! A "privileged host-execution broker," container side's client.
//!
//! The client sends its invocation (cwd, args, environment) through the broker
//! to the host, then acts as a proxy for the remote process:
//!   * stdin is streamed to the host, ending with an empty stdin message (EOF).
//!   * Forwarded signals (see `signals::FORWARDED`) are sent to the host.
//!   * Host stdout / stderr are written to the local stdout / stderr.
//!   * The client exits with the host's exit code.
//!
//! If the conversation with the host fails, the client exits with `EXIT_TRANSPORT_FAILURE`.

use std::io::{self, BufReader, BufWriter, ErrorKind, PipeReader, Read, Write};
use std::net::Shutdown;
use std::os::unix::net::UnixStream;
use std::sync::{Arc, Mutex};
use std::thread;

use surf_scoter_env_launcher::comm::client_broker;
use surf_scoter_env_launcher::comm::client_host::{self, Command, Invocation};

/// Prefix for the client's diagnostics on stderr.
const LOG_PREFIX: &str = "scoter-client";

/// Exit code used when the host's exit code can't be determined.
const EXIT_TRANSPORT_FAILURE: i32 = 255;

/// Maximum number of stdin bytes sent per message.
const STDIN_CHUNK_BYTES: usize = 64 * 1024;

fn main() {
    std::process::exit(run());
}

fn run() -> i32 {
    let invocation = match Invocation::new_current() {
        Ok(i) => i,
        Err(e) => {
            eprintln!("{LOG_PREFIX}: cannot read the current invocation: {e}");
            return EXIT_TRANSPORT_FAILURE;
        }
    };
    let stream = match UnixStream::connect(client_broker::UNIX_SOCKET) {
        Ok(s) => s,
        Err(e) => {
            eprintln!(
                "{LOG_PREFIX}: cannot connect to broker at {}: {e}",
                client_broker::UNIX_SOCKET
            );
            return EXIT_TRANSPORT_FAILURE;
        }
    };
    let signals = match signals::install() {
        Ok(pipe) => Some(pipe),
        Err(e) => {
            eprintln!("{LOG_PREFIX}: signals will not be forwarded: {e}");
            None
        }
    };
    run_session(
        stream,
        &invocation,
        io::stdin(),
        signals,
        io::stdout().lock(),
        io::stderr().lock(),
    )
}

/// Run the full conversation with the host over `stream`, returning the exit code to use.
///
/// `signals` is the read side of the signal self-pipe; each byte is a local signal number.
fn run_session<I, O, E>(
    stream: UnixStream,
    invocation: &Invocation,
    stdin: I,
    signals: Option<PipeReader>,
    mut out: O,
    mut err: E,
) -> i32
where
    I: Read + Send + 'static,
    O: Write,
    E: Write,
{
    let result = session(&stream, invocation, stdin, signals, &mut out, &mut err);
    // Unblocks the helper threads' writes; a blocked stdin read ends with the process.
    let _ = stream.shutdown(Shutdown::Both);
    match result {
        Ok(code) => code,
        Err(e) => {
            let _ = writeln!(err, "{LOG_PREFIX}: {e}");
            let _ = err.flush();
            EXIT_TRANSPORT_FAILURE
        }
    }
}

fn session<I, O, E>(
    stream: &UnixStream,
    invocation: &Invocation,
    stdin: I,
    signals: Option<PipeReader>,
    out: &mut O,
    err: &mut E,
) -> io::Result<i32>
where
    I: Read + Send + 'static,
    O: Write,
    E: Write,
{
    let mut writer = BufWriter::new(stream.try_clone()?);
    invocation.write(&mut writer)?;
    writer.flush()?;
    let writer = Arc::new(Mutex::new(writer));

    {
        let writer = writer.clone();
        // A failure here means the host side went away; the output pump reports that.
        thread::spawn(move || {
            let _ = pump_stdin(stdin, &writer);
        });
    }
    if let Some(pipe) = signals {
        let writer = writer.clone();
        thread::spawn(move || forward_signals(pipe, &writer));
    }

    pump_output(BufReader::new(stream), out, err)
}

/// Write one complete message while holding the lock, so messages from
/// different threads never interleave.
fn send_locked<W: Write>(
    out: &Mutex<W>,
    send: impl FnOnce(&mut W) -> io::Result<()>,
) -> io::Result<()> {
    let mut w = out
        .lock()
        .map_err(|_| io::Error::other("socket writer lock poisoned"))?;
    send(&mut w)?;
    w.flush()
}

/// Send all of `input` to the host as stdin messages, followed by an empty message for EOF.
fn pump_stdin<R: Read, W: Write>(mut input: R, out: &Mutex<W>) -> io::Result<()> {
    let mut buf = vec![0u8; STDIN_CHUNK_BYTES];
    loop {
        let n = match input.read(&mut buf) {
            Ok(n) => n,
            Err(e) if e.kind() == ErrorKind::Interrupted => continue,
            Err(e) => return Err(e),
        };
        send_locked(out, |w| client_host::send_stdin(w, &buf[..n]))?;
        if n == 0 {
            return Ok(());
        }
    }
}

/// Read host messages, writing stdout / stderr locally, until the exit code arrives.
///
/// A local output stream that fails to write (e.g. a closed pipe) is dropped, but
/// the messages keep draining so the real exit code is still returned.
fn pump_output<R: Read, O: Write, E: Write>(mut r: R, out: &mut O, err: &mut E) -> io::Result<i32> {
    let mut out_ok = true;
    let mut err_ok = true;
    loop {
        let cmd = Command::read(&mut r).map_err(|e| match e.kind() {
            // A reset happens when the peer closes with our stdin messages still unread.
            ErrorKind::UnexpectedEof | ErrorKind::ConnectionReset => io::Error::new(
                ErrorKind::UnexpectedEof,
                "connection to the broker closed before the command reported an exit code",
            ),
            _ => e,
        })?;
        match cmd {
            Command::Stdout(data) => {
                out_ok = out_ok && write_flush(out, &data);
            }
            Command::Stderr(data) => {
                err_ok = err_ok && write_flush(err, &data);
            }
            Command::ExitCode(code) => return Ok(code),
            Command::Stdin(_) | Command::Signal(_) => {
                return Err(io::Error::new(
                    ErrorKind::InvalidData,
                    "protocol error: unexpected message from host",
                ));
            }
        }
    }
}

fn write_flush<W: Write>(w: &mut W, data: &[u8]) -> bool {
    w.write_all(data).and_then(|_| w.flush()).is_ok()
}

/// Forward each signal number read from the self-pipe to the host.
/// Stops when the pipe closes or the host can no longer be written to.
fn forward_signals<R: Read, W: Write>(mut pipe: R, out: &Mutex<W>) {
    let mut byte = [0u8; 1];
    loop {
        match pipe.read(&mut byte) {
            Ok(0) => return,
            Ok(_) => {}
            Err(e) if e.kind() == ErrorKind::Interrupted => continue,
            Err(_) => return,
        }
        if let Some(sig) = signals::signal_to_wire(byte[0] as libc::c_int)
            && send_locked(out, |w| client_host::send_signal(w, sig)).is_err()
        {
            return;
        }
    }
}

mod signals {
    //! Catches the forwarded signals and turns them into bytes on a self-pipe,
    //! so a normal thread can do the actual (non async-signal-safe) forwarding.

    use std::io::{self, PipeReader, PipeWriter};
    use std::os::fd::AsRawFd;
    use std::sync::OnceLock;
    use std::sync::atomic::{AtomicI32, Ordering};

    use surf_scoter_env_launcher::comm::client_host;

    pub(crate) const FORWARDED: [libc::c_int; 7] = [
        libc::SIGHUP,
        libc::SIGINT,
        libc::SIGQUIT,
        libc::SIGUSR1,
        libc::SIGUSR2,
        libc::SIGTERM,
        libc::SIGWINCH,
    ];

    static WRITE_FD: AtomicI32 = AtomicI32::new(-1);
    /// Keeps the write side of the pipe open for the life of the process.
    static WRITER: OnceLock<PipeWriter> = OnceLock::new();

    /// Map a local signal number to its protocol value.
    pub(crate) fn signal_to_wire(sig: libc::c_int) -> Option<u32> {
        match sig {
            libc::SIGHUP => Some(client_host::SIGNAL_HUP),
            libc::SIGINT => Some(client_host::SIGNAL_INT),
            libc::SIGQUIT => Some(client_host::SIGNAL_QUIT),
            libc::SIGUSR1 => Some(client_host::SIGNAL_USR1),
            libc::SIGUSR2 => Some(client_host::SIGNAL_USR2),
            libc::SIGTERM => Some(client_host::SIGNAL_TERM),
            libc::SIGWINCH => Some(client_host::SIGNAL_WINCH),
            _ => None,
        }
    }

    /// Install handlers for the `FORWARDED` signals.  Returns the pipe that receives
    /// one byte (the local signal number) per caught signal.  May only be called once.
    pub(crate) fn install() -> io::Result<PipeReader> {
        let (reader, writer) = io::pipe()?;
        let fd = writer.as_raw_fd();
        if WRITER.set(writer).is_err() {
            return Err(io::Error::new(
                io::ErrorKind::AlreadyExists,
                "signal handlers already installed",
            ));
        }

        // Never block inside the signal handler; if the pipe is full, the signal is dropped.
        // SAFETY: fd is a valid, open pipe descriptor owned by WRITER.
        unsafe {
            let flags = libc::fcntl(fd, libc::F_GETFL);
            if flags < 0 || libc::fcntl(fd, libc::F_SETFL, flags | libc::O_NONBLOCK) < 0 {
                return Err(io::Error::last_os_error());
            }
        }
        WRITE_FD.store(fd, Ordering::SeqCst);

        // SAFETY: the sigaction struct is fully initialized (zeroed, then the
        // relevant fields set), and on_signal is async-signal-safe.
        unsafe {
            let mut action: libc::sigaction = std::mem::zeroed();
            action.sa_sigaction = on_signal as extern "C" fn(libc::c_int) as libc::sighandler_t;
            action.sa_flags = libc::SA_RESTART;
            libc::sigemptyset(&mut action.sa_mask);
            for sig in FORWARDED {
                if libc::sigaction(sig, &action, std::ptr::null_mut()) != 0 {
                    return Err(io::Error::last_os_error());
                }
            }
        }
        Ok(reader)
    }

    extern "C" fn on_signal(sig: libc::c_int) {
        let fd = WRITE_FD.load(Ordering::Relaxed);
        if fd < 0 {
            return;
        }
        let byte = sig as u8;
        // SAFETY: write(2) is async-signal-safe; errno is restored so the
        // interrupted code doesn't observe a change.
        unsafe {
            let errno = errno_location();
            let saved = *errno;
            libc::write(fd, &byte as *const u8 as *const libc::c_void, 1);
            *errno = saved;
        }
    }

    #[cfg(any(target_os = "linux", target_os = "android"))]
    unsafe fn errno_location() -> *mut libc::c_int {
        unsafe { libc::__errno_location() }
    }

    #[cfg(any(target_os = "macos", target_os = "ios", target_os = "freebsd"))]
    unsafe fn errno_location() -> *mut libc::c_int {
        unsafe { libc::__error() }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Cursor;

    /// Decode every message in `bytes`.
    fn decode_all(bytes: &[u8]) -> Vec<Command> {
        let mut cur = Cursor::new(bytes);
        let mut cmds = Vec::new();
        while (cur.position() as usize) < bytes.len() {
            cmds.push(Command::read(&mut cur).expect("decode"));
        }
        cmds
    }

    fn encode(build: impl FnOnce(&mut Vec<u8>) -> io::Result<()>) -> Vec<u8> {
        let mut buf = Vec::new();
        build(&mut buf).unwrap();
        buf
    }

    /// A writer that always fails.
    struct FailingWriter;

    impl Write for FailingWriter {
        fn write(&mut self, _: &[u8]) -> io::Result<usize> {
            Err(io::Error::new(ErrorKind::BrokenPipe, "closed"))
        }
        fn flush(&mut self) -> io::Result<()> {
            Ok(())
        }
    }

    /// A reader that returns `Interrupted` before each piece of data.
    struct InterruptingReader {
        pieces: Vec<&'static [u8]>,
        interrupt_next: bool,
    }

    impl Read for InterruptingReader {
        fn read(&mut self, buf: &mut [u8]) -> io::Result<usize> {
            if self.pieces.is_empty() {
                return Ok(0);
            }
            if self.interrupt_next {
                self.interrupt_next = false;
                return Err(io::Error::from(ErrorKind::Interrupted));
            }
            self.interrupt_next = true;
            let piece = self.pieces.remove(0);
            buf[..piece.len()].copy_from_slice(piece);
            Ok(piece.len())
        }
    }

    // ---- pump_stdin ----

    #[test]
    fn pump_stdin_chunks_and_eof() {
        let data: Vec<u8> = (0..(STDIN_CHUNK_BYTES * 2 + 123))
            .map(|i| (i % 251) as u8)
            .collect();
        let out = Mutex::new(Vec::new());
        pump_stdin(Cursor::new(data.clone()), &out).unwrap();

        let cmds = decode_all(&out.into_inner().unwrap());
        assert_eq!(cmds.len(), 4);
        let mut joined = Vec::new();
        for cmd in &cmds[..3] {
            match cmd {
                Command::Stdin(d) => {
                    assert!(!d.is_empty() && d.len() <= STDIN_CHUNK_BYTES);
                    joined.extend_from_slice(d);
                }
                other => panic!("unexpected {other:?}"),
            }
        }
        assert_eq!(joined, data);
        assert_eq!(cmds[3], Command::Stdin(vec![]));
    }

    #[test]
    fn pump_stdin_empty_sends_eof() {
        let out = Mutex::new(Vec::new());
        pump_stdin(Cursor::new(Vec::new()), &out).unwrap();
        assert_eq!(
            decode_all(&out.into_inner().unwrap()),
            vec![Command::Stdin(vec![])]
        );
    }

    #[test]
    fn pump_stdin_interrupted() {
        let input = InterruptingReader {
            pieces: vec![b"ab", b"cd"],
            interrupt_next: true,
        };
        let out = Mutex::new(Vec::new());
        pump_stdin(input, &out).unwrap();
        assert_eq!(
            decode_all(&out.into_inner().unwrap()),
            vec![
                Command::Stdin(b"ab".to_vec()),
                Command::Stdin(b"cd".to_vec()),
                Command::Stdin(vec![]),
            ]
        );
    }

    #[test]
    fn pump_stdin_read_error() {
        struct BadReader;
        impl Read for BadReader {
            fn read(&mut self, _: &mut [u8]) -> io::Result<usize> {
                Err(io::Error::new(ErrorKind::PermissionDenied, "nope"))
            }
        }
        let out = Mutex::new(Vec::new());
        let err = pump_stdin(BadReader, &out).unwrap_err();
        assert_eq!(err.kind(), ErrorKind::PermissionDenied);
        assert!(out.into_inner().unwrap().is_empty());
    }

    #[test]
    fn pump_stdin_write_error() {
        let out = Mutex::new(FailingWriter);
        let err = pump_stdin(Cursor::new(b"data".to_vec()), &out).unwrap_err();
        assert_eq!(err.kind(), ErrorKind::BrokenPipe);
    }

    // ---- pump_output ----

    #[test]
    fn pump_output_routes_streams() {
        for code in [0, 1, -3, 255] {
            let input = encode(|b| {
                client_host::send_stdout(b, b"out1 ")?;
                client_host::send_stderr(b, b"err1 ")?;
                client_host::send_stdout(b, b"out2")?;
                client_host::send_stderr(b, b"err2")?;
                client_host::send_stdout(b, b"")?;
                client_host::send_exit_code(b, code)
            });
            let (mut out, mut err) = (Vec::new(), Vec::new());
            assert_eq!(
                pump_output(Cursor::new(input), &mut out, &mut err).unwrap(),
                code
            );
            assert_eq!(out, b"out1 out2");
            assert_eq!(err, b"err1 err2");
        }
    }

    #[test]
    fn pump_output_stops_at_exit() {
        let input = encode(|b| {
            client_host::send_exit_code(b, 7)?;
            client_host::send_stdout(b, b"after")
        });
        let (mut out, mut err) = (Vec::new(), Vec::new());
        assert_eq!(
            pump_output(Cursor::new(input), &mut out, &mut err).unwrap(),
            7
        );
        assert!(out.is_empty());
    }

    #[test]
    fn pump_output_early_eof() {
        let input = encode(|b| client_host::send_stdout(b, b"partial"));
        let (mut out, mut err) = (Vec::new(), Vec::new());
        let e = pump_output(Cursor::new(input), &mut out, &mut err).unwrap_err();
        assert_eq!(e.kind(), ErrorKind::UnexpectedEof);
        assert_eq!(out, b"partial");
    }

    #[test]
    fn pump_output_early_reset() {
        struct ResetReader;
        impl Read for ResetReader {
            fn read(&mut self, _: &mut [u8]) -> io::Result<usize> {
                Err(io::Error::from(ErrorKind::ConnectionReset))
            }
        }
        let (mut out, mut err) = (Vec::new(), Vec::new());
        let e = pump_output(ResetReader, &mut out, &mut err).unwrap_err();
        assert_eq!(e.kind(), ErrorKind::UnexpectedEof);
        assert!(e.to_string().contains("exit code"), "{e}");
    }

    #[test]
    fn pump_output_passes_errors() {
        struct DeniedReader;
        impl Read for DeniedReader {
            fn read(&mut self, _: &mut [u8]) -> io::Result<usize> {
                Err(io::Error::from(ErrorKind::PermissionDenied))
            }
        }
        let (mut out, mut err) = (Vec::new(), Vec::new());
        let e = pump_output(DeniedReader, &mut out, &mut err).unwrap_err();
        assert_eq!(e.kind(), ErrorKind::PermissionDenied);
    }

    #[test]
    fn pump_output_truncated_message() {
        let mut input = encode(|b| client_host::send_stdout(b, b"partial"));
        input.truncate(input.len() - 2);
        let (mut out, mut err) = (Vec::new(), Vec::new());
        let e = pump_output(Cursor::new(input), &mut out, &mut err).unwrap_err();
        assert_eq!(e.kind(), ErrorKind::UnexpectedEof);
    }

    #[test]
    fn pump_output_bad_message() {
        let stdin_msg = encode(|b| client_host::send_stdin(b, b"x"));
        let signal_msg = encode(|b| client_host::send_signal(b, client_host::SIGNAL_INT));
        for input in [stdin_msg, signal_msg, vec![99]] {
            let (mut out, mut err) = (Vec::new(), Vec::new());
            let e = pump_output(Cursor::new(input), &mut out, &mut err).unwrap_err();
            assert_eq!(e.kind(), ErrorKind::InvalidData);
        }
    }

    #[test]
    fn pump_output_bad_stdout() {
        let input = encode(|b| {
            client_host::send_stdout(b, b"lost")?;
            client_host::send_stderr(b, b"kept")?;
            client_host::send_stdout(b, b"lost again")?;
            client_host::send_exit_code(b, 4)
        });
        let mut err = Vec::new();
        assert_eq!(
            pump_output(Cursor::new(input), &mut FailingWriter, &mut err).unwrap(),
            4
        );
        assert_eq!(err, b"kept");
    }

    #[test]
    fn pump_output_bad_stderr() {
        let input = encode(|b| {
            client_host::send_stderr(b, b"lost")?;
            client_host::send_stdout(b, b"kept")?;
            client_host::send_exit_code(b, 0)
        });
        let mut out = Vec::new();
        assert_eq!(
            pump_output(Cursor::new(input), &mut out, &mut FailingWriter).unwrap(),
            0
        );
        assert_eq!(out, b"kept");
    }

    // ---- signals ----

    #[test]
    fn signal_to_wire_maps_all() {
        let expected = [
            (libc::SIGHUP, client_host::SIGNAL_HUP),
            (libc::SIGINT, client_host::SIGNAL_INT),
            (libc::SIGQUIT, client_host::SIGNAL_QUIT),
            (libc::SIGUSR1, client_host::SIGNAL_USR1),
            (libc::SIGUSR2, client_host::SIGNAL_USR2),
            (libc::SIGTERM, client_host::SIGNAL_TERM),
            (libc::SIGWINCH, client_host::SIGNAL_WINCH),
        ];
        assert_eq!(expected.len(), signals::FORWARDED.len());
        for (sig, wire) in expected {
            assert!(signals::FORWARDED.contains(&sig));
            assert_eq!(signals::signal_to_wire(sig), Some(wire), "signal {sig}");
        }
    }

    #[test]
    fn signal_to_wire_rejects_others() {
        for sig in [
            0,
            libc::SIGCHLD,
            libc::SIGKILL,
            libc::SIGPIPE,
            libc::SIGALRM,
            255,
        ] {
            assert_eq!(signals::signal_to_wire(sig), None, "signal {sig}");
        }
    }

    #[test]
    fn forward_signals_maps() {
        let (reader, mut writer) = io::pipe().unwrap();
        writer
            .write_all(&[
                libc::SIGINT as u8,
                libc::SIGCHLD as u8,
                libc::SIGTERM as u8,
                libc::SIGWINCH as u8,
            ])
            .unwrap();
        drop(writer);

        let out = Mutex::new(Vec::new());
        forward_signals(reader, &out);
        assert_eq!(
            decode_all(&out.into_inner().unwrap()),
            vec![
                Command::Signal(client_host::SIGNAL_INT),
                Command::Signal(client_host::SIGNAL_TERM),
                Command::Signal(client_host::SIGNAL_WINCH),
            ]
        );
    }

    #[test]
    fn forward_signals_write_error() {
        let (reader, mut writer) = io::pipe().unwrap();
        writer.write_all(&[libc::SIGINT as u8]).unwrap();
        // Writer intentionally kept open: forward_signals must return due to the
        // write failure, not the pipe closing.
        let out = Mutex::new(FailingWriter);
        forward_signals(reader, &out);
        drop(writer);
    }

    #[test]
    fn install_reports_signals() {
        let mut reader = signals::install().unwrap();
        // SAFETY: raise is safe to call; SIGUSR1 / SIGUSR2 are now handled.
        unsafe {
            assert_eq!(libc::raise(libc::SIGUSR1), 0);
            assert_eq!(libc::raise(libc::SIGUSR2), 0);
        }
        let mut got = [0u8; 2];
        reader.read_exact(&mut got).unwrap();
        assert_eq!(got, [libc::SIGUSR1 as u8, libc::SIGUSR2 as u8]);

        let again = signals::install().unwrap_err();
        assert_eq!(again.kind(), ErrorKind::AlreadyExists);
    }

    // ---- run_session ----

    fn test_invocation() -> Invocation {
        Invocation {
            cwd: "/workspace".to_string(),
            args: vec!["docker".to_string(), "ps".to_string()],
            envs: vec![("HOME".to_string(), "/home/developer".to_string())],
        }
    }

    /// Read stdin messages from the host side until the EOF message.
    fn read_host_stdin(host: &mut impl Read) -> Vec<u8> {
        let mut data = Vec::new();
        loop {
            match Command::read(host).unwrap() {
                Command::Stdin(d) if d.is_empty() => return data,
                Command::Stdin(d) => data.extend_from_slice(&d),
                other => panic!("unexpected {other:?}"),
            }
        }
    }

    #[test]
    fn run_session_full_conversation() {
        let (client, mut host) = UnixStream::pair().unwrap();
        let fake_host = thread::spawn(move || {
            assert_eq!(Invocation::read(&mut host).unwrap(), test_invocation());
            assert_eq!(read_host_stdin(&mut host), b"some input");
            client_host::send_stdout(&mut host, b"hello ").unwrap();
            client_host::send_stderr(&mut host, b"warning").unwrap();
            client_host::send_stdout(&mut host, b"world").unwrap();
            client_host::send_exit_code(&mut host, 42).unwrap();
        });

        let (mut out, mut err) = (Vec::new(), Vec::new());
        let code = run_session(
            client,
            &test_invocation(),
            Cursor::new(b"some input".to_vec()),
            None,
            &mut out,
            &mut err,
        );
        fake_host.join().unwrap();

        assert_eq!(code, 42);
        assert_eq!(out, b"hello world");
        assert_eq!(err, b"warning");
    }

    #[test]
    fn run_session_forwards_signals() {
        let (client, mut host) = UnixStream::pair().unwrap();
        let (sig_reader, mut sig_writer) = io::pipe().unwrap();
        let fake_host = thread::spawn(move || {
            assert_eq!(Invocation::read(&mut host).unwrap(), test_invocation());
            // stdin EOF and the signal may arrive in either order.
            let mut seen = Vec::new();
            while seen.len() < 2 {
                seen.push(Command::read(&mut host).unwrap());
            }
            assert!(seen.contains(&Command::Stdin(vec![])));
            assert!(seen.contains(&Command::Signal(client_host::SIGNAL_INT)));
            client_host::send_exit_code(&mut host, 130).unwrap();
        });
        sig_writer.write_all(&[libc::SIGINT as u8]).unwrap();

        let (mut out, mut err) = (Vec::new(), Vec::new());
        let code = run_session(
            client,
            &test_invocation(),
            io::empty(),
            Some(sig_reader),
            &mut out,
            &mut err,
        );
        fake_host.join().unwrap();
        assert_eq!(code, 130);
    }

    #[test]
    fn run_session_no_exit_code() {
        let (client, mut host) = UnixStream::pair().unwrap();
        let fake_host = thread::spawn(move || {
            Invocation::read(&mut host).unwrap();
            // Drain stdin first so the close is a clean EOF rather than a reset.
            read_host_stdin(&mut host);
            client_host::send_stdout(&mut host, b"partial").unwrap();
            // Dropping `host` closes the connection.
        });

        let (mut out, mut err) = (Vec::new(), Vec::new());
        let code = run_session(
            client,
            &test_invocation(),
            io::empty(),
            None,
            &mut out,
            &mut err,
        );
        fake_host.join().unwrap();

        assert_eq!(code, EXIT_TRANSPORT_FAILURE);
        assert_eq!(out, b"partial");
        let msg = String::from_utf8(err).unwrap();
        assert!(msg.starts_with("scoter-client: "), "{msg}");
        assert!(msg.contains("exit code"), "{msg}");
    }

    #[test]
    fn run_session_protocol_error() {
        let (client, mut host) = UnixStream::pair().unwrap();
        let fake_host = thread::spawn(move || {
            Invocation::read(&mut host).unwrap();
            host.write_all(&[99]).unwrap();
        });

        let (mut out, mut err) = (Vec::new(), Vec::new());
        let code = run_session(
            client,
            &test_invocation(),
            io::empty(),
            None,
            &mut out,
            &mut err,
        );
        fake_host.join().unwrap();

        assert_eq!(code, EXIT_TRANSPORT_FAILURE);
        assert!(!err.is_empty());
    }

    #[test]
    fn run_session_broker_closed() {
        let (client, host) = UnixStream::pair().unwrap();
        drop(host);
        let (mut out, mut err) = (Vec::new(), Vec::new());
        let code = run_session(
            client,
            &test_invocation(),
            io::empty(),
            None,
            &mut out,
            &mut err,
        );
        assert_eq!(code, EXIT_TRANSPORT_FAILURE);
        assert!(!err.is_empty());
    }
}
