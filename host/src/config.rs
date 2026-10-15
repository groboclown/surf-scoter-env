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
pub mod environment;
pub mod pattern;
pub mod project;
pub mod read;
pub mod system;

use std::fmt;
use std::path::{Path, PathBuf};

use serde::Deserialize;

use dev_env::DevContainer;
use environment::{EnvPlan, EnvRules};
use project::{ContainerRef, ProjectConfig};
use system::{Env, LoadedFrom, SetupBase, SetupInputs, SetupOptions, SetupRecord, SystemConfig};

use crate::universal::ActiveConfigLocations;

/// The `scoter` section's `path-lists` key searched for a `dev-container: NAME`.
pub const DEV_CONTAINERS_SEARCH: &str = "dev-containers-search";

/// Where to find the configuration.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct LoadOptions {
    /// The project configuration file, if any.
    pub project: Option<PathBuf>,
    /// The project directory, in place of the project file's (`--project-dir`).
    pub project_dir: Option<PathBuf>,
    /// The dev container's mounts: the project workspace (`--dev-container-workspace-dir`)
    /// and the settings directory (`--dev-container-settings-dir`).
    pub dev_container_workspace_dir: Option<PathBuf>,
    pub dev_container_settings_dir: Option<PathBuf>,
    /// Extra system setup files, with the highest priority (`--user-setup`),
    /// in the order given: a later one wins.  Only these are read from a
    /// project; the tools never look for them there.
    pub user_setup: Vec<PathBuf>,
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
    #[serde(default)]
    loaded_from: LoadedFrom,
    /// The project file and `dev-env.yaml`, as written; only
    /// `load_universal` uses them.
    #[serde(default)]
    project: Option<serde_json::Value>,
    #[serde(default)]
    dev_container: Option<serde_json::Value>,
    /// The commands, as they run.
    #[serde(default)]
    host_brokered: Vec<serde_json::Value>,
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
    let text = read::read_limited_to(path, read::MAX_UNIVERSAL_FILE_SIZE)
        .map_err(|m| ConfigError::new(Some(path), "", m))?;
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

/// Read the whole configuration from a universal configuration file alone,
/// using the process environment to find executables.
pub fn load_universal(path: &Path) -> Result<FullConfig, ConfigErrors> {
    load_universal_with_env(path, Env::from_process())
}

