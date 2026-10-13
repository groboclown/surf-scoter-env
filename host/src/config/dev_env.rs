//! The dev container: a directory holding its `Dockerfile` and a
//! `dev-env.yaml` describing how to run it.  See `docs/project-dockerfile.md`.
//!
//! ```yaml
//! user: ubuntu
//! mounts:
//!     workspace: /workspace
//!     setup: /setup
//! broker-bin: /usr/local/bin/scoter-container
//! sshd-port: 22022
//! ```
//!
//! Other keys are allowed, and kept in `DevEnv::raw`.

use std::path::{Path, PathBuf};

use yaml_serde::{Mapping, Value};

use super::project::ContainerRef;
use super::read::{parse_yaml, read_limited, tag_errors};
use super::{ConfigError, child_key};

pub const DOCKERFILE: &str = "Dockerfile";
pub const DEV_ENV_FILE: &str = "dev-env.yaml";

/// A dev container directory.
#[derive(Debug, Clone, PartialEq)]
pub struct DevContainer {
    pub dir: PathBuf,
    pub env: DevEnv,
}

impl DevContainer {
    pub fn dockerfile(&self) -> PathBuf {
        self.dir.join(DOCKERFILE)
    }

    pub fn env_file(&self) -> PathBuf {
        self.dir.join(DEV_ENV_FILE)
    }
}

/// The contents of `dev-env.yaml`.
#[derive(Debug, Clone, PartialEq)]
pub struct DevEnv {
    /// The user the container runs as, and that `sshd` accepts.
    pub user: String,
    pub mounts: Mounts,
    /// Absolute path of the broker executable inside the container.
    pub broker_bin: String,
    /// The port `sshd` listens on, on the container's loopback interface.
    pub sshd_port: u16,
    /// The whole file, as read.
    pub raw: Value,
}

/// Where directories are mounted in the container.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Mounts {
    pub workspace: String,
    pub setup: String,
}

/// Find the project's dev container.  `project` is the project file, which
/// `dev-container-path` is relative to; `lookup` holds the directories to
/// search for a `dev-container` name.
pub fn find(
    reference: &ContainerRef,
    project: &Path,
    lookup: &[PathBuf],
) -> Result<DevContainer, Vec<ConfigError>> {
    match reference {
        ContainerRef::Path(path) => {
            let base = project.parent().unwrap_or(Path::new(""));
            load(&base.join(path))
        }
        ContainerRef::Name(name) => {
            for dir in lookup {
                let candidate = dir.join(name);
                if candidate.join(DOCKERFILE).is_file() && candidate.join(DEV_ENV_FILE).is_file() {
                    return load(&candidate);
                }
            }
            let message = if lookup.is_empty() {
                format!(
                    "can't find dev container '{name}': the system configuration sets no `dev-container-paths`"
                )
            } else {
                let searched: Vec<String> =
                    lookup.iter().map(|d| d.display().to_string()).collect();
                format!(
                    "can't find dev container '{name}' (a directory with {DOCKERFILE} and {DEV_ENV_FILE}) in: {}",
                    searched.join(", ")
                )
            };
            Err(vec![ConfigError::new(
                Some(project),
                "container.dev-container",
                message,
            )])
        }
    }
}

/// Read the dev container in `dir`.
pub fn load(dir: &Path) -> Result<DevContainer, Vec<ConfigError>> {
    if !dir.is_dir() {
        return Err(vec![ConfigError::new(
            Some(dir),
            "",
            "the dev container directory doesn't exist",
        )]);
    }
    let mut errors = Vec::new();
    if !dir.join(DOCKERFILE).is_file() {
        errors.push(ConfigError::new(
            Some(dir),
            "",
            format!("the dev container has no {DOCKERFILE}"),
        ));
    }
    let env_file = dir.join(DEV_ENV_FILE);
    let env = read_limited(&env_file)
        .and_then(|text| parse_yaml(&text))
        .map_err(|m| vec![ConfigError::new(Some(&env_file), "", m)])
        .and_then(|value| parse(value, &env_file));
    // Canonical, so it reads well in messages and the universal configuration.
    let dir = dir.canonicalize().unwrap_or_else(|_| dir.to_path_buf());
    let dir = dir.as_path();
    match env {
        Ok(env) if errors.is_empty() => Ok(DevContainer {
            dir: dir.to_path_buf(),
            env,
        }),
        Ok(_) => Err(errors),
        Err(e) => {
            errors.extend(e);
            Err(errors)
        }
    }
}

