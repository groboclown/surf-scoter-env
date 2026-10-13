//! Runs an approved request as a host process, speaking the `client_host`
//! protocol with the client.
//!
//! The process gets the host's environment, and runs in the directory chosen
//! by `analyze` or else a fresh temporary directory.  Temporary file arguments
//! are placed in a temporary directory too; it's removed once the process
//! exits.  Client stdin is passed to the process,
//! its stdout / stderr are passed back, and forwarded signals are delivered
//! (on Windows, the termination signals kill the process; others are ignored).
//! If the client goes away, the process is killed.

use std::ffi::OsString;
use std::fs;
use std::io::{self, ErrorKind, Read, Write};
use std::path::{Path, PathBuf};
use std::process::{Child, ChildStdin, Command as Process, ExitStatus, Stdio};
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::mpsc::{self, Receiver, SyncSender};
use std::sync::{Arc, Mutex, MutexGuard};
use std::thread;
use std::time::Duration;

use surf_scoter_env_launcher::comm::client_host::{self, Command};

/// Exit code when the request isn't allowed.
pub(crate) const EXIT_DENIED: i32 = 126;
/// Exit code when the process couldn't be started.
pub(crate) const EXIT_START_FAILED: i32 = 127;

/// Maximum number of output bytes sent per message.
const OUTPUT_CHUNK: usize = 64 * 1024;
/// Stdin chunks buffered for a process that isn't reading them yet.
const STDIN_BACKLOG: usize = 64;
/// How often to check whether the process exited, once its output has closed.
const WAIT_POLL: Duration = Duration::from_millis(10);

/// What to run.  Built by `analyze`.
#[derive(Debug, PartialEq, Eq)]
pub(crate) struct ExecRequest {
    pub program: String,
    pub args: Vec<Arg>,
    /// The host directory to run in; `None` for a new temporary directory.
    pub cwd: Option<PathBuf>,
}

/// One argument of an approved request.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) enum Arg {
    /// Passed to the process as is.
    Plain(String),
    /// A resolved host path, passed as is.
    HostPath(PathBuf),
    /// A file in the request's temporary directory, named `name` or else a
    /// generated name.
    TempFile { name: Option<String> },
}

impl ExecRequest {
    /// The arguments as given to the host process; temporary files are placed in `temp_dir`.
    fn host_args(&self, temp_dir: Option<&Path>) -> Vec<OsString> {
        self.args
            .iter()
            .enumerate()
            .map(|(i, arg)| match arg {
                Arg::Plain(value) => OsString::from(value),
                Arg::HostPath(path) => path.clone().into_os_string(),
                Arg::TempFile { name } => {
                    let dir = temp_dir.expect("a temporary directory for temporary files");
                    let name = name.clone().unwrap_or_else(|| format!("arg-{}", i + 1));
                    dir.join(name).into_os_string()
                }
            })
            .collect()
    }

    fn needs_temp_dir(&self) -> bool {
        self.cwd.is_none()
            || self
                .args
                .iter()
                .any(|arg| matches!(arg, Arg::TempFile { .. }))
    }
}

/// Where client-bound messages go.  Each call carries one complete `client_host` message.
pub(crate) trait FrameSink: Clone + Send + 'static {
    fn send_frame(&self, frame: Vec<u8>) -> io::Result<()>;
}

/// Encode one message and send it whole.
pub(crate) fn emit<S: FrameSink>(
    sink: &S,
    build: impl FnOnce(&mut Vec<u8>) -> io::Result<()>,
) -> io::Result<()> {
    let mut frame = Vec::new();
    build(&mut frame)?;
    sink.send_frame(frame)
}

/// Report a failed request to the client: `message` on stderr, then `exit_code`.
/// Returns `exit_code`.
pub(crate) fn fail<S: FrameSink>(sink: &S, message: &str, exit_code: i32) -> i32 {
    let _ = emit(sink, |f| {
        client_host::send_stderr(f, format!("scoter-host: {message}\n").as_bytes())
    });
    let _ = emit(sink, |f| client_host::send_exit_code(f, exit_code));
    exit_code
}

