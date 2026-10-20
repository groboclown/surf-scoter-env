//! The host side: launches the broker inside the container, then runs the
//! processes that clients request, after `analyze` approves them.
//!
//! The broker's messages are routed by process id.  Each process gets its own
//! thread, which reads the client's invocation from the reassembled byte
//! stream, decides what to do with it, and runs it.

pub(crate) mod analyze;
pub(crate) mod container;
pub(crate) mod exec;
pub(crate) mod workspace;

use std::collections::HashMap;
use std::ffi::OsString;
use std::io::{self, BufRead, BufReader, BufWriter, ErrorKind, Read, Write};
use std::path::PathBuf;
use std::sync::mpsc::{self, Receiver, Sender};
use std::sync::{Arc, Mutex, MutexGuard};
use std::thread::{self, JoinHandle};

use surf_scoter_env_launcher::comm::broker_host::{self, ProcessId};
use surf_scoter_env_launcher::comm::client_host::Invocation;

use scoter_host::config;
use scoter_host::config::project::Commands;
use scoter_host::config::system::{DEV_CONTAINER_WORKSPACE_DIR, SCOTER_SECTION};

use analyze::Decision;
use exec::FrameSink;
use workspace::Workspace;

const EXIT_OK: i32 = 0;
const EXIT_FAILURE: i32 = 1;
const EXIT_USAGE: i32 = 2;

const USAGE: &str = "Usage: scoter-host --universal UNIVERSAL.json CONTAINER_NAME";

/// Run the host with the command line arguments (without the program name).
/// Returns the process exit code.
pub(crate) fn run<I: IntoIterator<Item = OsString>>(args: I) -> i32 {
    let cli = match parse_args(args) {
        Ok(Some(cli)) => cli,
        Ok(None) => {
            println!("{USAGE}");
            println!("Runs the broker in the container, and the commands its clients request.");
            println!(
                "--universal gives the universal configuration file from gen-scoter-uc, with the project file and the dev container's workspace directory."
            );
            return EXIT_OK;
        }
        Err(e) => {
            eprintln!("scoter-host: {e}");
            eprintln!("{USAGE}");
            return EXIT_USAGE;
        }
    };
    // Only the universal file is read, so this runs with the configuration
    // its maker saw, even if the files it came from change.
    let config = match config::load_universal(&cli.universal) {
        Ok(c) => c,
        Err(errors) => {
            for e in &errors.0 {
                eprintln!("scoter-host: {e}");
            }
            return EXIT_FAILURE;
        }
    };
    let universal = cli.universal.display();
    // Both are there when there's a project file.
    let (Some(mut project), Some(dev_container)) = (config.project, config.dev_container) else {
        eprintln!(
            "scoter-host: {universal}: has no project configuration file; make it with `gen-scoter-uc --config`"
        );
        return EXIT_FAILURE;
    };
    // `@NAME@` values come from this program's environment.
    if let Err(errors) = config::expand_host_environments(&mut project, &config.system) {
        for e in &errors.0 {
            eprintln!("scoter-host: {e}");
        }
        return EXIT_FAILURE;
    }
    let Some(workspace_dir) = config
        .system
        .get_path(SCOTER_SECTION, DEV_CONTAINER_WORKSPACE_DIR)
    else {
        eprintln!(
            "scoter-host: {universal}: has no {DEV_CONTAINER_WORKSPACE_DIR}; make it with `gen-scoter-uc --{DEV_CONTAINER_WORKSPACE_DIR}`"
        );
        return EXIT_FAILURE;
    };
    let workspace = match Workspace::new(&workspace_dir, &dev_container.env.mounts.workspace) {
        Ok(w) => w,
        Err(e) => {
            eprintln!("scoter-host: {universal}: the workspace: {e}");
            return EXIT_FAILURE;
        }
    };
    let broker = container::broker_argv(
        &config.system,
        &cli.container,
        &dev_container.env.broker_bin,
    )
    .and_then(|argv| container::launch(&argv).map_err(|e| e.to_string()));
    let broker = match broker {
        Ok(b) => b,
        Err(e) => {
            eprintln!(
                "scoter-host: cannot start the broker in container '{}': {e}",
                cli.container
            );
            return EXIT_FAILURE;
        }
    };

    eprintln!(
        "scoter-host: workspace {} is mounted at {} in the container",
        workspace.host.display(),
        workspace.container,
    );
    let settings = Arc::new(Settings {
        commands: project.commands,
        workspace,
    });

    let container::Broker {
        mut child,
        stdin,
        stdout,
    } = broker;
    // Returns once the broker closes its stdout; the broker's stdin is closed on the way out.
    let result = serve(stdout, stdin, settings);
    let status = child.wait();
    match (result, status) {
        (Err(e), _) => {
            eprintln!("scoter-host: lost the connection to the broker: {e}");
            EXIT_FAILURE
        }
        (Ok(()), Ok(status)) if status.success() => EXIT_OK,
        (Ok(()), Ok(status)) => {
            eprintln!("scoter-host: the broker exited with {status}");
            EXIT_FAILURE
        }
        (Ok(()), Err(e)) => {
            eprintln!("scoter-host: cannot get the broker's exit status: {e}");
            EXIT_FAILURE
        }
    }
}