/// Check a parsed `dev-env.yaml`; `file` is where it came from.
pub fn parse(value: Value, file: &Path) -> Result<DevEnv, Vec<ConfigError>> {
    let error = |key: &str, message: String| ConfigError::new(Some(file), key, message);
    let tags = tag_errors(&value);
    if !tags.is_empty() {
        return Err(tags
            .into_iter()
            .map(|(key, message)| error(&key, message))
            .collect());
    }
    let empty = Mapping::new();
    let map = match &value {
        Value::Mapping(map) => map,
        Value::Null => &empty,
        _ => return Err(vec![error("", "must be a mapping".to_string())]),
    };

    let mut problems: Vec<(String, String)> = Vec::new();
    let user = text_field(map, "", "user", false, &mut problems);
    let (workspace, setup) = match map.get("mounts") {
        Some(Value::Mapping(mounts)) => (
            text_field(mounts, "mounts", "workspace", true, &mut problems),
            text_field(mounts, "mounts", "setup", true, &mut problems),
        ),
        Some(_) => {
            problems.push((
                "mounts".to_string(),
                "must be a mapping with `workspace` and `setup`".to_string(),
            ));
            (None, None)
        }
        None => {
            problems.push(("mounts".to_string(), "is required".to_string()));
            (None, None)
        }
    };
    let broker_bin = text_field(map, "", "broker-bin", true, &mut problems);
    let sshd_port = match map.get("sshd-port") {
        Some(Value::Number(n)) => match n
            .as_u64()
            .and_then(|n| u16::try_from(n).ok())
            .filter(|n| *n > 0)
        {
            Some(port) => Some(port),
            None => {
                problems.push((
                    "sshd-port".to_string(),
                    "must be a port number, 1 to 65535".to_string(),
                ));
                None
            }
        },
        Some(_) => {
            problems.push((
                "sshd-port".to_string(),
                "must be a port number, 1 to 65535".to_string(),
            ));
            None
        }
        None => {
            problems.push(("sshd-port".to_string(), "is required".to_string()));
            None
        }
    };

    match (user, workspace, setup, broker_bin, sshd_port) {
        (Some(user), Some(workspace), Some(setup), Some(broker_bin), Some(sshd_port))
            if problems.is_empty() =>
        {
            Ok(DevEnv {
                user,
                mounts: Mounts { workspace, setup },
                broker_bin,
                sshd_port,
                raw: value,
            })
        }
        _ => Err(problems
            .into_iter()
            .map(|(key, message)| error(&key, message))
            .collect()),
    }
}