/// Run `request` until it exits, reading the client's stdin and signals from `input`.
/// Output and the exit code go to `sink`.  Returns the exit code.
pub(crate) fn run<R: Read + Send + 'static, S: FrameSink>(
    request: &ExecRequest,
    input: R,
    sink: &S,
) -> i32 {
    // For the working directory and temporary files; removed once the process exits.
    let temp_dir = if request.needs_temp_dir() {
        match TempDir::new() {
            Ok(dir) => Some(dir),
            Err(e) => {
                return fail(
                    sink,
                    &format!("cannot create a temporary directory: {e}"),
                    EXIT_START_FAILED,
                );
            }
        }
    } else {
        None
    };
    let temp_path = temp_dir.as_ref().map(TempDir::path);
    let cwd = match &request.cwd {
        Some(dir) => dir.as_path(),
        None => temp_path.expect("a temporary working directory"),
    };
    let child = Process::new(&request.program)
        .args(request.host_args(temp_path))
        // TODO race: `cwd` and the host path arguments were checked by
        //      `analyze`, but are used here by path; see `analyze::resolve`.
        .current_dir(cwd)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn();
    let mut child = match child {
        Ok(child) => child,
        Err(e) => {
            return fail(
                sink,
                &format!("cannot run {}: {e}", request.program),
                EXIT_START_FAILED,
            );
        }
    };
    let stdin = child.stdin.take().expect("stdin is piped");
    let stdout = child.stdout.take().expect("stdout is piped");
    let stderr = child.stderr.take().expect("stderr is piped");
    let running = Arc::new(Mutex::new(Running {
        child,
        status: None,
    }));

    {
        // Not joined: it ends when the client's input closes.
        let running = running.clone();
        thread::spawn(move || pump_input(input, stdin, &running));
    }
    let out = {
        let sink = sink.clone();
        thread::spawn(move || pump_output(stdout, &sink, client_host::send_stdout))
    };
    let err = {
        let sink = sink.clone();
        thread::spawn(move || pump_output(stderr, &sink, client_host::send_stderr))
    };
    // All output is sent before the exit code.
    let _ = out.join();
    let _ = err.join();

    match wait(&running) {
        Ok(status) => {
            let code = exit_code(status);
            let _ = emit(sink, |f| client_host::send_exit_code(f, code));
            code
        }
        Err(e) => fail(
            sink,
            &format!("lost track of {}: {e}", request.program),
            EXIT_START_FAILED,
        ),
    }
}

/// The child process, and its exit status once it has been reaped.
/// Signals are only delivered before the status is known, so the pid can't
/// have been reused by another process.
struct Running {
    child: Child,
    status: Option<ExitStatus>,
}

fn lock(running: &Mutex<Running>) -> MutexGuard<'_, Running> {
    running.lock().unwrap_or_else(|e| e.into_inner())
}

fn wait(running: &Mutex<Running>) -> io::Result<ExitStatus> {
    loop {
        {
            let mut r = lock(running);
            if let Some(status) = r.child.try_wait()? {
                r.status = Some(status);
                return Ok(status);
            }
        }
        thread::sleep(WAIT_POLL);
    }
}

fn kill(running: &Mutex<Running>) {
    let mut r = lock(running);
    if r.status.is_none() {
        let _ = r.child.kill();
    }
}

fn deliver_signal(running: &Mutex<Running>, wire: u32) {
    // Only Windows needs `mut`, to kill the child.
    #[cfg_attr(unix, allow(unused_mut))]
    let mut r = lock(running);
    if r.status.is_some() {
        return;
    }
    match delivery_for(wire) {
        #[cfg(unix)]
        Delivery::Signal(sig) => {
            // SAFETY: kill(2) has no memory-safety requirements; the pid belongs
            // to our unreaped child.
            unsafe { libc::kill(r.child.id() as libc::pid_t, sig) };
        }
        #[cfg(not(unix))]
        Delivery::Kill => {
            let _ = r.child.kill();
        }
        Delivery::Ignore => {}
    }
}