#[derive(Debug, PartialEq, Eq)]
struct Cli {
    /// The universal configuration file.
    universal: PathBuf,
    container: String,
}

/// Parse the arguments.  `Ok(None)` means help was requested.
/// Options take a value as `--name VALUE` or `--name=VALUE`.
fn parse_args<I: IntoIterator<Item = OsString>>(args: I) -> Result<Option<Cli>, String> {
    let mut universal: Option<OsString> = None;
    let mut container: Option<String> = None;
    let mut args = args.into_iter();
    while let Some(arg) = args.next() {
        match arg.to_str() {
            Some("-h" | "--help") => return Ok(None),
            Some(s) if s.starts_with('-') => {
                let (name, inline) = match s.split_once('=') {
                    Some((name, value)) => (name, Some(OsString::from(value))),
                    None => (s, None),
                };
                let slot = match name {
                    "--universal" => &mut universal,
                    _ => return Err(format!("unknown option '{s}'")),
                };
                let value = match inline {
                    Some(value) => value,
                    None => args.next().ok_or_else(|| format!("{name} needs a value"))?,
                };
                if value.is_empty() {
                    return Err(format!("{name} needs a value"));
                }
                if slot.is_some() {
                    return Err(format!("{name} given more than once"));
                }
                *slot = Some(value);
            }
            _ if container.is_some() => {
                return Err(format!("unexpected argument '{}'", arg.to_string_lossy()));
            }
            _ => {
                let name = arg
                    .into_string()
                    .map_err(|a| format!("invalid container name '{}'", a.to_string_lossy()))?;
                container = Some(name);
            }
        }
    }
    Ok(Some(Cli {
        universal: PathBuf::from(universal.ok_or("--universal is required")?),
        container: container.ok_or("a container name is required")?,
    }))
}

/// What client requests are checked against.
struct Settings {
    commands: Commands,
    workspace: Workspace,
}

/// Client input for each running process.
type Inputs = Arc<Mutex<HashMap<ProcessId, Sender<Vec<u8>>>>>;

fn lock<T>(m: &Mutex<T>) -> MutexGuard<'_, T> {
    m.lock().unwrap_or_else(|e| e.into_inner())
}

/// Talk to the broker until it closes `from_broker`.  Running processes are
/// then killed and finished, and `to_broker` is closed.
fn serve<R, W>(from_broker: R, to_broker: W, settings: Arc<Settings>) -> io::Result<()>
where
    R: Read,
    W: Write + Send + 'static,
{
    let (tx, rx) = mpsc::channel();
    let writer = thread::spawn(move || broker_host::send_all(&rx, &mut BufWriter::new(to_broker)));

    let inputs: Inputs = Arc::default();
    let mut sessions = Vec::new();
    let result = route(
        BufReader::new(from_broker),
        &tx,
        &inputs,
        &settings,
        &mut sessions,
    );

    // Closing every client's input kills whatever is still running.
    lock(&inputs).clear();
    for session in sessions {
        let _ = session.join();
    }
    drop(tx);
    let written = writer
        .join()
        .unwrap_or_else(|_| Err(io::Error::other("broker writer panicked")));
    result.and(written)
}