/// A required, non-empty string; `absolute` requires an absolute container path.
fn text_field(
    map: &Mapping,
    parent: &str,
    key: &str,
    absolute: bool,
    problems: &mut Vec<(String, String)>,
) -> Option<String> {
    let at = child_key(parent, key);
    let problem = match map.get(key) {
        None => "is required",
        Some(Value::String(s)) if s.is_empty() => "must not be empty",
        Some(Value::String(s)) if absolute && !s.starts_with('/') => "must be an absolute path",
        Some(Value::String(s)) => return Some(s.clone()),
        Some(_) => "must be a string",
    };
    problems.push((at, problem.to_string()));
    None
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::config::testing::TempDir;

    const DEV_ENV: &str = "user: ubuntu\nmounts:\n  workspace: /workspace\n  setup: /setup\nbroker-bin: /usr/local/bin/scoter-container\nsshd-port: 22022\n";

    fn parse_text(text: &str) -> Result<DevEnv, Vec<String>> {
        parse(parse_yaml(text).unwrap(), Path::new("/c/dev-env.yaml")).map_err(|errors| {
            errors
                .into_iter()
                .map(|e| format!("{}: {}", e.key.unwrap_or_default(), e.message))
                .collect()
        })
    }

    fn container(dir: &TempDir, at: &str) -> PathBuf {
        dir.write(&format!("{at}/{DOCKERFILE}"), "FROM scratch\n");
        dir.write(&format!("{at}/{DEV_ENV_FILE}"), DEV_ENV);
        dir.path().join(at)
    }

    #[test]
    fn parse_full() {
        let env = parse_text(&format!("{DEV_ENV}extra:\n  anything: [1, 2]\n")).unwrap();
        assert_eq!(env.user, "ubuntu");
        assert_eq!(
            env.mounts,
            Mounts {
                workspace: "/workspace".to_string(),
                setup: "/setup".to_string()
            }
        );
        assert_eq!(env.broker_bin, "/usr/local/bin/scoter-container");
        assert_eq!(env.sshd_port, 22022);
        // Everything is kept, including keys the tools don't use.
        assert!(env.raw.get("extra").is_some());
    }

    #[test]
    fn parse_errors_are_gathered() {
        assert_eq!(
            parse_text("").unwrap_err(),
            [
                "user: is required",
                "mounts: is required",
                "broker-bin: is required",
                "sshd-port: is required",
            ]
        );
        assert_eq!(
            parse_text("user: 5\nmounts:\n  workspace: rel\n  setup: ''\nbroker-bin: bin/broker\nsshd-port: 70000\n").unwrap_err(),
            [
                "user: must be a string",
                "mounts.workspace: must be an absolute path",
                "mounts.setup: must not be empty",
                "broker-bin: must be an absolute path",
                "sshd-port: must be a port number, 1 to 65535",
            ],
        );
        assert_eq!(parse_text("[a]\n").unwrap_err(), [": must be a mapping"]);
        assert_eq!(
            parse_text(&DEV_ENV.replace("22022", "'22'")).unwrap_err(),
            ["sshd-port: must be a port number, 1 to 65535"]
        );
        assert_eq!(
            parse_text(&DEV_ENV.replace("22022", "0")).unwrap_err(),
            ["sshd-port: must be a port number, 1 to 65535"]
        );
    }

    #[test]
    fn parse_rejects_tags() {
        let errors = parse_text(&format!("{DEV_ENV}x: !include y\n")).unwrap_err();
        assert_eq!(errors.len(), 1);
        assert!(errors[0].starts_with("x: YAML tags"), "{errors:?}");
    }

    #[test]
    fn find_by_path() {
        let dir = TempDir::new();
        let expected = container(&dir, "proj/.scoter/env");
        let project = dir.path().join("proj/scoter.yaml");
        let found = find(
            &ContainerRef::Path("./.scoter/env".to_string()),
            &project,
            &[],
        )
        .unwrap();
        assert_eq!(found.dir.canonicalize().unwrap(), expected);
        assert_eq!(found.env.sshd_port, 22022);
        // An absolute path is used as is.
        let abs = expected.display().to_string();
        assert_eq!(
            find(&ContainerRef::Path(abs), &project, &[]).unwrap().dir,
            expected
        );
    }

    #[test]
    fn find_by_name() {
        let dir = TempDir::new();
        // Incomplete: no dev-env.yaml.
        dir.write("a/env/Dockerfile", "");
        let expected = container(&dir, "b/env");
        container(&dir, "c/env");
        let lookup = [
            dir.path().join("missing"),
            dir.path().join("a"),
            dir.path().join("b"),
            dir.path().join("c"),
        ];
        let found = find(
            &ContainerRef::Name("env".to_string()),
            Path::new("/p/scoter.yaml"),
            &lookup,
        )
        .unwrap();
        assert_eq!(found.dir, expected);
        assert_eq!(found.dockerfile(), expected.join(DOCKERFILE));
        assert_eq!(found.env_file(), expected.join(DEV_ENV_FILE));
    }

    #[test]
    fn find_by_name_errors() {
        let dir = TempDir::new();
        let project = Path::new("/p/scoter.yaml");
        let name = ContainerRef::Name("env".to_string());
        let errors = find(&name, project, &[]).unwrap_err();
        assert_eq!(
            errors[0].to_string(),
            "/p/scoter.yaml: container.dev-container: can't find dev container 'env': the system configuration sets no `dev-container-paths`",
        );
        let errors = find(&name, project, &[dir.path().to_path_buf()]).unwrap_err();
        assert!(
            errors[0]
                .message
                .ends_with(&format!("in: {}", dir.path().display())),
            "{}",
            errors[0]
        );
    }

    #[test]
    fn load_errors() {
        let dir = TempDir::new();
        let missing = load(&dir.path().join("nothing")).unwrap_err();
        assert_eq!(missing.len(), 1);
        assert!(missing[0].message.contains("doesn't exist"));

        dir.write("bad/dev-env.yaml", "user: ubuntu\n");
        let errors = load(&dir.path().join("bad")).unwrap_err();
        let messages: Vec<String> = errors.iter().map(|e| e.message.clone()).collect();
        // No Dockerfile, plus the three missing fields.
        assert_eq!(messages.len(), 4, "{messages:?}");
        assert_eq!(messages[0], "the dev container has no Dockerfile");

        dir.write("big/Dockerfile", "");
        dir.write("big/dev-env.yaml", &"#".repeat(3 * 1024 * 1024));
        let errors = load(&dir.path().join("big")).unwrap_err();
        assert_eq!(errors[0].message, "the file is larger than the 2 MB limit");
    }
}
