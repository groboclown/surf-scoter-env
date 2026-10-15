//! Command that passes the invoked command to the listening host.
//! A "privileged host-execution broker," container side's listener.
//! This uses stdout & stdin to communicate with the host.
//!
//! This acts as a simple broker, just passing messages between the
//! client and the host process.  Each client connection on the Unix socket
//! becomes a process id; see `comm::broker_host` for the message rules.
//!
//! The broker exits when the host closes its stdin (exit 0), or when the host
//! sends a malformed message or can no longer be written to (exit 1).
//! Diagnostics go to stderr, since stdout carries the host protocol.

use std::collections::HashMap;
use std::fs;
use std::io::{self, BufRead, BufReader, BufWriter, ErrorKind, Read, Write};
use std::net::Shutdown;
use std::os::unix::fs::{FileTypeExt, MetadataExt};
use std::os::unix::net::{UnixListener, UnixStream};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::mpsc::{self, Receiver, SendError, Sender};
use std::sync::{Arc, Mutex, MutexGuard};
use std::thread;
use std::time::Duration;

use surf_scoter_env_launcher::comm::broker_host::{self, Command, ExitCode, ProcessId};
use surf_scoter_env_launcher::comm::client_broker;

const EXIT_OK: i32 = 0;
const EXIT_FAILURE: i32 = 1;

/// Exit code reported to the host when the client closed its connection.
const CLIENT_CLOSED: ExitCode = 0;
/// Exit code reported to the host when the client connection failed.
const CLIENT_IO_ERROR: ExitCode = 122;

/// Maximum number of client bytes sent per message.
const CLIENT_CHUNK: usize = 64 * 1024;

/// Pause after a failed accept, so a persistent error doesn't spin.
const ACCEPT_RETRY_DELAY: Duration = Duration::from_millis(100);

/// Messages from the host to a client connection; `None` means close it.
type ClientMessage = Option<Vec<u8>>;

/// Pass host messages (read from `r`) on to the clients, until the host closes the stream.
fn pump_host_input<R: BufRead>(mut r: R, data: &Mutex<InnerClientBroker>) -> io::Result<()> {
    loop {
        // Only a close between messages is clean; anywhere else, `recv` reports it.
        match r.fill_buf() {
            Ok([]) => return Ok(()),
            Ok(_) => {}
            Err(e) if e.kind() == ErrorKind::Interrupted => continue,
            Err(e) => return Err(e),
        }
        match Command::recv(&mut r)? {
            Command::Message((proc, msg)) => lock(data).host_message(proc, msg),
            Command::Terminated((proc, _)) => lock(data).host_closed(proc),
            Command::Started(proc) => {
                return Err(io::Error::new(
                    ErrorKind::InvalidData,
                    format!("host sent process started ({proc})"),
                ));
            }
        }
    }
}

struct Client {
    proc: ProcessId,
    host_stdin: Receiver<ClientMessage>,
    data: Arc<Mutex<InnerClientBroker>>,
}

impl Client {
    /// Run the connection until either side closes it.
    fn handle_client(self, stream: UnixStream) {
        let Client {
            proc,
            host_stdin,
            data,
        } = self;
        let to_client = match stream.try_clone() {
            Ok(writer) => thread::spawn(move || write_to_client(host_stdin, writer)),
            Err(_) => {
                let _ = lock(&data).client_closed(proc, CLIENT_IO_ERROR);
                return;
            }
        };

        let code = read_from_client(proc, &stream, &data);
        // Does nothing if the host closed this process first.
        let _ = lock(&data).client_closed(proc, code);
        // Also wakes the writer if it's stuck writing to a client that stopped reading.
        let _ = stream.shutdown(Shutdown::Both);
        let _ = to_client.join();
    }
}

