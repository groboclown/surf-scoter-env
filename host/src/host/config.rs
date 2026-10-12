//! Host configuration, read from a YAML file.
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
//! container:
//!   # argv prefix used to run the broker; e.g. [podman] or [docker, --context, remote]
//!   command: [docker]
//!   # optional; path of the broker inside the container
//!   broker: /usr/local/bin/scoter-container
//!
//! # What clients may run: one entry per allowed form of a command.
//! commands:
//!   - command: aws
//!     args: [s3, cp, {any: {prefix: "s3://some-name/"}}, {path: {within: cwd}}]
//!   - command: aws
//!     # optional; the executable to run on the host (default: `command`, found on the PATH)
//!     program: /usr/local/bin/aws
//!     args: [codebuild, start-build, --project-name, {any: {}}]
//!   - command: docker
//!     args: [compose, up]
//!     # optional; run in the client's directory, when it matches one of these
//!     cwd:
//!       allowed-workspace-dirs:
//!         - services/*
//!         - infra/**
//! ```
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
use std::fs;
use std::io;
use std::path::{Path, PathBuf};

use serde::de::{self, MapAccess, Visitor};
use serde::{Deserialize, Deserializer};


pub(crate) const DEFAULT_BROKER: &str = "/usr/local/bin/scoter-container";


#[derive(Debug, PartialEq, Eq, Deserialize)]
#[serde(deny_unknown_fields, rename_all = "kebab-case")]
pub(crate) struct Config {
    pub container: ContainerConfig,
    /// The allowed forms of commands, in match order.  Anything not listed is refused.
    #[serde(default)]
    pub commands: Commands,
}

pub(crate) type Commands = Vec<CommandRule>;

/// One allowed form of a command.
#[derive(Debug, PartialEq, Eq, Deserialize)]
#[serde(deny_unknown_fields, rename_all = "kebab-case")]
pub(crate) struct CommandRule {
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
}

#[derive(Debug, PartialEq, Eq, Deserialize)]
#[serde(deny_unknown_fields, rename_all = "kebab-case")]
pub(crate) struct CwdRule {
    /// The client's directory must match one of these.
    pub allowed_workspace_dirs: Vec<WorkspacePattern>,
}

/// A directory pattern relative to the workspace, such as `infra/**`.
#[derive(Debug, PartialEq, Eq)]
pub(crate) struct WorkspacePattern {
    /// As written in the configuration.
    pub text: String,
    /// One part per directory name below the workspace.
    parts: Vec<PatternPart>,
}

#[derive(Debug, PartialEq, Eq)]
enum PatternPart {
    /// `**`: any number of directories, including none.
    AnyDirs,
    /// One directory name, which may use `*` and `?`.
    Name(String),
}

impl WorkspacePattern {
    pub(crate) fn parse(text: &str) -> Result<Self, String> {
        if text.is_empty() {
            return Err("a workspace pattern must not be empty; use '.' for the workspace itself".to_string());
        }
        if text.starts_with('/') {
            return Err(format!(
                "workspace pattern '{text}' must be relative to the workspace; use '.' for the workspace itself"
            ));
        }
        if text.contains("{workspace}") {
            return Err(format!(
                "workspace pattern '{text}' is already relative to the workspace; leave out {{workspace}}"
            ));
        }
        let mut parts: Vec<PatternPart> = Vec::new();
        for name in text.split('/').filter(|n| !n.is_empty() && *n != ".") {
            let part = match name {
                ".." => return Err(format!("workspace pattern '{text}' must not contain '..'")),
                "**" => PatternPart::AnyDirs,
                _ if name.contains("**") => {
                    return Err(format!("workspace pattern '{text}': '**' must be a whole directory name"));
                }
                _ => PatternPart::Name(name.to_string()),
            };
            // Repeated `**` mean the same as one.
            if part == PatternPart::AnyDirs && parts.last() == Some(&PatternPart::AnyDirs) {
                continue;
            }
            parts.push(part);
        }
        Ok(WorkspacePattern { text: text.to_string(), parts })
    }

