//! Decides whether a client's request may run on the host, and how.
//!
//! The host bypass is for actions the container can't perform itself because
//! they need the host's privileges.  Since those actions run on the host,
//! every file they're given is restricted to the project clone (or a throwaway
//! temporary directory): paths are mapped from the container's project clone
//! mount to the host's project clone directory and resolved, following symlinks,
//! before they're checked.  Commands the container or the user's host can run
//! on their own don't belong in the configuration.
//!
//! The request is allowed by the first configured entry for the command whose
//! argument pattern it exactly matches, and whose `cwd` rule (if it has one)
//! allows the client's working directory (see `config` for the format).
//! Anything else is refused.
//!
//! For an entry with a `cwd` rule, the process runs in the resolved host
//! directory matching the client's working directory.  Otherwise it runs in a
//! temporary directory.  `path` arguments are passed as resolved host paths
//! (or temporary files).  The process's environment comes from the matching
//! entry's `env_plan`: only the container variables it allows come through,
//! and a container variable matching one of its `error` patterns refuses the
//! request (see `config::environment`).

use std::cell::OnceCell;
use std::fs;
use std::io::ErrorKind;
use std::path::{Component, Path, PathBuf};

use surf_scoter_env_launcher::comm::client_host::Invocation;

use scoter_host::config::project::{Commands, MatchOptions, PathOptions, Token, Within};

use super::exec::{Arg, ExecRequest};
use super::project_clone::ProjectClone;

#[derive(Debug, PartialEq, Eq)]
pub(crate) enum Decision {
    Allow(ExecRequest),
    /// The reason, shown to the client.
    Deny(String),
}

/// `workspace` (the project clone) is how container paths map to host paths.
pub(crate) fn analyze(
    invocation: &Invocation,
    commands: &Commands,
    workspace: &ProjectClone,
) -> Decision {
    let Some(arg0) = invocation.args.first() else {
        return Decision::Deny("no command given".to_string());
    };
    let name = command_name(arg0);
    let args = &invocation.args[1..];
    let request = Request {
        cwd: &invocation.cwd,
        workspace,
        client_dir: OnceCell::new(),
    };
    let mut known = false;
    let mut args_matched = false;
    for rule in commands.iter().filter(|rule| rule.command == name) {
        known = true;
        let Some(args) = match_args(&rule.args, args, &request) else {
            continue;
        };
        args_matched = true;
        let cwd = match &rule.cwd {
            None => None,
            Some(cwd_rule) => match request.client_dir() {
                Some(dir)
                    if cwd_rule
                        .allowed_project_clone_dirs
                        .iter()
                        .any(|pattern| pattern.matches(&dir.names())) =>
                {
                    Some(dir.path.clone())
                }
                _ => continue,
            },
        };
        if rule.unavailable {
            return Decision::Deny(format!(
                "'{name}' is not available: its program wasn't found on the host when the configuration was made"
            ));
        }
        let envs = match rule.env_plan.apply(&invocation.envs) {
            Ok(envs) => envs,
            Err(reason) => return Decision::Deny(reason),
        };
        let program = rule.program.clone().unwrap_or_else(|| name.to_string());
        return Decision::Allow(ExecRequest {
            program,
            args,
            cwd,
            envs,
        });
    }
    if args_matched {
        Decision::Deny(format!("'{name}' is not allowed to run in this directory"))
    } else if known {
        Decision::Deny(format!("'{name}' is not allowed with these arguments"))
    } else {
        Decision::Deny(format!("'{name}' is not allowed to run on the host"))
    }
}

/// The client's side of a request, for resolving paths.
struct Request<'a> {
    /// The client's working directory, in the container.
    cwd: &'a str,
    workspace: &'a ProjectClone,
    /// Resolved on first use.
    client_dir: OnceCell<Option<HostPath>>,
}

impl Request<'_> {
    /// The client's working directory on the host; `None` when it isn't in the project clone.
    fn client_dir(&self) -> Option<&HostPath> {
        self.client_dir
            .get_or_init(|| resolve_dir(self.cwd, self.workspace))
            .as_ref()
    }
}

/// A resolved path inside the host's project clone directory.
#[derive(Debug, PartialEq, Eq)]
struct HostPath {
    path: PathBuf,
    /// The names below the project clone directory.
    below_workspace: Vec<String>,
}

impl HostPath {
    fn names(&self) -> Vec<&str> {
        self.below_workspace.iter().map(String::as_str).collect()
    }
}

/// Resolve a container directory on the host; see `resolve`.  It must exist as a directory.
fn resolve_dir(container_dir: &str, workspace: &ProjectClone) -> Option<HostPath> {
    resolve(container_dir, workspace, false).filter(|dir| dir.path.is_dir())
}