/// How a forwarded signal reaches the process on this platform.
#[derive(Debug, PartialEq, Eq)]
enum Delivery {
    #[cfg(unix)]
    Signal(libc::c_int),
    #[cfg(not(unix))]
    Kill,
    Ignore,
}

#[cfg(unix)]
fn delivery_for(wire: u32) -> Delivery {
    match wire {
        client_host::SIGNAL_HUP => Delivery::Signal(libc::SIGHUP),
        client_host::SIGNAL_INT => Delivery::Signal(libc::SIGINT),
        client_host::SIGNAL_QUIT => Delivery::Signal(libc::SIGQUIT),
        client_host::SIGNAL_USR1 => Delivery::Signal(libc::SIGUSR1),
        client_host::SIGNAL_USR2 => Delivery::Signal(libc::SIGUSR2),
        client_host::SIGNAL_TERM => Delivery::Signal(libc::SIGTERM),
        client_host::SIGNAL_WINCH => Delivery::Signal(libc::SIGWINCH),
        _ => Delivery::Ignore,
    }
}

#[cfg(not(unix))]
fn delivery_for(wire: u32) -> Delivery {
    match wire {
        client_host::SIGNAL_HUP
        | client_host::SIGNAL_INT
        | client_host::SIGNAL_QUIT
        | client_host::SIGNAL_TERM => Delivery::Kill,
        _ => Delivery::Ignore,
    }
}

/// The exit code to report for `status`.  Death by signal is reported the way
/// shells do, as 128 + the signal number.
fn exit_code(status: ExitStatus) -> i32 {
    if let Some(code) = status.code() {
        return code;
    }
    #[cfg(unix)]
    {
        use std::os::unix::process::ExitStatusExt;
        if let Some(sig) = status.signal() {
            return 128 + sig;
        }
    }
    1
}

/// Handle the client's messages: stdin goes to the process, signals are delivered.
/// When the client goes away (or misbehaves), the process is killed.
fn pump_input<R: Read>(mut input: R, stdin: ChildStdin, running: &Mutex<Running>) {
    // Writing happens on its own thread, so signals still get through while
    // the process isn't reading its stdin.  Once `STDIN_BACKLOG` chunks are
    // waiting, reading the client blocks (and with it, signal delivery) until
    // the process catches up; the client can't make the host buffer without bound.
    let (tx, rx) = mpsc::sync_channel(STDIN_BACKLOG);
    thread::spawn(move || write_stdin(rx, stdin));
    let mut stdin_tx: Option<SyncSender<Vec<u8>>> = Some(tx);
    loop {
        match Command::read(&mut input) {
            // Dropping the sender closes the process's stdin.
            Ok(Command::Stdin(data)) if data.is_empty() => stdin_tx = None,
            Ok(Command::Stdin(data)) => {
                if let Some(tx) = &stdin_tx
                    && tx.send(data).is_err()
                {
                    // The process closed its stdin.
                    stdin_tx = None;
                }
            }
            Ok(Command::Signal(sig)) => deliver_signal(running, sig),
            Ok(_) | Err(_) => {
                kill(running);
                return;
            }
        }
    }
}

fn write_stdin(rx: Receiver<Vec<u8>>, mut stdin: ChildStdin) {
    for data in rx {
        if stdin.write_all(&data).is_err() {
            return;
        }
    }
}

/// Send everything read from `r` to the client, using `send` to frame it.
fn pump_output<R: Read, S: FrameSink>(
    mut r: R,
    sink: &S,
    send: fn(&mut Vec<u8>, &[u8]) -> io::Result<()>,
) {
    let mut buf = vec![0u8; OUTPUT_CHUNK];
    loop {
        let n = match r.read(&mut buf) {
            Ok(0) => return,
            Ok(n) => n,
            Err(e) if e.kind() == ErrorKind::Interrupted => continue,
            Err(_) => return,
        };
        if emit(sink, |f| send(f, &buf[..n])).is_err() {
            // The client is gone; the input side kills the process.
            return;
        }
    }
}