    /// Whether a directory, given as its names below the workspace, matches.
    pub(crate) fn matches(&self, dirs: &[&str]) -> bool {
        match_parts(&self.parts, dirs)
    }
}

fn match_parts(parts: &[PatternPart], dirs: &[&str]) -> bool {
    match parts.split_first() {
        None => dirs.is_empty(),
        Some((PatternPart::AnyDirs, rest)) => (0..=dirs.len()).any(|skip| match_parts(rest, &dirs[skip..])),
        Some((PatternPart::Name(glob), rest)) => match dirs.split_first() {
            Some((dir, dirs)) => glob_matches(glob, dir) && match_parts(rest, dirs),
            None => false,
        },
    }
}

/// Match one name against a glob of `*` (any characters) and `?` (one character).
fn glob_matches(glob: &str, name: &str) -> bool {
    let glob: Vec<char> = glob.chars().collect();
    let name: Vec<char> = name.chars().collect();
    let (mut g, mut n) = (0, 0);
    // Where to resume after the last `*`: the glob position after it, and the name position it's covering up to.
    let mut star: Option<(usize, usize)> = None;
    while n < name.len() {
        if g < glob.len() && (glob[g] == '?' || glob[g] == name[n]) {
            g += 1;
            n += 1;
        } else if g < glob.len() && glob[g] == '*' {
            star = Some((g + 1, n));
            g += 1;
        } else if let Some((after_star, covered)) = star {
            // Let the `*` cover one more character.
            g = after_star;
            n = covered + 1;
            star = Some((after_star, covered + 1));
        } else {
            return false;
        }
    }
    glob[g..].iter().all(|&c| c == '*')
}

impl<'de> Deserialize<'de> for WorkspacePattern {
    fn deserialize<D: Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        struct PatternVisitor;

        impl Visitor<'_> for PatternVisitor {
            type Value = WorkspacePattern;

            fn expecting(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
                f.write_str("a directory pattern relative to the workspace, such as infra/**")
            }

            fn visit_str<E: de::Error>(self, v: &str) -> Result<WorkspacePattern, E> {
                WorkspacePattern::parse(v).map_err(E::custom)
            }
        }

        deserializer.deserialize_str(PatternVisitor)
    }
}

#[derive(Debug, PartialEq, Eq)]
pub(crate) enum Token {
    /// The argument must be exactly this.
    Literal(String),
    /// Any argument accepted by the options.
    Any(MatchOptions),
    /// A file path, mapped to the host.
    Path(PathOptions),
}

#[derive(Debug, Default, PartialEq, Eq, Deserialize)]
#[serde(deny_unknown_fields, rename_all = "kebab-case")]
pub(crate) struct MatchOptions {
    #[serde(default)]
    pub prefix: String,
    #[serde(default)]
    pub allow_leading_dash: bool,
    #[serde(default)]
    pub allow_empty: bool,
}

#[derive(Debug, PartialEq, Eq, Deserialize)]
#[serde(deny_unknown_fields, rename_all = "kebab-case")]
pub(crate) struct PathOptions {
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
pub(crate) enum Within {
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
                Ok(match Placeholder::deserialize(de::value::MapAccessDeserializer::new(map))? {
                    Placeholder::Any(options) => Token::Any(options),
                    Placeholder::Path(options) => Token::Path(options),
                })
            }
        }

        deserializer.deserialize_any(TokenVisitor)
    }
}

#[derive(Debug, PartialEq, Eq, Deserialize)]
#[serde(deny_unknown_fields, rename_all = "kebab-case")]
pub(crate) struct ContainerConfig {
    /// The container tool and any leading arguments, e.g. `[docker]`.
    pub command: Vec<String>,
    /// Absolute path of the broker executable inside the container.
    #[serde(default = "default_broker")]
    pub broker: String,
}