/// Forward the client's bytes to the host; returns the exit code to report for the client.
fn read_from_client(
    proc: ProcessId,
    mut stream: &UnixStream,
    data: &Mutex<InnerClientBroker>,
) -> ExitCode {
    let mut buf = vec![0u8; CLIENT_CHUNK];
    loop {
        match stream.read(&mut buf) {
            Ok(0) => return CLIENT_CLOSED,
            Ok(n) => {
                if lock(data).send_msg(proc, buf[..n].to_vec()).is_err() {
                    // The host output is gone; the broker is shutting down.
                    return CLIENT_IO_ERROR;
                }
            }
            Err(e) if e.kind() == ErrorKind::Interrupted => continue,
            Err(_) => return CLIENT_IO_ERROR,
        }
    }
}

/// Write the host's bytes to the client, until the host closes the process
/// or the client side has finished.
fn write_to_client(host_stdin: Receiver<ClientMessage>, mut stream: UnixStream) {
    for msg in host_stdin {
        match msg {
            Some(bytes) => {
                if stream.write_all(&bytes).is_err() {
                    break;
                }
            }
            None => break,
        }
    }
    // Ends the reader too, if it's still waiting on the client.
    let _ = stream.shutdown(Shutdown::Both);
}

struct SocketListener {
    data: Arc<Mutex<InnerClientBroker>>,
    stopping: Arc<AtomicBool>,
}

impl SocketListener {
    /// Accept clients until `stopping` is set (followed by a connection to wake
    /// the accept) or the host output is gone.
    fn handle_server(self, listener: UnixListener) {
        let mut proc_idx: ProcessId = 0;
        for stream in listener.incoming() {
            if self.stopping.load(Ordering::SeqCst) {
                return;
            }
            match stream {
                Ok(stream) => {
                    let rx = match lock(&self.data).add_proc(proc_idx) {
                        Ok(rx) => rx,
                        Err(_) => {
                            eprintln!(
                                "scoter-broker: host output closed; no longer accepting clients"
                            );
                            return;
                        }
                    };
                    let client = Client {
                        proc: proc_idx,
                        host_stdin: rx,
                        data: self.data.clone(),
                    };
                    thread::spawn(move || client.handle_client(stream));
                    proc_idx += 1;
                }
                Err(e) => {
                    eprintln!("scoter-broker: failed to accept a client connection: {e}");
                    thread::sleep(ACCEPT_RETRY_DELAY);
                }
            }
        }
    }
}

struct InnerClientBroker {
    inp: Sender<Command>,
    proc_out: HashMap<ProcessId, Sender<ClientMessage>>,
}

impl InnerClientBroker {
    fn new() -> (Arc<Mutex<Self>>, Receiver<Command>) {
        let (tx, rx) = mpsc::channel();
        (
            Arc::new(Mutex::new(Self {
                inp: tx,
                proc_out: HashMap::new(),
            })),
            rx,
        )
    }

    /// Register a new client connection and tell the host about it.
    fn add_proc(&mut self, proc: ProcessId) -> Result<Receiver<ClientMessage>, SendError<Command>> {
        self.inp.send(Command::Started(proc))?;
        let (tx, rx) = mpsc::channel();
        self.proc_out.insert(proc, tx);
        Ok(rx)
    }

    /// Pass client bytes to the host.  Dropped if the host already closed the process.
    fn send_msg(&mut self, proc: ProcessId, data: Vec<u8>) -> Result<(), SendError<Command>> {
        if !self.proc_out.contains_key(&proc) {
            return Ok(());
        }
        self.inp.send(Command::Message((proc, data)))
    }

    /// The client connection ended.  Tells the host, unless the host closed the process first.
    fn client_closed(
        &mut self,
        proc: ProcessId,
        exit_code: ExitCode,
    ) -> Result<(), SendError<Command>> {
        if self.proc_out.remove(&proc).is_none() {
            return Ok(());
        }
        self.inp.send(Command::Terminated((proc, exit_code)))
    }

