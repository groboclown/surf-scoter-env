//! The Surf Scoter configuration, gathered from all its sources.
//!
//!   * The system setup files (`system`): per-computer settings, such as which
//!     executables to use and how, and the search paths.  See
//!     `docs/system-config-file.md`.
//!   * The project configuration file (`project`): which dev container the
//!     project uses, and the commands its clients may run on the host.  See
//!     `docs/project-configuration-file.md`.
//!   * The dev container's `dev-env.yaml` (`dev_env`): how to run that
//!     container.  See `docs/project-dockerfile.md`.
//!
//! `load_all` reads them all.  It doesn't stop at the first problem: it
//! reports as many as it can find, so they can all be fixed at once.
//!
//! It can also start from a universal configuration file made earlier (see
//! `universal`): its system setup is used as written, without reading the
//! setup files again, and its project file is used unless another is given.
//! That allows building the configuration up in steps.

pub mod command;
pub mod dev_env;
pub mod pattern;
pub mod project;
pub mod read;
pub mod system;

use std::fmt;
use std::path::{Path, PathBuf};

use serde::Deserialize;

use dev_env::DevContainer;
use project::{ContainerRef, ProjectConfig};
use system::{Env, SetupBase, SetupInputs, SetupOptions, SetupRecord, SystemConfig};

use crate::universal::ActiveConfigLocations;

/// The `scoter` section's `path-lists` key searched for a `dev-container: NAME`.
pub const DEV_CONTAINERS: &str = "dev-containers";

/// Where to find the configuration.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct LoadOptions {
    /// The project configuration file, if any.
    pub project: Option<PathBuf>,
    /// The project directory, in place of the project file's (`--project-dir`).
    pub project_dir: Option<PathBuf>,
    /// An extra system setup file, with the highest priority (`--user-setup`).
    pub user_setup: Option<PathBuf>,
    /// Where a distribution installed Surf Scoter, if it did.
    pub distribution_dir: Option<PathBuf>,
    /// A universal configuration file to start from.
    pub universal: Option<PathBuf>,
    /// Don't look for the user's setup file or the install directory's.
    pub no_setup_search: bool,
}

/// What `load_all` uses from a universal configuration file: the setup, and
/// the values that were in effect.
#[derive(Debug, Deserialize)]
#[serde(rename_all = "kebab-case")]
struct UniversalInput {
    #[serde(default)]
    active_config_locations: ActiveConfigLocations,
    setup: SetupRecord,
}

impl UniversalInput {
    fn inputs(&self) -> SetupInputs {
        let active = &self.active_config_locations;
        SetupInputs {
            user_setup_file: active.user_setup_file.clone(),
            user_dir: active.user_dir.clone(),
            distribution_dir: active.distribution_dir.clone(),
        }
    }
}

