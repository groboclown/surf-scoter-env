//! The Surf Scoter configuration, gathered from all its sources.
//!
//!   * The system setup files (`system`): per-computer settings, such as which
//!     executables to use and how, and the lookup paths.  See
//!     `docs/system-config-file.md`.
//!   * The project configuration file (`project`): which dev container the
//!     project uses, and the commands its clients may run on the host.  See
//!     `docs/project-configuration-file.md`.
//!   * The dev container's `dev-env.yaml` (`dev_env`): how to run that
//!     container.  See `docs/project-dockerfile.md`.
//!
//! `load_all` reads them all.  It doesn't stop at the first problem: it
//! reports as many as it can find, so they can all be fixed at once.

pub mod command;
pub mod dev_env;
pub mod pattern;
pub mod project;
pub mod read;
pub mod system;

use std::fmt;
use std::path::{Path, PathBuf};

use dev_env::DevContainer;
use project::{ContainerRef, ProjectConfig};
use system::{Env, SystemConfig};

/// The lookup paths (`dev-container-paths`) searched for a `dev-container: NAME`.
pub const DEV_CONTAINER_PATHS: &str = "dev-container";

/// Where to find the configuration.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct LoadOptions {
    /// The project configuration file, if any.
    pub project: Option<PathBuf>,
    /// An extra system setup file, with the highest priority (`--user-setup`).
    pub user_setup: Option<PathBuf>,
    /// Where a distribution installed Surf Scoter, if it did.
    pub distribution_dir: Option<PathBuf>,
}

/// The whole configuration.
#[derive(Debug)]
pub struct FullConfig {
    pub system: SystemConfig,
    /// Only when a project file was given.
    pub project: Option<ProjectConfig>,
    /// The project's dev container; present whenever `project` is.
    pub dev_container: Option<DevContainer>,
}

/// Read the whole configuration, using the process environment to find the
/// user's setup file and executables.
pub fn load_all(options: &LoadOptions) -> Result<FullConfig, ConfigErrors> {
    load_all_with_env(options, Env::from_process())
}

/// `load_all`, with the environment given.
pub fn load_all_with_env(options: &LoadOptions, env: Env) -> Result<FullConfig, ConfigErrors> {
    let mut errors = Vec::new();
    let project_path = options.project.as_deref().map(absolute);
    let (project, container) = match project_path.as_deref() {
        Some(path) => {
            let (project, container) = project::load_checked(path);
            (project.map_err(|e| errors.extend(e)).ok(), container)
        }
        None => (None, None),
    };
    let project_dir = project_path.as_deref().and_then(Path::parent);
    let system = system::load(
        options.user_setup.as_deref(),
        options.distribution_dir.as_deref(),
        project_dir,
        env,
    )
    .map_err(|e| errors.extend(e))
    .ok();
    let lookup = match (&container, &system) {
        (Some(_), Some(system)) => Some(system.lookup_path(DEV_CONTAINER_PATHS)),
        // A path doesn't need the system configuration.
        (Some(ContainerRef::Path(_)), None) => Some(&[][..]),
        _ => None,
    };
    let dev_container = match (&container, project_path.as_deref(), lookup) {
        (Some(container), Some(path), Some(lookup)) => dev_env::find(container, path, lookup)
            .map_err(|e| errors.extend(e))
            .ok(),
        _ => None,
    };
    match system {
        Some(system) if errors.is_empty() => Ok(FullConfig {
            system,
            project,
            dev_container,
        }),
        _ => Err(ConfigErrors(errors)),
    }
}

/// `path` made absolute (without resolving symlinks).
pub(crate) fn absolute(path: &Path) -> PathBuf {
    std::path::absolute(path).unwrap_or_else(|_| path.to_path_buf())
}

/// One problem in the configuration.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ConfigError {
    /// The file (or directory) it's in, if any.
    pub file: Option<PathBuf>,
    /// Where in the file, as a key path such as `commands[3].args[1]`.
    pub key: Option<String>,
    pub message: String,
}

impl ConfigError {
    pub fn new(file: Option<&Path>, key: &str, message: impl Into<String>) -> Self {
        ConfigError {
            file: file.map(Path::to_path_buf),
            key: (!key.is_empty()).then(|| key.to_string()),
            message: message.into(),
        }
    }
}