    /// Pass host bytes to the client.  Ignored for processes that are already closed.
    fn host_message(&mut self, proc: ProcessId, data: Vec<u8>) {
        if let Some(tx) = self.proc_out.get(&proc) {
            // A failure means the client connection is already finishing up.
            let _ = tx.send(Some(data));
        }
    }

    /// The host is done with the process: close the client once pending bytes are written.
    fn host_closed(&mut self, proc: ProcessId) {
        if let Some(tx) = self.proc_out.remove(&proc) {
            let _ = tx.send(None);
        }
    }

    /// Close every client connection.
    fn close_all(&mut self) {
        for (_, tx) in self.proc_out.drain() {
            let _ = tx.send(None);
        }
    }
}

/// The shared state stays consistent even if a holder panicked, so poisoning is ignored.
fn lock(data: &Mutex<InnerClientBroker>) -> MutexGuard<'_, InnerClientBroker> {
    data.lock().unwrap_or_else(|e| e.into_inner())
}

/// The listening socket, remembered so the broker only removes its own socket file.
struct BoundSocket {
    listener: UnixListener,
    path: PathBuf,
    dev: u64,
    ino: u64,
}

impl BoundSocket {
    /// Bind `path` with mode 0600, replacing a stale socket left behind by an earlier broker.
    fn bind(path: &Path) -> io::Result<Self> {
        // The umask is process-wide, so concurrent binds must not interleave.
        static UMASK_LOCK: Mutex<()> = Mutex::new(());

        remove_stale_socket(path)?;
        let listener = {
            let _guard = UMASK_LOCK.lock().unwrap_or_else(|e| e.into_inner());
            // SAFETY: umask has no failure modes; the previous mask is restored right after bind.
            let old_mask = unsafe { libc::umask(0o177) };
            let listener = UnixListener::bind(path);
            unsafe { libc::umask(old_mask) };
            listener?
        };
        let meta = fs::symlink_metadata(path)?;
        Ok(BoundSocket {
            listener,
            path: path.to_path_buf(),
            dev: meta.dev(),
            ino: meta.ino(),
        })
    }

    /// Remove the socket file, if it's still the one this broker created.
    fn remove(&self) {
        if let Ok(meta) = fs::symlink_metadata(&self.path)
            && meta.dev() == self.dev
            && meta.ino() == self.ino
        {
            let _ = fs::remove_file(&self.path);
        }
    }
}

/// Remove `path` if it's a socket.  Anything else there is left alone and reported.
fn remove_stale_socket(path: &Path) -> io::Result<()> {
    match fs::symlink_metadata(path) {
        Ok(meta) if meta.file_type().is_socket() => fs::remove_file(path),
        Ok(_) => Err(io::Error::new(
            ErrorKind::AlreadyExists,
            format!("{} exists and is not a socket", path.display()),
        )),
        Err(e) if e.kind() == ErrorKind::NotFound => Ok(()),
        Err(e) => Err(e),
    }
}