/// Read a universal configuration file made earlier.
fn read_universal(path: &Path) -> Result<UniversalInput, ConfigError> {
    let text = read::read_limited(path).map_err(|m| ConfigError::new(Some(path), "", m))?;
    serde_json::from_str(&text).map_err(|e| {
        ConfigError::new(
            Some(path),
            "",
            format!("not a universal configuration file: {e}"),
        )
    })
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
    let universal_path = options.universal.as_deref().map(absolute);
    let universal = universal_path
        .as_deref()
        .and_then(|path| read_universal(path).map_err(|e| errors.push(e)).ok());
    let universal_inputs = universal.as_ref().map(UniversalInput::inputs);
    let project_path = options
        .project
        .as_deref()
        .map(Path::to_path_buf)
        .or_else(|| {
            let active = &universal.as_ref()?.active_config_locations;
            active.project_config.as_ref().map(PathBuf::from)
        })
        .map(|p| absolute(&p));
    let (project, container) = match project_path.as_deref() {
        Some(path) => {
            let (project, container) = project::load_checked(path);
            (project.map_err(|e| errors.extend(e)).ok(), container)
        }
        None => (None, None),
    };
    let project_file_dir = || {
        project_path
            .as_deref()
            .and_then(Path::parent)
            .map(Path::to_path_buf)
    };
    let project_dir = match &options.project_dir {
        Some(dir) => {
            let dir = absolute(dir);
            if !dir.is_dir() {
                errors.push(ConfigError::new(
                    Some(&dir),
                    "",
                    "the --project-dir directory doesn't exist",
                ));
            }
            Some(dir)
        }
        // A project file given this time has its own directory.
        None if options.project.is_some() => project_file_dir(),
        None => universal
            .as_ref()
            .and_then(|u| u.active_config_locations.project_dir.as_ref())
            .map(PathBuf::from)
            .or_else(project_file_dir),
    };
    let setup = SetupOptions {
        user_setup: options.user_setup.as_deref(),
        distribution_dir: options.distribution_dir.as_deref(),
        project_dir: project_dir.as_deref(),
        project_config: project_path.as_deref(),
        dev_container: container.is_some(),
        no_search: options.no_setup_search,
        // Only a project file given this time is recorded.
        given_project_config: options.project.as_ref().and(project_path.as_deref()),
        base: universal
            .as_ref()
            .zip(universal_path.as_deref())
            .zip(universal_inputs.as_ref())
            .map(|((u, origin), inputs)| SetupBase {
                record: &u.setup,
                origin,
                inputs,
            }),
    };
    // Without the universal file it was asked to start from, the result
    // would be misleading.
    let mut system = if options.universal.is_some() && universal.is_none() {
        None
    } else {
        system::load_with(&setup, env)
            .map_err(|e| errors.extend(e))
            .ok()
    };
    let lookup = match (&container, &system) {
        (Some(_), Some(system)) => Some(system.path_list(system::SCOTER_SECTION, DEV_CONTAINERS)),
        // A path doesn't need the system configuration.
        (Some(ContainerRef::Path(_)), None) => Some(Vec::new()),
        _ => None,
    };
    let dev_container = match (&container, project_path.as_deref(), lookup) {
        (Some(container), Some(path), Some(lookup)) => dev_env::find(container, path, &lookup)
            .map_err(|e| errors.extend(e))
            .ok(),
        _ => None,
    };
    if let (Some(system), Some(dev)) = (&mut system, &dev_container) {
        system.set_dev_container_path(&dev.dir);
    }
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
            ..LoadOptions::default()
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
            "scoter:\n  path-lists:\n    dev-containers:\n      - ./missing\n      - {}/containers\n",
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
    fn load_in_two_steps() {
        let dir = TempDir::new();
        dev_container(&dir, "project/env");
        let setup = dir.write(
            "setup.yaml",
            "a:\n  paths:\n    local: ./here\n  values:\n    project-file: ./scoter.yaml\n",
        );
        let first = LoadOptions {
            user_setup: Some(setup),
            ..LoadOptions::default()
        };
        let first = load_all_with_env(&first, Env::default()).unwrap();
        assert!(first.project.is_none());
        assert_eq!(first.system.get("a", "local").as_deref(), Some("./here"));
        let universal = crate::universal::build(&first).unwrap();
        let first_file = dir.write("first.json", &crate::universal::to_json(&universal));

        // The second step adds the project file found with the first's help.
        let project = dir.write(
            "project/scoter.yaml",
            "container:\n  dev-container-path: env\n",
        );
        let second = LoadOptions {
            project: Some(project.clone()),
            universal: Some(first_file.clone()),
            no_setup_search: true,
            ..LoadOptions::default()
        };
        let second = load_all_with_env(&second, Env::default()).unwrap();
        assert_eq!(second.project.as_ref().unwrap().path, project);
        assert_eq!(
            second.system.get("a", "local"),
            Some(dir.path().join("project/here").display().to_string())
        );
        assert!(second.dev_container.is_some());

        // The project file given is recorded, most recent first.
        let universal = crate::universal::build(&second).unwrap();
        assert_eq!(
            universal.setup.project_config,
            [project.display().to_string()]
        );
        let second_file = dir.write("second.json", &crate::universal::to_json(&universal));

        // Without `--config`, the most recent project file is used, and the
        // record is left as it is.
        let third = LoadOptions {
            universal: Some(second_file.clone()),
            no_setup_search: true,
            ..LoadOptions::default()
        };
        let third = load_all_with_env(&third, Env::default()).unwrap();
        assert_eq!(third.project.as_ref().unwrap().path, project);
        assert_eq!(
            third.system.setup_record().project_config,
            [project.display().to_string()]
        );

        // Another project file goes first.
        let other = dir.write(
            "project/other.yaml",
            "container:\n  dev-container-path: env\n",
        );
        let fourth = LoadOptions {
            project: Some(other.clone()),
            universal: Some(second_file),
            no_setup_search: true,
            ..LoadOptions::default()
        };
        let fourth = load_all_with_env(&fourth, Env::default()).unwrap();
        assert_eq!(fourth.project.unwrap().path, other);
        assert_eq!(
            fourth.system.setup_record().project_config,
            [other.display().to_string(), project.display().to_string()]
        );
    }

    #[test]
    fn in_effect_values_carry_over_without_the_search() {
        let dir = TempDir::new();
        let user = dir.write(
            "home/.config/surf-scoter.d/setup.yaml",
            "a:\n  values:\n    v: 1\n",
        );
        let env = Env {
            home: Some(dir.path().join("home")),
            ..Env::default()
        };
        let first = load_all_with_env(&LoadOptions::default(), env.clone()).unwrap();
        let universal = crate::universal::build(&first).unwrap();
        let first_file = dir.write("first.json", &crate::universal::to_json(&universal));

        // The user file is gone, and isn't looked for: the record stands in.
        std::fs::remove_file(&user).unwrap();
        let second = LoadOptions {
            universal: Some(first_file),
            no_setup_search: true,
            ..LoadOptions::default()
        };
        let second = load_all_with_env(&second, env).unwrap();
        assert_eq!(
            second
                .system
                .get(system::SCOTER_SECTION, system::USER_SETUP_FILE),
            Some(user.display().to_string())
        );
        assert_eq!(
            second.system.get(system::SCOTER_SECTION, system::USER_DIR),
            Some(
                dir.path()
                    .join("home/.config/surf-scoter.d")
                    .display()
                    .to_string()
            )
        );
        assert_eq!(
            second.system.value("a", "v"),
            Some(system::ScalarValue::Int(1))
        );
    }

    #[test]
    fn project_dir_given() {
        let dir = TempDir::new();
        dev_container(&dir, "project/env");
        let elsewhere = dir.path().join("elsewhere");
        std::fs::create_dir_all(&elsewhere).unwrap();
        let project_dir = |config: &FullConfig| {
            config
                .system
                .get_path(system::SCOTER_SECTION, system::PROJECT_DIR)
        };
        let mut opts = options(
            &dir,
            Some("container:\n  dev-container-path: env\n"),
            Some("a:\n  paths:\n    x: ./x\n"),
        );
        let config = load_all_with_env(&opts, Env::default()).unwrap();
        assert_eq!(project_dir(&config), Some(dir.path().join("project")));

        // `./` paths start there; the project file's own paths don't.
        opts.project_dir = Some(elsewhere.clone());
        let config = load_all_with_env(&opts, Env::default()).unwrap();
        assert_eq!(project_dir(&config), Some(elsewhere.clone()));
        assert_eq!(config.system.get_path("a", "x"), Some(elsewhere.join("x")));
        assert_eq!(
            config.dev_container.as_ref().unwrap().dir,
            dir.path().join("project/env")
        );

        // It carries over, unless another project file is given.
        let universal = crate::universal::build(&config).unwrap();
        let first = dir.write("first.json", &crate::universal::to_json(&universal));
        let again = LoadOptions {
            universal: Some(first.clone()),
            no_setup_search: true,
            ..LoadOptions::default()
        };
        let config = load_all_with_env(&again, Env::default()).unwrap();
        assert_eq!(project_dir(&config), Some(elsewhere));
        let other = dir.write(
            "project/other.yaml",
            "container:\n  dev-container-path: env\n",
        );
        let again = LoadOptions {
            project: Some(other),
            ..again
        };
        let config = load_all_with_env(&again, Env::default()).unwrap();
        assert_eq!(project_dir(&config), Some(dir.path().join("project")));

        opts.project_dir = Some(dir.path().join("missing"));
        let errors = load_all_with_env(&opts, Env::default()).unwrap_err();
        assert_eq!(
            errors.to_string(),
            format!(
                "{}: the --project-dir directory doesn't exist",
                dir.path().join("missing").display()
            )
        );
    }

    #[test]
    fn load_from_a_bad_universal_file() {
        let dir = TempDir::new();
        let bad = dir.write("bad.json", "{\"resolved\": {}}");
        let options = LoadOptions {
            universal: Some(bad.clone()),
            ..LoadOptions::default()
        };
        let errors = load_all_with_env(&options, Env::default()).unwrap_err();
        assert_eq!(errors.0.len(), 1, "{errors}");
        assert_eq!(errors.0[0].file.as_deref(), Some(bad.as_path()));
        assert!(
            errors.0[0]
                .message
                .starts_with("not a universal configuration file: missing field `setup`"),
            "{errors}"
        );
        let options = LoadOptions {
            universal: Some(dir.path().join("missing.json")),
            ..LoadOptions::default()
        };
        assert!(load_all_with_env(&options, Env::default()).is_err());
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
