//! The project configuration file, shared by everyone working on the project.
//!
//! It lives in the project's files directory (the system configuration's
//! `scoter` `values: project-files-dir`, normally `.surf-scoter.d`), as
//! `values: project-file-name` (normally `project.yaml`), with the project's
//! dev containers.  The project directory is the one holding that directory;
//! the tools are given the project directory, and the system configuration
//! says where the file is in it.
//!
//! The host bypass exists so the container can perform an action that needs
//! privileges the container doesn't have (credentials, the Docker daemon, and
//! so on).  Because those actions run on the host, everything they touch on
//! the file system is restricted to the workspace (or a throwaway temporary
//! directory).  Only allow commands that truly need the host's privileges: a
//! command that works inside the container, or that the user can run on the
//! host themselves, doesn't belong here.
//!
//! ```yaml
//! # optional; how the project's files are kept, for the `scoter` script.
//! # The host tools don't use it.
//! vcs: git
//!
//! container:
//!   # The dev container: a directory with a Dockerfile and dev-env.yaml,
//!   # relative to this file, inside the project's files directory...
//!   dev-container-path: ./dev-env
//!   # ...or the name of one found in the system's `scoter` `path-lists: dev-containers-search`.
//!   # dev-container: dev-env
//!
//! # The environment of the host processes, for every command (optional).
//! env:
//!   set:
//!     HOME: '@HOME@'
//!
//! # What clients may run: one entry per allowed form of a command.
//! commands:
//!   - command: aws
//!     args: [s3, cp, {any: {prefix: "s3://some-name/"}}, {path: {within: cwd}}]
//!   - command: aws
//!     # optional; the executable to run on the host (default: `command`, found on the PATH)
//!     program: /usr/local/bin/aws
//!     args: [codebuild, start-build, --project-name, {any: {}}]
//!     # optional; added to the project's `env`
//!     env:
//!       allow: ['AWS_*']
//!       drop: [AWS_SESSION_TOKEN]
//!       default: {AWS_REGION: us-east-1}
//!   - command: docker
//!     args: [compose, up]
//!     # optional; run in the client's directory, when it matches one of these
//!     cwd:
//!       allowed-workspace-dirs:
//!         - services/*
//!         - infra/**
//! ```
//!
//! The host process starts from an empty environment, built by the `env`
//! blocks (see `environment`).  A command's `env` adds its patterns to the
//! project's, and its `set` and `default` values win over the project's.
//!
//! `command` is the name the client was run as in the container.  A request is
//! allowed by the first entry with that command whose `args` match (and whose
//! `cwd` allows the client's directory, when the entry has one).
//!
//! Without `cwd`, the process runs in a new temporary directory.  With `cwd`,
//! it runs in the host directory matching the client's working directory.  That
//! directory must be inside the workspace once symlinks are resolved, and its
//! resolved path must match one of the `allowed-workspace-dirs` patterns:
//!   * Patterns are relative to the workspace directory; `.` is the workspace
//!     itself, and a leading `./` is allowed.
//!   * `*` matches any characters within one directory name, and `?` matches
//!     one character.
//!   * `**`, as a whole directory name, matches any number of directories,
//!     including none: `infra/**` allows `infra` too, and `**` allows the
//!     whole workspace.
//!   * Quote patterns that start with `*`; YAML reads a bare `*...` as an alias.
//!
//! `args` is a list of tokens, matched one-to-one against the client's
//! arguments.  A string token must match exactly.  A map token is a
//! placeholder, `any` or `path`.
//!
//! `any` matches a value, with these options:
//!   * `prefix`: the value must start with this text.
//!   * `allow-leading-dash`: allow values starting with '-' (normally refused,
//!     so a placeholder can't be used to slip in an option).  Text given in
//!     `prefix` doesn't count; only the part the client chose is checked.
//!   * `allow-empty`: allow an empty value (or nothing after the prefix).
//!
//! `path` matches a file path, which the host process receives as the
//! resolved host path.  `within` says where it may be:
//!   * `{path: {within: cwd}}`: at or below the client's working directory.
//!   * `{path: {within: workspace, allowed: [build/**, '*.txt']}}`: in the
//!     workspace, matching one of `allowed` (patterns like
//!     `allowed-workspace-dirs`, but they may name files too).
//!   * `{path: {within: temp}}`: a throwaway file in a temporary directory,
//!     deleted when the process exits.  The client's value only picks the
//!     file name, and only with `keep-name: true`; otherwise the name is
//!     generated.  Kept names may not contain separators or characters
//!     Windows refuses, nor be `.`, `..` or a Windows device name like `NUL`.
//!
//! Client paths are relative to the client's working directory, or absolute
//! container paths; either way they must be inside the workspace mount.  They
//! are resolved on the host following symlinks, and the result must still be
//! in the workspace (and match the `within` rule).  A path that doesn't exist
//! yet (such as an output file) is allowed when its directory exists.
//! `allow-leading-dash` is also a `path` option.
//!
//! Keys are kebab-case.  Unknown keys are rejected, so typos don't silently
//! fall back to defaults.