/// A private, uniquely named directory, removed (with its contents) on drop.
pub(crate) struct TempDir(PathBuf);

impl TempDir {
    pub(crate) fn new() -> io::Result<Self> {
        static COUNTER: AtomicUsize = AtomicUsize::new(0);
        let base = std::env::temp_dir();
        for _ in 0..1000 {
            let n = COUNTER.fetch_add(1, Ordering::Relaxed);
            let path = base.join(format!("scoter-host-{}-{n}", std::process::id()));
            // Only Unix needs `mut`, to set the mode.
            #[cfg_attr(not(unix), allow(unused_mut))]
            let mut builder = fs::DirBuilder::new();
            #[cfg(unix)]
            {
                use std::os::unix::fs::DirBuilderExt;
                builder.mode(0o700);
            }
            match builder.create(&path) {
                Ok(()) => return Ok(TempDir(path)),
                // Left over from an earlier host with the same pid.
                Err(e) if e.kind() == ErrorKind::AlreadyExists => continue,
                Err(e) => return Err(e),
            }
        }
        Err(io::Error::new(
            ErrorKind::AlreadyExists,
            "no free temporary directory name",
        ))
    }

    pub(crate) fn path(&self) -> &Path {
        &self.0
    }
}

impl Drop for TempDir {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.0);
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::{Cursor, PipeWriter};

    /// Collects the frames sent to the client.
    #[derive(Clone, Default)]
    struct VecSink(Arc<Mutex<Vec<Vec<u8>>>>);

    impl FrameSink for VecSink {
        fn send_frame(&self, frame: Vec<u8>) -> io::Result<()> {
            self.0.lock().unwrap().push(frame);
            Ok(())
        }
    }

    impl VecSink {
        /// Each frame must hold exactly one message.
        fn commands(&self) -> Vec<Command> {
            self.0
                .lock()
                .unwrap()
                .iter()
                .map(|frame| {
                    let mut cur = Cursor::new(frame);
                    let cmd = Command::read(&mut cur).unwrap();
                    assert_eq!(
                        cur.position() as usize,
                        frame.len(),
                        "one message per frame"
                    );
                    cmd
                })
                .collect()
        }

        #[cfg_attr(not(unix), allow(dead_code))]
        fn stdout(&self) -> Vec<u8> {
            self.commands()
                .into_iter()
                .filter_map(|c| match c {
                    Command::Stdout(d) => Some(d),
                    _ => None,
                })
                .flatten()
                .collect()
        }

        fn stderr(&self) -> Vec<u8> {
            self.commands()
                .into_iter()
                .filter_map(|c| match c {
                    Command::Stderr(d) => Some(d),
                    _ => None,
                })
                .flatten()
                .collect()
        }

        fn last(&self) -> Command {
            self.commands().pop().expect("no messages")
        }
    }

    #[derive(Clone)]
    struct FailingSink;

    impl FrameSink for FailingSink {
        fn send_frame(&self, _: Vec<u8>) -> io::Result<()> {
            Err(io::Error::from(ErrorKind::BrokenPipe))
        }
    }

    fn request(program: &str, args: &[&str]) -> ExecRequest {
        ExecRequest {
            program: program.to_string(),
            args: args.iter().map(|s| Arg::Plain(s.to_string())).collect(),
            cwd: None,
        }
    }

    #[test]
    fn host_args_mapping() {
        let req = ExecRequest {
            program: "p".to_string(),
            args: vec![
                Arg::Plain("a".to_string()),
                Arg::HostPath(PathBuf::from("/h/x")),
                Arg::TempFile { name: None },
                Arg::TempFile {
                    name: Some("keep.txt".to_string()),
                },
            ],
            cwd: Some(PathBuf::from("/h")),
        };
        assert!(req.needs_temp_dir());
        assert_eq!(
            req.host_args(Some(Path::new("/t"))),
            vec![
                OsString::from("a"),
                OsString::from("/h/x"),
                Path::new("/t").join("arg-3").into_os_string(),
                Path::new("/t").join("keep.txt").into_os_string(),
            ]
        );
        let plain = ExecRequest {
            program: "p".to_string(),
            args: vec![Arg::Plain("a".to_string())],
            cwd: Some(PathBuf::from("/h")),
        };
        assert!(!plain.needs_temp_dir());
        assert_eq!(plain.host_args(None), vec![OsString::from("a")]);
        let no_cwd = ExecRequest {
            program: "p".to_string(),
            args: vec![],
            cwd: None,
        };
        assert!(no_cwd.needs_temp_dir());
    }

    /// Client input that has already finished: just the stdin EOF message,
    /// and the stream stays open (as if the client is waiting for output).
    fn idle_client() -> (std::io::PipeReader, PipeWriter) {
        let (reader, mut writer) = io::pipe().unwrap();
        client_host::send_stdin(&mut writer, b"").unwrap();
        (reader, writer)
    }

    #[test]
    fn emit_sends_one_frame() {
        let sink = VecSink::default();
        emit(&sink, |f| client_host::send_stdout(f, b"x")).unwrap();
        assert_eq!(sink.0.lock().unwrap().len(), 1);
        assert_eq!(sink.commands(), vec![Command::Stdout(b"x".to_vec())]);
    }

    #[test]
    fn fail_reports_message_and_code() {
        let sink = VecSink::default();
        assert_eq!(fail(&sink, "no way", EXIT_DENIED), EXIT_DENIED);
        assert_eq!(
            sink.commands(),
            vec![
                Command::Stderr(b"scoter-host: no way\n".to_vec()),
                Command::ExitCode(EXIT_DENIED),
            ]
        );
    }

    #[test]
    fn fail_ignores_sink_errors() {
        assert_eq!(fail(&FailingSink, "x", 3), 3);
    }

    #[test]
    fn start_failure() {
        let sink = VecSink::default();
        let (input, _w) = idle_client();
        assert_eq!(
            run(&request("/nonexistent/program", &[]), input, &sink),
            EXIT_START_FAILED
        );
        let stderr = String::from_utf8(sink.stderr()).unwrap();
        assert!(
            stderr.starts_with("scoter-host: cannot run /nonexistent/program"),
            "{stderr}"
        );
        assert_eq!(sink.last(), Command::ExitCode(EXIT_START_FAILED));
    }

    #[cfg(unix)]
    mod unix {
        use super::*;
        use std::thread::JoinHandle;

        fn run_in_background(
            req: ExecRequest,
            input: std::io::PipeReader,
        ) -> (VecSink, JoinHandle<i32>) {
            let sink = VecSink::default();
            let handle = {
                let sink = sink.clone();
                thread::spawn(move || run(&req, input, &sink))
            };
            (sink, handle)
        }

        #[test]
        fn echo_output_and_exit_code() {
            let sink = VecSink::default();
            let (input, _w) = idle_client();
            assert_eq!(run(&request("echo", &["hello", "world"]), input, &sink), 0);
            assert_eq!(sink.stdout(), b"hello world\n");
            assert_eq!(sink.last(), Command::ExitCode(0));
        }

        #[test]
        fn host_paths_are_passed_as_is() {
            let sink = VecSink::default();
            let (input, _w) = idle_client();
            let req = ExecRequest {
                program: "echo".to_string(),
                args: vec![
                    Arg::Plain("to".to_string()),
                    Arg::HostPath(PathBuf::from("/home/me/ws/out.txt")),
                ],
                cwd: None,
            };
            assert_eq!(run(&req, input, &sink), 0);
            assert_eq!(sink.stdout(), b"to /home/me/ws/out.txt\n");
        }

        /// Run `script` with temporary file arguments, printing `$1`, `$2`, ...
        fn run_with_temp_files(
            cwd: Option<PathBuf>,
            names: &[Option<&str>],
            script: &str,
        ) -> Vec<String> {
            let sink = VecSink::default();
            let (input, _w) = idle_client();
            let mut args = vec![
                Arg::Plain("-c".to_string()),
                Arg::Plain(script.to_string()),
                Arg::Plain("sh".to_string()),
            ];
            args.extend(names.iter().map(|n| Arg::TempFile {
                name: n.map(str::to_string),
            }));
            let req = ExecRequest {
                program: "sh".to_string(),
                args,
                cwd,
            };
            assert_eq!(
                run(&req, input, &sink),
                0,
                "{}",
                String::from_utf8_lossy(&sink.stderr())
            );
            String::from_utf8(sink.stdout())
                .unwrap()
                .lines()
                .map(str::to_string)
                .collect()
        }

        #[test]
        fn temp_files_are_generated_and_removed() {
            let out = run_with_temp_files(
                None,
                &[None, Some("out.json")],
                "echo x > \"$1\"; echo y > \"$2\"; echo \"$1\"; echo \"$2\"; pwd",
            );
            let (first, second, cwd) = (
                PathBuf::from(&out[0]),
                PathBuf::from(&out[1]),
                PathBuf::from(&out[2]),
            );
            // Generated names are numbered by argument position.
            assert_eq!(first.file_name().unwrap(), "arg-4");
            assert_eq!(second.file_name().unwrap(), "out.json");
            // Both live in the temporary working directory, which is gone afterwards.
            assert_eq!(first.parent().unwrap().canonicalize().ok(), None);
            assert_eq!(first.parent(), second.parent());
            assert!(cwd.ends_with(first.parent().unwrap().file_name().unwrap()));
            assert!(!first.exists() && !second.exists());
        }

        #[test]
        fn temp_files_with_a_requested_cwd() {
            let dir = TempDir::new().unwrap();
            let out = run_with_temp_files(
                Some(dir.path().to_path_buf()),
                &[None],
                "echo x > \"$1\"; echo \"$1\"; pwd",
            );
            let temp_file = PathBuf::from(&out[0]);
            // The temporary file isn't in the requested directory, which is left alone.
            assert_ne!(temp_file.parent().unwrap(), dir.path());
            assert_eq!(PathBuf::from(&out[1]), dir.path().canonicalize().unwrap());
            assert!(!temp_file.exists());
            assert!(dir.path().exists());
            assert_eq!(fs::read_dir(dir.path()).unwrap().count(), 0);
        }

        #[test]
        fn stdout_and_stderr_are_separate() {
            let sink = VecSink::default();
            let (input, _w) = idle_client();
            let code = run(
                &request("sh", &["-c", "echo out; echo err >&2; exit 3"]),
                input,
                &sink,
            );
            assert_eq!(code, 3);
            assert_eq!(sink.stdout(), b"out\n");
            assert_eq!(sink.stderr(), b"err\n");
            assert_eq!(sink.last(), Command::ExitCode(3));
        }

        #[test]
        fn large_output_is_chunked() {
            let sink = VecSink::default();
            let (input, _w) = idle_client();
            let code = run(
                &request("head", &["-c", "200000", "/dev/zero"]),
                input,
                &sink,
            );
            assert_eq!(code, 0);
            assert_eq!(sink.stdout(), vec![0u8; 200_000]);
            for cmd in sink.commands() {
                if let Command::Stdout(d) = cmd {
                    assert!(d.len() <= OUTPUT_CHUNK);
                }
            }
        }

        #[test]
        fn runs_in_private_temp_dir_that_is_removed() {
            let sink = VecSink::default();
            let (input, _w) = idle_client();
            assert_eq!(
                run(&request("sh", &["-c", "pwd; ls -ld ."]), input, &sink),
                0
            );
            let out = String::from_utf8(sink.stdout()).unwrap();
            let mut lines = out.lines();
            let dir = PathBuf::from(lines.next().unwrap());
            let canonical_tmp = std::env::temp_dir().canonicalize().unwrap();
            assert!(dir.starts_with(&canonical_tmp), "{dir:?}");
            assert!(lines.next().unwrap().starts_with("drwx------"), "{out}");
            assert!(!dir.exists(), "working directory should be removed");
        }

        #[test]
        fn runs_in_requested_directory_and_keeps_it() {
            let dir = TempDir::new().unwrap();
            fs::write(dir.path().join("marker"), b"").unwrap();
            let sink = VecSink::default();
            let (input, _w) = idle_client();
            let mut req = request("sh", &["-c", "pwd; ls"]);
            req.cwd = Some(dir.path().to_path_buf());
            assert_eq!(run(&req, input, &sink), 0);
            let out = String::from_utf8(sink.stdout()).unwrap();
            assert_eq!(
                out,
                format!("{}\nmarker\n", dir.path().canonicalize().unwrap().display())
            );
            assert!(
                dir.path().join("marker").exists(),
                "the requested directory must not be removed"
            );
        }

        #[test]
        fn missing_requested_directory_fails_to_start() {
            let sink = VecSink::default();
            let (input, _w) = idle_client();
            let mut req = request("true", &[]);
            req.cwd = Some(PathBuf::from("/nonexistent/scoter-cwd"));
            assert_eq!(run(&req, input, &sink), EXIT_START_FAILED);
            assert_eq!(sink.last(), Command::ExitCode(EXIT_START_FAILED));
        }

        #[test]
        fn uses_host_environment() {
            let sink = VecSink::default();
            let (input, _w) = idle_client();
            assert_eq!(
                run(&request("sh", &["-c", "printf %s \"$PATH\""]), input, &sink),
                0
            );
            assert_eq!(
                String::from_utf8(sink.stdout()).unwrap(),
                std::env::var("PATH").unwrap()
            );
        }

        #[test]
        fn stdin_is_passed_through_then_closed() {
            let (input, mut w) = io::pipe().unwrap();
            client_host::send_stdin(&mut w, b"abc").unwrap();
            client_host::send_stdin(&mut w, b"def\n").unwrap();
            client_host::send_stdin(&mut w, b"").unwrap();
            let sink = VecSink::default();
            assert_eq!(run(&request("cat", &[]), input, &sink), 0);
            assert_eq!(sink.stdout(), b"abcdef\n");
        }

        #[test]
        fn stdin_after_process_stopped_reading_is_harmless() {
            let (input, mut w) = io::pipe().unwrap();
            client_host::send_stdin(&mut w, b"ignored").unwrap();
            let sink = VecSink::default();
            assert_eq!(run(&request("true", &[]), input, &sink), 0);
            // More input for the finished process doesn't break anything.
            let _ = client_host::send_stdin(&mut w, b"more");
        }

        #[test]
        fn term_signal_is_delivered() {
            let (input, mut w) = io::pipe().unwrap();
            let (sink, handle) = run_in_background(request("sleep", &["30"]), input);
            client_host::send_signal(&mut w, client_host::SIGNAL_TERM).unwrap();
            assert_eq!(handle.join().unwrap(), 128 + libc::SIGTERM);
            assert_eq!(sink.last(), Command::ExitCode(128 + libc::SIGTERM));
        }

        #[test]
        fn int_signal_is_delivered() {
            let (input, mut w) = io::pipe().unwrap();
            let (_sink, handle) = run_in_background(request("sleep", &["30"]), input);
            client_host::send_signal(&mut w, client_host::SIGNAL_INT).unwrap();
            assert_eq!(handle.join().unwrap(), 128 + libc::SIGINT);
        }

        #[test]
        fn signal_reaches_process_not_reading_stdin() {
            // A full stdin pipe must not hold up signal delivery, as long as
            // the backlog isn't full: 8 x 32 KiB is more than a pipe holds.
            let (input, mut w) = io::pipe().unwrap();
            let (_sink, handle) = run_in_background(request("sleep", &["30"]), input);
            let writer = thread::spawn(move || {
                let chunk = vec![b'x'; 32 * 1024];
                for _ in 0..8 {
                    client_host::send_stdin(&mut w, &chunk).unwrap();
                }
                client_host::send_signal(&mut w, client_host::SIGNAL_TERM).unwrap();
                w
            });
            assert_eq!(handle.join().unwrap(), 128 + libc::SIGTERM);
            drop(writer.join().unwrap());
        }

        #[test]
        fn unknown_signal_is_ignored() {
            let (input, mut w) = io::pipe().unwrap();
            client_host::send_signal(&mut w, 999).unwrap();
            client_host::send_stdin(&mut w, b"still here").unwrap();
            client_host::send_stdin(&mut w, b"").unwrap();
            let sink = VecSink::default();
            assert_eq!(run(&request("cat", &[]), input, &sink), 0);
            assert_eq!(sink.stdout(), b"still here");
        }

        #[test]
        fn client_disconnect_kills_process() {
            let (input, w) = io::pipe().unwrap();
            let (_sink, handle) = run_in_background(request("sleep", &["30"]), input);
            drop(w);
            assert_eq!(handle.join().unwrap(), 128 + libc::SIGKILL);
        }

        #[test]
        fn protocol_error_kills_process() {
            let (input, mut w) = io::pipe().unwrap();
            let (_sink, handle) = run_in_background(request("sleep", &["30"]), input);
            // Stdout is never sent by a client.
            client_host::send_stdout(&mut w, b"bogus").unwrap();
            assert_eq!(handle.join().unwrap(), 128 + libc::SIGKILL);
        }

        #[test]
        fn delivery_mapping() {
            let expected = [
                (client_host::SIGNAL_HUP, libc::SIGHUP),
                (client_host::SIGNAL_INT, libc::SIGINT),
                (client_host::SIGNAL_QUIT, libc::SIGQUIT),
                (client_host::SIGNAL_USR1, libc::SIGUSR1),
                (client_host::SIGNAL_USR2, libc::SIGUSR2),
                (client_host::SIGNAL_TERM, libc::SIGTERM),
                (client_host::SIGNAL_WINCH, libc::SIGWINCH),
            ];
            for (wire, sig) in expected {
                assert_eq!(delivery_for(wire), Delivery::Signal(sig), "wire {wire}");
            }
            for wire in [0, 9, 17, 999, u32::MAX] {
                assert_eq!(delivery_for(wire), Delivery::Ignore, "wire {wire}");
            }
        }

        #[test]
        fn signal_after_exit_is_not_delivered() {
            let child = Process::new("true").spawn().unwrap();
            let running = Mutex::new(Running {
                child,
                status: None,
            });
            wait(&running).unwrap();
            // Would target a possibly reused pid if delivered.
            deliver_signal(&running, client_host::SIGNAL_TERM);
            kill(&running);
            assert!(lock(&running).status.unwrap().success());
        }

        #[test]
        fn exit_code_conversion() {
            let status = |script: &str| Process::new("sh").args(["-c", script]).status().unwrap();
            assert_eq!(exit_code(status("exit 0")), 0);
            assert_eq!(exit_code(status("exit 42")), 42);
            assert_eq!(exit_code(status("kill -TERM $$")), 128 + libc::SIGTERM);
            assert_eq!(exit_code(status("kill -KILL $$")), 128 + libc::SIGKILL);
        }

        #[test]
        fn temp_dir_is_private_and_removed() {
            use std::os::unix::fs::PermissionsExt;
            let a = TempDir::new().unwrap();
            let b = TempDir::new().unwrap();
            assert_ne!(a.path(), b.path());
            let mode = fs::metadata(a.path()).unwrap().permissions().mode();
            assert_eq!(mode & 0o777, 0o700);
            fs::write(a.path().join("file"), b"x").unwrap();
            let path = a.path().to_path_buf();
            drop(a);
            assert!(!path.exists());
        }
    }
}