/// Map an absolute container path to the host, following symlinks.  `None`
/// when it isn't under the project clone mount, doesn't exist on the host, or
/// resolves to somewhere outside the host's project clone directory.
///
/// With `may_be_missing`, a path that doesn't exist yet (such as an output
/// file) is allowed when its directory exists, as long as it isn't a dangling
/// symlink.
fn resolve(
    container_path: &str,
    workspace: &ProjectClone,
    may_be_missing: bool,
) -> Option<HostPath> {
    let rest = container_path.strip_prefix(workspace.container.as_str())?;
    if !rest.is_empty() && !rest.starts_with('/') {
        // A sibling such as `/workspace2`.
        return None;
    }
    let names: Vec<&str> = rest.split('/').filter(|name| !name.is_empty()).collect();
    let mut host = workspace.host.clone();
    for name in &names {
        host.push(name);
    }
    // Symlinks and `..` are resolved here; the result is checked below.
    // TODO race (time-of-check to time-of-use): the container can still change
    //      the workspace between this check and `exec` using `path` (as the
    //      working directory or an argument).  Replacing a directory along
    //      `path` with a symlink in that window would point the process
    //      wherever the symlink points.  Closing it needs the checked
    //      directory held open (e.g. openat / fchdir).
    let path = match fs::canonicalize(&host) {
        Ok(path) => path,
        Err(e) if may_be_missing && e.kind() == ErrorKind::NotFound => {
            // Only the last name may be missing, and it must be a plain name.
            let name = names.last()?;
            let mut parts = Path::new(name).components();
            if !matches!(
                (parts.next(), parts.next()),
                (Some(Component::Normal(_)), None)
            ) {
                return None;
            }
            let parent = fs::canonicalize(host.parent()?).ok()?;
            if !parent.is_dir() {
                return None;
            }
            let path = parent.join(name);
            // A dangling symlink would send the process wherever it points.
            if fs::symlink_metadata(&path).is_ok() {
                return None;
            }
            path
        }
        Err(_) => return None,
    };
    let below = path.strip_prefix(&workspace.host).ok()?;
    let below_workspace = below
        .components()
        .map(|c| c.as_os_str().to_str().map(str::to_string))
        .collect::<Option<Vec<_>>>()?;
    Some(HostPath {
        path,
        below_workspace,
    })
}

/// The command a client asked for: the file name of its argv[0].  The client
/// is installed in the container under the names of the commands it forwards.
fn command_name(arg0: &str) -> &str {
    arg0.rsplit('/').next().unwrap_or(arg0)
}

/// Match every argument against the pattern's token in the same position.
fn match_args(pattern: &[Token], args: &[String], request: &Request) -> Option<Vec<Arg>> {
    if pattern.len() != args.len() {
        return None;
    }
    pattern
        .iter()
        .zip(args)
        .map(|(token, arg)| match_token(token, arg, request))
        .collect()
}

fn match_token(token: &Token, value: &str, request: &Request) -> Option<Arg> {
    match token {
        Token::Literal(literal) => (literal == value).then(|| Arg::Plain(value.to_string())),
        Token::Any(options) => accepts(options, value).then(|| Arg::Plain(value.to_string())),
        Token::Path(options) => match_path(options, value, request),
    }
}

/// Whether an `any` placeholder with `options` accepts `value`.
fn accepts(options: &MatchOptions, value: &str) -> bool {
    let Some(rest) = value.strip_prefix(options.prefix.as_str()) else {
        return false;
    };
    if rest.is_empty() && !options.allow_empty {
        return false;
    }
    // A configured prefix is the config author's choice; only check the part
    // the client chose, when it starts the argument.
    if options.prefix.is_empty() && value.starts_with('-') && !options.allow_leading_dash {
        return false;
    }
    true
}

/// Match a `path` placeholder, producing the host path (or temporary file) to pass.
fn match_path(options: &PathOptions, value: &str, request: &Request) -> Option<Arg> {
    if value.is_empty() || (value.starts_with('-') && !options.allow_leading_dash) {
        return None;
    }
    if options.within == Within::Temp {
        let name = if options.keep_name {
            Some(temp_file_name(value)?.to_string())
        } else {
            None
        };
        return Some(Arg::TempFile { name });
    }
    let container_path = if value.starts_with('/') {
        value.to_string()
    } else if request.cwd.starts_with('/') {
        format!("{}/{value}", request.cwd)
    } else {
        return None;
    };
    let resolved = resolve(&container_path, request.workspace, true)?;
    let allowed = match options.within {
        // At or below: `starts_with` compares whole names.
        Within::Cwd => resolved.path.starts_with(&request.client_dir()?.path),
        Within::ProjectClone => options
            .allowed
            .iter()
            .any(|pattern| pattern.matches(&resolved.names())),
        Within::Temp => false,
    };
    allowed.then_some(Arg::HostPath(resolved.path))
}