/// Dispatch the broker's messages until it closes the stream.
fn route<R: BufRead>(
    mut r: R,
    tx: &Sender<broker_host::Command>,
    inputs: &Inputs,
    settings: &Arc<Settings>,
    sessions: &mut Vec<JoinHandle<()>>,
) -> io::Result<()> {
    loop {
        // Only a close between messages is clean; anywhere else, `recv` reports it.
        match r.fill_buf() {
            Ok([]) => return Ok(()),
            Ok(_) => {}
            Err(e) if e.kind() == ErrorKind::Interrupted => continue,
            Err(e) => return Err(e),
        }
        match broker_host::Command::recv(&mut r)? {
            broker_host::Command::Started(proc) => {
                let (input_tx, input_rx) = mpsc::channel();
                lock(inputs).insert(proc, input_tx);
                let outbox = Outbox {
                    proc,
                    tx: tx.clone(),
                };
                let inputs = inputs.clone();
                let settings = settings.clone();
                sessions.retain(|s| !s.is_finished());
                sessions.push(thread::spawn(move || {
                    let code =
                        handle_process(proc, ChannelReader::new(input_rx), &outbox, &settings);
                    lock(&inputs).remove(&proc);
                    outbox.close(code);
                }));
            }
            broker_host::Command::Message((proc, data)) => {
                if let Some(input) = lock(inputs).get(&proc) {
                    // A failure means the process is finishing up.
                    let _ = input.send(data);
                }
            }
            broker_host::Command::Terminated((proc, _)) => {
                // The client is gone; closing its input kills the process.
                lock(inputs).remove(&proc);
            }
        }
    }
}

/// Read the client's request and act on it.  Returns the exit code.
fn handle_process<R: Read + Send + 'static, S: FrameSink>(
    proc: ProcessId,
    mut input: R,
    sink: &S,
    settings: &Settings,
) -> i32 {
    let invocation = match Invocation::read(&mut input) {
        Ok(inv) => inv,
        Err(e) if e.kind() == ErrorKind::UnexpectedEof => {
            // The client left before finishing its request; there's no one to tell.
            eprintln!("scoter-host: process {proc}: client disconnected before sending a request");
            return EXIT_FAILURE;
        }
        Err(e) => {
            eprintln!("scoter-host: process {proc}: malformed request: {e}");
            return exec::fail(sink, &format!("malformed request: {e}"), EXIT_FAILURE);
        }
    };
    match analyze::analyze(&invocation, &settings.commands, &settings.workspace) {
        Decision::Deny(reason) => {
            eprintln!(
                "scoter-host: process {proc}: denied {:?}: {reason}",
                invocation.args
            );
            exec::fail(sink, &reason, exec::EXIT_DENIED)
        }
        Decision::Allow(request) => {
            match &request.cwd {
                Some(dir) => eprintln!(
                    "scoter-host: process {proc}: running {} {:?} in {}",
                    request.program,
                    request.args,
                    dir.display(),
                ),
                None => eprintln!(
                    "scoter-host: process {proc}: running {} {:?}",
                    request.program, request.args
                ),
            }
            exec::run(&request, input, sink)
        }
    }
}

/// Sends a process's client-bound messages to the broker.
#[derive(Clone)]
struct Outbox {
    proc: ProcessId,
    tx: Sender<broker_host::Command>,
}

impl Outbox {
    /// Tell the broker the process is done, so it closes the client connection.
    fn close(&self, exit_code: i32) {
        let _ = self
            .tx
            .send(broker_host::Command::Terminated((self.proc, exit_code)));
    }
}

impl FrameSink for Outbox {
    fn send_frame(&self, frame: Vec<u8>) -> io::Result<()> {
        self.tx
            .send(broker_host::Command::Message((self.proc, frame)))
            .map_err(|_| io::Error::new(ErrorKind::BrokenPipe, "the broker connection is closed"))
    }
}

/// Reads the byte chunks sent on a channel as one stream; it ends when the
/// channel closes.
struct ChannelReader {
    rx: Receiver<Vec<u8>>,
    chunk: Vec<u8>,
    pos: usize,
}

impl ChannelReader {
    fn new(rx: Receiver<Vec<u8>>) -> Self {
        ChannelReader {
            rx,
            chunk: Vec::new(),
            pos: 0,
        }
    }
}