use std::fmt;
use std::path::{Path, PathBuf};

use serde::de::{self, MapAccess, Visitor};
use serde::{Deserialize, Deserializer};
use yaml_serde::Value;

use super::environment::{EnvPlan, EnvRules};
use super::pattern::WorkspacePattern;
use super::read::{parse_yaml, read_regular_file, tag_errors};
use super::{ConfigError, child_key, index_key};

/// The project configuration file.
#[derive(Debug, PartialEq, Eq)]
pub struct ProjectConfig {
    /// The file, as an absolute path.
    pub path: PathBuf,
    pub container: ContainerRef,
    /// How the project's files are kept, for the `scoter` script: any
    /// non-empty text.
    pub vcs: Option<String>,
    /// The environment rules for every command.
    pub env: EnvRules,
    /// The allowed forms of commands, in match order.  Anything not listed is refused.
    pub commands: Commands,
    /// Each of `commands` as written in the file.
    pub raw_commands: Vec<Value>,
    /// The whole file, as written.
    pub raw: Value,
}

impl ProjectConfig {
    /// The directory holding the file (the project files directory); relative
    /// paths start here.
    pub fn dir(&self) -> &Path {
        self.path.parent().unwrap_or(Path::new(""))
    }
}

/// Which dev container the project uses.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ContainerRef {
    /// `dev-container-path`: its directory, relative to the project file.
    Path(String),
    /// `dev-container`: a directory name, found in the `scoter` `path-lists: dev-containers-search`.
    Name(String),
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields, rename_all = "kebab-case")]
struct ContainerSection {
    #[serde(default)]
    dev_container_path: Option<String>,
    #[serde(default)]
    dev_container: Option<String>,
}

pub type Commands = Vec<CommandRule>;

/// Commands, and each one as written.
pub(crate) type ParsedCommands = (Commands, Vec<Value>);

/// One allowed form of a command.
#[derive(Debug, PartialEq, Eq, Deserialize)]
#[serde(deny_unknown_fields, rename_all = "kebab-case")]
pub struct CommandRule {
    /// The name the client was run as.
    pub command: String,
    /// The host executable; defaults to `command`, found on the host's PATH.
    #[serde(default)]
    pub program: Option<String>,
    /// One token per argument.
    pub args: Vec<Token>,
    /// Where the process may run; without it, a temporary directory.
    #[serde(default)]
    pub cwd: Option<CwdRule>,
    /// Environment rules added to the project's.
    #[serde(default)]
    pub env: EnvRules,
    /// The project's and this command's `env` worked out against the system
    /// configuration, by `config::load_all` (or `load_universal`), with
    /// `@NAME@` references left for the program running the command to
    /// expand (`config::expand_host_environments`).  Until then, it's empty:
    /// nothing comes through.
    #[serde(skip)]
    pub env_plan: EnvPlan,
    /// The program wasn't found when the universal configuration file was
    /// made (its `host-brokered` `program` is `null`): requests for it are
    /// refused.
    #[serde(skip)]
    pub unavailable: bool,
}

#[derive(Debug, PartialEq, Eq, Deserialize)]
#[serde(deny_unknown_fields, rename_all = "kebab-case")]
pub struct CwdRule {
    /// The client's directory must match one of these.
    pub allowed_workspace_dirs: Vec<WorkspacePattern>,
}

#[derive(Debug, PartialEq, Eq)]
pub enum Token {
    /// The argument must be exactly this.
    Literal(String),
    /// Any argument accepted by the options.
    Any(MatchOptions),
    /// A file path, mapped to the host.
    Path(PathOptions),
}

#[derive(Debug, Default, PartialEq, Eq, Deserialize)]
#[serde(deny_unknown_fields, rename_all = "kebab-case")]
pub struct MatchOptions {
    #[serde(default)]
    pub prefix: String,
    #[serde(default)]
    pub allow_leading_dash: bool,
    #[serde(default)]
    pub allow_empty: bool,
}

#[derive(Debug, PartialEq, Eq, Deserialize)]
#[serde(deny_unknown_fields, rename_all = "kebab-case")]
pub struct PathOptions {
    pub within: Within,
    /// For `within: workspace`: the path must match one of these.
    #[serde(default)]
    pub allowed: Vec<WorkspacePattern>,
    /// For `within: temp`: use the client's file name instead of a generated one.
    #[serde(default)]
    pub keep_name: bool,
    #[serde(default)]
    pub allow_leading_dash: bool,
}

/// Where a `path` argument may point.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Deserialize)]
#[serde(rename_all = "kebab-case")]
pub enum Within {
    /// At or below the client's working directory.
    Cwd,
    /// In the workspace, matching `allowed`.
    Workspace,
    /// A throwaway temporary file.
    Temp,
}