/// The file name in the client's `value`, when it's safe to create on any host OS.
fn temp_file_name(value: &str) -> Option<&str> {
    let name = value.rsplit('/').next().unwrap_or(value);
    if name.is_empty() || name == "." || name == ".." {
        return None;
    }
    // Characters Windows refuses in names (including both separators), and control characters.
    if name
        .chars()
        .any(|c| c.is_control() || matches!(c, '\\' | '<' | '>' | ':' | '"' | '|' | '?' | '*'))
    {
        return None;
    }
    // Windows drops these, so `nul.` or `con ` would name a device.
    if name.ends_with('.') || name.ends_with(' ') {
        return None;
    }
    // Windows device names, with or without an extension.
    let stem = name
        .split('.')
        .next()
        .unwrap_or(name)
        .trim_end()
        .to_ascii_uppercase();
    let device = matches!(stem.as_str(), "CON" | "PRN" | "AUX" | "NUL")
        || ((stem.starts_with("COM") || stem.starts_with("LPT"))
            && stem.len() == 4
            && matches!(stem.as_bytes()[3], b'1'..=b'9'));
    if device {
        return None;
    }
    Some(name)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::host::exec::TempDir;
    use scoter_host::config::environment::EnvPlan;
    use scoter_host::config::project;
    use std::collections::BTreeMap;
    use std::path::Path;

    /// The rules from the configuration example, plus a few for edge cases.
    fn commands() -> Commands {
        parse_commands(concat!(
            "  - command: aws\n",
            "    args: [s3, cp, {any: {prefix: \"s3://some-name/\"}}, {path: {within: cwd}}]\n",
            "  - command: aws\n",
            "    args: [codebuild, start-build, --project-name, {any: {}}]\n",
            "  - {command: tool, args: []}\n",
            "  - {command: tool, args: [dash, {any: {allow-leading-dash: true}}]}\n",
            "  - {command: tool, args: [empty, {any: {allow-empty: true}}]}\n",
            "  - {command: tool, args: [prefix-empty, {any: {prefix: 'id=', allow-empty: true}}]}\n",
            "  - {command: tool, args: [dash-prefix, {any: {prefix: '--name='}}]}\n",
            "  - {command: tool, args: [lit, '']}\n",
        ))
    }

    fn workspace() -> ProjectClone {
        ProjectClone {
            host: std::path::PathBuf::from("/home/me/ws"),
            container: "/workspace".to_string(),
        }
    }

    /// Parse the entries under `commands:`.
    fn parse_commands(entries: &str) -> Commands {
        project::parse(
            &format!("container:\n  dev-container: dev-env\ncommands:\n{entries}"),
            Path::new("/project/scoter.yaml"),
        )
        .unwrap()
        .commands
    }

    fn invocation(args: &[&str]) -> Invocation {
        Invocation {
            cwd: "/workspace".to_string(),
            args: args.iter().map(|s| s.to_string()).collect(),
            envs: vec![("LD_PRELOAD".to_string(), "/tmp/evil.so".to_string())],
        }
    }

    #[test]
    fn environment_from_the_plan() {
        let mut commands = parse_commands("  - command: aws\n    args: [s3, ls]\n");
        commands[0].env_plan = EnvPlan {
            allow: vec!["AWS_*".to_string()],
            drop: vec!["AWS_SESSION_TOKEN".to_string()],
            error: vec!["AWS_SECRET_*".to_string()],
            set: BTreeMap::from([("HOME".to_string(), "/home/me".to_string())]),
            ..EnvPlan::default()
        };
        let mut inv = invocation(&["aws", "s3", "ls"]);
        inv.envs.extend([
            ("AWS_PROFILE".to_string(), "dev".to_string()),
            ("AWS_SESSION_TOKEN".to_string(), "t".to_string()),
        ]);
        match analyze(&inv, &commands, &workspace()) {
            Decision::Allow(req) => assert_eq!(
                req.envs,
                BTreeMap::from([
                    ("AWS_PROFILE".to_string(), "dev".to_string()),
                    ("HOME".to_string(), "/home/me".to_string()),
                ])
            ),
            other => panic!("expected allow, got {other:?}"),
        }
        inv.envs
            .push(("AWS_SECRET_ACCESS_KEY".to_string(), "s".to_string()));
        assert_eq!(
            analyze(&inv, &commands, &workspace()),
            Decision::Deny(
                "the container may not set the environment variable AWS_SECRET_ACCESS_KEY (it matches 'AWS_SECRET_*')"
                    .to_string()
            )
        );
    }

    #[test]
    fn unavailable_program_refused() {
        let mut commands = parse_commands("  - command: aws\n    args: [s3, ls]\n");
        commands[0].unavailable = true;
        assert_eq!(
            analyze(&invocation(&["aws", "s3", "ls"]), &commands, &workspace()),
            Decision::Deny(
                "'aws' is not available: its program wasn't found on the host when the configuration was made"
                    .to_string()
            )
        );
    }

    /// Decide in a fresh project clone, with the client at the project clone root.
    fn decide(args: &[&str]) -> Decision {
        let ws = TestProjectClone::new();
        analyze(&invocation(args), &commands(), &ws.workspace)
    }

    fn allowed(args: &[&str]) -> Vec<Arg> {
        match decide(args) {
            Decision::Allow(req) => {
                // Without `program`, the command name is run.
                assert_eq!(req.program, command_name(args[0]));
                req.args
            }
            other => panic!("{args:?}: expected allow, got {other:?}"),
        }
    }

    fn assert_denied(args: &[&str]) {
        assert!(
            matches!(decide(args), Decision::Deny(_)),
            "{args:?} should be denied"
        );
    }

    fn plain(s: &str) -> Arg {
        Arg::Plain(s.to_string())
    }

    fn host_path(path: PathBuf) -> Arg {
        Arg::HostPath(path)
    }

    // ---- the example rules ----

    #[test]
    fn s3_copy_bucket_to_container() {
        let ws = TestProjectClone::new();
        let decide = |args: &[&str]| analyze(&invocation(args), &commands(), &ws.workspace);
        assert_eq!(
            decide(&[
                "aws",
                "s3",
                "cp",
                "s3://some-name/dir/file.txt",
                "/workspace/file.txt"
            ]),
            Decision::Allow(ExecRequest {
                program: "aws".to_string(),
                args: vec![
                    plain("s3"),
                    plain("cp"),
                    plain("s3://some-name/dir/file.txt"),
                    host_path(ws.host("file.txt"))
                ],
                cwd: None,
                envs: BTreeMap::new(),
            })
        );
        // Relative targets are resolved from the client's directory.
        let target = |args: &[&str]| match decide(args) {
            Decision::Allow(req) => req.args[3].clone(),
            other => panic!("{other:?}"),
        };
        assert_eq!(
            target(&["aws", "s3", "cp", "s3://some-name/k", "out"]),
            host_path(ws.host("out"))
        );
        assert_eq!(
            target(&["aws", "s3", "cp", "s3://some-name/k", "services"]),
            host_path(ws.host("services"))
        );
        // Any key, including odd ones; S3 keys are opaque.
        target(&["aws", "s3", "cp", "s3://some-name/../x", "out"]);
        target(&["aws", "s3", "cp", "s3://some-name/-k", "out"]);
    }

    #[test]
    fn s3_copy_denied_forms() {
        // Other buckets, including look-alike names.
        assert_denied(&["aws", "s3", "cp", "s3://other/k", "out"]);
        assert_denied(&["aws", "s3", "cp", "s3://some-name-evil/k", "out"]);
        assert_denied(&["aws", "s3", "cp", "s3://some-name", "out"]);
        // No key.
        assert_denied(&["aws", "s3", "cp", "s3://some-name/", "out"]);
        // Wrong direction: container to bucket.
        assert_denied(&["aws", "s3", "cp", "out", "s3://some-name/k"]);
        // Target that's an option, stdout, or empty.
        assert_denied(&["aws", "s3", "cp", "s3://some-name/k", "--recursive"]);
        assert_denied(&["aws", "s3", "cp", "s3://some-name/k", "-"]);
        assert_denied(&["aws", "s3", "cp", "s3://some-name/k", ""]);
        // Extra or missing arguments.
        assert_denied(&["aws", "s3", "cp", "s3://some-name/k", "out", "--recursive"]);
        assert_denied(&[
            "aws",
            "--profile",
            "x",
            "s3",
            "cp",
            "s3://some-name/k",
            "out",
        ]);
        assert_denied(&["aws", "s3", "cp", "s3://some-name/k"]);
        // Other subcommands.
        assert_denied(&["aws", "s3", "rm", "s3://some-name/k", "out"]);
        assert_denied(&["aws", "s3", "mv", "s3://some-name/k", "out"]);
    }

    #[test]
    fn codebuild_start_build() {
        assert_eq!(
            allowed(&[
                "aws",
                "codebuild",
                "start-build",
                "--project-name",
                "my-project"
            ]),
            vec![
                plain("codebuild"),
                plain("start-build"),
                plain("--project-name"),
                plain("my-project"),
            ]
        );
    }

    #[test]
    fn codebuild_denied_forms() {
        assert_denied(&[
            "aws",
            "codebuild",
            "start-build",
            "--project-name",
            "--debug",
        ]);
        assert_denied(&["aws", "codebuild", "start-build", "--project-name", ""]);
        assert_denied(&[
            "aws",
            "codebuild",
            "start-build",
            "--project-name=my-project",
        ]);
        assert_denied(&[
            "aws",
            "codebuild",
            "start-build",
            "--project-name",
            "p",
            "--source-version",
            "x",
        ]);
        assert_denied(&["aws", "codebuild", "stop-build", "--project-name", "p"]);
        assert_denied(&["aws", "codebuild", "start-build", "--projectname", "p"]);
        assert_denied(&["aws"]);
    }

    // ---- placeholder options ----

    #[test]
    fn leading_dash_is_opt_in() {
        assert_eq!(
            allowed(&["tool", "dash", "--verbose"]),
            vec![plain("dash"), plain("--verbose")]
        );
        allowed(&["tool", "dash", "-"]);
        assert_denied(&["tool", "dash", ""]);
    }

    #[test]
    fn empty_is_opt_in() {
        assert_eq!(
            allowed(&["tool", "empty", ""]),
            vec![plain("empty"), plain("")]
        );
        assert_denied(&["tool", "empty", "-x"]);
        assert_eq!(allowed(&["tool", "prefix-empty", "id="])[1], plain("id="));
        allowed(&["tool", "prefix-empty", "id=7"]);
        assert_denied(&["tool", "prefix-empty", "id"]);
    }

    #[test]
    fn prefix_may_start_with_dash() {
        allowed(&["tool", "dash-prefix", "--name=x"]);
        // The client's part still can't be empty.
        assert_denied(&["tool", "dash-prefix", "--name="]);
        assert_denied(&["tool", "dash-prefix", "--other=x"]);
    }

    #[test]
    fn literals_match_exactly() {
        allowed(&["tool", "lit", ""]);
        assert_denied(&["tool", "lit", " "]);
        assert_denied(&["tool", "LIT", ""]);
    }

    #[test]
    fn empty_rule_allows_bare() {
        assert_eq!(allowed(&["tool"]), vec![]);
        assert_eq!(allowed(&["/usr/local/bin/tool"]), vec![]);
    }

    // ---- command selection ----

    #[test]
    fn denies_unlisted_commands() {
        assert_eq!(
            decide(&["docker", "ps"]),
            Decision::Deny("'docker' is not allowed to run on the host".to_string())
        );
        assert_eq!(
            decide(&["scoter-client"]),
            Decision::Deny("'scoter-client' is not allowed to run on the host".to_string())
        );
        // Nothing is allowed without configuration.
        assert!(matches!(
            analyze(&invocation(&["aws"]), &Commands::new(), &workspace()),
            Decision::Deny(_)
        ));
    }

    #[test]
    fn mismatch_reason() {
        assert_eq!(
            decide(&["aws", "s3", "ls"]),
            Decision::Deny("'aws' is not allowed with these arguments".to_string())
        );
    }

    #[test]
    fn denies_lookalike_commands() {
        for arg0 in [
            "AWS",
            "aws.exe",
            "aws ",
            "/usr/bin/aws/",
            "",
            "xaws",
            "aws/x",
        ] {
            assert_denied(&[arg0, "codebuild", "start-build", "--project-name", "p"]);
        }
    }

    #[test]
    fn denies_missing_command() {
        assert_eq!(decide(&[]), Decision::Deny("no command given".to_string()));
    }

    #[test]
    fn command_name_is_basename() {
        assert_eq!(command_name("echo"), "echo");
        assert_eq!(command_name("/usr/bin/echo"), "echo");
        assert_eq!(command_name("bin/echo"), "echo");
        assert_eq!(command_name("/"), "");
    }

    #[test]
    fn first_matching_entry_wins() {
        let commands = parse_commands(concat!(
            "  - {command: cp, program: first, args: [{any: {}}]}\n",
            "  - {command: cp, program: second, args: [{path: {within: cwd}}]}\n",
        ));
        assert_eq!(
            analyze(&invocation(&["cp", "x"]), &commands, &workspace()),
            Decision::Allow(ExecRequest {
                program: "first".to_string(),
                args: vec![plain("x")],
                cwd: None,
                envs: BTreeMap::new(),
            })
        );
    }

    #[test]
    fn program_is_per_entry() {
        let commands = parse_commands(concat!(
            "  - {command: aws, program: aws-v1, args: [s3, ls]}\n",
            "  - {command: aws, args: [sts, get-caller-identity]}\n",
            "  - {command: other, program: other-aws, args: [s3, ls]}\n",
        ));
        let program = |args: &[&str]| match analyze(&invocation(args), &commands, &workspace()) {
            Decision::Allow(req) => req.program,
            other => panic!("{other:?}"),
        };
        assert_eq!(program(&["aws", "s3", "ls"]), "aws-v1");
        assert_eq!(program(&["aws", "sts", "get-caller-identity"]), "aws");
        assert_eq!(program(&["/usr/bin/aws", "s3", "ls"]), "aws-v1");
    }

    #[test]
    fn other_commands_do_not_match() {
        let commands = parse_commands("  - {command: other, args: [s3, ls]}\n");
        assert_eq!(
            analyze(&invocation(&["aws", "s3", "ls"]), &commands, &workspace()),
            Decision::Deny("'aws' is not allowed to run on the host".to_string()),
        );
    }

    // ---- cwd ----

    /// A real project clone directory on the host:
    ///   services/api, services/web, infra/a/b, notes.txt
    struct TestProjectClone {
        /// Removes the directory when the test ends.
        _dir: TempDir,
        workspace: ProjectClone,
    }

    impl TestProjectClone {
        fn new() -> Self {
            let dir = TempDir::new().unwrap();
            for sub in ["services/api", "services/web", "infra/a/b"] {
                fs::create_dir_all(dir.path().join(sub)).unwrap();
            }
            fs::write(dir.path().join("notes.txt"), b"").unwrap();
            let workspace = ProjectClone {
                host: dir.path().canonicalize().unwrap(),
                container: "/workspace".to_string(),
            };
            TestProjectClone {
                _dir: dir,
                workspace,
            }
        }

        fn host(&self, sub: &str) -> PathBuf {
            self.workspace.host.join(sub)
        }

        fn decide(&self, commands: &Commands, cwd: &str) -> Decision {
            let mut inv = invocation(&["docker", "compose", "up"]);
            inv.cwd = cwd.to_string();
            analyze(&inv, commands, &self.workspace)
        }

        /// The directory an allowed request runs in.
        fn cwd_of(&self, commands: &Commands, cwd: &str) -> Option<PathBuf> {
            match self.decide(commands, cwd) {
                Decision::Allow(req) => req.cwd,
                other => panic!("{cwd}: expected allow, got {other:?}"),
            }
        }

        fn assert_denied_in(&self, commands: &Commands, cwd: &str) {
            assert_eq!(
                self.decide(commands, cwd),
                Decision::Deny("'docker' is not allowed to run in this directory".to_string()),
                "{cwd}",
            );
        }
    }

    fn compose_commands() -> Commands {
        parse_commands(concat!(
            "  - command: docker\n",
            "    args: [compose, up]\n",
            "    cwd:\n",
            "      allowed-project-clone-dirs: [services/*, infra/**]\n",
        ))
    }

    #[test]
    fn cwd_allowed_runs_in_host_dir() {
        let ws = TestProjectClone::new();
        let commands = compose_commands();
        assert_eq!(
            ws.decide(&commands, "/workspace/services/api"),
            Decision::Allow(ExecRequest {
                program: "docker".to_string(),
                args: vec![plain("compose"), plain("up")],
                cwd: Some(ws.host("services/api")),
                envs: BTreeMap::new(),
            })
        );
        assert_eq!(
            ws.cwd_of(&commands, "/workspace/infra"),
            Some(ws.host("infra"))
        );
        assert_eq!(
            ws.cwd_of(&commands, "/workspace/infra/a/b"),
            Some(ws.host("infra/a/b"))
        );
    }

    #[test]
    fn cwd_client_path_is_normalized() {
        let ws = TestProjectClone::new();
        let commands = compose_commands();
        assert_eq!(
            ws.cwd_of(&commands, "/workspace/services/./api/"),
            Some(ws.host("services/api"))
        );
        assert_eq!(
            ws.cwd_of(&commands, "/workspace//services//web"),
            Some(ws.host("services/web"))
        );
        assert_eq!(
            ws.cwd_of(&commands, "/workspace/infra/a/../a"),
            Some(ws.host("infra/a"))
        );
    }

    #[test]
    fn cwd_not_matching_patterns() {
        let ws = TestProjectClone::new();
        let commands = compose_commands();
        ws.assert_denied_in(&commands, "/workspace");
        ws.assert_denied_in(&commands, "/workspace/services");
        // `..` that lands outside the patterns.
        ws.assert_denied_in(&commands, "/workspace/services/api/..");
    }

    #[test]
    fn cwd_outside_workspace() {
        let ws = TestProjectClone::new();
        let commands = compose_commands();
        ws.assert_denied_in(&commands, "/");
        ws.assert_denied_in(&commands, "/tmp");
        ws.assert_denied_in(&commands, "/workspacex/services/api");
        ws.assert_denied_in(&commands, "workspace/services/api");
        ws.assert_denied_in(&commands, "/workspace/../workspace/services/api");
        ws.assert_denied_in(&commands, "/workspace/services/api/../../..");
    }

    #[test]
    fn cwd_must_exist_as_a_directory() {
        let ws = TestProjectClone::new();
        let commands = compose_commands();
        ws.assert_denied_in(&commands, "/workspace/services/missing");
        ws.assert_denied_in(&commands, "/workspace/infra/notes.txt");
        fs::write(ws.host("services/file"), b"").unwrap();
        ws.assert_denied_in(&commands, "/workspace/services/file");
    }

    #[test]
    fn cwd_mismatch_tries_next_entry() {
        let ws = TestProjectClone::new();
        let commands = parse_commands(concat!(
            "  - {command: docker, args: [compose, up], cwd: {allowed-project-clone-dirs: [services/*]}}\n",
            "  - {command: docker, args: [compose, up]}\n",
        ));
        assert_eq!(
            ws.cwd_of(&commands, "/workspace/services/api"),
            Some(ws.host("services/api"))
        );
        // Falls through to the entry without `cwd`: a temporary directory.
        assert_eq!(ws.cwd_of(&commands, "/workspace"), None);
        assert_eq!(ws.cwd_of(&commands, "/elsewhere"), None);
    }

    #[test]
    fn cwd_later_entry_allows_others() {
        let ws = TestProjectClone::new();
        let commands = parse_commands(concat!(
            "  - {command: docker, program: docker-a, args: [compose, up], cwd: {allowed-project-clone-dirs: [services/*]}}\n",
            "  - {command: docker, program: docker-b, args: [compose, up], cwd: {allowed-project-clone-dirs: [.]}}\n",
        ));
        let program = |cwd: &str| match ws.decide(&commands, cwd) {
            Decision::Allow(req) => req.program,
            other => panic!("{other:?}"),
        };
        assert_eq!(program("/workspace/services/web"), "docker-a");
        assert_eq!(program("/workspace"), "docker-b");
        ws.assert_denied_in(&commands, "/workspace/infra");
    }

    #[test]
    fn cwd_ignored_without_rule() {
        let ws = TestProjectClone::new();
        let commands = parse_commands("  - {command: docker, args: [compose, up]}\n");
        assert_eq!(ws.cwd_of(&commands, "/workspace/services/api"), None);
        assert_eq!(ws.cwd_of(&commands, "not even a path"), None);
    }

    #[test]
    fn cwd_mismatch_reason_unchanged() {
        let ws = TestProjectClone::new();
        let mut inv = invocation(&["docker", "compose", "down"]);
        inv.cwd = "/workspace/services/api".to_string();
        assert_eq!(
            analyze(&inv, &compose_commands(), &ws.workspace),
            Decision::Deny("'docker' is not allowed with these arguments".to_string()),
        );
    }

    #[test]
    fn mount_trailing_name_overlap() {
        // The mount itself may be nested, e.g. /home/dev/ws.
        let ws = TestProjectClone::new();
        let workspace = ProjectClone {
            host: ws.workspace.host.clone(),
            container: "/home/dev/ws".to_string(),
        };
        assert_eq!(
            resolve_dir("/home/dev/ws/services/api", &workspace).map(|d| d.path),
            Some(ws.host("services/api")),
        );
        assert_eq!(
            resolve_dir("/home/dev/ws", &workspace).map(|d| d.below_workspace),
            Some(vec![])
        );
        assert_eq!(resolve_dir("/home/dev/wsx", &workspace), None);
        assert_eq!(resolve_dir("/home/dev", &workspace), None);
    }

    #[cfg(unix)]
    #[test]
    fn cwd_symlink_out_is_refused() {
        let ws = TestProjectClone::new();
        let outside = TempDir::new().unwrap();
        fs::create_dir(outside.path().join("api")).unwrap();
        // A link the container could create, pointing at a host directory.
        std::os::unix::fs::symlink(outside.path(), ws.host("services/escape")).unwrap();
        std::os::unix::fs::symlink(outside.path(), ws.host("infra/escape")).unwrap();
        let commands = compose_commands();
        ws.assert_denied_in(&commands, "/workspace/services/escape");
        ws.assert_denied_in(&commands, "/workspace/infra/escape");
        ws.assert_denied_in(&commands, "/workspace/infra/escape/api");
    }

    #[cfg(unix)]
    #[test]
    fn cwd_symlink_matches_target() {
        let ws = TestProjectClone::new();
        let commands = compose_commands();
        // services/link -> infra/a: the name matches `services/*`, but the
        // target is what's checked (and it matches `infra/**`).
        std::os::unix::fs::symlink(ws.host("infra/a"), ws.host("services/link")).unwrap();
        assert_eq!(
            ws.cwd_of(&commands, "/workspace/services/link"),
            Some(ws.host("infra/a"))
        );
        // infra/up -> the workspace root, which no pattern allows.
        std::os::unix::fs::symlink(&ws.workspace.host, ws.host("infra/up")).unwrap();
        ws.assert_denied_in(&commands, "/workspace/infra/up");
    }

    #[test]
    fn resolve_dir_reports_names() {
        let ws = TestProjectClone::new();
        let dir = resolve_dir("/workspace/infra/a/b", &ws.workspace).unwrap();
        assert_eq!(dir.below_workspace, vec!["infra", "a", "b"]);
        assert!(Path::new(&dir.path).is_absolute());
    }

    // ---- path arguments ----

    /// Entries with one path argument, for each mode.
    fn path_commands() -> Commands {
        parse_commands(concat!(
            "  - {command: tool, args: [cwd, {path: {within: cwd}}]}\n",
            "  - {command: tool, args: [cwd-dash, {path: {within: cwd, allow-leading-dash: true}}]}\n",
            "  - {command: tool, args: [ws, {path: {within: project-clone, allowed: ['infra/**', 'services/*/*.json', notes.txt]}}]}\n",
            "  - {command: tool, args: [temp, {path: {within: temp}}]}\n",
            "  - {command: tool, args: [temp-name, {path: {within: temp, keep-name: true}}]}\n",
        ))
    }

    impl TestProjectClone {
        /// The path argument an allowed `tool MODE VALUE` gets, run from `cwd`.
        fn path_arg(&self, cwd: &str, mode: &str, value: &str) -> Option<Arg> {
            let mut inv = invocation(&["tool", mode, value]);
            inv.cwd = cwd.to_string();
            match analyze(&inv, &path_commands(), &self.workspace) {
                Decision::Allow(req) => Some(req.args[1].clone()),
                Decision::Deny(_) => None,
            }
        }

        fn host_arg(&self, sub: &str) -> Option<Arg> {
            Some(Arg::HostPath(self.host(sub)))
        }
    }

    #[test]
    fn path_within_cwd() {
        let ws = TestProjectClone::new();
        let cwd = "/workspace/services";
        // At or below the client's directory, existing or not.
        assert_eq!(ws.path_arg(cwd, "cwd", "."), ws.host_arg("services"));
        assert_eq!(ws.path_arg(cwd, "cwd", "api"), ws.host_arg("services/api"));
        assert_eq!(
            ws.path_arg(cwd, "cwd", "./api/"),
            ws.host_arg("services/api")
        );
        assert_eq!(
            ws.path_arg(cwd, "cwd", "api/new.txt"),
            ws.host_arg("services/api/new.txt")
        );
        assert_eq!(
            ws.path_arg(cwd, "cwd", "/workspace/services/web"),
            ws.host_arg("services/web")
        );
        assert_eq!(
            ws.path_arg(cwd, "cwd", "api/../web"),
            ws.host_arg("services/web")
        );
        // Above or beside it.
        assert_eq!(ws.path_arg(cwd, "cwd", ".."), None);
        assert_eq!(ws.path_arg(cwd, "cwd", "../notes.txt"), None);
        assert_eq!(ws.path_arg(cwd, "cwd", "/workspace/infra"), None);
        assert_eq!(ws.path_arg(cwd, "cwd", "/workspace/services-old"), None);
        // Outside the workspace altogether.
        assert_eq!(ws.path_arg(cwd, "cwd", "/etc/passwd"), None);
        assert_eq!(ws.path_arg(cwd, "cwd", "../../.."), None);
    }

    #[test]
    fn cwd_path_needs_client_inside() {
        let ws = TestProjectClone::new();
        assert_eq!(ws.path_arg("/", "cwd", "workspace/notes.txt"), None);
        assert_eq!(
            ws.path_arg("/home/dev", "cwd", "/workspace/notes.txt"),
            None
        );
        assert_eq!(ws.path_arg("relative", "cwd", "notes.txt"), None);
        // The workspace root itself is fine.
        assert_eq!(
            ws.path_arg("/workspace", "cwd", "notes.txt"),
            ws.host_arg("notes.txt")
        );
    }

    #[test]
    fn path_within_workspace() {
        let ws = TestProjectClone::new();
        let cwd = "/workspace/services";
        assert_eq!(ws.path_arg(cwd, "ws", "../infra"), ws.host_arg("infra"));
        assert_eq!(
            ws.path_arg(cwd, "ws", "/workspace/infra/a/b/out.log"),
            ws.host_arg("infra/a/b/out.log")
        );
        assert_eq!(
            ws.path_arg(cwd, "ws", "api/config.json"),
            ws.host_arg("services/api/config.json")
        );
        assert_eq!(
            ws.path_arg(cwd, "ws", "../notes.txt"),
            ws.host_arg("notes.txt")
        );
        // Not matching any pattern.
        assert_eq!(ws.path_arg(cwd, "ws", "api"), None);
        assert_eq!(ws.path_arg(cwd, "ws", "api/config.yaml"), None);
        assert_eq!(ws.path_arg(cwd, "ws", "/workspace"), None);
        assert_eq!(ws.path_arg(cwd, "ws", "/etc/hosts"), None);
        // Works wherever the client is, as long as the path is in the workspace.
        assert_eq!(
            ws.path_arg("/home/dev", "ws", "/workspace/notes.txt"),
            ws.host_arg("notes.txt")
        );
    }

    #[test]
    fn path_missing_parent_refused() {
        let ws = TestProjectClone::new();
        assert_eq!(ws.path_arg("/workspace", "cwd", "missing/out.txt"), None);
        assert_eq!(ws.path_arg("/workspace", "cwd", "notes.txt/out.txt"), None);
        assert_eq!(ws.path_arg("/workspace", "ws", "infra/missing/x"), None);
        // `..` after a missing name can't be resolved either.
        assert_eq!(ws.path_arg("/workspace", "cwd", "missing/.."), None);
    }

    #[test]
    fn path_dash_and_empty() {
        let ws = TestProjectClone::new();
        assert_eq!(ws.path_arg("/workspace", "cwd", ""), None);
        assert_eq!(ws.path_arg("/workspace", "cwd", "-"), None);
        assert_eq!(ws.path_arg("/workspace", "cwd", "--out"), None);
        assert_eq!(
            ws.path_arg("/workspace", "cwd-dash", "-out"),
            ws.host_arg("-out")
        );
        assert_eq!(ws.path_arg("/workspace", "temp", "-x"), None);
    }

    #[test]
    fn path_mount_lookalike() {
        let ws = TestProjectClone::new();
        assert_eq!(
            ws.path_arg("/workspace", "cwd", "/workspacex/notes.txt"),
            None
        );
        assert_eq!(
            ws.path_arg("/workspace", "cwd", "/workspace/../workspace/notes.txt"),
            None
        );
    }

    #[cfg(unix)]
    #[test]
    fn path_symlinks_are_resolved() {
        use std::os::unix::fs::symlink;
        let ws = TestProjectClone::new();
        let outside = TempDir::new().unwrap();
        fs::write(outside.path().join("secret"), b"").unwrap();
        symlink(outside.path(), ws.host("services/api/escape")).unwrap();
        symlink(
            outside.path().join("secret"),
            ws.host("services/api/secret-link"),
        )
        .unwrap();
        symlink(ws.host("infra"), ws.host("services/api/to-infra")).unwrap();
        let cwd = "/workspace/services/api";
        // Links that leave the workspace, including new files under them.
        assert_eq!(ws.path_arg(cwd, "cwd", "escape"), None);
        assert_eq!(ws.path_arg(cwd, "cwd", "escape/new.txt"), None);
        assert_eq!(ws.path_arg(cwd, "cwd", "secret-link"), None);
        assert_eq!(ws.path_arg(cwd, "ws", "to-infra/../../../etc"), None);
        // A link to elsewhere in the workspace is judged by its target.
        assert_eq!(ws.path_arg(cwd, "cwd", "to-infra"), None);
        assert_eq!(ws.path_arg(cwd, "ws", "to-infra"), ws.host_arg("infra"));
        assert_eq!(
            ws.path_arg(cwd, "ws", "to-infra/a/new.log"),
            ws.host_arg("infra/a/new.log")
        );
    }

    #[cfg(unix)]
    #[test]
    fn path_dangling_symlink_refused() {
        let ws = TestProjectClone::new();
        let outside = TempDir::new().unwrap();
        // Writing to `out.txt` would create the file outside the workspace.
        std::os::unix::fs::symlink(outside.path().join("created"), ws.host("services/out.txt"))
            .unwrap();
        assert_eq!(ws.path_arg("/workspace/services", "cwd", "out.txt"), None);
        std::os::unix::fs::symlink("/nonexistent/x", ws.host("infra/out.log")).unwrap();
        assert_eq!(ws.path_arg("/workspace", "ws", "infra/out.log"), None);
    }

    #[test]
    fn path_within_temp() {
        let ws = TestProjectClone::new();
        let generated = Some(Arg::TempFile { name: None });
        // The client's value doesn't matter, not even outside the workspace.
        assert_eq!(ws.path_arg("/workspace", "temp", "out.txt"), generated);
        assert_eq!(ws.path_arg("/", "temp", "/etc/passwd"), generated);
        assert_eq!(ws.path_arg("/workspace", "temp", "NUL"), generated);
    }

    #[test]
    fn path_within_temp_keeps_name() {
        let ws = TestProjectClone::new();
        let kept = |name: &str| {
            Some(Arg::TempFile {
                name: Some(name.to_string()),
            })
        };
        assert_eq!(
            ws.path_arg("/workspace", "temp-name", "out.json"),
            kept("out.json")
        );
        // Only the file name is used.
        assert_eq!(
            ws.path_arg("/workspace", "temp-name", "/etc/ssl/out.pem"),
            kept("out.pem")
        );
        assert_eq!(
            ws.path_arg("/workspace", "temp-name", "../x/.hidden"),
            kept(".hidden")
        );
        assert_eq!(ws.path_arg("/workspace", "temp-name", "dir/"), None);
    }

    #[test]
    fn temp_file_names() {
        for good in [
            "out.txt",
            "a",
            ".env",
            "file.tar.gz",
            "console",
            "nul-device",
            "com10",
            "lpt",
            "\u{FC}n\u{EF}c\u{F8}d\u{E9}",
        ] {
            assert_eq!(temp_file_name(good), Some(good), "{good}");
        }
        for bad in [
            "",
            ".",
            "..",
            "a\\b",
            "a:b",
            "a<b",
            "a>b",
            "a\"b",
            "a|b",
            "a?b",
            "a*b",
            "a\nb",
            "a\u{7f}b",
            "trailing.",
            "trailing ",
            "NUL",
            "nul",
            "Nul.txt",
            "nul .txt",
            "CON",
            "con.json",
            "PRN",
            "AUX.x",
            "COM1",
            "com9.log",
            "LPT1",
            "lpt9",
        ] {
            assert_eq!(temp_file_name(bad), None, "{bad:?}");
        }
        assert_eq!(temp_file_name("a/b/c.txt"), Some("c.txt"));
    }
}