impl Read for ChannelReader {
    fn read(&mut self, buf: &mut [u8]) -> io::Result<usize> {
        if buf.is_empty() {
            return Ok(0);
        }
        while self.pos >= self.chunk.len() {
            match self.rx.recv() {
                Ok(chunk) => {
                    self.chunk = chunk;
                    self.pos = 0;
                }
                Err(_) => return Ok(0),
            }
        }
        let n = buf.len().min(self.chunk.len() - self.pos);
        buf[..n].copy_from_slice(&self.chunk[self.pos..self.pos + n]);
        self.pos += n;
        Ok(n)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use scoter_host::config::LoadOptions;
    use std::io::Cursor;
    use std::path::Path;
    use surf_scoter_env_launcher::comm::client_host::{self, Command};

    fn os_args(args: &[&str]) -> Vec<OsString> {
        args.iter().map(OsString::from).collect()
    }

    fn cli(universal: &str, container: &str) -> Option<Cli> {
        Some(Cli {
            universal: PathBuf::from(universal),
            container: container.to_string(),
        })
    }

    // ---- parse_args ----

    #[test]
    fn args_valid() {
        assert_eq!(
            parse_args(os_args(&["--universal", "u.json", "dev"])),
            Ok(cli("u.json", "dev"))
        );
        assert_eq!(
            parse_args(os_args(&["dev", "--universal=/tmp/u.json"])),
            Ok(cli("/tmp/u.json", "dev")),
        );
        // Only the first '=' separates the value.
        assert_eq!(
            parse_args(os_args(&["--universal=a=b.json", "dev"])),
            Ok(cli("a=b.json", "dev")),
        );
    }

    #[test]
    fn args_help() {
        assert_eq!(parse_args(os_args(&["-h"])), Ok(None));
        assert_eq!(
            parse_args(os_args(&["--universal", "x", "--help"])),
            Ok(None)
        );
    }

    #[test]
    fn args_errors() {
        let err = |args: &[&str]| parse_args(os_args(args)).unwrap_err();
        assert_eq!(err(&[]), "--universal is required");
        assert_eq!(err(&["dev"]), "--universal is required");
        assert_eq!(
            err(&["--universal", "u.json"]),
            "a container name is required"
        );
        assert_eq!(err(&["dev", "--universal"]), "--universal needs a value");
        assert_eq!(err(&["--universal=", "dev"]), "--universal needs a value");
        assert_eq!(
            err(&["--universal", "a", "--universal", "b", "dev"]),
            "--universal given more than once"
        );
        assert_eq!(
            err(&["--universal", "u.json", "dev", "other"]),
            "unexpected argument 'other'"
        );
        // The configuration only comes through the universal file now.
        for old in [
            "--config",
            "--user-setup",
            "--distribution-dir",
            "--workspace",
        ] {
            assert_eq!(err(&[old, "x", "dev"]), format!("unknown option '{old}'"));
        }
        assert_eq!(err(&["--verbose=1", "dev"]), "unknown option '--verbose=1'");
    }

    #[cfg(unix)]
    #[test]
    fn args_non_utf8() {
        use std::os::unix::ffi::OsStringExt;
        let bad = OsString::from_vec(vec![b'd', 0xff]);
        let err = parse_args(vec!["--universal".into(), "u.json".into(), bad.clone()]).unwrap_err();
        assert!(err.starts_with("invalid container name"), "{err}");
        // The universal file's path doesn't need to be UTF-8.
        let args = vec!["--universal".into(), bad.clone(), "dev".into()];
        assert_eq!(
            parse_args(args).unwrap().unwrap().universal,
            PathBuf::from(bad)
        );
    }

    // ---- run ----

    /// A project with a dev container, and a system setup using `/bin/sh`
    /// as the container tool, in `dir`.
    struct TestProject {
        project: PathBuf,
        setup: PathBuf,
    }

    impl TestProject {
        fn new(dir: &Path) -> Self {
            let write = |name: &str, text: &str| {
                let path = dir.join(name);
                std::fs::create_dir_all(path.parent().unwrap()).unwrap();
                std::fs::write(&path, text).unwrap();
                path
            };
            // The user's setup file must be outside the project, and its workspace.
            write("project/.surf-scoter.d/env/Dockerfile", "FROM scratch\n");
            write(
                "project/.surf-scoter.d/env/dev-env.yaml",
                "user: u\nmounts: {workspace: /w, setup: /s}\nbroker-bin: /b\nsshd-port: 22\n",
            );
            TestProject {
                project: write(
                    "project/.surf-scoter.d/project.yaml",
                    "container:\n  dev-container-path: env\n",
                ),
                setup: write(
                    "home/setup.yaml",
                    "docker:\n  programs:\n    exec: /bin/sh\nscoter:\n  values:\n    project-files-dir: .surf-scoter.d\n",
                ),
            }
        }

        /// The universal configuration file, as `gen-scoter-uc` would write it.
        fn universal(&self, with_project: bool, workspace: Option<&Path>) -> PathBuf {
            let options = LoadOptions {
                project: with_project.then(|| self.project.clone()),
                dev_container_workspace_dir: workspace.map(Path::to_path_buf),
                ..LoadOptions::default()
            };
            // The setup file as the user's, and nothing else.
            let env = config::system::Env {
                scoter_home: self.setup.parent().map(Path::to_path_buf),
                ..config::system::Env::default()
            };
            let config = config::load_all_with_env(&options, env).unwrap();
            let json =
                scoter_host::universal::to_json(&scoter_host::universal::build(&config).unwrap());
            let path = self.project.with_file_name(format!(
                "universal-{with_project}-{}.json",
                workspace.is_some()
            ));
            std::fs::write(&path, json).unwrap();
            path
        }
    }

    fn universal_args(universal: &Path, container: &str) -> Vec<OsString> {
        vec!["--universal".into(), universal.into(), container.into()]
    }

    #[test]
    fn run_usage_and_missing_file() {
        assert_eq!(run(os_args(&["--help"])), EXIT_OK);
        assert_eq!(run(os_args(&["dev"])), EXIT_USAGE);
        assert_eq!(
            run(os_args(&["--universal", "/nonexistent/u.json", "dev"])),
            EXIT_FAILURE
        );
    }

    #[test]
    fn run_needs_the_project_and_workspace() {
        let dir = exec::TempDir::new().unwrap();
        let test = TestProject::new(dir.path());
        let ws = dir.path().join("ws");
        std::fs::create_dir(&ws).unwrap();
        assert_eq!(
            run(universal_args(&test.universal(false, Some(&ws)), "dev")),
            EXIT_FAILURE
        );
        assert_eq!(
            run(universal_args(&test.universal(true, None), "dev")),
            EXIT_FAILURE
        );
        // The workspace directory must exist.
        assert_eq!(
            run(universal_args(
                &test.universal(true, Some(&dir.path().join("missing"))),
                "dev"
            )),
            EXIT_FAILURE
        );
    }

    #[test]
    fn run_rejects_bad_container_name() {
        let dir = exec::TempDir::new().unwrap();
        let test = TestProject::new(dir.path());
        let ws = dir.path().join("ws");
        std::fs::create_dir(&ws).unwrap();
        let universal = test.universal(true, Some(&ws));
        assert_eq!(
            run(universal_args(&universal, "--privileged=x")),
            EXIT_USAGE
        );
        assert_eq!(run(universal_args(&universal, "bad name")), EXIT_FAILURE);
    }

    #[test]
    fn run_reports_a_bad_universal_file() {
        let dir = exec::TempDir::new().unwrap();
        let bad = dir.path().join("bad.json");
        std::fs::write(&bad, "{\"resolved\": {}}").unwrap();
        assert_eq!(run(universal_args(&bad, "dev")), EXIT_FAILURE);
    }

    // ---- ChannelReader ----

    #[test]
    fn channel_reader_joins_chunks() {
        let (tx, rx) = mpsc::channel();
        tx.send(b"ab".to_vec()).unwrap();
        tx.send(Vec::new()).unwrap();
        tx.send(b"cde".to_vec()).unwrap();
        drop(tx);
        let mut out = Vec::new();
        ChannelReader::new(rx).read_to_end(&mut out).unwrap();
        assert_eq!(out, b"abcde");
    }

    #[test]
    fn channel_reader_small_buffer() {
        let (tx, rx) = mpsc::channel();
        tx.send(b"hello".to_vec()).unwrap();
        let mut reader = ChannelReader::new(rx);
        let mut buf = [0u8; 2];
        assert_eq!(reader.read(&mut buf).unwrap(), 2);
        assert_eq!(&buf, b"he");
        assert_eq!(reader.read(&mut buf).unwrap(), 2);
        assert_eq!(&buf, b"ll");
        assert_eq!(reader.read(&mut buf).unwrap(), 1);
        assert_eq!(buf[0], b'o');
        assert_eq!(reader.read(&mut []).unwrap(), 0);
        drop(tx);
        assert_eq!(reader.read(&mut buf).unwrap(), 0);
    }

    // ---- Outbox ----

    #[test]
    fn outbox_wraps_frames_with_process_id() {
        let (tx, rx) = mpsc::channel();
        let outbox = Outbox { proc: 7, tx };
        outbox.send_frame(b"frame".to_vec()).unwrap();
        outbox.close(3);
        assert_eq!(
            rx.try_iter().collect::<Vec<_>>(),
            vec![
                broker_host::Command::Message((7, b"frame".to_vec())),
                broker_host::Command::Terminated((7, 3)),
            ]
        );
    }

    #[test]
    fn outbox_reports_closed_broker() {
        let (tx, rx) = mpsc::channel();
        drop(rx);
        let err = Outbox { proc: 0, tx }.send_frame(Vec::new()).unwrap_err();
        assert_eq!(err.kind(), ErrorKind::BrokenPipe);
    }

    // ---- handle_process ----

    /// Allows `echo` with one or two arguments.
    fn test_settings() -> Settings {
        let commands = config::project::parse(
            concat!(
                "container:\n  dev-container: dev-env\n",
                "commands:\n",
                "  - {command: echo, args: [{any: {}}]}\n",
                "  - {command: echo, args: [{any: {}}, {any: {}}]}\n",
            ),
            std::path::Path::new("/project/scoter.yaml"),
        )
        .unwrap()
        .commands;
        let workspace = Workspace {
            host: std::env::temp_dir(),
            container: "/workspace".to_string(),
        };
        Settings {
            commands,
            workspace,
        }
    }

    fn invocation(args: &[&str]) -> Invocation {
        Invocation {
            cwd: "/workspace".to_string(),
            args: args.iter().map(|s| s.to_string()).collect(),
            envs: vec![],
        }
    }

    /// The client's whole input: the invocation, then stdin EOF.
    fn request_bytes(args: &[&str]) -> Vec<u8> {
        let mut buf = Vec::new();
        invocation(args).write(&mut buf).unwrap();
        client_host::send_stdin(&mut buf, b"").unwrap();
        buf
    }

    /// Decode the client-bound frames sent through an outbox.
    fn client_commands(rx: &Receiver<broker_host::Command>) -> Vec<Command> {
        let mut bytes = Vec::new();
        for cmd in rx.try_iter() {
            match cmd {
                broker_host::Command::Message((_, data)) => bytes.extend(data),
                other => panic!("unexpected {other:?}"),
            }
        }
        let mut cur = Cursor::new(&bytes);
        let mut cmds = Vec::new();
        while (cur.position() as usize) < bytes.len() {
            cmds.push(Command::read(&mut cur).unwrap());
        }
        cmds
    }

    #[test]
    fn handle_denied_request() {
        let (tx, rx) = mpsc::channel();
        let code = handle_process(
            0,
            Cursor::new(request_bytes(&["docker", "ps"])),
            &Outbox { proc: 0, tx },
            &test_settings(),
        );
        assert_eq!(code, exec::EXIT_DENIED);
        assert_eq!(
            client_commands(&rx),
            vec![
                Command::Stderr(
                    b"scoter-host: 'docker' is not allowed to run on the host\n".to_vec()
                ),
                Command::ExitCode(exec::EXIT_DENIED),
            ]
        );
    }

    #[test]
    fn handle_denied_arguments() {
        let (tx, rx) = mpsc::channel();
        let code = handle_process(
            0,
            Cursor::new(request_bytes(&["echo", "a", "b", "c"])),
            &Outbox { proc: 0, tx },
            &test_settings(),
        );
        assert_eq!(code, exec::EXIT_DENIED);
        assert_eq!(
            client_commands(&rx),
            vec![
                Command::Stderr(
                    b"scoter-host: 'echo' is not allowed with these arguments\n".to_vec()
                ),
                Command::ExitCode(exec::EXIT_DENIED),
            ]
        );
    }

    #[test]
    fn handle_client_gone_before_request() {
        let (tx, rx) = mpsc::channel();
        let mut partial = request_bytes(&["echo"]);
        partial.truncate(3);
        assert_eq!(
            handle_process(
                0,
                Cursor::new(partial),
                &Outbox { proc: 0, tx },
                &test_settings()
            ),
            EXIT_FAILURE
        );
        assert!(client_commands(&rx).is_empty());
    }

    #[test]
    fn handle_malformed_request() {
        let (tx, rx) = mpsc::channel();
        // A cwd that isn't UTF-8.
        let bytes = vec![0, 0, 0, 1, 0xff];
        assert_eq!(
            handle_process(
                0,
                Cursor::new(bytes),
                &Outbox { proc: 0, tx },
                &test_settings()
            ),
            EXIT_FAILURE
        );
        let cmds = client_commands(&rx);
        assert!(
            matches!(&cmds[0], Command::Stderr(m) if m.starts_with(b"scoter-host: malformed request"))
        );
        assert_eq!(cmds[1], Command::ExitCode(EXIT_FAILURE));
    }

    #[cfg(unix)]
    #[test]
    fn handle_allowed_request() {
        let (tx, rx) = mpsc::channel();
        // The client's input stays open while it waits for the exit code;
        // closing it would mean the client left, which kills the process.
        let (input, mut client) = io::pipe().unwrap();
        client
            .write_all(&request_bytes(&["/usr/local/bin/echo", "hi", "there"]))
            .unwrap();
        let code = handle_process(0, input, &Outbox { proc: 0, tx }, &test_settings());
        drop(client);
        assert_eq!(code, 0);
        assert_eq!(
            client_commands(&rx),
            vec![
                Command::Stdout(b"hi there\n".to_vec()),
                Command::ExitCode(0),
            ]
        );
    }

    // ---- serve ----

    /// A fake broker: the host reads `to_host` and writes `from_host`.
    struct FakeBroker {
        to_host: Option<io::PipeWriter>,
        from_host: io::PipeReader,
        serving: JoinHandle<io::Result<()>>,
    }

    impl FakeBroker {
        fn start() -> Self {
            let (host_in, to_host) = io::pipe().unwrap();
            let (from_host, host_out) = io::pipe().unwrap();
            let serving =
                thread::spawn(move || serve(host_in, host_out, Arc::new(test_settings())));
            FakeBroker {
                to_host: Some(to_host),
                from_host,
                serving,
            }
        }

        fn send(&mut self, cmd: broker_host::Command) {
            cmd.send(self.to_host.as_mut().unwrap()).unwrap();
        }

        /// Send `bytes` for `proc` one byte per message, to exercise reassembly.
        #[cfg_attr(not(unix), allow(dead_code))]
        fn send_bytes_split(&mut self, proc: ProcessId, bytes: &[u8]) {
            for b in bytes {
                self.send(broker_host::Command::Message((proc, vec![*b])));
            }
        }

        /// Read until `proc` is closed; returns the client-bound messages and the exit code.
        fn finish(&mut self, proc: ProcessId) -> (Vec<Command>, i32) {
            let mut bytes = Vec::new();
            loop {
                match broker_host::Command::recv(&mut self.from_host).unwrap() {
                    broker_host::Command::Message((p, data)) if p == proc => bytes.extend(data),
                    broker_host::Command::Terminated((p, code)) if p == proc => {
                        let mut cur = Cursor::new(&bytes);
                        let mut cmds = Vec::new();
                        while (cur.position() as usize) < bytes.len() {
                            cmds.push(Command::read(&mut cur).unwrap());
                        }
                        return (cmds, code);
                    }
                    other => panic!("unexpected {other:?}"),
                }
            }
        }

        /// Close the host's input; returns `serve`'s result and whatever the
        /// host sent before closing its side.
        fn close_with_rest(mut self) -> (io::Result<()>, Vec<broker_host::Command>) {
            self.to_host = None;
            let result = self.serving.join().unwrap();
            let mut rest = Vec::new();
            self.from_host.read_to_end(&mut rest).unwrap();
            let mut cur = Cursor::new(&rest);
            let mut cmds = Vec::new();
            while (cur.position() as usize) < rest.len() {
                cmds.push(broker_host::Command::recv(&mut cur).unwrap());
            }
            (result, cmds)
        }

        /// Close the host's input, expecting nothing more from the host.
        fn close(self) -> io::Result<()> {
            let (result, rest) = self.close_with_rest();
            assert!(rest.is_empty(), "{rest:?}");
            result
        }
    }

    #[cfg(unix)]
    #[test]
    fn serve_runs_allowed_request() {
        let mut broker = FakeBroker::start();
        broker.send(broker_host::Command::Started(0));
        broker.send_bytes_split(0, &request_bytes(&["echo", "a", "b"]));
        assert_eq!(
            broker.finish(0),
            (
                vec![Command::Stdout(b"a b\n".to_vec()), Command::ExitCode(0)],
                0
            )
        );
        broker.close().unwrap();
    }

    #[test]
    fn serve_denies_request() {
        let mut broker = FakeBroker::start();
        broker.send(broker_host::Command::Started(4));
        broker.send(broker_host::Command::Message((
            4,
            request_bytes(&["rm", "-rf", "/"]),
        )));
        let (cmds, code) = broker.finish(4);
        assert_eq!(code, exec::EXIT_DENIED);
        assert_eq!(cmds.last(), Some(&Command::ExitCode(exec::EXIT_DENIED)));
        broker.close().unwrap();
    }

    #[cfg(unix)]
    #[test]
    fn serve_handles_interleaved_processes() {
        let mut broker = FakeBroker::start();
        broker.send(broker_host::Command::Started(0));
        broker.send(broker_host::Command::Started(1));
        let a = request_bytes(&["echo", "first"]);
        let b = request_bytes(&["nope"]);
        for i in 0..a.len().max(b.len()) {
            if i < a.len() {
                broker.send(broker_host::Command::Message((0, vec![a[i]])));
            }
            if i < b.len() {
                broker.send(broker_host::Command::Message((1, vec![b[i]])));
            }
        }
        // Read everything, then sort it out per process.
        let mut per_proc: HashMap<ProcessId, (Vec<u8>, Option<i32>)> = HashMap::new();
        while per_proc.values().filter(|(_, code)| code.is_some()).count() < 2 {
            match broker_host::Command::recv(&mut broker.from_host).unwrap() {
                broker_host::Command::Message((p, data)) => {
                    per_proc.entry(p).or_default().0.extend(data)
                }
                broker_host::Command::Terminated((p, code)) => {
                    per_proc.entry(p).or_default().1 = Some(code)
                }
                other => panic!("unexpected {other:?}"),
            }
        }
        assert_eq!(per_proc[&0].1, Some(0));
        assert_eq!(per_proc[&1].1, Some(exec::EXIT_DENIED));
        let mut first = Vec::new();
        client_host::send_stdout(&mut first, b"first\n").unwrap();
        assert!(per_proc[&0].0.starts_with(&first));
        broker.close().unwrap();
    }

    #[test]
    fn serve_ignores_unknown_processes() {
        let mut broker = FakeBroker::start();
        broker.send(broker_host::Command::Message((99, b"x".to_vec())));
        broker.send(broker_host::Command::Terminated((99, 0)));
        broker.close().unwrap();
    }

    #[test]
    fn serve_client_gone_before_request() {
        let mut broker = FakeBroker::start();
        broker.send(broker_host::Command::Started(0));
        broker.send(broker_host::Command::Message((0, vec![0, 0])));
        broker.send(broker_host::Command::Terminated((0, 0)));
        // The host still closes the process, though no one is listening.
        assert_eq!(broker.finish(0), (vec![], EXIT_FAILURE));
        broker.close().unwrap();
    }

    #[test]
    fn serve_broker_closes_mid_request() {
        let mut broker = FakeBroker::start();
        broker.send(broker_host::Command::Started(0));
        broker.send(broker_host::Command::Message((0, vec![0, 0])));
        // Closing finishes the waiting session before serve returns.
        let (result, rest) = broker.close_with_rest();
        result.unwrap();
        assert_eq!(
            rest,
            vec![broker_host::Command::Terminated((0, EXIT_FAILURE))]
        );
    }

    #[test]
    fn serve_rejects_malformed_broker_stream() {
        let mut broker = FakeBroker::start();
        broker.to_host.as_mut().unwrap().write_all(&[42]).unwrap();
        let err = broker.serving.join().unwrap().unwrap_err();
        assert_eq!(err.kind(), ErrorKind::InvalidData);
    }

    #[test]
    fn serve_rejects_truncated_broker_stream() {
        let mut broker = FakeBroker::start();
        broker
            .to_host
            .as_mut()
            .unwrap()
            .write_all(&[0, 0, 0])
            .unwrap();
        let err = broker.close().unwrap_err();
        assert_eq!(err.kind(), ErrorKind::UnexpectedEof);
    }
}