impl fmt::Display for ConfigError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        if let Some(file) = &self.file {
            write!(f, "{}: ", file.display())?;
        }
        if let Some(key) = &self.key {
            write!(f, "{key}: ")?;
        }
        f.write_str(&self.message)
    }
}

/// All the problems found; never empty.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ConfigErrors(pub Vec<ConfigError>);

impl fmt::Display for ConfigErrors {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        for (i, error) in self.0.iter().enumerate() {
            if i > 0 {
                f.write_str("\n")?;
            }
            write!(f, "{error}")?;
        }
        Ok(())
    }
}

/// `parent.name`; either may be empty.
pub(crate) fn child_key(parent: &str, name: &str) -> String {
    match (parent.is_empty(), name.is_empty()) {
        (true, _) => name.to_string(),
        (false, true) => parent.to_string(),
        (false, false) => format!("{parent}.{name}"),
    }
}

/// `parent[i]`.
pub(crate) fn index_key(parent: &str, i: usize) -> String {
    format!("{parent}[{i}]")
}

#[cfg(test)]
pub(crate) mod testing {
    use std::fs;
    use std::path::{Path, PathBuf};
    use std::sync::atomic::{AtomicUsize, Ordering};

    /// A uniquely named directory, removed (with its contents) on drop.
    pub(crate) struct TempDir(PathBuf);

    impl TempDir {
        pub(crate) fn new() -> Self {
            static COUNTER: AtomicUsize = AtomicUsize::new(0);
            let n = COUNTER.fetch_add(1, Ordering::Relaxed);
            let path =
                std::env::temp_dir().join(format!("scoter-config-test-{}-{n}", std::process::id()));
            let _ = fs::remove_dir_all(&path);
            fs::create_dir_all(&path).unwrap();
            TempDir(path.canonicalize().unwrap())
        }

        pub(crate) fn path(&self) -> &Path {
            &self.0
        }

        /// Write `text` to `name` (creating directories), returning its path.
        pub(crate) fn write(&self, name: &str, text: &str) -> PathBuf {
            let path = self.0.join(name);
            fs::create_dir_all(path.parent().unwrap()).unwrap();
            fs::write(&path, text).unwrap();
            path
        }
    }