/// Run the broker on `socket_path`, talking to the host over `host_in` / `host_out`.
/// Returns the process exit code.
fn run<R, W>(socket_path: &Path, host_in: R, host_out: W) -> i32
where
    R: Read + Send + 'static,
    W: Write + Send + 'static,
{
    let socket = match BoundSocket::bind(socket_path) {
        Ok(s) => s,
        Err(e) => {
            eprintln!(
                "scoter-broker: cannot listen on {}: {e}",
                socket_path.display()
            );
            return EXIT_FAILURE;
        }
    };
    let listener = match socket.listener.try_clone() {
        Ok(l) => l,
        Err(e) => {
            eprintln!(
                "scoter-broker: cannot listen on {}: {e}",
                socket_path.display()
            );
            socket.remove();
            return EXIT_FAILURE;
        }
    };

    let (data, rx) = InnerClientBroker::new();
    // Whichever host direction ends first decides the exit code.
    let (done_tx, done_rx) = mpsc::channel::<i32>();

    {
        let done = done_tx.clone();
        thread::spawn(move || {
            let code = match broker_host::send_all(&rx, &mut BufWriter::new(host_out)) {
                Ok(()) => EXIT_OK,
                Err(e) => {
                    eprintln!("scoter-broker: cannot write to host: {e}");
                    EXIT_FAILURE
                }
            };
            let _ = done.send(code);
        });
    }
    {
        let data = data.clone();
        let done = done_tx;
        thread::spawn(move || {
            let code = match pump_host_input(BufReader::new(host_in), &data) {
                Ok(()) => EXIT_OK,
                Err(e) => {
                    eprintln!("scoter-broker: bad input from host: {e}");
                    EXIT_FAILURE
                }
            };
            let _ = done.send(code);
        });
    }
    let stopping = Arc::new(AtomicBool::new(false));
    let accepting = {
        let server = SocketListener {
            data: data.clone(),
            stopping: stopping.clone(),
        };
        thread::spawn(move || server.handle_server(listener))
    };

    let code = done_rx.recv().unwrap_or(EXIT_FAILURE);

    stopping.store(true, Ordering::SeqCst);
    // Wake the accept so the listener sees `stopping`.
    if UnixStream::connect(socket_path).is_ok() {
        let _ = accepting.join();
    }
    lock(&data).close_all();
    socket.remove();
    code
}