/// The map form of a token: `{any: {...}}` or `{path: {...}}`.
#[derive(Deserialize)]
#[serde(deny_unknown_fields, rename_all = "kebab-case")]
enum Placeholder {
    Any(MatchOptions),
    Path(PathOptions),
}

impl<'de> Deserialize<'de> for Token {
    fn deserialize<D: Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        struct TokenVisitor;

        impl<'de> Visitor<'de> for TokenVisitor {
            type Value = Token;

            fn expecting(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
                f.write_str("a string (quote numbers and booleans), or a placeholder such as {any: {}} or {path: {within: cwd}}")
            }

            fn visit_str<E: de::Error>(self, v: &str) -> Result<Token, E> {
                Ok(Token::Literal(v.to_string()))
            }

            fn visit_map<A: MapAccess<'de>>(self, map: A) -> Result<Token, A::Error> {
                Ok(
                    match Placeholder::deserialize(de::value::MapAccessDeserializer::new(map))? {
                        Placeholder::Any(options) => Token::Any(options),
                        Placeholder::Path(options) => Token::Path(options),
                    },
                )
            }
        }

        deserializer.deserialize_any(TokenVisitor)
    }
}

/// Read and check the project file at `path` (made absolute).
pub fn load(path: &Path) -> Result<ProjectConfig, Vec<ConfigError>> {
    load_checked(path).0
}

/// `load`, also giving the `container` setting when it's valid, even if
/// something else in the file isn't; problems with the dev container can then
/// be reported too.
pub(crate) fn load_checked(
    path: &Path,
) -> (
    Result<ProjectConfig, Vec<ConfigError>>,
    Option<ContainerRef>,
) {
    let path = super::absolute(path);
    if let Err(m) = check_location(&path) {
        return (Err(vec![ConfigError::new(Some(&path), "", m)]), None);
    }
    match read_regular_file(&path) {
        Ok(text) => parse_checked(&text, &path),
        Err(m) => (Err(vec![ConfigError::new(Some(&path), "", m)]), None),
    }
}

/// The directory holding the project file must not be a symlink: everything
/// the tools read from the project, and the build context of its dev
/// containers, stays in there.  (Its name is checked by `config::load_all`.)
fn check_location(path: &Path) -> Result<(), String> {
    let Some(files_dir) = path.parent() else {
        return Ok(());
    };
    match files_dir.symlink_metadata() {
        Ok(metadata) if metadata.file_type().is_symlink() => {
            Err("the project file's directory is a symlink; it must be a directory".to_string())
        }
        // A missing directory is reported when the file is read.
        _ => Ok(()),
    }
}

/// Parse and check project file text; `path` is where it came from.
/// Every problem found is reported, not just the first.
pub fn parse(text: &str, path: &Path) -> Result<ProjectConfig, Vec<ConfigError>> {
    parse_checked(text, path).0
}

fn parse_checked(
    text: &str,
    path: &Path,
) -> (
    Result<ProjectConfig, Vec<ConfigError>>,
    Option<ContainerRef>,
) {
    let error = |key: &str, message: String| ConfigError::new(Some(path), key, message);
    let value = match parse_yaml(text) {
        Ok(value) => value,
        Err(m) => return (Err(vec![error("", m)]), None),
    };
    let tags = tag_errors(&value);
    if !tags.is_empty() {
        return (
            Err(tags
                .into_iter()
                .map(|(key, message)| error(&key, message))
                .collect()),
            None,
        );
    }
    let raw = value.clone();
    let map = match value {
        Value::Mapping(map) => map,
        Value::Null => {
            return (
                Err(vec![error(
                    "",
                    "the file is empty; it needs a `container` section".to_string(),
                )]),
                None,
            );
        }
        _ => {
            return (
                Err(vec![error(
                    "",
                    "must be a mapping with `container` and `commands`".to_string(),
                )]),
                None,
            );
        }
    };

    let mut problems: Vec<(String, String)> = Vec::new();
    let mut container = None;
    let mut commands = Commands::new();
    let mut raw_commands = Vec::new();
    let mut env = EnvRules::default();
    let mut vcs = None;
    let mut has_container = false;
    for (key, value) in map {
        match key.as_str() {
            Some("container") => {
                has_container = true;
                container = parse_container(value, &mut problems);
            }
            Some("commands") => {
                (commands, raw_commands) = parse_commands(value, "commands", &mut problems)
            }
            Some("env") => env = parse_env(value, &mut problems),
            Some("vcs") => match value {
                Value::String(text) if !text.is_empty() => vcs = Some(text),
                Value::String(_) => {
                    problems.push(("vcs".to_string(), "must not be empty".to_string()))
                }
                _ => problems.push(("vcs".to_string(), "must be text".to_string())),
            },
            Some(other) => problems.push((other.to_string(), "unknown key".to_string())),
            None => problems.push((String::new(), format!("key {key:?} must be a string"))),
        }
    }
    if !has_container {
        problems.push(("container".to_string(), "is required".to_string()));
    }
    let result = match &container {
        Some(container) if problems.is_empty() => Ok(ProjectConfig {
            path: path.to_path_buf(),
            container: container.clone(),
            vcs,
            env,
            commands,
            raw_commands,
            raw,
        }),
        _ => Err(problems
            .into_iter()
            .map(|(key, message)| error(&key, message))
            .collect()),
    };
    (result, container)
}