fn default_broker() -> String {
    DEFAULT_BROKER.to_string()
}


#[derive(Debug)]
pub(crate) enum ConfigError {
    Read(io::Error),
    Parse(yaml_serde::Error),
    Invalid(String),
}

impl fmt::Display for ConfigError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            ConfigError::Read(e) => write!(f, "cannot read the configuration: {e}"),
            ConfigError::Parse(e) => write!(f, "invalid configuration: {e}"),
            ConfigError::Invalid(msg) => write!(f, "invalid configuration: {msg}"),
        }
    }
}


/// The workspace directory, shared between the host and the container.
/// Given on the command line, not in the configuration file.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct Workspace {
    /// Canonical path of the directory on the host.
    pub host: PathBuf,
    /// Absolute path where it's mounted in the container, without a trailing '/'.
    pub container: String,
}

impl Workspace {
    /// Parse `HOST_DIR:CONTAINER_PATH`, split at the last ':' (so a Windows
    /// drive letter stays with the host directory).  The host directory must exist.
    pub(crate) fn parse(spec: &str) -> Result<Self, String> {
        let Some((host, container)) = spec.rsplit_once(':') else {
            return Err(format!("'{spec}' must be HOST_DIR:CONTAINER_PATH"));
        };
        if host.is_empty() {
            return Err(format!("'{spec}' is missing the host directory"));
        }
        let container = normalize_container_path(container)?;
        let host_dir = fs::canonicalize(host).map_err(|e| format!("host directory '{host}': {e}"))?;
        if !host_dir.is_dir() {
            return Err(format!("host directory '{host}' is not a directory"));
        }
        Ok(Workspace { host: host_dir, container })
    }
}

/// Check that `path` is an absolute container path, and drop empty and `.` components.
fn normalize_container_path(path: &str) -> Result<String, String> {
    if !path.starts_with('/') {
        return Err(format!("container path '{path}' must be absolute"));
    }
    let parts: Vec<&str> = path.split('/').filter(|p| !p.is_empty() && *p != ".").collect();
    if parts.contains(&"..") {
        return Err(format!("container path '{path}' must not contain '..'"));
    }
    if parts.is_empty() {
        return Err("the container path can't be '/'".to_string());
    }
    Ok(format!("/{}", parts.join("/")))
}


/// Read and validate the configuration file at `path`.
pub(crate) fn load(path: &Path) -> Result<Config, ConfigError> {
    let text = fs::read_to_string(path).map_err(ConfigError::Read)?;
    parse(&text)
}