    impl Drop for TempDir {
        fn drop(&mut self) {
            let _ = fs::remove_dir_all(&self.0);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::testing::TempDir;
    use super::*;

    const DEV_ENV: &str = "user: ubuntu\nmounts:\n  workspace: /workspace\n  setup: /setup\nbroker-bin: /usr/local/bin/scoter-container\nsshd-port: 22022\n";

    fn dev_container(dir: &TempDir, at: &str) {
        dir.write(&format!("{at}/Dockerfile"), "FROM scratch\n");
        dir.write(&format!("{at}/dev-env.yaml"), DEV_ENV);
    }

    fn options(dir: &TempDir, project: Option<&str>, setup: Option<&str>) -> LoadOptions {
        LoadOptions {
            project: project.map(|p| dir.write("project/scoter.yaml", p)),
            user_setup: setup.map(|s| dir.write("setup.yaml", s)),
            distribution_dir: None,
        }
    }

    fn messages(errors: ConfigErrors) -> Vec<String> {
        errors
            .0
            .into_iter()
            .map(|e| format!("{}: {}", e.key.unwrap_or_default(), e.message))
            .collect()
    }

    #[test]
    fn load_with_dev_container_path() {
        let dir = TempDir::new();
        dev_container(&dir, "project/env");
        let opts = options(&dir, Some("container:\n  dev-container-path: env\n"), None);
        let config = load_all_with_env(&opts, Env::default()).unwrap();
        assert_eq!(
            config.project.unwrap().path,
            dir.path().join("project/scoter.yaml")
        );
        let dev = config.dev_container.unwrap();
        assert_eq!(dev.dir, dir.path().join("project/env"));
        assert_eq!(dev.env.broker_bin, "/usr/local/bin/scoter-container");
        assert_eq!(config.system.files(), &[] as &[PathBuf]);
    }

    #[test]
    fn load_with_dev_container_name() {
        let dir = TempDir::new();
        dev_container(&dir, "containers/dev-env");
        let setup = format!(
            "dev-container-paths:\n  - ./missing\n  - {}/containers\n",
            dir.path().display()
        );
        let opts = options(
            &dir,
            Some("container:\n  dev-container: dev-env\n"),
            Some(&setup),
        );
        let config = load_all_with_env(&opts, Env::default()).unwrap();
        assert_eq!(
            config.dev_container.unwrap().dir,
            dir.path().join("containers/dev-env")
        );
        assert_eq!(config.system.files(), &[dir.path().join("setup.yaml")]);
    }

    #[test]
    fn load_without_project() {
        let dir = TempDir::new();
        let opts = options(&dir, None, Some("ssh:\n  paths:\n    home: /h\n"));
        let config = load_all_with_env(&opts, Env::default()).unwrap();
        assert!(config.project.is_none());
        assert!(config.dev_container.is_none());
        assert_eq!(config.system.get("ssh", "home").as_deref(), Some("/h"));
    }

    #[test]
    fn load_dev_container_name_not_found() {
        let dir = TempDir::new();
        let opts = options(&dir, Some("container:\n  dev-container: dev-env\n"), None);
        let errors = messages(load_all_with_env(&opts, Env::default()).unwrap_err());
        assert_eq!(errors.len(), 1, "{errors:?}");
        assert!(
            errors[0].starts_with("container.dev-container: can't find dev container 'dev-env'"),
            "{errors:?}"
        );
    }

    #[test]
    fn load_gathers_errors_from_every_file() {
        let dir = TempDir::new();
        dev_container(&dir, "project/env");
        dir.write("project/env/dev-env.yaml", "user: ''\nsshd-port: 0\n");
        let opts = options(
            &dir,
            Some(concat!(
                "container:\n  dev-container-path: env\n",
                "commands:\n",
                "  - {command: a/b, args: []}\n",
                "  - {command: ok, args: [5]}\n",
                "  - {command: ok, program: '', args: []}\n",
            )),
            Some("ssh:\n  paths:\n    a: ${nope}\n    b: ${ssh:missing}\n"),
        );
        let errors = messages(load_all_with_env(&opts, Env::default()).unwrap_err());
        // 3 command entries + 2 references + 4 dev-env.yaml fields.
        assert_eq!(errors.len(), 9, "{errors:#?}");
        assert!(
            errors.iter().any(|e| e.starts_with("commands[0]: ")),
            "{errors:#?}"
        );
        assert!(
            errors.iter().any(|e| e.starts_with("commands[1]: ")),
            "{errors:#?}"
        );
        assert!(
            errors.iter().any(|e| e.starts_with("commands[2]: ")),
            "{errors:#?}"
        );
        assert!(
            errors
                .iter()
                .any(|e| e == "ssh.paths.a: unknown reference `${ssh:nope}`"),
            "{errors:#?}"
        );
        assert!(
            errors
                .iter()
                .any(|e| e == "ssh.paths.b: unknown reference `${ssh:missing}`"),
            "{errors:#?}"
        );
        assert!(
            errors.iter().any(|e| e.starts_with("sshd-port: ")),
            "{errors:#?}"
        );
    }

    #[test]
    fn load_project_errors_skip_dev_container() {
        let dir = TempDir::new();
        let opts = options(
            &dir,
            Some("container: {dev-container: a, dev-container-path: b}\n"),
            None,
        );
        let errors = messages(load_all_with_env(&opts, Env::default()).unwrap_err());
        assert_eq!(errors.len(), 1, "{errors:?}");
    }

    #[test]
    fn error_display() {
        let error = ConfigError::new(Some(Path::new("/a/b.yaml")), "commands[0]", "bad");
        assert_eq!(error.to_string(), "/a/b.yaml: commands[0]: bad");
        assert_eq!(ConfigError::new(None, "", "bad").to_string(), "bad");
        let errors = ConfigErrors(vec![error, ConfigError::new(None, "k", "worse")]);
        assert_eq!(errors.to_string(), "/a/b.yaml: commands[0]: bad\nk: worse");
    }
}