fn parse_container(value: Value, problems: &mut Vec<(String, String)>) -> Option<ContainerRef> {
    let mut problem =
        |key: &str, message: String| problems.push((child_key("container", key), message));
    if value.is_null() {
        problem(
            "",
            "needs `dev-container-path` or `dev-container`".to_string(),
        );
        return None;
    }
    let section: ContainerSection = match yaml_serde::from_value(value) {
        Ok(section) => section,
        Err(e) => {
            problem("", e.to_string());
            return None;
        }
    };
    match (section.dev_container_path, section.dev_container) {
        (Some(path), None) if path.is_empty() => {
            problem("dev-container-path", "must not be empty".to_string())
        }
        (Some(path), None) => return Some(ContainerRef::Path(path)),
        (None, Some(name)) if !valid_name(&name) => problem(
            "dev-container",
            format!("'{name}' must be a directory name, without '/' or '\\'"),
        ),
        (None, Some(name)) => return Some(ContainerRef::Name(name)),
        (Some(_), Some(_)) => problem(
            "",
            "set only one of `dev-container-path` and `dev-container`".to_string(),
        ),
        (None, None) => problem(
            "",
            "needs `dev-container-path` or `dev-container`".to_string(),
        ),
    }
    None
}

fn parse_env(value: Value, problems: &mut Vec<(String, String)>) -> EnvRules {
    if value.is_null() {
        return EnvRules::default();
    }
    match yaml_serde::from_value::<EnvRules>(value) {
        Ok(rules) => {
            rules.validate("env", problems);
            rules
        }
        Err(e) => {
            problems.push(("env".to_string(), e.to_string()));
            EnvRules::default()
        }
    }
}

fn valid_name(name: &str) -> bool {
    !name.is_empty() && name != "." && name != ".." && !name.contains(['/', '\\'])
}

/// A list of command entries, such as `commands`, checked as `commands` is;
/// problems are (key, message), with keys under `key`.
pub(crate) fn parse_command_list(
    value: Value,
    key: &str,
) -> Result<ParsedCommands, Vec<(String, String)>> {
    let mut problems = Vec::new();
    let parsed = parse_commands(value, key, &mut problems);
    if problems.is_empty() {
        Ok(parsed)
    } else {
        Err(problems)
    }
}

/// The valid entries, both checked and as written.
fn parse_commands(
    value: Value,
    list_key: &str,
    problems: &mut Vec<(String, String)>,
) -> (Commands, Vec<Value>) {
    let entries = match value {
        Value::Null => return (Commands::new(), Vec::new()),
        Value::Sequence(entries) => entries,
        _ => {
            problems.push((
                list_key.to_string(),
                "must be a list of entries".to_string(),
            ));
            return (Commands::new(), Vec::new());
        }
    };
    let mut commands = Commands::new();
    let mut raw = Vec::new();
    for (i, entry) in entries.into_iter().enumerate() {
        let key = index_key(list_key, i);
        match yaml_serde::from_value::<CommandRule>(entry.clone()) {
            Ok(rule) => {
                let before = problems.len();
                rule.validate(&key, problems);
                if problems.len() == before {
                    commands.push(rule);
                    raw.push(entry);
                }
            }
            Err(e) => problems.push((key, e.to_string())),
        }
    }
    (commands, raw)
}

impl CommandRule {
    fn validate(&self, key: &str, problems: &mut Vec<(String, String)>) {
        // Clients are matched by the file name they were invoked as.
        let name = &self.command;
        if name.is_empty() || name.contains('/') || name == "." || name == ".." {
            problems.push((
                key.to_string(),
                format!("'{name}' is not a valid command name"),
            ));
        }
        if self.program.as_ref().is_some_and(|p| p.is_empty()) {
            problems.push((key.to_string(), "program must not be empty".to_string()));
        }
        if self
            .cwd
            .as_ref()
            .is_some_and(|cwd| cwd.allowed_workspace_dirs.is_empty())
        {
            problems.push((
                key.to_string(),
                "cwd.allowed-workspace-dirs must not be empty".to_string(),
            ));
        }
        self.env.validate(&child_key(key, "env"), problems);
        for (j, token) in self.args.iter().enumerate() {
            if let Token::Path(options) = token
                && let Err(message) = options.validate()
            {
                problems.push((child_key(key, &index_key("args", j)), message));
            }
        }
    }
}