/// `load_universal`, with the environment given.
///
/// No other file is read: the setup comes from the file's `setup` record,
/// the project file and the dev container's `dev-env.yaml` from its
/// `project` and `dev-container` contents, and the locations (including
/// `cwd`) from its `active-config-locations`.
///
/// The project's commands are its `host-brokered` list, as written there:
/// each one's `program` (`null` making it unavailable) and its `env`, which
/// is already the whole plan, with only `@NAME@` left to expand (see
/// `expand_host_environments`).  The `project` contents are only a record of
/// what the list was made from.  So the configuration is the
/// one the file was made with, however the files change since; and the file
/// alone recreates it, say for debugging.
pub fn load_universal_with_env(path: &Path, mut env: Env) -> Result<FullConfig, ConfigErrors> {
    let path = absolute(path);
    let universal = read_universal(&path).map_err(|e| ConfigErrors(vec![e]))?;
    let active = &universal.active_config_locations;
    let mut errors = Vec::new();
    let mismatch = |key: &str, location: &str| {
        ConfigError::new(
            Some(&path),
            key,
            format!("goes with `active-config-locations.{location}`, which doesn't match it"),
        )
    };
    // JSON is YAML, so the contents are read as the files would be.
    let as_yaml = |contents: &serde_json::Value| read::parse_yaml(&contents.to_string());

    let project = match (&universal.project, &active.project_config) {
        (Some(contents), Some(file)) => project::parse(&contents.to_string(), Path::new(file))
            .map_err(|e| errors.extend(e))
            .ok(),
        (None, None) => None,
        _ => {
            errors.push(mismatch("project", "project-config"));
            None
        }
    };
    let dev_container = match (&universal.dev_container, &active.dev_container_path) {
        (Some(contents), Some(dir)) => {
            let dir = PathBuf::from(dir);
            let file = dir.join(dev_env::DEV_ENV_FILE);
            as_yaml(contents)
                .map_err(|m| vec![ConfigError::new(Some(&path), "dev-container", m)])
                .and_then(|value| dev_env::parse(value, &file))
                .map(|env| DevContainer { dir, env })
                .map_err(|e| errors.extend(e))
                .ok()
        }
        (None, None) => None,
        _ => {
            errors.push(mismatch("dev-container", "dev-container-path"));
            None
        }
    };
    if universal.project.is_some() && universal.dev_container.is_none() {
        errors.push(ConfigError::new(
            Some(&path),
            "dev-container",
            "is missing; a project always has a dev container",
        ));
    }

    // As recorded, not wherever this program runs.
    env.cwd = active.cwd.as_ref().map(PathBuf::from);
    fn path_of(location: &Option<String>) -> Option<&Path> {
        location.as_deref().map(Path::new)
    }
    let inputs = universal.inputs();
    let setup = SetupOptions {
        project_dir: path_of(&active.project_dir),
        project_config: path_of(&active.project_config),
        dev_container_workspace_dir: path_of(&active.dev_container_workspace_dir),
        dev_container_settings_dir: path_of(&active.dev_container_settings_dir),
        dev_container: universal.dev_container.is_some(),
        no_search: true,
        base: Some(SetupBase {
            record: &universal.setup,
            loaded_from: &universal.loaded_from,
            origin: &path,
            inputs: &inputs,
        }),
        ..SetupOptions::default()
    };
    let mut system = system::load_with(&setup, env)
        .map_err(|e| errors.extend(e))
        .ok();
    if let (Some(system), Some(dev)) = (&mut system, &dev_container) {
        system.set_dev_container_path(&dev.dir);
    }
    let mut project = project;
    match (
        &mut project,
        brokered_commands(&universal.host_brokered, &path),
    ) {
        (Some(project), Ok((commands, raw))) => {
            project.commands = commands;
            project.raw_commands = raw;
        }
        // Commands without any project; a project that's wrong is reported above.
        (None, Ok((commands, _)))
            if !commands.is_empty()
                && universal.project.is_none()
                && active.project_config.is_none() =>
        {
            errors.push(mismatch("host-brokered", "project-config"))
        }
        (None, Ok(_)) => {}
        (_, Err(e)) => errors.extend(e),
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
    let (mut project, container) = match project_path.as_deref() {
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
    // Given, or else carried over.  They needn't exist yet.
    let carried = |given: &Option<PathBuf>,
                   active: fn(&ActiveConfigLocations) -> &Option<String>| {
        given.as_deref().map(absolute).or_else(|| {
            universal
                .as_ref()
                .and_then(|u| active(&u.active_config_locations).as_ref())
                .map(PathBuf::from)
        })
    };
    let dev_container_workspace_dir = carried(&options.dev_container_workspace_dir, |a| {
        &a.dev_container_workspace_dir
    });
    let dev_container_settings_dir = carried(&options.dev_container_settings_dir, |a| {
        &a.dev_container_settings_dir
    });
    let setup = SetupOptions {
        user_setup: &options.user_setup,
        distribution_dir: options.distribution_dir.as_deref(),
        project_dir: project_dir.as_deref(),
        project_config: project_path.as_deref(),
        dev_container_workspace_dir: dev_container_workspace_dir.as_deref(),
        dev_container_settings_dir: dev_container_settings_dir.as_deref(),
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
                loaded_from: &u.loaded_from,
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
        (Some(_), Some(system)) => {
            Some(system.path_list(system::SCOTER_SECTION, DEV_CONTAINERS_SEARCH))
        }
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
    // After the dev container, as `env` values can refer to it.
    if let (Some(system), Some(project)) = (&system, &mut project) {
        errors.extend(plan_environments(project, system));
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

/// The commands of a universal configuration file's `host-brokered` list
/// (from `file`), and each entry as written.
fn brokered_commands(
    entries: &[serde_json::Value],
    file: &Path,
) -> Result<project::ParsedCommands, Vec<ConfigError>> {
    const KEY: &str = "host-brokered";
    let error = |key: &str, message: String| ConfigError::new(Some(file), key, message);
    let list = serde_json::Value::Array(entries.to_vec());
    let value = read::parse_yaml(&list.to_string()).map_err(|m| vec![error(KEY, m)])?;
    let (mut commands, raw) = project::parse_command_list(value, KEY).map_err(|problems| {
        problems
            .into_iter()
            .map(|(key, message)| error(&key, message))
            .collect::<Vec<_>>()
    })?;
    let mut errors = Vec::new();
    for (i, (rule, entry)) in commands.iter_mut().zip(entries).enumerate() {
        match entry.get("program") {
            Some(serde_json::Value::Null) => rule.unavailable = true,
            Some(_) => {}
            None => errors.push(error(
                &index_key(KEY, i),
                "needs `program`: the program found, or null".to_string(),
            )),
        }
        rule.env_plan = EnvPlan::as_written(&rule.env);
    }
    if errors.is_empty() {
        Ok((commands, raw))
    } else {
        Err(errors)
    }
}

/// Expand the `@NAME@` references in each command's `env_plan`, with the
/// environment `system` was loaded with: for the program that runs the
/// commands, once the configuration is loaded.
pub fn expand_host_environments(
    project: &mut ProjectConfig,
    system: &SystemConfig,
) -> Result<(), ConfigErrors> {
    let mut errors = Vec::new();
    for (i, rule) in project.commands.iter_mut().enumerate() {
        let key = child_key(&index_key("commands", i), "env");
        match rule.env_plan.expand_host(system, &key) {
            Ok(plan) => rule.env_plan = plan,
            Err(problems) => errors.extend(
                problems
                    .into_iter()
                    .map(|(key, message)| ConfigError::new(Some(&project.path), &key, message)),
            ),
        }
    }
    if errors.is_empty() {
        Ok(())
    } else {
        Err(ConfigErrors(errors))
    }
}

/// Work out each command's `env_plan`: the project's `env` with the
/// command's added, its `${section:key}` references expanded against
/// `system`.
fn plan_environments(project: &mut ProjectConfig, system: &SystemConfig) -> Vec<ConfigError> {
    let mut errors = Vec::new();
    let mut resolve = |rules: &EnvRules, key: &str| {
        EnvPlan::resolve(rules, system, key)
            .map_err(|problems| {
                errors.extend(
                    problems
                        .into_iter()
                        .map(|(key, message)| ConfigError::new(Some(&project.path), &key, message)),
                )
            })
            .unwrap_or_default()
    };
    let common = resolve(&project.env, "env");
    let plans: Vec<EnvPlan> = project
        .commands
        .iter()
        .enumerate()
        .map(|(i, rule)| {
            let key = child_key(&index_key("commands", i), "env");
            common.merged(&resolve(&rule.env, &key))
        })
        .collect();
    for (rule, plan) in project.commands.iter_mut().zip(plans) {
        rule.env_plan = plan;
    }
    errors
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
    use std::collections::HashMap;

    const DEV_ENV: &str = "user: ubuntu\nmounts:\n  workspace: /workspace\n  setup: /setup\nbroker-bin: /usr/local/bin/scoter-container\nsshd-port: 22022\n";

    fn dev_container(dir: &TempDir, at: &str) {
        dir.write(&format!("{at}/Dockerfile"), "FROM scratch\n");
        dir.write(&format!("{at}/dev-env.yaml"), DEV_ENV);
    }

    fn options(dir: &TempDir, project: Option<&str>, setup: Option<&str>) -> LoadOptions {
        LoadOptions {
            project: project.map(|p| dir.write("project/scoter.yaml", p)),
            user_setup: setup
                .map(|s| dir.write("setup.yaml", s))
                .into_iter()
                .collect(),
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
            "scoter:\n  path-lists:\n    dev-containers-search:\n      - ./missing\n      - {}/containers\n",
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
            user_setup: vec![setup],
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
            universal.loaded_from.project_config,
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
            third.system.loaded_from().project_config,
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
            fourth.system.loaded_from().project_config,
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
    fn container_mount_dirs() {
        let dir = TempDir::new();
        let get = |config: &FullConfig, key| config.system.get_path(system::SCOTER_SECTION, key);
        let first = LoadOptions {
            dev_container_workspace_dir: Some(dir.path().join("ws")),
            // Made absolute; it needn't exist yet.
            dev_container_settings_dir: Some(PathBuf::from("relative/settings")),
            ..LoadOptions::default()
        };
        let config = load_all_with_env(&first, Env::default()).unwrap();
        assert_eq!(
            get(&config, system::DEV_CONTAINER_WORKSPACE_DIR),
            Some(dir.path().join("ws"))
        );
        let settings = absolute(Path::new("relative/settings"));
        assert_eq!(
            get(&config, system::DEV_CONTAINER_SETTINGS_DIR),
            Some(settings.clone())
        );
        let universal = crate::universal::build(&config).unwrap();
        assert_eq!(
            universal.active_config_locations.dev_container_settings_dir,
            Some(settings.display().to_string())
        );

        // They carry over, unless given again.
        let first_file = dir.write("first.json", &crate::universal::to_json(&universal));
        let again = LoadOptions {
            universal: Some(first_file),
            no_setup_search: true,
            dev_container_workspace_dir: Some(dir.path().join("other")),
            ..LoadOptions::default()
        };
        let config = load_all_with_env(&again, Env::default()).unwrap();
        assert_eq!(
            get(&config, system::DEV_CONTAINER_WORKSPACE_DIR),
            Some(dir.path().join("other"))
        );
        assert_eq!(
            get(&config, system::DEV_CONTAINER_SETTINGS_DIR),
            Some(settings)
        );

        // Without them, they're not set.
        let config = load_all_with_env(&LoadOptions::default(), Env::default()).unwrap();
        assert_eq!(get(&config, system::DEV_CONTAINER_WORKSPACE_DIR), None);
    }

    #[test]
    fn command_environments_are_worked_out() {
        let dir = TempDir::new();
        dev_container(&dir, "project/env");
        let opts = options(
            &dir,
            Some(concat!(
                "container:\n  dev-container-path: env\n",
                "env:\n  allow: [LANG]\n  set: {HOME: '@HOME@', TOOL: '${a:t}'}\n",
                "commands:\n",
                "  - {command: one, args: []}\n",
                "  - command: two\n    args: []\n    env:\n",
                "      allow: [TERM]\n      set: {TOOL: own, DEV: '${scoter:dev-container-path}'}\n",
            )),
            Some("a:\n  paths:\n    t: /opt/t\n"),
        );
        let env = Env {
            vars: HashMap::from([("HOME".to_string(), "/home/me".to_string())]),
            ..Env::default()
        };
        let config = load_all_with_env(&opts, env).unwrap();
        let mut project = config.project.unwrap();
        // `@NAME@` is left for the program running the commands.
        assert_eq!(project.commands[0].env_plan.set["HOME"], "@HOME@");
        expand_host_environments(&mut project, &config.system).unwrap();
        let commands = &project.commands;
        let one = &commands[0].env_plan;
        assert_eq!(one.allow, ["LANG"]);
        assert_eq!(one.set["HOME"], "/home/me");
        assert_eq!(one.set["TOOL"], "/opt/t");
        let two = &commands[1].env_plan;
        assert_eq!(two.allow, ["LANG", "TERM"]);
        assert_eq!(two.set["HOME"], "/home/me");
        assert_eq!(two.set["TOOL"], "own");
        assert_eq!(
            two.set["DEV"],
            dir.path().join("project/env").display().to_string()
        );
    }

    #[test]
    fn command_environment_errors() {
        let dir = TempDir::new();
        dev_container(&dir, "project/env");
        let opts = options(
            &dir,
            Some(concat!(
                "container:\n  dev-container-path: env\n",
                "env:\n  set: {A: '${a:missing}'}\n",
                "commands:\n",
                "  - command: one\n    args: []\n    env:\n      default: {B: '${b:missing}'}\n",
                "  - {command: two, args: []}\n",
            )),
            None,
        );
        let errors = messages(load_all_with_env(&opts, Env::default()).unwrap_err());
        // The project's block is reported once, not per command.
        assert_eq!(
            errors,
            [
                "env.set.A: unknown reference `${a:missing}`",
                "commands[0].env.default.B: unknown reference `${b:missing}`",
            ]
        );
    }

    /// A universal configuration file for a project with a dev container,
    /// made in `dir`, as `gen-scoter-uc` would; and the files it came from.
    fn universal_for(dir: &TempDir, env: Env) -> (PathBuf, Vec<PathBuf>) {
        dev_container(dir, "project/env");
        let opts = LoadOptions {
            project: Some(dir.write(
                "project/scoter.yaml",
                concat!(
                    "container:\n  dev-container-path: env\n",
                    "env:\n  allow: [LANG]\n  set: {LOCAL: '${a:local}', HOME: '@HOME@'}\n",
                    "commands:\n  - {command: one, args: [x]}\n",
                ),
            )),
            user_setup: vec![dir.write("setup.yaml", "a:\n  paths:\n    local: ./here\n")],
            no_setup_search: true,
            ..LoadOptions::default()
        };
        let config = load_all_with_env(&opts, env).unwrap();
        let json = crate::universal::to_json(&crate::universal::build(&config).unwrap());
        let sources = vec![
            dir.path().join("project/scoter.yaml"),
            dir.path().join("project/env/dev-env.yaml"),
            dir.path().join("setup.yaml"),
        ];
        (dir.write("universal.json", &json), sources)
    }

    #[test]
    fn load_universal_reads_no_other_file() {
        let dir = TempDir::new();
        let made_with = Env {
            vars: HashMap::from([("HOME".to_string(), "/home/me".to_string())]),
            cwd: Some(PathBuf::from("/where/it/was/made")),
            ..Env::default()
        };
        let (universal, sources) = universal_for(&dir, made_with.clone());
        let expected = load_universal_with_env(&universal, made_with.clone()).unwrap();

        // The files it came from change, or go away: nothing changes.
        for file in &sources {
            std::fs::remove_file(file).unwrap();
        }
        // Run with another environment: it's that one's `@NAME@` values.
        let now = Env {
            cwd: Some(PathBuf::from("/somewhere/else")),
            vars: HashMap::from([("HOME".to_string(), "/home/runner".to_string())]),
            ..made_with
        };
        let mut config = load_universal_with_env(&universal, now).unwrap();
        expand_host_environments(config.project.as_mut().unwrap(), &config.system).unwrap();
        let project = config.project.as_ref().unwrap();
        assert_eq!(project.path, dir.path().join("project/scoter.yaml"));
        assert_eq!(
            project.commands[0].args,
            expected.project.as_ref().unwrap().commands[0].args
        );
        let plan = &project.commands[0].env_plan;
        assert_eq!(plan.allow, ["LANG"]);
        assert_eq!(
            plan.set["LOCAL"],
            dir.path().join("project/here").display().to_string()
        );
        assert_eq!(plan.set["HOME"], "/home/runner");
        assert_eq!(config.dev_container, expected.dev_container);
        assert_eq!(
            config.dev_container.unwrap().env.broker_bin,
            "/usr/local/bin/scoter-container"
        );
        // The locations are as recorded, `cwd` included.
        assert_eq!(
            config.system.get_path(system::SCOTER_SECTION, system::CWD),
            Some(PathBuf::from("/where/it/was/made"))
        );
        assert_eq!(
            config
                .system
                .get_path(system::SCOTER_SECTION, system::DEV_CONTAINER_PATH),
            Some(dir.path().join("project/env"))
        );
    }

    #[test]
    fn load_universal_contents_must_match_locations() {
        let dir = TempDir::new();
        let (universal, _) = universal_for(&dir, Env::default());
        let edit = |change: &dyn Fn(&mut serde_json::Value)| {
            let mut json: serde_json::Value =
                serde_json::from_str(&std::fs::read_to_string(&universal).unwrap()).unwrap();
            change(&mut json);
            let file = dir.write("edited.json", &json.to_string());
            messages(load_universal_with_env(&file, Env::default()).unwrap_err())
        };
        assert_eq!(
            edit(&|j| j["project"] = serde_json::Value::Null),
            ["project: goes with `active-config-locations.project-config`, which doesn't match it"]
        );
        assert_eq!(
            edit(&|j| j["active-config-locations"]["dev-container-path"] = serde_json::Value::Null),
            [
                "dev-container: goes with `active-config-locations.dev-container-path`, which doesn't match it"
            ]
        );
        // The contents are checked as the files would be.
        let errors = edit(&|j| j["dev-container"]["sshd-port"] = serde_json::json!("nope"));
        assert!(errors[0].starts_with("sshd-port: "), "{errors:?}");
        let errors = edit(&|j| j["project"]["commands"] = serde_json::json!("nope"));
        assert_eq!(errors, ["commands: must be a list of entries"]);

        // Without a project, there's none, nor a dev container.
        let config = load_universal_with_env(
            &dir.write(
                "plain.json",
                &crate::universal::to_json(
                    &crate::universal::build(
                        &load_all_with_env(&LoadOptions::default(), Env::default()).unwrap(),
                    )
                    .unwrap(),
                ),
            ),
            Env::default(),
        )
        .unwrap();
        assert!(config.project.is_none() && config.dev_container.is_none());
    }

    #[test]
    fn load_universal_runs_the_host_brokered_list() {
        let dir = TempDir::new();
        let (universal, _) = universal_for(&dir, Env::default());
        let load = |change: &dyn Fn(&mut serde_json::Value)| {
            let mut json: serde_json::Value =
                serde_json::from_str(&std::fs::read_to_string(&universal).unwrap()).unwrap();
            change(&mut json);
            let file = dir.write("edited.json", &json.to_string());
            load_universal_with_env(&file, Env::default())
        };

        // What's in `host-brokered` runs, not what's in `project`.
        let config = load(&|j| {
            let entry = &mut j["host-brokered"][0];
            entry["args"] = serde_json::json!(["y"]);
            entry["program"] = serde_json::json!("/usr/bin/one");
            entry["env"]["set"]["EXTRA"] = serde_json::json!("from the list");
        })
        .unwrap();
        let project = config.project.unwrap();
        let rule = &project.commands[0];
        assert_eq!(rule.args, [project::Token::Literal("y".to_string())]);
        assert_eq!(rule.program.as_deref(), Some("/usr/bin/one"));
        assert!(!rule.unavailable);
        assert_eq!(rule.env_plan.set["EXTRA"], "from the list");
        // Already expanded when the file was made; `@NAME@` is still to do.
        assert_eq!(rule.env_plan.set["HOME"], "@HOME@");
        assert_eq!(project.raw["commands"][0]["args"][0], "x");

        // A program that wasn't found makes the command unavailable.
        let config = load(&|j| j["host-brokered"][0]["program"] = serde_json::Value::Null).unwrap();
        assert!(config.project.unwrap().commands[0].unavailable);

        let errors = messages(
            load(&|j| {
                j["host-brokered"][0]
                    .as_object_mut()
                    .unwrap()
                    .remove("program");
            })
            .unwrap_err(),
        );
        assert_eq!(
            errors,
            ["host-brokered[0]: needs `program`: the program found, or null"]
        );
        let errors = messages(
            load(&|j| j["host-brokered"][0]["shell"] = serde_json::json!(true)).unwrap_err(),
        );
        assert!(
            errors[0].starts_with("host-brokered[0]: unknown field `shell`"),
            "{errors:?}"
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