fn main() {
    std::process::exit(run(
        Path::new(client_broker::UNIX_SOCKET),
        io::stdin(),
        io::stdout(),
    ));
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::{Cursor, PipeReader, PipeWriter};
    use std::os::unix::fs::PermissionsExt;
    use std::sync::atomic::AtomicUsize;

    const TIMEOUT: Option<Duration> = Some(Duration::from_secs(10));

    /// A unique socket path for this test, removed when dropped.
    struct TempSocket(PathBuf);

    impl TempSocket {
        fn new() -> Self {
            static COUNTER: AtomicUsize = AtomicUsize::new(0);
            let n = COUNTER.fetch_add(1, Ordering::SeqCst);
            TempSocket(std::env::temp_dir().join(format!(
                "scoter-broker-test-{}-{n}.sock",
                std::process::id()
            )))
        }
    }

    impl Drop for TempSocket {
        fn drop(&mut self) {
            let _ = fs::remove_file(&self.0);
        }
    }

    fn encode(cmds: &[Command]) -> Vec<u8> {
        let mut buf = Vec::new();
        for cmd in cmds {
            cmd.send(&mut buf).unwrap();
        }
        buf
    }

    fn recv_timeout<T>(rx: &Receiver<T>) -> T {
        rx.recv_timeout(Duration::from_secs(10)).expect("timed out")
    }

    /// Read from the client side until EOF.
    fn read_to_end(mut stream: &UnixStream) -> Vec<u8> {
        stream.set_read_timeout(TIMEOUT).unwrap();
        let mut buf = Vec::new();
        stream.read_to_end(&mut buf).unwrap();
        buf
    }

    // ---- InnerClientBroker ----

    #[test]
    fn add_proc_announces_process() {
        let (data, rx) = InnerClientBroker::new();
        let _client = lock(&data).add_proc(3).unwrap();
        assert_eq!(rx.try_recv().unwrap(), Command::Started(3));
        assert!(lock(&data).proc_out.contains_key(&3));
    }

    #[test]
    fn add_proc_fails_when_host_output_gone() {
        let (data, rx) = InnerClientBroker::new();
        drop(rx);
        assert!(lock(&data).add_proc(0).is_err());
        assert!(lock(&data).proc_out.is_empty());
    }

    #[test]
    fn send_msg_forwards_only_open_processes() {
        let (data, rx) = InnerClientBroker::new();
        let _client = lock(&data).add_proc(1).unwrap();
        rx.try_recv().unwrap();
        lock(&data).send_msg(1, b"abc".to_vec()).unwrap();
        lock(&data).send_msg(2, b"unknown".to_vec()).unwrap();
        assert_eq!(
            rx.try_recv().unwrap(),
            Command::Message((1, b"abc".to_vec()))
        );
        assert!(rx.try_recv().is_err());
    }

    #[test]
    fn client_closed_reports_once() {
        let (data, rx) = InnerClientBroker::new();
        let _client = lock(&data).add_proc(1).unwrap();
        rx.try_recv().unwrap();
        lock(&data).client_closed(1, CLIENT_IO_ERROR).unwrap();
        lock(&data).client_closed(1, CLIENT_CLOSED).unwrap();
        assert_eq!(
            rx.try_recv().unwrap(),
            Command::Terminated((1, CLIENT_IO_ERROR))
        );
        assert!(rx.try_recv().is_err());
        // Bytes after the close are dropped.
        lock(&data).send_msg(1, b"late".to_vec()).unwrap();
        assert!(rx.try_recv().is_err());
    }

    #[test]
    fn host_close_suppresses_client_terminated() {
        let (data, rx) = InnerClientBroker::new();
        let client = lock(&data).add_proc(1).unwrap();
        rx.try_recv().unwrap();
        lock(&data).host_closed(1);
        lock(&data).client_closed(1, CLIENT_CLOSED).unwrap();
        assert!(rx.try_recv().is_err());
        assert_eq!(client.try_recv().unwrap(), None);
    }

    #[test]
    fn host_message_and_close_delivered_in_order() {
        let (data, _rx) = InnerClientBroker::new();
        let client = lock(&data).add_proc(4).unwrap();
        lock(&data).host_message(4, b"one".to_vec());
        lock(&data).host_message(4, b"two".to_vec());
        lock(&data).host_closed(4);
        lock(&data).host_message(4, b"after close".to_vec());
        assert_eq!(
            client.try_iter().collect::<Vec<_>>(),
            vec![Some(b"one".to_vec()), Some(b"two".to_vec()), None,]
        );
        assert!(lock(&data).proc_out.is_empty());
    }

    #[test]
    fn host_messages_for_unknown_or_gone_clients_are_ignored() {
        let (data, _rx) = InnerClientBroker::new();
        lock(&data).host_message(9, b"x".to_vec());
        lock(&data).host_closed(9);
        let client = lock(&data).add_proc(1).unwrap();
        drop(client);
        lock(&data).host_message(1, b"x".to_vec());
        lock(&data).host_closed(1);
    }

    #[test]
    fn close_all_closes_every_client() {
        let (data, _rx) = InnerClientBroker::new();
        let a = lock(&data).add_proc(0).unwrap();
        let b = lock(&data).add_proc(1).unwrap();
        lock(&data).close_all();
        assert_eq!(a.try_recv().unwrap(), None);
        assert_eq!(b.try_recv().unwrap(), None);
        assert!(lock(&data).proc_out.is_empty());
    }

    // ---- pump_host_input ----

    #[test]
    fn host_input_routes_messages() {
        let (data, _rx) = InnerClientBroker::new();
        let a = lock(&data).add_proc(0).unwrap();
        let b = lock(&data).add_proc(1).unwrap();
        let input = encode(&[
            Command::Message((0, b"to a".to_vec())),
            Command::Message((1, b"to b".to_vec())),
            Command::Message((7, b"nobody".to_vec())),
            Command::Terminated((0, 5)),
            Command::Terminated((7, 0)),
        ]);
        pump_host_input(Cursor::new(input), &data).unwrap();
        assert_eq!(
            a.try_iter().collect::<Vec<_>>(),
            vec![Some(b"to a".to_vec()), None]
        );
        assert_eq!(
            b.try_iter().collect::<Vec<_>>(),
            vec![Some(b"to b".to_vec())]
        );
    }

    #[test]
    fn host_input_clean_eof() {
        let (data, _rx) = InnerClientBroker::new();
        pump_host_input(Cursor::new(Vec::new()), &data).unwrap();
    }

    #[test]
    fn host_input_eof_mid_message() {
        let (data, _rx) = InnerClientBroker::new();
        let mut input = encode(&[Command::Message((0, b"abc".to_vec()))]);
        input.pop();
        let err = pump_host_input(Cursor::new(input), &data).unwrap_err();
        assert_eq!(err.kind(), ErrorKind::UnexpectedEof);
    }

    #[test]
    fn host_input_rejects_process_started() {
        let (data, _rx) = InnerClientBroker::new();
        let input = encode(&[Command::Started(0)]);
        let err = pump_host_input(Cursor::new(input), &data).unwrap_err();
        assert_eq!(err.kind(), ErrorKind::InvalidData);
    }

    #[test]
    fn host_input_rejects_bad_command() {
        let (data, _rx) = InnerClientBroker::new();
        let err = pump_host_input(Cursor::new(vec![77]), &data).unwrap_err();
        assert_eq!(err.kind(), ErrorKind::InvalidData);
    }

    #[test]
    fn host_input_retries_interrupted() {
        struct InterruptOnce {
            inner: Cursor<Vec<u8>>,
            interrupted: bool,
        }
        impl Read for InterruptOnce {
            fn read(&mut self, buf: &mut [u8]) -> io::Result<usize> {
                if !self.interrupted {
                    self.interrupted = true;
                    return Err(io::Error::from(ErrorKind::Interrupted));
                }
                self.inner.read(buf)
            }
        }
        let (data, _rx) = InnerClientBroker::new();
        let client = lock(&data).add_proc(0).unwrap();
        let input = InterruptOnce {
            inner: Cursor::new(encode(&[Command::Message((0, b"x".to_vec()))])),
            interrupted: false,
        };
        pump_host_input(BufReader::new(input), &data).unwrap();
        assert_eq!(client.try_recv().unwrap(), Some(b"x".to_vec()));
    }

    // ---- Client ----

    /// Start `handle_client` for process 0 on one end of a socket pair.
    fn start_client() -> (
        Arc<Mutex<InnerClientBroker>>,
        Receiver<Command>,
        UnixStream,
        thread::JoinHandle<()>,
    ) {
        let (data, rx) = InnerClientBroker::new();
        let host_stdin = lock(&data).add_proc(0).unwrap();
        assert_eq!(rx.try_recv().unwrap(), Command::Started(0));
        let (broker_side, client_side) = UnixStream::pair().unwrap();
        let client = Client {
            proc: 0,
            host_stdin,
            data: data.clone(),
        };
        let handle = thread::spawn(move || client.handle_client(broker_side));
        (data, rx, client_side, handle)
    }

    #[test]
    fn client_bytes_reach_host_then_client_close_reported() {
        let (_data, rx, mut client, handle) = start_client();
        let payload: Vec<u8> = (0..(CLIENT_CHUNK * 2 + 17))
            .map(|i| (i % 253) as u8)
            .collect();
        client.write_all(&payload).unwrap();
        client.shutdown(Shutdown::Write).unwrap();

        let mut received = Vec::new();
        loop {
            match recv_timeout(&rx) {
                Command::Message((0, bytes)) => {
                    assert!(bytes.len() <= CLIENT_CHUNK);
                    received.extend_from_slice(&bytes);
                }
                Command::Terminated((0, code)) => {
                    assert_eq!(code, CLIENT_CLOSED);
                    break;
                }
                other => panic!("unexpected {other:?}"),
            }
        }
        assert_eq!(received, payload);
        handle.join().unwrap();
        // The broker closes its side too.
        assert!(read_to_end(&client).is_empty());
    }

    #[test]
    fn host_bytes_reach_client_then_host_close() {
        let (data, rx, client, handle) = start_client();
        lock(&data).host_message(0, b"hello ".to_vec());
        lock(&data).host_message(0, b"world".to_vec());
        lock(&data).host_closed(0);

        assert_eq!(read_to_end(&client), b"hello world");
        handle.join().unwrap();
        // Host-initiated close: no terminated message back to the host.
        assert!(rx.try_recv().is_err());
    }

    #[test]
    fn host_close_while_client_still_sending() {
        let (data, rx, mut client, handle) = start_client();
        client.write_all(b"early").unwrap();
        assert_eq!(recv_timeout(&rx), Command::Message((0, b"early".to_vec())));
        lock(&data).host_closed(0);
        assert!(read_to_end(&client).is_empty());
        handle.join().unwrap();
        // Further client writes go nowhere and nothing more reaches the host.
        let _ = client.write_all(b"late");
        assert!(rx.try_recv().is_err());
    }

    #[test]
    fn client_vanishes_without_reading() {
        let (data, rx, client, handle) = start_client();
        drop(client);
        assert_eq!(recv_timeout(&rx), Command::Terminated((0, CLIENT_CLOSED)));
        handle.join().unwrap();
        // Late host messages for the process are ignored.
        lock(&data).host_message(0, b"x".to_vec());
        lock(&data).host_closed(0);
    }

    // ---- SocketListener / BoundSocket ----

    #[test]
    fn listener_assigns_increasing_process_ids() {
        let path = TempSocket::new();
        let socket = BoundSocket::bind(&path.0).unwrap();
        let (data, rx) = InnerClientBroker::new();
        let stopping = Arc::new(AtomicBool::new(false));
        let server = SocketListener {
            data: data.clone(),
            stopping: stopping.clone(),
        };
        let listener = socket.listener.try_clone().unwrap();
        let accepting = thread::spawn(move || server.handle_server(listener));

        let _a = UnixStream::connect(&path.0).unwrap();
        assert_eq!(recv_timeout(&rx), Command::Started(0));
        let _b = UnixStream::connect(&path.0).unwrap();
        assert_eq!(recv_timeout(&rx), Command::Started(1));

        stopping.store(true, Ordering::SeqCst);
        let _wake = UnixStream::connect(&path.0).unwrap();
        accepting.join().unwrap();
        // The wake-up connection isn't treated as a client.
        assert!(rx.try_recv().is_err());
    }

    #[test]
    fn listener_stops_when_host_output_gone() {
        let path = TempSocket::new();
        let socket = BoundSocket::bind(&path.0).unwrap();
        let (data, rx) = InnerClientBroker::new();
        drop(rx);
        let server = SocketListener {
            data,
            stopping: Arc::new(AtomicBool::new(false)),
        };
        let listener = socket.listener.try_clone().unwrap();
        let accepting = thread::spawn(move || server.handle_server(listener));
        let _a = UnixStream::connect(&path.0).unwrap();
        accepting.join().unwrap();
    }

    #[test]
    fn bind_sets_owner_only_mode() {
        let path = TempSocket::new();
        let _socket = BoundSocket::bind(&path.0).unwrap();
        let mode = fs::symlink_metadata(&path.0).unwrap().permissions().mode();
        assert_eq!(mode & 0o777, 0o600);
    }

    #[test]
    fn bind_replaces_stale_socket() {
        let path = TempSocket::new();
        drop(UnixListener::bind(&path.0).unwrap());
        assert!(path.0.exists());
        let socket = BoundSocket::bind(&path.0).unwrap();
        let _conn = UnixStream::connect(&path.0).unwrap();
        socket.listener.accept().unwrap();
    }

    #[test]
    fn bind_refuses_non_socket() {
        let path = TempSocket::new();
        fs::write(&path.0, b"keep me").unwrap();
        let err = BoundSocket::bind(&path.0).err().unwrap();
        assert_eq!(err.kind(), ErrorKind::AlreadyExists);
        assert_eq!(fs::read(&path.0).unwrap(), b"keep me");
    }

    #[test]
    fn remove_deletes_own_socket() {
        let path = TempSocket::new();
        let socket = BoundSocket::bind(&path.0).unwrap();
        socket.remove();
        assert!(!path.0.exists());
    }

    #[test]
    fn remove_leaves_replacement_socket() {
        let path = TempSocket::new();
        let old = BoundSocket::bind(&path.0).unwrap();
        let new = BoundSocket::bind(&path.0).unwrap();
        old.remove();
        assert!(path.0.exists());
        new.remove();
        assert!(!path.0.exists());
    }

    // ---- run ----

    struct RunningBroker {
        path: TempSocket,
        to_broker: PipeWriter,
        from_broker: PipeReader,
        handle: thread::JoinHandle<i32>,
    }

    fn start_broker() -> RunningBroker {
        let path = TempSocket::new();
        let (host_in, to_broker) = io::pipe().unwrap();
        let (from_broker, host_out) = io::pipe().unwrap();
        let socket_path = path.0.clone();
        let handle = thread::spawn(move || run(&socket_path, host_in, host_out));
        // Wait for the listener.
        for _ in 0..1000 {
            if path.0.exists() {
                break;
            }
            thread::sleep(Duration::from_millis(5));
        }
        RunningBroker {
            path,
            to_broker,
            from_broker,
            handle,
        }
    }

    #[test]
    fn run_full_conversation() {
        let mut broker = start_broker();
        let mut client = UnixStream::connect(&broker.path.0).unwrap();
        client.write_all(b"request").unwrap();

        assert_eq!(
            Command::recv(&mut broker.from_broker).unwrap(),
            Command::Started(0)
        );
        assert_eq!(
            Command::recv(&mut broker.from_broker).unwrap(),
            Command::Message((0, b"request".to_vec()))
        );

        broker
            .to_broker
            .write_all(&encode(&[
                Command::Message((0, b"response".to_vec())),
                Command::Terminated((0, 0)),
            ]))
            .unwrap();
        assert_eq!(read_to_end(&client), b"response");

        // A second client gets the next process id; its close is reported.
        let second = UnixStream::connect(&broker.path.0).unwrap();
        assert_eq!(
            Command::recv(&mut broker.from_broker).unwrap(),
            Command::Started(1)
        );
        drop(second);
        assert_eq!(
            Command::recv(&mut broker.from_broker).unwrap(),
            Command::Terminated((1, CLIENT_CLOSED))
        );

        // Host goes away: clean exit, socket removed.
        let idle = UnixStream::connect(&broker.path.0).unwrap();
        assert_eq!(
            Command::recv(&mut broker.from_broker).unwrap(),
            Command::Started(2)
        );
        drop(broker.to_broker);
        assert_eq!(broker.handle.join().unwrap(), EXIT_OK);
        assert!(!broker.path.0.exists());
        // Remaining clients are disconnected.
        assert!(read_to_end(&idle).is_empty());
    }

    #[test]
    fn run_exits_on_malformed_host_input() {
        let mut broker = start_broker();
        let client = UnixStream::connect(&broker.path.0).unwrap();
        assert_eq!(
            Command::recv(&mut broker.from_broker).unwrap(),
            Command::Started(0)
        );
        broker
            .to_broker
            .write_all(&encode(&[Command::Started(5)]))
            .unwrap();
        assert_eq!(broker.handle.join().unwrap(), EXIT_FAILURE);
        assert!(!broker.path.0.exists());
        assert!(read_to_end(&client).is_empty());
    }

    #[test]
    fn run_exits_when_host_stops_reading() {
        let broker = start_broker();
        drop(broker.from_broker);
        // The next event for the host fails to write.
        let _client = UnixStream::connect(&broker.path.0).unwrap();
        assert_eq!(broker.handle.join().unwrap(), EXIT_FAILURE);
        assert!(!broker.path.0.exists());
    }

    #[test]
    fn run_fails_when_socket_cannot_be_bound() {
        let path = std::env::temp_dir()
            .join("scoter-broker-test-missing-dir")
            .join("x.sock");
        assert_eq!(run(&path, io::empty(), io::sink()), EXIT_FAILURE);
    }
}