/// Parse and validate configuration text.
pub(crate) fn parse(text: &str) -> Result<Config, ConfigError> {
    let config: Config = yaml_serde::from_str(text).map_err(ConfigError::Parse)?;
    config.validate()?;
    Ok(config)
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

impl Config {
    fn validate(&self) -> Result<(), ConfigError> {
        match self.container.command.first() {
            None => return Err(ConfigError::Invalid("container.command must not be empty".to_string())),
            Some(program) if program.is_empty() => {
                return Err(ConfigError::Invalid("container.command must start with a program name".to_string()));
            }
            Some(_) => {}
        }
        if !self.container.broker.starts_with('/') {
            return Err(ConfigError::Invalid("container.broker must be an absolute path".to_string()));
        }
        for (i, rule) in self.commands.iter().enumerate() {
            // Clients are matched by the file name they were invoked as.
            let name = &rule.command;
            if name.is_empty() || name.contains('/') || name == "." || name == ".." {
                return Err(ConfigError::Invalid(format!("commands[{i}]: '{name}' is not a valid command name")));
            }
            if rule.program.as_ref().is_some_and(|p| p.is_empty()) {
                return Err(ConfigError::Invalid(format!("commands[{i}]: program must not be empty")));
            }
            if rule.cwd.as_ref().is_some_and(|cwd| cwd.allowed_workspace_dirs.is_empty()) {
                return Err(ConfigError::Invalid(format!("commands[{i}]: cwd.allowed-workspace-dirs must not be empty")));
            }
            for (j, token) in rule.args.iter().enumerate() {
                if let Token::Path(options) = token {
                    options.validate().map_err(|msg| ConfigError::Invalid(format!("commands[{i}].args[{j}]: {msg}")))?;
                }
            }
        }
        Ok(())
    }
}


#[cfg(test)]
mod tests {
    use super::*;

    fn invalid_message(text: &str) -> String {
        match parse(text) {
            Err(ConfigError::Invalid(msg)) => msg,
            other => panic!("expected an invalid config, got {other:?}"),
        }
    }

    const CONTAINER: &str = "container:\n  command: [docker]\n";

    /// Parse a single entry's `args` (flow style).
    fn args(yaml_args: &str) -> Vec<Token> {
        let mut config = parse(&format!("{CONTAINER}commands:\n  - command: cmd\n    args: {yaml_args}\n")).unwrap();
        assert_eq!(config.commands.len(), 1);
        config.commands.remove(0).args
    }

    fn parse_error(text: &str) -> String {
        match parse(text) {
            Err(ConfigError::Parse(e)) => e.to_string(),
            other => panic!("expected a parse error, got {other:?}"),
        }
    }

    fn args_error(yaml_args: &str) -> String {
        parse_error(&format!("{CONTAINER}commands:\n  - command: cmd\n    args: {yaml_args}\n"))
    }

    fn lit(s: &str) -> Token {
        Token::Literal(s.to_string())
    }

    fn opts(prefix: &str, allow_leading_dash: bool, allow_empty: bool) -> MatchOptions {
        MatchOptions { prefix: prefix.to_string(), allow_leading_dash, allow_empty }
    }

    fn path_opts(within: Within) -> PathOptions {
        PathOptions { within, allowed: vec![], keep_name: false, allow_leading_dash: false }
    }

    fn args_invalid(yaml_args: &str) -> String {
        invalid_message(&format!("{CONTAINER}commands:\n  - command: cmd\n    args: {yaml_args}\n"))
    }

    // ---- path tokens ----

    #[test]
    fn parse_path_modes() {
        assert_eq!(args("[{path: {within: cwd}}]"), vec![Token::Path(path_opts(Within::Cwd))]);
        assert_eq!(args("[{path: {within: temp, keep-name: true}}]"), vec![Token::Path(PathOptions {
            keep_name: true,
            ..path_opts(Within::Temp)
        })]);
        assert_eq!(args("[{path: {within: workspace, allowed: [build/**, '*.txt'], allow-leading-dash: true}}]"), vec![
            Token::Path(PathOptions {
                allowed: vec![WorkspacePattern::parse("build/**").unwrap(), WorkspacePattern::parse("*.txt").unwrap()],
                allow_leading_dash: true,
                ..path_opts(Within::Workspace)
            }),
        ]);
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
        assert!(args_error("[{path: {within: workspace, allowed: [/abs]}}]").contains("must be relative to the workspace"));
    }

    #[test]
    fn validate_path_options() {
        assert_eq!(
            args_invalid("[x, {path: {within: workspace}}]"),
            "commands[0].args[1]: a path within the workspace needs `allowed` patterns",
        );
        assert_eq!(
            args_invalid("[{path: {within: cwd, allowed: ['**']}}]"),
            "commands[0].args[0]: `allowed` only applies to paths within the workspace",
        );
        assert_eq!(
            args_invalid("[{path: {within: temp, allowed: ['**']}}]"),
            "commands[0].args[0]: `allowed` only applies to paths within the workspace",
        );
        assert_eq!(
            args_invalid("[{path: {within: cwd, keep-name: true}}]"),
            "commands[0].args[0]: `keep-name` only applies to paths within temp",
        );
    }

    #[test]
    fn parse_minimal() {
        let config = parse(CONTAINER).unwrap();
        assert_eq!(config, Config {
            container: ContainerConfig {
                command: vec!["docker".to_string()],
                broker: DEFAULT_BROKER.to_string(),
            },
            commands: Commands::new(),
        });
    }

    #[test]
    fn parse_example_rules() {
        let config = parse(concat!(
            "container:\n  command: [docker]\n",
            "commands:\n",
            "  - command: aws\n",
            "    args: [s3, cp, {any: {prefix: \"s3://some-name/\"}}, {path: {within: cwd}}]\n",
            "  - command: aws\n",
            "    program: /usr/local/bin/aws\n",
            "    args: [codebuild, start-build, --project-name, {any: {}}]\n",
        ))
        .unwrap();
        assert_eq!(config.commands, vec![
            CommandRule {
                command: "aws".to_string(),
                program: None,
                args: vec![lit("s3"), lit("cp"), Token::Any(opts("s3://some-name/", false, false)), Token::Path(path_opts(Within::Cwd))],
                cwd: None,
            },
            CommandRule {
                command: "aws".to_string(),
                program: Some("/usr/local/bin/aws".to_string()),
                args: vec![lit("codebuild"), lit("start-build"), lit("--project-name"), Token::Any(MatchOptions::default())],
                cwd: None,
            },
        ]);
    }

    #[test]
    fn parse_entry_requires_command_and_args() {
        assert!(parse_error(&format!("{CONTAINER}commands:\n  - args: []\n")).contains("command"));
        assert!(parse_error(&format!("{CONTAINER}commands:\n  - command: aws\n")).contains("args"));
    }

    #[test]
    fn parse_entry_rejects_unknown_keys() {
        let err = parse_error(&format!("{CONTAINER}commands:\n  - command: aws\n    args: []\n    env: {{}}\n"));
        assert!(err.contains("env"), "{err}");
    }

    #[test]
    fn parse_old_map_form_is_rejected() {
        assert!(!parse_error(&format!("{CONTAINER}commands:\n  aws:\n    - [s3]\n")).is_empty());
    }

    #[test]
    fn parse_placeholder_options_are_kebab_case() {
        assert_eq!(args("[{any: {allow-leading-dash: true, allow-empty: true}}]"), vec![Token::Any(opts("", true, true))]);
        assert_eq!(args("[{any: {prefix: /workspace/, allow-empty: true}}]"), vec![Token::Any(opts("/workspace/", false, true))]);
        assert!(args_error("[{any: {allow_leading_dash: true}}]").contains("allow_leading_dash"));
    }

    #[test]
    fn parse_empty_args_and_block_style() {
        assert_eq!(args("[]"), vec![]);
        let config = parse(&format!("{CONTAINER}commands:\n  - command: cmd\n    args:\n      - version\n      - path:\n          within: temp\n")).unwrap();
        assert_eq!(config.commands[0].args, vec![lit("version"), Token::Path(path_opts(Within::Temp))]);
    }

    #[test]
    fn parse_rejects_bad_tokens() {
        // Numbers and booleans must be quoted, so they match exactly as written.
        assert!(args_error("[--max-items, 5]").contains("quote numbers"));
        assert!(args_error("[true]").contains("quote numbers"));
        assert_eq!(args("[--max-items, '5']"), vec![lit("--max-items"), lit("5")]);
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
            let text = format!("{CONTAINER}commands:\n  - command: ok\n    args: []\n  - command: {name}\n    args: []\n");
            match parse(&text) {
                Err(ConfigError::Invalid(msg)) => assert!(msg.starts_with("commands[1]: "), "{msg}"),
                other => panic!("{name}: expected invalid, got {other:?}"),
            }
        }
    }

    #[test]
    fn validate_program() {
        let text = format!("{CONTAINER}commands:\n  - command: aws\n    program: ''\n    args: []\n");
        assert_eq!(invalid_message(&text), "commands[0]: program must not be empty");
        // Any non-empty program is accepted, including Windows paths.
        parse(&format!("{CONTAINER}commands:\n  - command: aws\n    program: 'C:\\aws\\aws.exe'\n    args: []\n")).unwrap();
    }

    #[test]
    fn parse_full() {
        let config = parse(
            "container:\n  command:\n    - docker\n    - --context\n    - remote\n  broker: /opt/scoter/broker\n",
        )
        .unwrap();
        assert_eq!(config.container.command, vec!["docker", "--context", "remote"]);
        assert_eq!(config.container.broker, "/opt/scoter/broker");
    }

    #[test]
    fn parse_rejects_unknown_keys() {
        assert!(matches!(parse("container:\n  command: [docker]\n  comand: [podman]\n"), Err(ConfigError::Parse(_))));
        assert!(matches!(parse("container:\n  command: [docker]\nextra: 1\n"), Err(ConfigError::Parse(_))));
    }

    #[test]
    fn parse_requires_fields() {
        assert!(matches!(parse(""), Err(ConfigError::Parse(_))));
        assert!(matches!(parse("container: {}\n"), Err(ConfigError::Parse(_))));
    }

    #[test]
    fn parse_rejects_wrong_types() {
        assert!(matches!(parse("container:\n  command: docker\n"), Err(ConfigError::Parse(_))));
        assert!(matches!(parse("container:\n  command: [docker]\n  broker: [a]\n"), Err(ConfigError::Parse(_))));
    }

    #[test]
    fn parse_rejects_malformed_yaml() {
        assert!(matches!(parse("container: [\n"), Err(ConfigError::Parse(_))));
    }

    #[test]
    fn validate_command() {
        assert!(invalid_message("container:\n  command: []\n").contains("container.command"));
        assert!(invalid_message("container:\n  command: ['']\n").contains("container.command"));
    }

    #[test]
    fn validate_broker_is_absolute() {
        let msg = invalid_message("container:\n  command: [docker]\n  broker: scoter-container\n");
        assert!(msg.contains("container.broker"));
    }

    #[test]
    fn load_reads_file() {
        let path = std::env::temp_dir().join(format!("scoter-host-config-test-{}.yaml", std::process::id()));
        fs::write(&path, "container:\n  command: [podman]\n").unwrap();
        let config = load(&path);
        fs::remove_file(&path).unwrap();
        assert_eq!(config.unwrap().container.command, vec!["podman"]);
    }

    #[test]
    fn load_missing_file() {
        let err = load(Path::new("/nonexistent/scoter/host.yaml")).unwrap_err();
        assert!(matches!(&err, ConfigError::Read(e) if e.kind() == io::ErrorKind::NotFound));
        assert!(err.to_string().starts_with("cannot read the configuration"));
    }

    #[test]
    fn error_messages() {
        assert!(parse("container: [\n").unwrap_err().to_string().starts_with("invalid configuration: "));
        assert_eq!(
            parse("container:\n  command: []\n").unwrap_err().to_string(),
            "invalid configuration: container.command must not be empty",
        );
    }

    // ---- Workspace ----

    /// A canonical, existing directory to use as the host side.
    fn host_dir() -> PathBuf {
        std::env::temp_dir().canonicalize().unwrap()
    }

    fn workspace_error(spec: &str) -> String {
        Workspace::parse(spec).unwrap_err()
    }

    #[test]
    fn workspace_parse() {
        let host = host_dir();
        let spec = format!("{}:/workspace", host.display());
        assert_eq!(Workspace::parse(&spec), Ok(Workspace { host: host.clone(), container: "/workspace".to_string() }));
    }

    #[test]
    fn workspace_host_is_canonical() {
        let host = host_dir();
        let spec = format!("{}/./:/workspace", host.display());
        assert_eq!(Workspace::parse(&spec).unwrap().host, host);
    }

    #[test]
    fn workspace_container_path_is_normalized() {
        let host = host_dir();
        for (given, expected) in [("/workspace/", "/workspace"), ("//a/./b//", "/a/b"), ("/a", "/a")] {
            let spec = format!("{}:{given}", host.display());
            assert_eq!(Workspace::parse(&spec).unwrap().container, expected, "{given}");
        }
    }

    #[test]
    fn workspace_splits_at_last_colon() {
        // A Windows-style host path keeps its drive letter.
        let err = workspace_error("C:\\ws:/workspace");
        assert!(err.starts_with("host directory 'C:\\ws'"), "{err}");
    }

    #[test]
    fn workspace_syntax_errors() {
        let host = host_dir().display().to_string();
        assert_eq!(workspace_error("/workspace"), "'/workspace' must be HOST_DIR:CONTAINER_PATH");
        assert_eq!(workspace_error(":/workspace"), "':/workspace' is missing the host directory");
        assert_eq!(workspace_error(&format!("{host}:")), "container path '' must be absolute");
        assert_eq!(workspace_error(&format!("{host}:workspace")), "container path 'workspace' must be absolute");
        assert_eq!(workspace_error(&format!("{host}:/a/../b")), "container path '/a/../b' must not contain '..'");
        assert_eq!(workspace_error(&format!("{host}:/")), "the container path can't be '/'");
        assert_eq!(workspace_error(&format!("{host}:/./")), "the container path can't be '/'");
    }

    #[test]
    fn workspace_host_must_be_existing_directory() {
        assert!(workspace_error("/nonexistent/scoter-ws:/workspace").starts_with("host directory '/nonexistent/scoter-ws': "));
        let file = std::env::temp_dir().join(format!("scoter-host-ws-file-{}", std::process::id()));
        fs::write(&file, b"").unwrap();
        let err = workspace_error(&format!("{}:/workspace", file.display()));
        fs::remove_file(&file).unwrap();
        assert!(err.ends_with("is not a directory"), "{err}");
    }

    // ---- cwd ----

    fn pattern(text: &str) -> WorkspacePattern {
        WorkspacePattern::parse(text).unwrap()
    }

    fn matches(text: &str, dir: &str) -> bool {
        let dirs: Vec<&str> = dir.split('/').filter(|d| !d.is_empty()).collect();
        pattern(text).matches(&dirs)
    }

    fn cwd_entry(cwd: &str) -> String {
        format!("{CONTAINER}commands:\n  - command: docker\n    args: []\n    cwd: {cwd}\n")
    }

    #[test]
    fn parse_cwd_section_uses_workspace_patterns() {
        let config = parse(&format!(
            "{CONTAINER}commands:\n  - command: docker\n    args: [compose, up]\n    cwd:\n      allowed-workspace-dirs:\n        - services/*\n        - .\n        - '**'\n",
        ))
        .unwrap();
        let cwd = config.commands[0].cwd.as_ref().unwrap();
        assert_eq!(cwd.allowed_workspace_dirs, vec![pattern("services/*"), pattern("."), pattern("**")]);
        assert_eq!(cwd.allowed_workspace_dirs[0].text, "services/*");
    }

    #[test]
    fn parse_cwd_errors() {
        assert!(parse_error(&cwd_entry("{allowed-workspace-dirs: ['/abs/path']}")).contains("must be relative to the workspace"));
        assert!(parse_error(&cwd_entry("{allowed-workspace-dirs: ['{workspace}/x']}")).contains("leave out {workspace}"));
        // A bare {...} is a YAML map, not a pattern.
        assert!(parse_error(&cwd_entry("{allowed-workspace-dirs: [{a: b}]}")).contains("a directory pattern relative to the workspace"));
        // The old key, and the snake_case spelling, are unknown.
        assert!(!parse_error(&cwd_entry("{allowed: [services]}")).is_empty());
        assert!(!parse_error(&cwd_entry("{allowed_workspace_dirs: [services]}")).is_empty());
        assert!(!parse_error(&cwd_entry("{}")).is_empty());
        assert_eq!(
            invalid_message(&cwd_entry("{allowed-workspace-dirs: []}")),
            "commands[0]: cwd.allowed-workspace-dirs must not be empty",
        );
    }

    #[test]
    fn pattern_syntax_errors() {
        let err = |text: &str| WorkspacePattern::parse(text).unwrap_err();
        assert_eq!(err(""), "a workspace pattern must not be empty; use '.' for the workspace itself");
        assert_eq!(
            err("/services"),
            "workspace pattern '/services' must be relative to the workspace; use '.' for the workspace itself",
        );
        assert_eq!(err("../x"), "workspace pattern '../x' must not contain '..'");
        assert_eq!(err("a/../b"), "workspace pattern 'a/../b' must not contain '..'");
        assert_eq!(err("a**"), "workspace pattern 'a**': '**' must be a whole directory name");
        assert!(err("{workspace}").contains("leave out {workspace}"));
    }

    #[test]
    fn pattern_workspace_root() {
        assert!(matches(".", ""));
        assert!(matches("./", ""));
        assert!(!matches(".", "a"));
        assert!(matches("**", ""));
        assert!(matches("**", "anything/at/all"));
    }

    #[test]
    fn pattern_literal_names() {
        assert!(matches("services/api", "services/api"));
        assert!(matches("./services/api", "services/api"));
        assert!(matches("services//./api/", "services/api"));
        assert!(!matches("services/api", "services"));
        assert!(!matches("services/api", "services/api/v2"));
        assert!(!matches("services/api", "Services/api"));
        // Braces are ordinary characters now.
        assert!(matches("{x}", "{x}"));
    }

    #[test]
    fn pattern_star_stays_within_a_name() {
        assert!(matches("services/*", "services/api"));
        assert!(matches("services/*", "services/.hidden"));
        assert!(!matches("services/*", "services"));
        assert!(!matches("services/*", "services/api/v2"));
        assert!(matches("*/api", "services/api"));
        assert!(!matches("*", ""));
        assert!(matches("svc-*", "svc-a"));
        assert!(matches("svc-*", "svc-"));
        assert!(!matches("svc-*", "svc"));
        assert!(matches("*-app-*", "my-app-v2"));
        assert!(!matches("*-app-*", "my-ap-v2"));
    }

    #[test]
    fn pattern_question_mark() {
        assert!(matches("v?", "v1"));
        assert!(!matches("v?", "v"));
        assert!(!matches("v?", "v10"));
        assert!(matches("??", "ün"));
    }

    #[test]
    fn pattern_double_star() {
        let p = "infra/**";
        assert!(matches(p, "infra"));
        assert!(matches(p, "infra/a"));
        assert!(matches(p, "infra/a/b/c"));
        assert!(!matches(p, ""));
        assert!(!matches(p, "other/infra"));
        assert!(matches("**/deploy", "deploy"));
        assert!(matches("**/deploy", "a/b/deploy"));
        assert!(!matches("**/deploy", "a/b/deploy/x"));
        assert!(matches("**/svc-*/**", "x/svc-a/y/z"));
        assert!(matches("a/**/**/b", "a/b"));
    }

    #[test]
    fn glob_backtracking() {
        assert!(glob_matches("*a*b", "xaxxab"));
        assert!(glob_matches("a*a*a", "aaaaa"));
        assert!(!glob_matches("a*a*a", "aa"));
        assert!(glob_matches("*", ""));
        assert!(glob_matches("**x", "x"));
        assert!(!glob_matches("", "a"));
        assert!(glob_matches("", ""));
    }
}