impl PathOptions {
    fn validate(&self) -> Result<(), String> {
        match self.within {
            Within::Workspace if self.allowed.is_empty() => {
                return Err("a path within the workspace needs `allowed` patterns".to_string());
            }
            Within::Cwd | Within::Temp if !self.allowed.is_empty() => {
                return Err("`allowed` only applies to paths within the workspace".to_string());
            }
            _ => {}
        }
        if self.keep_name && self.within != Within::Temp {
            return Err("`keep-name` only applies to paths within temp".to_string());
        }
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::config::testing::TempDir;
    use std::fs;

    const PROJECT: &str = "/proj/scoter.yaml";
    const CONTAINER: &str = "container:\n  dev-container: dev-env\n";

    fn parse_text(text: &str) -> Result<ProjectConfig, Vec<String>> {
        parse(text, Path::new(PROJECT)).map_err(|errors| {
            errors
                .into_iter()
                .map(|e| format!("{}: {}", e.key.unwrap_or_default(), e.message))
                .collect()
        })
    }

    /// The only error for `text`, as "key: message".
    fn error(text: &str) -> String {
        let errors = parse_text(text).unwrap_err();
        assert_eq!(errors.len(), 1, "{errors:?}");
        errors.into_iter().next().unwrap()
    }

    /// Parse a single entry's `args` (flow style).
    fn args(yaml_args: &str) -> Vec<Token> {
        let mut config = parse_text(&format!(
            "{CONTAINER}commands:\n  - command: cmd\n    args: {yaml_args}\n"
        ))
        .unwrap();
        assert_eq!(config.commands.len(), 1);
        config.commands.remove(0).args
    }

    fn args_error(yaml_args: &str) -> String {
        error(&format!(
            "{CONTAINER}commands:\n  - command: cmd\n    args: {yaml_args}\n"
        ))
    }

    fn lit(s: &str) -> Token {
        Token::Literal(s.to_string())
    }

    fn opts(prefix: &str, allow_leading_dash: bool, allow_empty: bool) -> MatchOptions {
        MatchOptions {
            prefix: prefix.to_string(),
            allow_leading_dash,
            allow_empty,
        }
    }

    fn path_opts(within: Within) -> PathOptions {
        PathOptions {
            within,
            allowed: vec![],
            keep_name: false,
            allow_leading_dash: false,
        }
    }

    fn pattern(text: &str) -> WorkspacePattern {
        WorkspacePattern::parse(text).unwrap()
    }

    // ---- container ----

    #[test]
    fn parse_minimal() {
        assert_eq!(
            parse_text(CONTAINER).unwrap(),
            ProjectConfig {
                path: PathBuf::from(PROJECT),
                container: ContainerRef::Name("dev-env".to_string()),
                vcs: None,
                env: EnvRules::default(),
                commands: Commands::new(),
                raw_commands: Vec::new(),
                raw: parse_yaml(CONTAINER).unwrap(),
            }
        );
        let config =
            parse_text("container:\n  dev-container-path: ./.scoter/env\ncommands:\n").unwrap();
        assert_eq!(
            config.container,
            ContainerRef::Path("./.scoter/env".to_string())
        );
        assert_eq!(config.dir(), Path::new("/proj"));
    }

    #[test]
    fn container_errors() {
        assert_eq!(
            error(""),
            ": the file is empty; it needs a `container` section"
        );
        assert_eq!(error("commands: []\n"), "container: is required");
        assert_eq!(
            error("container:\n"),
            "container: needs `dev-container-path` or `dev-container`"
        );
        assert_eq!(
            error("container: {}\n"),
            "container: needs `dev-container-path` or `dev-container`"
        );
        assert_eq!(
            error("container: {dev-container: a, dev-container-path: b}\n"),
            "container: set only one of `dev-container-path` and `dev-container`",
        );
        assert_eq!(
            error("container: {dev-container-path: ''}\n"),
            "container.dev-container-path: must not be empty"
        );
        for name in ["''", "a/b", "'..'", "'.'", "'a\\b'"] {
            assert!(
                error(&format!("container: {{dev-container: {name}}}\n"))
                    .starts_with("container.dev-container: "),
                "{name}"
            );
        }
        // The old form, and unknown keys.
        assert!(error("container:\n  command: [docker]\n").contains("command"));
        assert!(error("container: [a]\n").starts_with("container: "));
    }

    #[test]
    fn top_level_errors() {
        assert_eq!(
            error("[a]\n"),
            ": must be a mapping with `container` and `commands`"
        );
        assert_eq!(
            error(&format!("{CONTAINER}extra: 1\n")),
            "extra: unknown key"
        );
        assert!(error("container: [\n").starts_with(": invalid YAML: "));
        assert_eq!(
            error(&format!("{CONTAINER}commands: {{}}\n")),
            "commands: must be a list of entries"
        );
    }

    #[test]
    fn tags_are_rejected() {
        let errors = parse_text(
            "container: !include other.yaml\ncommands:\n  - {command: a, args: [!x y]}\n",
        )
        .unwrap_err();
        assert_eq!(errors.len(), 2, "{errors:?}");
        assert!(
            errors[0].starts_with("container: YAML tags such as `!include`"),
            "{errors:?}"
        );
        assert!(errors[1].starts_with("commands[0].args[0]: "), "{errors:?}");
    }

    #[test]
    fn errors_are_gathered() {
        let errors = parse_text(concat!(
            "container: {}\n",
            "commands:\n",
            "  - {command: a/b, program: '', args: []}\n",
            "  - {command: ok, args: [5]}\n",
            "  - {command: ok, args: []}\n",
            "  - {command: ok, args: [{path: {within: workspace}}, {path: {within: cwd, keep-name: true}}]}\n",
            "extra: 1\n",
        ))
        .unwrap_err();
        assert_eq!(errors.len(), 7, "{errors:#?}");
        assert_eq!(
            errors[0],
            "container: needs `dev-container-path` or `dev-container`"
        );
        assert_eq!(errors[1], "commands[0]: 'a/b' is not a valid command name");
        assert_eq!(errors[2], "commands[0]: program must not be empty");
        assert!(
            errors[3].starts_with("commands[1]: invalid type: integer `5`"),
            "{}",
            errors[3]
        );
        assert_eq!(
            errors[4],
            "commands[3].args[0]: a path within the workspace needs `allowed` patterns"
        );
        assert_eq!(
            errors[5],
            "commands[3].args[1]: `keep-name` only applies to paths within temp"
        );
        assert_eq!(errors[6], "extra: unknown key");
    }

    #[test]
    fn load_reads_file() {
        let dir = TempDir::new();
        let path = dir.write("proj/.surf-scoter.d/project.yaml", CONTAINER);
        let project = load(&path).unwrap();
        assert_eq!(project.path, path);
        assert_eq!(project.dir(), dir.path().join("proj/.surf-scoter.d"));
    }

    #[test]
    fn load_missing_file() {
        let errors = load(Path::new("/nonexistent/.surf-scoter.d/project.yaml")).unwrap_err();
        assert_eq!(errors.len(), 1);
        assert!(
            errors[0]
                .to_string()
                .starts_with("/nonexistent/.surf-scoter.d/project.yaml: cannot read the file: "),
            "{}",
            errors[0]
        );
    }

    #[cfg(unix)]
    #[test]
    fn load_refuses_symlinks() {
        let dir = TempDir::new();
        let real = dir.write("elsewhere/project.yaml", CONTAINER);

        // The project file itself.
        fs::create_dir_all(dir.path().join("a/.surf-scoter.d")).unwrap();
        let link = dir.path().join("a/.surf-scoter.d/project.yaml");
        std::os::unix::fs::symlink(&real, &link).unwrap();
        let errors = load(&link).unwrap_err();
        assert_eq!(
            errors[0].message,
            "the file is a symlink; it must be a regular file"
        );

        // The directory holding it.
        fs::create_dir_all(dir.path().join("b")).unwrap();
        std::os::unix::fs::symlink(
            dir.path().join("elsewhere"),
            dir.path().join("b/.surf-scoter.d"),
        )
        .unwrap();
        let errors = load(&dir.path().join("b/.surf-scoter.d/project.yaml")).unwrap_err();
        assert_eq!(
            errors[0].message,
            "the project file's directory is a symlink; it must be a directory"
        );
    }

    // ---- path tokens ----

    #[test]
    fn parse_path_modes() {
        assert_eq!(
            args("[{path: {within: cwd}}]"),
            vec![Token::Path(path_opts(Within::Cwd))]
        );
        assert_eq!(
            args("[{path: {within: temp, keep-name: true}}]"),
            vec![Token::Path(PathOptions {
                keep_name: true,
                ..path_opts(Within::Temp)
            })]
        );
        assert_eq!(
            args(
                "[{path: {within: workspace, allowed: [build/**, '*.txt'], allow-leading-dash: true}}]"
            ),
            vec![Token::Path(PathOptions {
                allowed: vec![pattern("build/**"), pattern("*.txt")],
                allow_leading_dash: true,
                ..path_opts(Within::Workspace)
            }),]
        );
    }

    #[test]
    fn parse_path_errors() {
        // `within` is required and must be a known mode.
        assert!(args_error("[{path: {}}]").contains("within"));
        assert!(!args_error("[{path: {within: home}}]").is_empty());
        // Options that only apply to `any`.
        assert!(args_error("[{path: {within: cwd, prefix: /x}}]").contains("prefix"));
        assert!(args_error("[{path: {within: cwd, allow-empty: true}}]").contains("allow-empty"));
        // Bad patterns.
        assert!(
            args_error("[{path: {within: workspace, allowed: [/abs]}}]")
                .contains("must be relative to the workspace")
        );
    }

    #[test]
    fn validate_path_options() {
        assert_eq!(
            args_error("[x, {path: {within: workspace}}]"),
            "commands[0].args[1]: a path within the workspace needs `allowed` patterns",
        );
        assert_eq!(
            args_error("[{path: {within: cwd, allowed: ['**']}}]"),
            "commands[0].args[0]: `allowed` only applies to paths within the workspace",
        );
        assert_eq!(
            args_error("[{path: {within: temp, allowed: ['**']}}]"),
            "commands[0].args[0]: `allowed` only applies to paths within the workspace",
        );
        assert_eq!(
            args_error("[{path: {within: cwd, keep-name: true}}]"),
            "commands[0].args[0]: `keep-name` only applies to paths within temp",
        );
    }

    // ---- command entries ----

    #[test]
    fn parse_example_rules() {
        let config = parse_text(concat!(
            "container:\n  dev-container: dev-env\n",
            "commands:\n",
            "  - command: aws\n",
            "    args: [s3, cp, {any: {prefix: \"s3://some-name/\"}}, {path: {within: cwd}}]\n",
            "  - command: aws\n",
            "    program: /usr/local/bin/aws\n",
            "    args: [codebuild, start-build, --project-name, {any: {}}]\n",
        ))
        .unwrap();
        assert_eq!(
            config.commands,
            vec![
                CommandRule {
                    command: "aws".to_string(),
                    program: None,
                    args: vec![
                        lit("s3"),
                        lit("cp"),
                        Token::Any(opts("s3://some-name/", false, false)),
                        Token::Path(path_opts(Within::Cwd))
                    ],
                    cwd: None,
                    env: EnvRules::default(),
                    env_plan: EnvPlan::default(),
                    unavailable: false,
                },
                CommandRule {
                    command: "aws".to_string(),
                    program: Some("/usr/local/bin/aws".to_string()),
                    args: vec![
                        lit("codebuild"),
                        lit("start-build"),
                        lit("--project-name"),
                        Token::Any(MatchOptions::default())
                    ],
                    cwd: None,
                    env: EnvRules::default(),
                    env_plan: EnvPlan::default(),
                    unavailable: false,
                },
            ]
        );
    }

    #[test]
    fn parse_entry_requires_command_and_args() {
        assert!(error(&format!("{CONTAINER}commands:\n  - args: []\n")).contains("command"));
        assert!(error(&format!("{CONTAINER}commands:\n  - command: aws\n")).contains("args"));
    }

    #[test]
    fn parse_entry_rejects_unknown_keys() {
        // Including the worked-out environment, which only the tool sets.
        let err = error(&format!(
            "{CONTAINER}commands:\n  - command: aws\n    args: []\n    env-plan: {{}}\n"
        ));
        assert!(err.contains("env-plan"), "{err}");
    }

    #[test]
    fn parse_vcs() {
        let config = parse_text(&format!("{CONTAINER}vcs: git\n")).unwrap();
        assert_eq!(config.vcs.as_deref(), Some("git"));
        // Kept in the file as written, for the universal configuration file.
        assert_eq!(config.raw["vcs"], "git");
        assert_eq!(parse_text(CONTAINER).unwrap().vcs, None);
        assert_eq!(
            parse_text(&format!("{CONTAINER}vcs: ''\n")).unwrap_err(),
            ["vcs: must not be empty"]
        );
        assert_eq!(
            parse_text(&format!("{CONTAINER}vcs: [git]\n")).unwrap_err(),
            ["vcs: must be text"]
        );
        // A number or boolean isn't text either; quote it.
        assert_eq!(
            parse_text(&format!("{CONTAINER}vcs: 1\n")).unwrap_err(),
            ["vcs: must be text"]
        );
    }

    #[test]
    fn parse_env_blocks() {
        let config = parse_text(&format!(
            "{CONTAINER}env:\n  allow: [LANG]\n  set: {{HOME: '@HOME@'}}\n\
             commands:\n  - command: aws\n    args: []\n\
             \x20   env:\n      allow: ['AWS_*']\n      drop: [AWS_SESSION_TOKEN]\n"
        ))
        .unwrap();
        assert_eq!(config.env.allow, ["LANG"]);
        assert_eq!(config.env.set["HOME"], "@HOME@");
        assert_eq!(config.commands[0].env.allow, ["AWS_*"]);
        assert_eq!(config.commands[0].env.drop, ["AWS_SESSION_TOKEN"]);
        // Worked out later, against the system configuration.
        assert_eq!(config.commands[0].env_plan, EnvPlan::default());
    }

    #[test]
    fn parse_env_errors() {
        let messages = parse_text(&format!(
            "{CONTAINER}env:\n  allow: ['']\n  pass: [X]\n\
             commands:\n  - command: aws\n    args: []\n    env:\n      set: {{'A*': v}}\n"
        ))
        .unwrap_err();
        assert_eq!(messages.len(), 2, "{messages:?}");
        assert!(
            messages[0].contains("env: unknown field `pass`"),
            "{messages:?}"
        );
        assert!(
            messages[1].ends_with("commands[0].env.set.A*: is a variable name, not a pattern"),
            "{messages:?}"
        );
    }

    #[test]
    fn parse_old_map_form_is_rejected() {
        assert_eq!(
            error(&format!("{CONTAINER}commands:\n  aws:\n    - [s3]\n")),
            "commands: must be a list of entries"
        );
    }

    #[test]
    fn parse_placeholder_options_are_kebab_case() {
        assert_eq!(
            args("[{any: {allow-leading-dash: true, allow-empty: true}}]"),
            vec![Token::Any(opts("", true, true))]
        );
        assert_eq!(
            args("[{any: {prefix: /workspace/, allow-empty: true}}]"),
            vec![Token::Any(opts("/workspace/", false, true))]
        );
        assert!(args_error("[{any: {allow_leading_dash: true}}]").contains("allow_leading_dash"));
    }

    #[test]
    fn parse_empty_args_and_block_style() {
        assert_eq!(args("[]"), vec![]);
        let config = parse_text(&format!(
            "{CONTAINER}commands:\n  - command: cmd\n    args:\n      - version\n      - path:\n          within: temp\n"
        ))
        .unwrap();
        assert_eq!(
            config.commands[0].args,
            vec![lit("version"), Token::Path(path_opts(Within::Temp))]
        );
    }

    #[test]
    fn parse_rejects_bad_tokens() {
        // Numbers and booleans must be quoted, so they match exactly as written.
        assert!(args_error("[--max-items, 5]").contains("quote numbers"));
        assert!(args_error("[true]").contains("quote numbers"));
        assert_eq!(
            args("[--max-items, '5']"),
            vec![lit("--max-items"), lit("5")]
        );
        // Unknown placeholder kinds and options.
        assert!(!args_error("[{regex: {}}]").is_empty());
        assert!(!args_error("[{any: {prefx: a}}]").is_empty());
        // Exactly one kind per placeholder.
        assert!(!args_error("[{any: {}, path: {within: cwd}}]").is_empty());
        // Nested lists aren't tokens.
        assert!(!args_error("[[a]]").is_empty());
    }

    #[test]
    fn validate_command_names() {
        for name in ["''", "a/b", "/usr/bin/aws", "'.'", "'..'"] {
            let text = format!(
                "{CONTAINER}commands:\n  - command: ok\n    args: []\n  - command: {name}\n    args: []\n"
            );
            assert!(error(&text).starts_with("commands[1]: "), "{name}");
        }
    }

    #[test]
    fn validate_program() {
        let text =
            format!("{CONTAINER}commands:\n  - command: aws\n    program: ''\n    args: []\n");
        assert_eq!(error(&text), "commands[0]: program must not be empty");
        // Any non-empty program is accepted, including Windows paths.
        parse_text(&format!("{CONTAINER}commands:\n  - command: aws\n    program: 'C:\\aws\\aws.exe'\n    args: []\n")).unwrap();
    }

    // ---- cwd ----

    fn cwd_entry(cwd: &str) -> String {
        format!("{CONTAINER}commands:\n  - command: docker\n    args: []\n    cwd: {cwd}\n")
    }

    #[test]
    fn parse_cwd_section_uses_workspace_patterns() {
        let config = parse_text(&format!(
            "{CONTAINER}commands:\n  - command: docker\n    args: [compose, up]\n    cwd:\n      allowed-workspace-dirs:\n        - services/*\n        - .\n        - '**'\n",
        ))
        .unwrap();
        let cwd = config.commands[0].cwd.as_ref().unwrap();
        assert_eq!(
            cwd.allowed_workspace_dirs,
            vec![pattern("services/*"), pattern("."), pattern("**")]
        );
        assert_eq!(cwd.allowed_workspace_dirs[0].text, "services/*");
    }

    #[test]
    fn parse_cwd_errors() {
        assert!(
            error(&cwd_entry("{allowed-workspace-dirs: ['/abs/path']}"))
                .contains("must be relative to the workspace")
        );
        assert!(
            error(&cwd_entry("{allowed-workspace-dirs: ['{workspace}/x']}"))
                .contains("leave out {workspace}")
        );
        // A bare {...} is a YAML map, not a pattern.
        assert!(
            error(&cwd_entry("{allowed-workspace-dirs: [{a: b}]}"))
                .contains("a directory pattern relative to the workspace")
        );
        // The old key, and the snake_case spelling, are unknown.
        assert!(!error(&cwd_entry("{allowed: [services]}")).is_empty());
        assert!(!error(&cwd_entry("{allowed_workspace_dirs: [services]}")).is_empty());
        assert!(!error(&cwd_entry("{}")).is_empty());
        assert_eq!(
            error(&cwd_entry("{allowed-workspace-dirs: []}")),
            "commands[0]: cwd.allowed-workspace-dirs must not be empty",
        );
    }
}
