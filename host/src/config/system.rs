//! The system setup files: settings for this computer.  See
//! `docs/system-config-file.md`.
//!
//! These files are read, highest priority first:
//!   1. the user's setup file, the first found of
//!      `$SURF_SCOTER_HOME/setup.{yaml,yml}`,
//!      `$XDG_CONFIG_HOME/surf-scoter.setup.{yaml,yml}` and
//!      `$XDG_CONFIG_HOME/surf-scoter.d/setup.{yaml,yml}` (`~/.config` when
//!      `XDG_CONFIG_HOME` isn't set, or isn't absolute);
//!   2. `${scoter:distribution-dir}/setup.{yaml,yml}`, using the settings so
//!      far: `--distribution-dir`, or else what the file above sets.
//!
//! No setup file is ever read from the project: a setup file can run any
//! program, and decides what the project may do, and a project can come
//! from anyone.  So `SURF_SCOTER_HOME`, the user's setup file, and a
//! `distribution-dir` from the setup files must all be outside the project's
//! directories and the repository holding them; a `distribution-dir` from
//! the setup files must also be absolute, and not refer to `${scoter:cwd}`.
//!
//! Each top-level key is a section, holding its keys grouped by the kind of
//! value they have:
//!
//! ```yaml
//! docker:
//!   programs:
//!     exec: /usr/sbin/podman
//!   runs:
//!     build: ${exec} build -t {x:tag} -f {x:dockerfile} {x:dirname}
//!   path-lists:
//!     compose: [./.scoter/compose, ~/compose]
//!   values:
//!     retries: 3
//! ```
//!
//!   * `paths`: file locations.  A leading `~` is the home directory, and a
//!     leading `./` is relative to the project directory.
//!   * `programs`: executables.  Their locations are expanded as `paths` are,
//!     then a bare name is found on the `PATH`.
//!   * `runs`: command templates (see `command`): each either one string,
//!     split into arguments with the quoting rules, or a list with one entry
//!     per argument.
//!   * `path-lists`: lists of file locations, such as search paths.  Without
//!     a project file, `./` entries are left out.
//!   * `values`: single values (text, numbers, `true`/`false` or null), used
//!     as they are.
//!
//! A key has one kind of value: it's an error to set a key in two groups,
//! whether in one file or in different files (the kind is part of the key's
//! meaning).  Sections are merged key by key, the highest-priority file's
//! value winning.
//!
//! Values refer to other keys as `${section:key}`, or `${key}` within the same
//! section; `$${` stands for `${`.  An unknown reference is an error.  `paths`,
//! `programs` and `values` are text, usable anywhere (a null value can't be
//! used).
//! `runs` and `path-lists` are lists: a reference that is a whole argument of
//! a `runs` command, or a whole entry of a `path-lists` list, splices the list
//! in; anywhere else it's an error.  `{x:name}` is left for the command that
//! uses the value; `paths`, `programs` and `path-lists` can't have any.
//!
//! References to `programs` in a `Section` give the program found.  A program
//! that isn't there has no value, and neither does anything referring to it.
//! `paths` are given whether or not they're there; whoever uses them checks,
//! when it matters.
//!
//! The tool sets the `scoter` section's `paths` that say where the
//! configuration came from: `project-config`, `project-dir`,
//! `dev-container-path`, `project-clone-dir`,
//! `dev-container-settings-dir`, `user-setup-file`,
//! `user-dir`, `distribution-dir`, `distribution-bin-dir` and `cwd`.  Setup files can set
//! `distribution-dir` (the tool's value, when it has one, wins), but none of the others.
//! `distribution-bin-dir` is `${distribution-dir}/bin`, unless an earlier run's
//! (which another program may have changed) goes with the same distribution
//! directory.

use std::collections::{BTreeMap, HashMap, HashSet};
use std::ffi::OsString;
use std::path::{Path, PathBuf};

use serde::{Deserialize, Serialize};
use yaml_serde::Value;

use super::command::{self, CommandTemplate, Part};
use super::host_commands::{self, Check, CheckRecord};
use super::read::{parse_yaml, read_limited, tag_errors};
use super::simple_checks::{self, EGRESS_HOSTS, PROJECT_FILES, SimpleCheck};
use super::{ConfigError, child_key, index_key};

pub const SCOTER_SECTION: &str = "scoter";
// The `scoter` section's `paths` the tool sets.
pub const PROJECT_CONFIG: &str = "project-config";
pub const PROJECT_DIR: &str = "project-dir";
pub const DEV_CONTAINER_PATH: &str = "dev-container-path";
/// The project's clone, mounted in the dev container at its `mounts:
/// project-clone`.
pub const PROJECT_CLONE_DIR: &str = "project-clone-dir";
/// The settings directory, mounted read-only in the dev container.
pub const DEV_CONTAINER_SETTINGS_DIR: &str = "dev-container-settings-dir";
pub const USER_SETUP_FILE: &str = "user-setup-file";
pub const USER_DIR: &str = "user-dir";
/// The one a setup file can also set.
pub const DISTRIBUTION_DIR: &str = "distribution-dir";
/// The distribution's programs: `${distribution-dir}/bin`, unless another
/// program changed it.
pub const DISTRIBUTION_BIN_DIR: &str = "distribution-bin-dir";
pub const CWD: &str = "cwd";
/// The setup section for what a review checks, and where acceptances are kept.
pub const VERIFICATION_SECTION: &str = "verification";
/// Where the user's acceptances of projects' settings are kept, in the
/// `verification` section's `paths`.  Set by the setup files, not the tool;
/// it decides what a project may do, so it must be absolute and outside the
/// project.
pub const VERIFIED_STORE: &str = "store";
/// Where the tools keep their own state, such as the running environments.
/// Like `verification:store`, it must be absolute and outside the project.
pub const STATE_DIR: &str = "state-dir";
/// The root of the repository holding the project, found by `scoter` from
/// the setup's VCS sections and given to the generator (`--vcs-root`).
pub const VCS_ROOT: &str = "vcs-root";
/// The project's id, the same for the same project directory: its name and a
/// hash of its canonical path (`environment_id`).  The verified store names
/// the project's directory by it, and the setup can use it to name a
/// project state directory outside the project.
pub const PROJECT_ID: &str = "project-id";
/// The project's branch (`--project-branch`, from the VCS section's
/// `current-branch`), made safe as a single path part (`path_part`).  Not
/// set without a branch.
pub const PROJECT_BRANCH: &str = "project-branch";
/// The `scoter` `paths` only the tool sets.
const TOOL_ONLY_KEYS: [&str; 12] = [
    PROJECT_CONFIG,
    PROJECT_DIR,
    PROJECT_ID,
    PROJECT_BRANCH,
    VCS_ROOT,
    DEV_CONTAINER_PATH,
    PROJECT_CLONE_DIR,
    DEV_CONTAINER_SETTINGS_DIR,
    USER_SETUP_FILE,
    USER_DIR,
    DISTRIBUTION_BIN_DIR,
    CWD,
];
/// The `programs` key conventionally naming a section's main program.
pub const EXEC: &str = "exec";
/// A section's file locations.
pub const PATHS: &str = "paths";
/// A section's executables, found on the `PATH`.
pub const PROGRAMS: &str = "programs";
/// A section's command templates.
pub const RUNS: &str = "runs";
/// A section's lists of file locations.
pub const PATH_LISTS: &str = "path-lists";
/// A section's lists of plain values.
pub const VALUE_LISTS: &str = "value-lists";
/// A section's plain values.
pub const VALUES: &str = "values";
/// A section's checks on the project's host commands.
pub const HOST_COMMANDS: &str = "host-commands";
const SETUP: &str = "setup";
const USER_SETUP: &str = "surf-scoter.setup";

/// What the process environment says about where things are.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct Env {
    /// The user's home directory.
    pub home: Option<PathBuf>,
    /// `SURF_SCOTER_HOME`.
    pub scoter_home: Option<PathBuf>,
    /// `XDG_CONFIG_HOME`, when it's an absolute path.
    pub config_home: Option<PathBuf>,
    /// `PATH`.
    pub path: Option<OsString>,
    /// `PATHEXT`, on Windows.
    pub path_ext: Option<OsString>,
    /// The current directory.
    pub cwd: Option<PathBuf>,
    /// The environment variables (those with UTF-8 names and values).
    pub vars: HashMap<String, String>,
}

impl Env {
    pub fn from_process() -> Self {
        let var = |name: &str| std::env::var_os(name).filter(|v| !v.is_empty());
        Env {
            home: var("HOME")
                .or_else(|| var("USERPROFILE"))
                .map(PathBuf::from),
            scoter_home: var("SURF_SCOTER_HOME").map(PathBuf::from),
            // The basedir spec says a relative value is to be ignored.
            config_home: var("XDG_CONFIG_HOME")
                .map(PathBuf::from)
                .filter(|p| p.is_absolute()),
            path: var("PATH"),
            path_ext: if cfg!(windows) { var("PATHEXT") } else { None },
            cwd: std::env::current_dir().ok(),
            vars: std::env::vars_os()
                .filter_map(|(k, v)| Some((k.into_string().ok()?, v.into_string().ok()?)))
                .collect(),
        }
    }

    /// The user's configuration directory: `XDG_CONFIG_HOME`, or else
    /// `~/.config`.
    pub fn config_dir(&self) -> Option<PathBuf> {
        self.config_home
            .clone()
            .or_else(|| self.home.as_ref().map(|home| home.join(".config")))
    }

    /// `text` with a leading `~` replaced by the home directory.
    pub fn expand_home(&self, text: &str) -> PathBuf {
        if let Some(home) = &self.home {
            if text == "~" {
                return home.clone();
            }
            let rest = text.strip_prefix("~/").or_else(|| {
                if cfg!(windows) {
                    text.strip_prefix("~\\")
                } else {
                    None
                }
            });
            if let Some(rest) = rest {
                return home.join(rest);
            }
        }
        PathBuf::from(text)
    }
}

/// A value, and the file that set it (`None` when the tool set it).
#[derive(Debug, Clone, PartialEq)]
struct Setting<T> {
    value: T,
    file: Option<PathBuf>,
}

/// A `runs` command, as written.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(untagged)]
pub enum RunValue {
    /// One string, split into arguments with the quoting rules.
    Line(String),
    /// One entry per argument, used as is.
    Args(Vec<String>),
}

/// A `values` entry: a single value.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(untagged)]
pub enum ScalarValue {
    Null,
    Bool(bool),
    Int(i64),
    UInt(u64),
    Float(f64),
    String(String),
}

impl ScalarValue {
    /// The value as text, for use in other values; `None` for null.
    fn text(&self) -> Option<String> {
        match self {
            ScalarValue::Null => None,
            ScalarValue::Bool(b) => Some(b.to_string()),
            ScalarValue::Int(i) => Some(i.to_string()),
            ScalarValue::UInt(u) => Some(u.to_string()),
            ScalarValue::Float(f) => Some(f.to_string()),
            ScalarValue::String(s) => Some(s.clone()),
        }
    }
}

/// One key's value, as written, with its kind.
#[derive(Debug, Clone, PartialEq)]
enum Entry {
    Path(String),
    Program(String),
    Run(RunValue),
    PathList(Vec<String>),
    ValueList(Vec<String>),
    Value(ScalarValue),
    /// A `host-commands` check; `None` (null) removes a lower priority one.
    HostCommand(Option<CheckRecord>),
    /// A `project-files` or `egress-hosts` check (the group's name, and the
    /// check); `None` (null) removes a lower priority one.
    Simple(&'static str, Option<SimpleCheck>),
}

impl Entry {
    /// The group the entry is written in.
    fn group(&self) -> &'static str {
        match self {
            Entry::Path(_) => PATHS,
            Entry::Program(_) => PROGRAMS,
            Entry::Run(_) => RUNS,
            Entry::PathList(_) => PATH_LISTS,
            Entry::ValueList(_) => VALUE_LISTS,
            Entry::Value(_) => VALUES,
            Entry::HostCommand(_) => HOST_COMMANDS,
            Entry::Simple(group, _) => group,
        }
    }
}

/// How a setup file was found, which sets its priority: earlier ones first.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Serialize, Deserialize)]
#[serde(rename_all = "kebab-case")]
pub enum Source {
    /// The user's setup file.
    User,
    /// The distribution directory's.
    Distribution,
}

/// The system setup as read: each file's settings as written, so they can be
/// expanded again (say, against a project file found later) without reading
/// the files again.
#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "kebab-case", deny_unknown_fields)]
pub struct SetupRecord {
    /// Highest priority first.
    pub files: Vec<SetupFileRecord>,
}

/// A history of the runs' inputs, most recent first.  They're only a record,
/// and aren't used to work out the configuration.  Duplicates are allowed,
/// but removed when written.
#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "kebab-case", deny_unknown_fields)]
pub struct LoadedFrom {
    /// The user setup files found.
    #[serde(default)]
    pub user_setup_file: Vec<String>,
    /// The user directories found.
    #[serde(default)]
    pub user_dir: Vec<String>,
    /// The `--distribution-dir` directories given.
    #[serde(default)]
    pub distribution_dir: Vec<String>,
    /// The project configuration files given.
    #[serde(default)]
    pub project_config: Vec<String>,
}

impl LoadedFrom {
    /// Without duplicates, keeping each value's most recent place.
    fn without_duplicates(mut self) -> Self {
        for list in [
            &mut self.user_setup_file,
            &mut self.user_dir,
            &mut self.distribution_dir,
            &mut self.project_config,
        ] {
            let mut seen = Vec::new();
            list.retain(|value| {
                let new = !seen.contains(value);
                if new {
                    seen.push(value.clone());
                }
                new
            });
        }
        self
    }
}

/// The tool's own inputs to the setup that were in effect: the user's setup
/// file and directory found, the distribution directory given (which
/// wins over the setup files'), and its `bin` directory.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct SetupInputs {
    pub user_setup_file: Option<String>,
    pub user_dir: Option<String>,
    pub distribution_dir: Option<String>,
    /// Kept while the distribution directory stays the same.
    pub distribution_bin_dir: Option<String>,
}

/// Setup already worked out: its record, its history, and the inputs that
/// were in effect (they aren't in any file).
#[derive(Debug, Clone, Copy)]
pub struct SetupBase<'a> {
    pub record: &'a SetupRecord,
    pub loaded_from: &'a LoadedFrom,
    /// The file it came from, for messages.
    pub origin: &'a Path,
    pub inputs: &'a SetupInputs,
}

/// One setup file's settings, as written.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "kebab-case", deny_unknown_fields)]
pub struct SetupFileRecord {
    pub file: String,
    pub source: Source,
    pub sections: BTreeMap<String, SectionRecord>,
}

/// One section of a setup file, as written.
#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "kebab-case", deny_unknown_fields)]
pub struct SectionRecord {
    #[serde(default)]
    pub paths: BTreeMap<String, String>,
    #[serde(default)]
    pub programs: BTreeMap<String, String>,
    #[serde(default)]
    pub runs: BTreeMap<String, RunValue>,
    #[serde(default)]
    pub path_lists: BTreeMap<String, Vec<String>>,
    #[serde(default, skip_serializing_if = "BTreeMap::is_empty")]
    pub value_lists: BTreeMap<String, Vec<String>>,
    #[serde(default)]
    pub values: BTreeMap<String, ScalarValue>,
    #[serde(default, skip_serializing_if = "BTreeMap::is_empty")]
    pub host_commands: BTreeMap<String, Option<CheckRecord>>,
    #[serde(default, skip_serializing_if = "BTreeMap::is_empty")]
    pub project_files: BTreeMap<String, Option<SimpleCheck>>,
    #[serde(default, skip_serializing_if = "BTreeMap::is_empty")]
    pub egress_hosts: BTreeMap<String, Option<SimpleCheck>>,
}

/// Where to find the system setup.
#[derive(Debug, Clone, Copy, Default)]
pub struct SetupOptions<'a> {
    pub distribution_dir: Option<&'a Path>,
    /// Where `./` paths start: `scoter:project-dir`.
    pub project_dir: Option<&'a Path>,
    /// The project file in effect: `scoter:project-config`.
    pub project_config: Option<&'a Path>,
    /// The root of the repository holding the project: `scoter:vcs-root`.
    pub vcs_root: Option<&'a Path>,
    /// The project's branch, as the VCS gave it: `scoter:project-branch`,
    /// once made safe as a path part.
    pub project_branch: Option<&'a str>,
    /// The dev container's mounts: `scoter:project-clone-dir` and
    /// `scoter:dev-container-settings-dir`.
    pub project_clone_dir: Option<&'a Path>,
    pub dev_container_settings_dir: Option<&'a Path>,
    /// The project has a dev container, whose directory is set as
    /// `scoter:dev-container-path` once it's found (with
    /// `SystemConfig::set_dev_container_path`).  Until then, references to it
    /// are checked as if it were set.
    pub dev_container: bool,
    /// Don't look for the user's setup file, or the distribution directory's
    /// when `distribution_dir` isn't given.
    pub no_search: bool,
    /// Setup already worked out.  Its files aren't read again.
    pub base: Option<SetupBase<'a>>,
    /// Work out `scoter:project-config`, the project file, from
    /// `project_dir` and the setup's names for the project files directory and
    /// the file, unless `project_config` gives it.
    pub find_project_config: bool,
    /// Record the project file in the history: a project directory was
    /// given this time.
    pub record_project: bool,
}

/// A section's merged keys.
type SectionSettings = BTreeMap<String, Setting<Entry>>;
type Sections = BTreeMap<String, SectionSettings>;

/// The merged system configuration.
#[derive(Debug)]
pub struct SystemConfig {
    /// The files read, highest priority first.
    files: Vec<PathBuf>,
    sections: Sections,
    env: Env,
    /// Where `./` paths start.
    project_dir: Option<PathBuf>,
    /// The setup, as read.
    record: SetupRecord,
    /// The history of the runs' inputs.
    loaded_from: LoadedFrom,
    /// The tool's own inputs that were in effect.
    inputs: SetupInputs,
}

/// A section, expanded.  A value is `None` (or a null `values` entry) when it
/// is, or refers to, a program that isn't there.  Paths are given whether or
/// not they're there: whoever uses them checks, when it matters.
#[derive(Debug, Clone, PartialEq)]
pub struct Section {
    pub name: String,
    /// The file locations.
    pub paths: BTreeMap<String, Option<PathBuf>>,
    /// The programs found.
    pub programs: BTreeMap<String, Option<PathBuf>>,
    /// The commands; a reference to a program, such as `${exec}`, is the
    /// program found.
    pub runs: BTreeMap<String, Option<CommandTemplate>>,
    /// The lists of file locations.
    pub path_lists: BTreeMap<String, Option<Vec<PathBuf>>>,
    /// The lists of plain values.
    pub value_lists: BTreeMap<String, Option<Vec<String>>>,
    /// The plain values.
    pub values: BTreeMap<String, ScalarValue>,
    /// The `host-commands` checks; removed (null) ones aren't here.
    pub host_commands: BTreeMap<String, Check>,
    /// The `project-files` and `egress-hosts` checks, as written; removed
    /// (null) ones aren't here.
    pub project_files: BTreeMap<String, SimpleCheck>,
    pub egress_hosts: BTreeMap<String, SimpleCheck>,
    /// The `programs` that aren't there: key -> the program it names.
    pub missing: BTreeMap<String, String>,
}

/// Values for keys no setup file sets, as (key, value) pairs.
#[derive(Debug, Clone, Copy, Default)]
pub struct SectionDefaults<'a> {
    pub paths: &'a [(&'a str, &'a str)],
    pub programs: &'a [(&'a str, &'a str)],
    pub runs: &'a [(&'a str, &'a str)],
}

/// A file location: a leading `./` (or `.`) is relative to `project_dir`, when
/// there is one, and a leading `~` is the home directory.
fn resolve_path(text: &str, env: &Env, project_dir: Option<&Path>) -> PathBuf {
    match project_dir {
        Some(dir) if is_project_relative(text) => {
            dir.join(text.trim_start_matches('.').trim_start_matches('/'))
        }
        _ => env.expand_home(text),
    }
}

fn is_project_relative(text: &str) -> bool {
    text == "." || text.starts_with("./")
}

/// Read the system configuration.  `project_dir` is where `./` paths start;
/// without it, `./` `path-lists` entries are left out, and `./` `paths`
/// values are kept as written.
pub fn load(
    distribution_dir: Option<&Path>,
    project_dir: Option<&Path>,
    env: Env,
) -> Result<SystemConfig, Vec<ConfigError>> {
    let options = SetupOptions {
        distribution_dir,
        project_dir,
        ..SetupOptions::default()
    };
    load_with(&options, env)
}

/// Read the system configuration, starting from `options.base` when given.
/// Files found that aren't in the base are added with their usual priority.
pub fn load_with(options: &SetupOptions, env: Env) -> Result<SystemConfig, Vec<ConfigError>> {
    let mut errors = Vec::new();
    let mut base_files = Vec::new();
    let mut record = SetupRecord::default();
    let mut history = LoadedFrom::default();
    // The tool's inputs: from the base, unless given or found again.
    let mut inputs = SetupInputs::default();
    if let Some(base) = options.base {
        for (i, file) in base.record.files.iter().enumerate() {
            match file_from_record(file, base.origin, i) {
                Ok(file) => base_files.push(file),
                Err(e) => errors.extend(e),
            }
        }
        record = base.record.clone();
        history = base.loaded_from.clone();
        inputs = base.inputs.clone();
    }
    // The distribution directory `inputs.distribution_bin_dir` goes with.
    let base_distribution_dir = inputs.distribution_dir.clone();
    let distribution_dir = options.distribution_dir.map(super::absolute);
    if let Some(dir) = &distribution_dir {
        inputs.distribution_dir = Some(path_text(dir));
        history.distribution_dir.insert(0, path_text(dir));
    }

    // Setup files found in the project, reported after the setup's own
    // problems.
    let mut location_errors = Vec::new();
    // `SURF_SCOTER_HOME` can be set by the project itself (say, through a
    // `.envrc` that runs when you enter its directory), so it's checked
    // whenever the project is known, whether or not it's searched this time.
    let home_in_project = if let Some(home) = env.scoter_home.as_deref().map(super::absolute)
        && let Some(name) = in_project(&home, options)
    {
        location_errors.push(ConfigError::new(
            Some(&home),
            "",
            format!(
                "`SURF_SCOTER_HOME` is in the project (`{name}`); the user's setup file is never read from a project"
            ),
        ));
        true
    } else {
        false
    };
    let mut found: Vec<(PathBuf, Source)> = Vec::new();
    if !options.no_search {
        match find_user_setup(&env) {
            Ok(Some((file, dir))) => {
                inputs.user_setup_file = Some(path_text(&file));
                inputs.user_dir = dir.as_deref().map(path_text);
                history.user_setup_file.insert(0, path_text(&file));
                if let Some(dir) = &inputs.user_dir {
                    history.user_dir.insert(0, dir.clone());
                }
                found.push((file, Source::User));
            }
            Ok(None) => {}
            Err(e) => errors.push(e),
        }
    }
    let mut tried: Vec<PathBuf> = base_files.iter().map(|f| f.path.clone()).collect();
    let mut files = Vec::new();
    for (path, source) in found {
        if !tried.iter().any(|t| same_file(t, &path)) {
            match load_file(&path, source) {
                Ok(file) => files.push(file),
                Err(e) => errors.extend(e),
            }
            tried.push(path);
        }
    }
    // New files come before the base's with the same priority.
    files.extend(base_files);
    files.sort_by_key(|f| f.source);

    let mut tool_values: Vec<(&str, Entry)> = Vec::new();
    let mut add =
        |key, value: Option<String>| tool_values.extend(value.map(|v| (key, Entry::Path(v))));
    add(PROJECT_CONFIG, options.project_config.map(path_text));
    add(PROJECT_DIR, options.project_dir.map(path_text));
    add(VCS_ROOT, options.vcs_root.map(path_text));
    add(PROJECT_CLONE_DIR, options.project_clone_dir.map(path_text));
    add(
        DEV_CONTAINER_SETTINGS_DIR,
        options.dev_container_settings_dir.map(path_text),
    );
    add(USER_SETUP_FILE, inputs.user_setup_file.clone());
    add(USER_DIR, inputs.user_dir.clone());
    add(DISTRIBUTION_DIR, inputs.distribution_dir.clone());
    add(CWD, env.cwd.as_deref().map(path_text));
    // Text, not paths: names to build paths from.
    let text = |value: String| Entry::Value(ScalarValue::String(value));
    tool_values.extend(
        options
            .project_dir
            .map(|dir| (PROJECT_ID, text(environment_id(dir)))),
    );
    tool_values.extend(
        options
            .project_branch
            .and_then(path_part)
            .map(|branch| (PROJECT_BRANCH, text(branch))),
    );
    let merge = |files: &[SetupFile]| {
        SystemConfig::merge(files, &tool_values, env.clone(), options.project_dir)
    };
    let mut config = merge(&files);
    // A `distribution-dir` from the setup files can't depend on where the
    // tool runs, which is likely the project: worked out again with another
    // `cwd`, it must be the same.  That finds `${scoter:cwd}` however it's
    // referred to.
    let given = inputs.distribution_dir.is_some();
    let uses_cwd = !given && {
        let elsewhere: Vec<(&str, Entry)> = tool_values
            .iter()
            .filter(|(key, _)| *key != CWD)
            .cloned()
            .chain([(CWD, Entry::Path("/surf-scoter-not-the-cwd".to_string()))])
            .collect();
        SystemConfig::merge(&files, &elsewhere, env.clone(), options.project_dir)
            .get(SCOTER_SECTION, DISTRIBUTION_DIR)
            != config.get(SCOTER_SECTION, DISTRIBUTION_DIR)
    };
    if uses_cwd {
        errors.push(ConfigError::new(
            None,
            "",
            format!(
                "`{SCOTER_SECTION}:{DISTRIBUTION_DIR}` can't refer to `${{{SCOTER_SECTION}:{CWD}}}`: setup files aren't read from wherever the tool runs; give `--distribution-dir` instead"
            ),
        ));
    }
    // The distribution directory's file comes last, as the files so far say
    // where it is.  A base's is already among its files, and a file already
    // read isn't read again.  Errors in `distribution-dir` itself are
    // reported with the rest, below.
    if !uses_cwd && (options.distribution_dir.is_some() || !options.no_search) {
        let distribution_dir = config.get_path(SCOTER_SECTION, DISTRIBUTION_DIR);
        let problem = distribution_dir
            .as_deref()
            .filter(|_| !given)
            .and_then(|dir| untrusted_setup_dir(dir, options));
        if let Some(problem) = problem {
            location_errors.push(ConfigError::new(None, "", problem));
        } else if let Some(dir) = distribution_dir {
            match find_setup(&dir, SETUP) {
                Ok(Some(path)) if !tried.iter().any(|t| same_file(t, &path)) => {
                    match load_file(&path, Source::Distribution) {
                        Ok(file) => {
                            files.push(file);
                            files.sort_by_key(|f| f.source);
                            config = merge(&files);
                        }
                        Err(e) => errors.extend(e),
                    }
                }
                Ok(_) => {}
                Err(e) => errors.push(e),
            }
        }
    }

    // Set last, as the distribution directory can come from any of the files.
    let distribution_dir = config.get(SCOTER_SECTION, DISTRIBUTION_DIR);
    inputs.distribution_bin_dir =
        if inputs.distribution_bin_dir.is_some() && base_distribution_dir == distribution_dir {
            inputs.distribution_bin_dir.take()
        } else {
            distribution_dir.map(|dir| path_text(&Path::new(&dir).join("bin")))
        };
    if let Some(dir) = &inputs.distribution_bin_dir {
        config.set_location(DISTRIBUTION_BIN_DIR, dir.clone());
    }

    // A base's user file was found before the project was known.
    for file in files
        .iter()
        .filter(|f| f.source == Source::User && !home_in_project)
    {
        let dir = file.path.parent().unwrap_or(Path::new(""));
        if let Some(name) = in_project(dir, options) {
            location_errors.push(ConfigError::new(
                Some(&file.path),
                "",
                format!(
                    "the user's setup file is in the project (`{name}`); it's never read from a project"
                ),
            ));
        }
    }
    // A base's distribution file was found before the project was known.
    if inputs.distribution_dir.is_none() {
        for file in files.iter().filter(|f| f.source == Source::Distribution) {
            let dir = file.path.parent().unwrap_or(Path::new(""));
            if let Some(problem) = untrusted_setup_dir(dir, options) {
                location_errors.push(ConfigError::new(Some(&file.path), "", problem));
            }
        }
    }
    // The accepted project settings decide what a project may do, and the
    // state says which environments run where, so neither can be kept where
    // the project, or the container, could change them.
    for (section, key, what) in [
        (
            VERIFICATION_SECTION,
            VERIFIED_STORE,
            "the accepted project settings are",
        ),
        (SCOTER_SECTION, STATE_DIR, "the tools' state is"),
    ] {
        let Some(dir) = config.get_path(section, key) else {
            continue;
        };
        let setting = format!("`{section}:{key}` {}", dir.display());
        if !dir.is_absolute() {
            errors.push(ConfigError::new(
                None,
                "",
                format!("{setting} must be an absolute path"),
            ));
        } else if let Some(name) = in_project(&dir, options) {
            location_errors.push(ConfigError::new(
                None,
                "",
                format!("{setting} is in the project (`{name}`); {what} never kept in a project"),
            ));
        }
    }
    errors.extend(location_errors);
    // The project file is in the project directory's files directory, as the
    // setup names them.  (A universal file made earlier gives it as it was.)
    if let (true, None, Some(project_dir)) = (
        options.find_project_config,
        options.project_config,
        options.project_dir,
    ) {
        match project_file_path(&config, project_dir) {
            Ok(path) => {
                if options.record_project {
                    history.project_config.insert(0, path_text(&path));
                }
                config.set_location(PROJECT_CONFIG, path_text(&path));
            }
            Err(e) => errors.push(e),
        }
    }

    errors.extend(kind_changes(&files));
    // Stand in for the dev container's directory until it's found.
    let mut later = Fallbacks::new();
    if options.dev_container {
        later.insert(
            (SCOTER_SECTION.to_string(), DEV_CONTAINER_PATH.to_string()),
            Entry::Path(DEV_CONTAINER_PATH.to_string()),
        );
    }
    errors.extend(config.validate(&later));
    record.files = files.iter().map(SetupFile::record).collect();
    config.record = record;
    config.loaded_from = history.without_duplicates();
    config.inputs = inputs;
    if errors.is_empty() {
        Ok(config)
    } else {
        Err(errors)
    }
}

/// The `scoter` section's `values` key naming the project files directory:
/// the directory, in the project directory, holding the project file and the
/// project's dev containers.
pub const PROJECT_FILES_DIR: &str = "project-files-dir";

/// The `scoter` section's `values` key naming the project file, in the
/// project files directory.
pub const PROJECT_FILE_NAME: &str = "project-file-name";

/// The project file for `project_dir`: `PROJECT_FILES_DIR/PROJECT_FILE_NAME`
/// in it, as the setup names them, each a single name.
fn project_file_path(config: &SystemConfig, project_dir: &Path) -> Result<PathBuf, ConfigError> {
    let name = |key: &str, what: &str| -> Result<String, ConfigError> {
        let setting = format!("the system configuration's `{SCOTER_SECTION}` `values: {key}`");
        let name = match config.value(SCOTER_SECTION, key) {
            Some(ScalarValue::String(name)) => name,
            Some(_) => {
                return Err(ConfigError::new(
                    None,
                    "",
                    format!("{setting} must be text"),
                ));
            }
            None => {
                return Err(ConfigError::new(
                    None,
                    "",
                    format!(
                        "the system configuration sets no `{SCOTER_SECTION}` `values: {key}`, {what}"
                    ),
                ));
            }
        };
        if name.is_empty() || name == "." || name == ".." || name.contains(['/', '\\']) {
            return Err(ConfigError::new(
                None,
                "",
                format!("{setting} ('{name}') must be a single name, without '/' or '\\'"),
            ));
        }
        Ok(name)
    };
    let files_dir = name(PROJECT_FILES_DIR, "the directory holding the project file")?;
    let file = name(PROJECT_FILE_NAME, "the project file's name")?;
    Ok(project_dir.join(files_dir).join(file))
}

/// Why the tools can't read a setup file they found by themselves in `dir`,
/// a `distribution-dir` that came from the setup files, if they can't: it's
/// relative (to the directory the tool runs in, likely the project), or in
/// the project.  A setup file can run any program, so it's never taken from
/// the project.
fn untrusted_setup_dir(dir: &Path, options: &SetupOptions) -> Option<String> {
    let setting = format!("`{SCOTER_SECTION}:{DISTRIBUTION_DIR}` {}", dir.display());
    if !dir.is_absolute() {
        return Some(format!(
            "{setting} must be absolute to find a setup file in it; give `--distribution-dir` instead"
        ));
    }
    in_project(dir, options).map(|name| {
        format!("{setting} is in the project (`{name}`); setup files are never read from a project")
    })
}

/// The name of the project location `dir` is in, once symlinks are
/// resolved, if it's in one: the project directory, the project files
/// directory, the project's clone, or the repository holding the
/// project (`scoter:vcs-root`).
fn in_project(dir: &Path, options: &SetupOptions) -> Option<String> {
    let canonical = resolve_partly;
    let dir = canonical(dir);
    let project_dir = options.project_dir.or_else(|| {
        options
            .project_config
            .and_then(Path::parent)
            .and_then(Path::parent)
    });
    let locations = [
        (
            format!("{SCOTER_SECTION}:{PROJECT_DIR}"),
            project_dir.map(Path::to_path_buf),
        ),
        (
            format!("{SCOTER_SECTION}:{PROJECT_CONFIG}"),
            options
                .project_config
                .and_then(Path::parent)
                .map(Path::to_path_buf),
        ),
        (
            format!("{SCOTER_SECTION}:{PROJECT_CLONE_DIR}"),
            options.project_clone_dir.map(Path::to_path_buf),
        ),
        (
            format!("{SCOTER_SECTION}:{VCS_ROOT}"),
            options.vcs_root.map(Path::to_path_buf),
        ),
    ];
    locations.into_iter().find_map(|(name, root)| {
        root.filter(|root| dir.starts_with(canonical(root)))
            .map(|_| name)
    })
}

/// `path` with symlinks resolved, as far as it exists: the longest part that
/// exists is made canonical, and the rest (which can't hold a symlink) is
/// added with `.` and `..` worked out.  A path that doesn't exist yet can then
/// still be compared with others: written as `a/../b`, it's where `b` is.
fn resolve_partly(path: &Path) -> PathBuf {
    use std::path::Component;
    let mut rest = Vec::new();
    let mut base = path;
    let mut resolved = loop {
        if let Ok(found) = base.canonicalize() {
            break found;
        }
        match (base.parent(), base.file_name()) {
            (Some(parent), Some(_)) => {
                rest.push(base.components().next_back());
                base = parent;
            }
            // Nothing of it exists, or it ends in `..`: work it out as written.
            _ => {
                rest.clear();
                break PathBuf::new();
            }
        }
    };
    if resolved.as_os_str().is_empty() {
        for part in path.components() {
            match part {
                Component::ParentDir => {
                    resolved.pop();
                }
                Component::CurDir => {}
                other => resolved.push(other),
            }
        }
        return resolved;
    }
    for part in rest.into_iter().rev().flatten() {
        match part {
            Component::ParentDir => {
                resolved.pop();
            }
            Component::CurDir => {}
            other => resolved.push(other),
        }
    }
    resolved
}

/// The user's setup file, and the user directory it's in (when it's in one).
fn find_user_setup(env: &Env) -> Result<Option<(PathBuf, Option<PathBuf>)>, ConfigError> {
    if let Some(dir) = env.scoter_home.as_deref().map(super::absolute)
        && let Some(file) = find_setup(&dir, SETUP)?
    {
        return Ok(Some((file, Some(dir))));
    }
    if let Some(config) = env.config_dir() {
        if let Some(file) = find_setup(&config, USER_SETUP)? {
            return Ok(Some((file, None)));
        }
        let dir = config.join("surf-scoter.d");
        if let Some(file) = find_setup(&dir, SETUP)? {
            return Ok(Some((file, Some(dir))));
        }
    }
    Ok(None)
}

/// `dir/stem.yaml` or `dir/stem.yml`; having both is an error.
fn find_setup(dir: &Path, stem: &str) -> Result<Option<PathBuf>, ConfigError> {
    let yaml = dir.join(format!("{stem}.yaml"));
    let yml = dir.join(format!("{stem}.yml"));
    match (yaml.is_file(), yml.is_file()) {
        (true, true) => Err(ConfigError::new(
            Some(dir),
            "",
            format!("both {stem}.yaml and {stem}.yml exist; keep only one"),
        )),
        (true, false) => Ok(Some(yaml)),
        (false, true) => Ok(Some(yml)),
        (false, false) => Ok(None),
    }
}

/// The variable `name` in `vars`, matched without regard to case when
/// `ignore_case`.
fn find_var<'a>(
    vars: &'a HashMap<String, String>,
    name: &str,
    ignore_case: bool,
) -> Option<&'a str> {
    if let Some(value) = vars.get(name) {
        return Some(value);
    }
    if !ignore_case {
        return None;
    }
    let name = name.to_uppercase();
    vars.iter()
        .find(|(key, _)| key.to_uppercase() == name)
        .map(|(_, value)| value.as_str())
}

/// Whether `section:key` is one of the locations the tool sets, which a run
/// may not have (`project-dir` without a project, say).
fn is_location(section: &str, key: &str) -> bool {
    is_tool_only(section, key) || (section == SCOTER_SECTION && key == DISTRIBUTION_DIR)
}

/// The most characters of the directory name in a project's id.
const ID_NAME_MAX_CHARS: usize = 32;
/// The id's name when the directory's name leaves nothing.
const ID_FALLBACK_NAME: &str = "project";
/// How many bytes of the path's SHA-256 the id's hash holds (two hex digits each).
const ID_HASH_BYTES: usize = 6;
/// The most characters `path_part` keeps.
const PATH_PART_MAX_CHARS: usize = 64;

/// The project's id: `NAME-HASH`, from the project directory's canonical path,
/// exactly as the verified store works it out (`store.environment_id` in the
/// scripts' library).
///
/// `NAME` is the directory's name, lower-cased, with each run of anything
/// outside `[a-z0-9-]` made one `-`, cut to 32 characters and trimmed of `-`
/// (`project` if that leaves nothing).  `HASH` is the first 12 hex digits of
/// the SHA-256 of the canonical path's bytes.
pub fn environment_id(project_dir: &Path) -> String {
    use sha2::{Digest, Sha256};
    let canonical = project_dir
        .canonicalize()
        .unwrap_or_else(|_| project_dir.to_path_buf());
    let base = canonical
        .file_name()
        .map(|n| n.to_string_lossy().to_lowercase())
        .unwrap_or_default();
    let mut name = String::new();
    let mut in_run = false;
    for c in base.chars() {
        if c.is_ascii_lowercase() || c.is_ascii_digit() || c == '-' {
            name.push(c);
            in_run = false;
        } else if !in_run {
            name.push('-');
            in_run = true;
        }
    }
    let name: String = name.chars().take(ID_NAME_MAX_CHARS).collect();
    let name = name.trim_matches('-');
    #[cfg(unix)]
    let bytes = {
        use std::os::unix::ffi::OsStrExt;
        canonical.as_os_str().as_bytes().to_vec()
    };
    #[cfg(not(unix))]
    let bytes = canonical.to_string_lossy().into_owned().into_bytes();
    let digest = Sha256::digest(&bytes);
    let hash: String = digest
        .iter()
        .take(ID_HASH_BYTES)
        .map(|b| format!("{b:02x}"))
        .collect();
    format!(
        "{}-{hash}",
        if name.is_empty() {
            ID_FALLBACK_NAME
        } else {
            name
        }
    )
}

/// `text` made safe as one path part, or None if nothing's left.
///
/// A branch name is the project's to choose (`../../.ssh`, or one holding a
/// newline or `${...}`), so only letters, digits, `.`, `_` and `-` are kept,
/// each run of anything else made one `-`; `-` and `.` are trimmed from the
/// ends (so `.` and `..` leave nothing), and it's cut to 64 characters.
pub fn path_part(text: &str) -> Option<String> {
    let mut out = String::new();
    let mut in_run = false;
    for c in text.chars() {
        if c.is_ascii_alphanumeric() || matches!(c, '.' | '_' | '-') {
            out.push(c);
            in_run = false;
        } else if !in_run {
            out.push('-');
            in_run = true;
        }
    }
    let cut: String = out.chars().take(PATH_PART_MAX_CHARS).collect();
    let part = cut.trim_matches(|c| c == '-' || c == '.');
    (!part.is_empty()).then(|| part.to_string())
}

/// The message for a reference to a location this run doesn't have.
fn unset_location(section: &str, key: &str) -> String {
    format!("`${{{section}:{key}}}` isn't set for this run")
}

/// Whether only the tool can set `section:key`.
fn is_tool_only(section: &str, key: &str) -> bool {
    section == SCOTER_SECTION && TOOL_ONLY_KEYS.contains(&key)
}

fn same_file(a: &Path, b: &Path) -> bool {
    match (a.canonicalize(), b.canonicalize()) {
        (Ok(a), Ok(b)) => a == b,
        _ => a == b,
    }
}

fn path_text(path: &Path) -> String {
    path.to_string_lossy().into_owned()
}

/// `GROUP.key` (such as `paths.key`): where a value is within its section.
fn group_key(group: &str, key: &str) -> String {
    format!("{group}.{key}")
}

/// One setup file's contents: each section's keys.
#[derive(Debug)]
struct SetupFile {
    path: PathBuf,
    source: Source,
    sections: BTreeMap<String, BTreeMap<String, Entry>>,
}

impl SetupFile {
    /// The settings as written, grouped by kind.
    fn record(&self) -> SetupFileRecord {
        let mut sections = BTreeMap::new();
        for (name, entries) in &self.sections {
            let mut section = SectionRecord::default();
            for (key, entry) in entries {
                let key = key.clone();
                match entry {
                    Entry::Path(path) => {
                        section.paths.insert(key, path.clone());
                    }
                    Entry::Program(program) => {
                        section.programs.insert(key, program.clone());
                    }
                    Entry::Run(run) => {
                        section.runs.insert(key, run.clone());
                    }
                    Entry::ValueList(list) => {
                        section.value_lists.insert(key, list.clone());
                    }
                    Entry::PathList(list) => {
                        section.path_lists.insert(key, list.clone());
                    }
                    Entry::Value(value) => {
                        section.values.insert(key, value.clone());
                    }
                    Entry::HostCommand(check) => {
                        section.host_commands.insert(key, check.clone());
                    }
                    Entry::Simple(group, check) => {
                        let checks = if *group == EGRESS_HOSTS {
                            &mut section.egress_hosts
                        } else {
                            &mut section.project_files
                        };
                        checks.insert(key, check.clone());
                    }
                }
            }
            sections.insert(name.clone(), section);
        }
        SetupFileRecord {
            file: path_text(&self.path),
            source: self.source,
            sections,
        }
    }
}

/// A setup file from a record (entry `i` of the record in `origin`), checked
/// as a setup file is.
fn file_from_record(
    record: &SetupFileRecord,
    origin: &Path,
    i: usize,
) -> Result<SetupFile, Vec<ConfigError>> {
    let mut errors = Vec::new();
    let mut sections = BTreeMap::new();
    for (name, groups) in &record.sections {
        let mut entries: BTreeMap<String, Entry> = BTreeMap::new();
        let all = groups
            .paths
            .iter()
            .map(|(k, v)| (k, Entry::Path(v.clone())))
            .chain(
                groups
                    .programs
                    .iter()
                    .map(|(k, v)| (k, Entry::Program(v.clone()))),
            )
            .chain(groups.runs.iter().map(|(k, v)| (k, Entry::Run(v.clone()))))
            .chain(
                groups
                    .path_lists
                    .iter()
                    .map(|(k, v)| (k, Entry::PathList(v.clone()))),
            )
            .chain(
                groups
                    .value_lists
                    .iter()
                    .map(|(k, v)| (k, Entry::ValueList(v.clone()))),
            )
            .chain(
                groups
                    .values
                    .iter()
                    .map(|(k, v)| (k, Entry::Value(v.clone()))),
            )
            .chain(
                groups
                    .host_commands
                    .iter()
                    .map(|(k, v)| (k, Entry::HostCommand(v.clone()))),
            )
            .chain(
                groups
                    .project_files
                    .iter()
                    .map(|(k, v)| (k, Entry::Simple(PROJECT_FILES, v.clone()))),
            )
            .chain(
                groups
                    .egress_hosts
                    .iter()
                    .map(|(k, v)| (k, Entry::Simple(EGRESS_HOSTS, v.clone()))),
            );
        for (key, entry) in all {
            let at = format!(
                "setup.files[{i}].sections.{}",
                child_key(name, &group_key(entry.group(), key))
            );
            let problems = match &entry {
                Entry::Simple(group, Some(check)) => check.problems(group, &at),
                _ => Vec::new(),
            };
            if !problems.is_empty() {
                errors.extend(
                    problems
                        .into_iter()
                        .map(|(key, message)| ConfigError::new(Some(origin), &key, message)),
                );
                continue;
            }
            if let Entry::HostCommand(Some(check)) = &entry {
                let problems = check.problems(&at);
                if !problems.is_empty() {
                    errors.extend(
                        problems
                            .into_iter()
                            .map(|(key, message)| ConfigError::new(Some(origin), &key, message)),
                    );
                    continue;
                }
            }
            if is_tool_only(name, key) {
                errors.push(ConfigError::new(
                    Some(origin),
                    &at,
                    "is set by the tool; remove it",
                ));
            } else if let Some(existing) = entries.get(key) {
                errors.push(ConfigError::new(
                    Some(origin),
                    &at,
                    format!(
                        "is also set in `{}`; a key has only one kind of value",
                        existing.group()
                    ),
                ));
            } else {
                entries.insert(key.clone(), entry);
            }
        }
        sections.insert(name.clone(), entries);
    }
    if errors.is_empty() {
        Ok(SetupFile {
            path: PathBuf::from(&record.file),
            source: record.source,
            sections,
        })
    } else {
        Err(errors)
    }
}

fn load_file(path: &Path, source: Source) -> Result<SetupFile, Vec<ConfigError>> {
    let error = |key: &str, message: String| ConfigError::new(Some(path), key, message);
    let text = read_limited(path).map_err(|m| vec![error("", m)])?;
    let value = parse_yaml(&text).map_err(|m| vec![error("", m)])?;
    let tags = tag_errors(&value);
    if !tags.is_empty() {
        return Err(tags
            .into_iter()
            .map(|(key, message)| error(&key, message))
            .collect());
    }

    let mut errors = Vec::new();
    let mut file = SetupFile {
        path: path.to_path_buf(),
        source,
        sections: BTreeMap::new(),
    };
    let map = match value {
        Value::Null => return Ok(file),
        Value::Mapping(map) => map,
        _ => {
            return Err(vec![error("", "must be a mapping of sections".to_string())]);
        }
    };
    for (key, value) in map {
        let Some(name) = key.as_str() else {
            errors.push(error("", format!("top-level key {key:?} must be a string")));
            continue;
        };
        let groups = match value {
            Value::Null => Default::default(),
            Value::Mapping(groups) => groups,
            Value::Sequence(_) => {
                errors.push(error(
                    name,
                    "must be a section; put lists of paths in a section's `path-lists`".to_string(),
                ));
                continue;
            }
            _ => {
                errors.push(error(
                    name,
                    "must be a section, with `paths`, `programs`, `runs`, `path-lists`, `value-lists`, `values`, `host-commands`, `project-files` and `egress-hosts` mappings"
                        .to_string(),
                ));
                continue;
            }
        };
        let section = file.sections.entry(name.to_string()).or_default();
        for (group, entries) in groups {
            let group = match group.as_str() {
                Some(PATHS) => PATHS,
                Some(PROGRAMS) => PROGRAMS,
                Some(RUNS) => RUNS,
                Some(PATH_LISTS) => PATH_LISTS,
                Some(VALUE_LISTS) => VALUE_LISTS,
                Some(VALUES) => VALUES,
                Some(HOST_COMMANDS) => HOST_COMMANDS,
                Some(PROJECT_FILES) => PROJECT_FILES,
                Some(EGRESS_HOSTS) => EGRESS_HOSTS,
                _ => {
                    let shown = group
                        .as_str()
                        .map_or_else(|| format!("{group:?}"), str::to_string);
                    errors.push(error(
                        &child_key(name, &shown),
                        "unknown key; a section has `paths`, `programs`, `runs`, `path-lists`, `value-lists`, `values`, `host-commands`, `project-files` and `egress-hosts`"
                            .to_string(),
                    ));
                    continue;
                }
            };
            let at_group = child_key(name, group);
            let entries = match entries {
                Value::Null => continue,
                Value::Mapping(entries) => entries,
                _ => {
                    errors.push(error(
                        &at_group,
                        "must be a mapping of names to values".to_string(),
                    ));
                    continue;
                }
            };
            for (key, value) in entries {
                let Some(key) = key.as_str() else {
                    errors.push(error(&at_group, format!("key {key:?} must be a string")));
                    continue;
                };
                let at = child_key(&at_group, key);
                if is_tool_only(name, key) {
                    errors.push(error(&at, "is set by the tool; remove it".to_string()));
                    continue;
                }
                let entry = match (group, &value) {
                    (RUNS, Value::Sequence(items)) => {
                        let mut args = Vec::new();
                        for (i, item) in items.iter().enumerate() {
                            match scalar_text(item) {
                                Ok(arg) => args.push(arg),
                                Err(message) => {
                                    errors.push(error(&index_key(&at, i), message.to_string()))
                                }
                            }
                        }
                        Entry::Run(RunValue::Args(args))
                    }
                    (PATH_LISTS, Value::Sequence(items)) => {
                        let mut list = Vec::new();
                        for (i, item) in items.iter().enumerate() {
                            match item {
                                Value::String(s) => list.push(s.clone()),
                                _ => errors
                                    .push(error(&index_key(&at, i), "must be a path".to_string())),
                            }
                        }
                        Entry::PathList(list)
                    }
                    (PATH_LISTS, _) => {
                        errors.push(error(&at, "must be a list of paths".to_string()));
                        continue;
                    }
                    (VALUE_LISTS, Value::Sequence(items)) => {
                        let mut list = Vec::new();
                        for (i, item) in items.iter().enumerate() {
                            match scalar_text(item) {
                                Ok(text) => list.push(text),
                                Err(message) => {
                                    errors.push(error(&index_key(&at, i), message.to_string()))
                                }
                            }
                        }
                        Entry::ValueList(list)
                    }
                    (VALUE_LISTS, _) => {
                        errors.push(error(&at, "must be a list of values".to_string()));
                        continue;
                    }
                    (PROJECT_FILES | EGRESS_HOSTS, _) => {
                        match simple_checks::parse(value.clone(), group, &at) {
                            Ok(check) => Entry::Simple(group, check),
                            Err(problems) => {
                                errors.extend(
                                    problems
                                        .into_iter()
                                        .map(|(key, message)| error(&key, message)),
                                );
                                continue;
                            }
                        }
                    }
                    (HOST_COMMANDS, _) => match host_commands::parse(value.clone(), &at) {
                        Ok(check) => Entry::HostCommand(check),
                        Err(problems) => {
                            errors.extend(
                                problems
                                    .into_iter()
                                    .map(|(key, message)| error(&key, message)),
                            );
                            continue;
                        }
                    },
                    (VALUES, _) => match scalar_value(&value) {
                        Ok(scalar) => Entry::Value(scalar),
                        Err(message) => {
                            errors.push(error(&at, message.to_string()));
                            continue;
                        }
                    },
                    _ => match scalar_text(&value) {
                        Ok(text) if group == RUNS => Entry::Run(RunValue::Line(text)),
                        Ok(text) if group == PROGRAMS => Entry::Program(text),
                        Ok(text) => Entry::Path(text),
                        Err(message) => {
                            errors.push(error(&at, message.to_string()));
                            continue;
                        }
                    },
                };
                if let Some(existing) = section.get(key) {
                    errors.push(error(
                        &at,
                        format!(
                            "is also set in `{}`; a key has only one kind of value",
                            existing.group()
                        ),
                    ));
                    continue;
                }
                section.insert(key.to_string(), entry);
            }
        }
    }
    if errors.is_empty() {
        Ok(file)
    } else {
        Err(errors)
    }
}

fn scalar_text(value: &Value) -> Result<String, &'static str> {
    match value {
        Value::String(s) => Ok(s.clone()),
        Value::Number(n) => Ok(n.to_string()),
        Value::Bool(b) => Ok(b.to_string()),
        Value::Null => Err("has no value"),
        _ => Err("must be a single value, not a list or mapping"),
    }
}

/// A `values` entry, as written.
fn scalar_value(value: &Value) -> Result<ScalarValue, &'static str> {
    Ok(match value {
        Value::Null => ScalarValue::Null,
        Value::Bool(b) => ScalarValue::Bool(*b),
        Value::String(s) => ScalarValue::String(s.clone()),
        Value::Number(n) => {
            if let Some(i) = n.as_i64() {
                ScalarValue::Int(i)
            } else if let Some(u) = n.as_u64() {
                ScalarValue::UInt(u)
            } else {
                match n.as_f64() {
                    Some(f) if f.is_finite() => ScalarValue::Float(f),
                    _ => return Err("must be a finite number"),
                }
            }
        }
        _ => return Err("must be a single value: text, a number, true/false or null"),
    })
}

/// Text by (section, key): values used in place of configured `paths` and
/// `programs`.
type Values = HashMap<(String, String), String>;

/// Entries by (section, key), for keys the configuration doesn't set.
type Fallbacks = HashMap<(String, String), Entry>;

/// Executables (section, key) that aren't there.
type Missing = HashSet<(String, String)>;

/// Keys whose kind of value differs between files (highest priority first).
/// The higher-priority file's setting is the one reported.
fn kind_changes(files: &[SetupFile]) -> Vec<ConfigError> {
    let mut errors = Vec::new();
    // The lowest-priority file decides a key's kind.
    let mut kinds: BTreeMap<(&str, &str), (&'static str, &Path)> = BTreeMap::new();
    for file in files.iter().rev() {
        for (name, entries) in &file.sections {
            for (key, entry) in entries {
                let group = entry.group();
                match kinds.get(&(name.as_str(), key.as_str())) {
                    Some((kind, origin)) if *kind != group => errors.push(ConfigError::new(
                        Some(&file.path),
                        &child_key(name, &group_key(group, key)),
                        format!(
                            "is set in `{kind}` by {}; a key's kind of value can't change",
                            origin.display()
                        ),
                    )),
                    Some(_) => {}
                    None => {
                        kinds.insert((name, key), (group, &file.path));
                    }
                }
            }
        }
    }
    errors
}

impl SystemConfig {
    /// Merge the files (highest priority first) with the values the tool
    /// sets, which win.
    fn merge(
        files: &[SetupFile],
        tool_values: &[(&str, Entry)],
        env: Env,
        project_dir: Option<&Path>,
    ) -> Self {
        let mut sections = Sections::new();
        for file in files {
            for (name, entries) in &file.sections {
                let section = sections.entry(name.clone()).or_default();
                for (key, entry) in entries {
                    section.entry(key.clone()).or_insert_with(|| Setting {
                        value: entry.clone(),
                        file: Some(file.path.clone()),
                    });
                }
            }
        }
        let scoter = sections.entry(SCOTER_SECTION.to_string()).or_default();
        for (key, value) in tool_values {
            scoter.insert(
                key.to_string(),
                Setting {
                    value: value.clone(),
                    file: None,
                },
            );
        }
        SystemConfig {
            files: files.iter().map(|f| f.path.clone()).collect(),
            sections,
            env,
            project_dir: project_dir.map(Path::to_path_buf),
            record: SetupRecord::default(),
            loaded_from: LoadedFrom::default(),
            inputs: SetupInputs::default(),
        }
    }

    fn expander<'a>(
        &'a self,
        overrides: &'a Values,
        fallbacks: &'a Fallbacks,
        missing: &'a Missing,
    ) -> Expander<'a> {
        Expander {
            sections: &self.sections,
            overrides,
            fallbacks,
            missing,
            env: &self.env,
            project_dir: self.project_dir.as_deref(),
        }
    }

    /// Set `scoter:dev-container-path`, once the dev container is found.
    pub fn set_dev_container_path(&mut self, dir: &Path) {
        self.set_location(DEV_CONTAINER_PATH, path_text(dir));
    }

    /// Set one of the `scoter` `paths` the tool sets.
    fn set_location(&mut self, key: &str, value: String) {
        self.sections
            .entry(SCOTER_SECTION.to_string())
            .or_default()
            .insert(
                key.to_string(),
                Setting {
                    value: Entry::Path(value),
                    file: None,
                },
            );
    }

    /// Expand every value, recording every problem.  `later` stands in for
    /// `paths` the tool sets later.
    fn validate(&self, later: &Fallbacks) -> Vec<ConfigError> {
        let mut errors: Vec<ConfigError> = Vec::new();
        let mut add = |error: ConfigError| {
            if !errors.contains(&error) {
                errors.push(error);
            }
        };
        let no_values = Values::new();
        let no_missing = Missing::new();
        // Programs are checked on their own; other values get stand-ins for
        // them, so a bad program doesn't hide their own problems.
        let stand_ins: Values = self
            .sections
            .iter()
            .flat_map(|(name, section)| {
                section
                    .iter()
                    .filter(|(_, s)| matches!(s.value, Entry::Program(_)))
                    .map(move |(key, _)| ((name.clone(), key.clone()), key.clone()))
            })
            .collect();
        let with_stand_ins = self.expander(&stand_ins, later, &no_missing);
        let plain = self.expander(&no_values, later, &no_missing);
        for (name, section) in &self.sections {
            for (key, setting) in section {
                let file = setting.file.as_deref();
                let at = child_key(name, &group_key(setting.value.group(), key));
                let expander = match setting.value {
                    Entry::Path(_) | Entry::Program(_) => &plain,
                    _ => &with_stand_ins,
                };
                match &setting.value {
                    Entry::PathList(entries) => {
                        // Each entry on its own, so all their problems are found.
                        for (i, entry) in entries.iter().enumerate() {
                            let mut stack = vec![(name.clone(), key.clone())];
                            if let Err(failure) =
                                expander.path_list_entry(name, key, i, entry, &mut stack)
                            {
                                add(self.failure_error(failure, file, &index_key(&at, i)));
                            }
                        }
                    }
                    Entry::ValueList(entries) => {
                        for (i, entry) in entries.iter().enumerate() {
                            let mut stack = vec![(name.clone(), key.clone())];
                            if let Err(failure) =
                                expander.value_list_entry(name, key, i, entry, &mut stack)
                                && !failure.no_value()
                            {
                                add(self.failure_error(failure, file, &index_key(&at, i)));
                            }
                        }
                    }
                    // Used as written: nothing to expand.
                    Entry::Simple(..) => {}
                    Entry::HostCommand(None) => {}
                    Entry::HostCommand(Some(check)) => {
                        if let Err(failure) = expander.check_locations(name, key, check)
                            && !failure.no_value()
                        {
                            add(self.failure_error(failure, file, &at));
                        }
                    }
                    // Only text is expanded.
                    Entry::Value(value) if !matches!(value, ScalarValue::String(_)) => {}
                    entry => match expander.resolve(name, key, &None, &mut Vec::new()) {
                        Ok(Resolved::List(words))
                            if words.is_empty() && matches!(entry, Entry::Run(_)) =>
                        {
                            add(ConfigError::new(file, &at, "the command is empty"));
                        }
                        Ok(_) => {}
                        // It just has no value this run.
                        Err(failure) if failure.no_value() => {}
                        Err(failure) => add(self.failure_error(failure, file, &at)),
                    },
                }
            }
        }
        errors
    }

    /// The file that set `key_path` (`GROUP.KEY`, possibly with an index) in
    /// `section`.
    fn setting_file(&self, section: &str, key_path: &str) -> Option<&Path> {
        let (_, key) = key_path.split_once('.')?;
        // A check's fields follow its key, as `KEY.command-map.NAME`.
        let key = key.split(['[', '.']).next().unwrap_or(key);
        self.sections.get(section)?.get(key)?.file.as_deref()
    }

    /// The error for a failed expansion: where the failure was found, if it
    /// was in another value, or else `file` and `key`.
    fn failure_error(&self, failure: Failure, file: Option<&Path>, key: &str) -> ConfigError {
        match &failure.origin {
            Some((section, key_path)) => {
                let origin_file = self.setting_file(section, key_path);
                ConfigError::new(origin_file, &child_key(section, key_path), failure.message)
            }
            None => ConfigError::new(file, key, failure.message),
        }
    }

    fn entry(&self, section: &str, key: &str) -> Option<&Entry> {
        Some(&self.sections.get(section)?.get(key)?.value)
    }

    /// The keys of a section whose entries match `kind`.
    fn keys_where(&self, section: &str, kind: fn(&Entry) -> bool) -> impl Iterator<Item = &str> {
        self.sections
            .get(section)
            .into_iter()
            .flatten()
            .filter(move |(_, setting)| kind(&setting.value))
            .map(|(key, _)| key.as_str())
    }

    /// The setup as read, to expand again later.
    pub fn setup_record(&self) -> &SetupRecord {
        &self.record
    }

    /// The history of the runs' inputs.
    pub fn loaded_from(&self) -> &LoadedFrom {
        &self.loaded_from
    }

    /// The tool's own inputs to the setup that were in effect.
    pub fn setup_inputs(&self) -> &SetupInputs {
        &self.inputs
    }

    /// The files read, highest priority first.
    pub fn files(&self) -> &[PathBuf] {
        &self.files
    }

    /// The section names, including `scoter`.
    pub fn sections(&self) -> impl Iterator<Item = &str> {
        self.sections.keys().map(String::as_str)
    }

    /// The `paths` keys set in a section.
    pub fn path_keys(&self, section: &str) -> impl Iterator<Item = &str> {
        self.keys_where(section, |e| matches!(e, Entry::Path(_)))
    }

    /// The `runs` keys set in a section.
    pub fn run_keys(&self, section: &str) -> impl Iterator<Item = &str> {
        self.keys_where(section, |e| matches!(e, Entry::Run(_)))
    }

    /// The `programs` keys set in a section.
    pub fn program_keys(&self, section: &str) -> impl Iterator<Item = &str> {
        self.keys_where(section, |e| matches!(e, Entry::Program(_)))
    }

    /// The file that set the `host-commands` check `section:key`.
    pub fn host_command_file(&self, section: &str, key: &str) -> Option<&Path> {
        self.setting_file(section, &group_key(HOST_COMMANDS, key))
    }

    /// The file that set the check `section:key` of `group` (such as `egress-hosts`).
    pub fn check_file(&self, section: &str, group: &str, key: &str) -> Option<&Path> {
        self.setting_file(section, &group_key(group, key))
    }

    /// The name of the project location `path` is in, once symlinks are
    /// resolved, if it's in one: the project directory, the project files
    /// directory, the project's clone, or the repository holding
    /// the project.
    pub fn project_location(&self, path: &Path) -> Option<String> {
        let location = |key| self.get_path(SCOTER_SECTION, key);
        let project_dir = location(PROJECT_DIR);
        let project_config = location(PROJECT_CONFIG);
        let workspace = location(PROJECT_CLONE_DIR);
        let vcs_root = location(VCS_ROOT);
        let options = SetupOptions {
            project_dir: project_dir.as_deref(),
            project_config: project_config.as_deref(),
            project_clone_dir: workspace.as_deref(),
            vcs_root: vcs_root.as_deref(),
            ..SetupOptions::default()
        };
        in_project(path, &options)
    }

    /// Find a program the way `programs` are found: a leading `~` is expanded,
    /// and a bare name is searched for on the `PATH`.
    pub fn find_program(&self, program: &str) -> Option<PathBuf> {
        command::find_executable(
            &self.env.expand_home(program),
            self.env.path.as_deref(),
            self.env.path_ext.as_deref(),
        )
    }

    /// The host's environment variable `name`.  On Windows, as there, the
    /// name is matched without regard to case.
    pub fn host_var(&self, name: &str) -> Option<&str> {
        find_var(&self.env.vars, name, cfg!(windows))
    }

    /// Whether any file (or the tool) sets `section:key`.
    pub fn is_set(&self, section: &str, key: &str) -> bool {
        self.entry(section, key).is_some()
    }

    /// The text a `${section:key}` reference outside the setup files stands
    /// for: a `paths` value as a file location, a `programs` value as the
    /// program found, or a `values` value as text.  Lists, null values and
    /// programs that aren't there are errors.
    pub fn scalar_text(&self, section: &str, key: &str) -> Result<String, String> {
        let reference = format!("`${{{section}:{key}}}`");
        let unexpanded = || format!("{reference} has no value");
        match self.entry(section, key) {
            None if is_location(section, key) => Err(unset_location(section, key)),
            None => Err(format!("unknown reference {reference}")),
            Some(Entry::Run(_) | Entry::PathList(_) | Entry::ValueList(_)) => {
                Err(format!("{reference} is a list; it can't be used here"))
            }
            Some(Entry::HostCommand(_) | Entry::Simple(..)) => Err(check_reference(section, key)),
            Some(Entry::Value(ScalarValue::Null)) => {
                Err(format!("{reference} is null, so it can't be used here"))
            }
            Some(Entry::Program(_)) => {
                let program = self.get(section, key).ok_or_else(unexpanded)?;
                self.find_program(&program)
                    .map(|found| path_text(&found))
                    .ok_or_else(|| format!("the program for {reference} isn't there"))
            }
            Some(Entry::Path(_) | Entry::Value(_)) => self.get(section, key).ok_or_else(unexpanded),
        }
    }

    /// A `paths` value as a file location, a `programs` value as configured
    /// (not looked up on the `PATH`), or a `values` value as text.
    pub fn get(&self, section: &str, key: &str) -> Option<String> {
        match self.entry(section, key)? {
            Entry::Path(_) | Entry::Program(_) | Entry::Value(_) => {}
            _ => return None,
        }
        let no_values = Values::new();
        let no_missing = Missing::new();
        // `load` already checked every value expands.
        match self
            .expander(&no_values, &Fallbacks::new(), &no_missing)
            .resolve(section, key, &None, &mut Vec::new())
        {
            Ok(Resolved::Text(parts)) => Some(command::render_parts(&parts)),
            _ => None,
        }
    }

    /// `get`, as a path.
    pub fn get_path(&self, section: &str, key: &str) -> Option<PathBuf> {
        self.get(section, key).map(PathBuf::from)
    }

    /// A `path-lists` list, expanded; empty if unset.
    pub fn path_list(&self, section: &str, key: &str) -> Vec<PathBuf> {
        if !matches!(self.entry(section, key), Some(Entry::PathList(_))) {
            return Vec::new();
        }
        let no_values = Values::new();
        let no_missing = Missing::new();
        match self
            .expander(&no_values, &Fallbacks::new(), &no_missing)
            .resolve(section, key, &None, &mut Vec::new())
        {
            Ok(Resolved::List(paths)) => paths
                .iter()
                .map(|parts| PathBuf::from(command::render_parts(parts)))
                .collect(),
            _ => Vec::new(),
        }
    }

    /// A `value-lists` list, expanded; empty if unset or it has no value.
    pub fn value_list(&self, section: &str, key: &str) -> Vec<String> {
        if !matches!(self.entry(section, key), Some(Entry::ValueList(_))) {
            return Vec::new();
        }
        let no_values = Values::new();
        let no_missing = Missing::new();
        match self
            .expander(&no_values, &Fallbacks::new(), &no_missing)
            .resolve(section, key, &None, &mut Vec::new())
        {
            Ok(Resolved::List(items)) => items.iter().map(|p| command::render_parts(p)).collect(),
            _ => Vec::new(),
        }
    }

    /// A `values` entry, with text expanded.
    pub fn value(&self, section: &str, key: &str) -> Option<ScalarValue> {
        match self.entry(section, key)? {
            Entry::Value(ScalarValue::String(_)) => self.get(section, key).map(ScalarValue::String),
            Entry::Value(value) => Some(value.clone()),
            _ => None,
        }
    }

    /// The section `name`, expanded, with its `programs` looked up on the
    /// `PATH`; references to them give the programs found.  A program that
    /// isn't there, and every value that refers to it, has no value.
    /// `defaults` gives values for keys no file sets.
    pub fn section(&self, name: &str, defaults: &SectionDefaults) -> Result<Section, ConfigError> {
        let is_set = |key: &str| self.entry(name, key).is_some();
        let mut fallbacks = Fallbacks::new();
        let mut add_defaults = |pairs: &[(&str, &str)], entry: fn(String) -> Entry| {
            for (key, value) in pairs.iter().filter(|(key, _)| !is_set(key)) {
                fallbacks.insert(
                    (name.to_string(), key.to_string()),
                    entry(value.to_string()),
                );
            }
        };
        add_defaults(defaults.paths, Entry::Path);
        add_defaults(defaults.programs, Entry::Program);
        let failure = |failure: Failure, group: &str, key: &str| {
            let key_path = group_key(group, key);
            let file = self.setting_file(name, &key_path);
            self.failure_error(failure, file, &child_key(name, &key_path))
        };
        // The keys of a kind, set or defaulted.
        let keys_of = |set: Vec<&str>, defaulted: &[(&str, &str)]| {
            let mut keys: Vec<String> = set.into_iter().map(str::to_string).collect();
            keys.extend(
                defaulted
                    .iter()
                    .filter(|(key, _)| !is_set(key))
                    .map(|(key, _)| key.to_string()),
            );
            keys.sort_unstable();
            keys.dedup();
            keys
        };
        let path_keys = keys_of(self.path_keys(name).collect(), defaults.paths);
        let program_keys = keys_of(self.program_keys(name).collect(), defaults.programs);

        // Every program is found on the `PATH`, this section's and the
        // others', so a reference to any of them gives the program found.
        let mut overrides = Values::new();
        let mut missing = Missing::new();
        let mut missing_programs = BTreeMap::new();
        let mut programs = BTreeMap::new();
        let no_values = Values::new();
        let no_missing = Missing::new();
        let others = self
            .sections
            .keys()
            .filter(|other| *other != name)
            .flat_map(|other| {
                self.program_keys(other)
                    .map(move |key| (other.clone(), key.to_string()))
            });
        let all: Vec<(String, String)> = program_keys
            .iter()
            .map(|key| (name.to_string(), key.clone()))
            .chain(others)
            .collect();
        for (section_name, key) in all {
            let own = section_name == name;
            let program = match self.expander(&no_values, &fallbacks, &no_missing).resolve(
                &section_name,
                &key,
                &None,
                &mut Vec::new(),
            ) {
                Ok(Resolved::Text(parts)) => command::render_parts(&parts),
                Ok(Resolved::List(_)) => unreachable!("programs are text"),
                Err(f) if own => return Err(failure(f, PROGRAMS, &key)),
                // Another section's problem is reported with that section;
                // here, it has no value.
                Err(_) => {
                    missing.insert((section_name, key));
                    continue;
                }
            };
            let found = self.find_program(&program);
            let id = (section_name, key.clone());
            match &found {
                Some(found) => {
                    overrides.insert(id, path_text(found));
                }
                None => {
                    missing.insert(id);
                    if own {
                        missing_programs.insert(key.clone(), program);
                    }
                }
            }
            if own {
                programs.insert(key, found);
            }
        }
        let expander = self.expander(&overrides, &fallbacks, &missing);
        // `None` when the value needs a program that isn't there.
        let resolve =
            |group: &str, key: &str| match expander.resolve(name, key, &None, &mut Vec::new()) {
                Ok(resolved) => Ok(Some(resolved)),
                Err(f) if f.no_value() => Ok(None),
                Err(f) => Err(failure(f, group, key)),
            };
        let template = |key: &str, words: Vec<Vec<Part>>| {
            CommandTemplate::new(words)
                .map_err(|message| failure(Failure::at(name, RUNS, key, message), RUNS, key))
        };
        let render_list = |list: Vec<Vec<Part>>| -> Vec<PathBuf> {
            list.iter()
                .map(|parts| PathBuf::from(command::render_parts(parts)))
                .collect()
        };

        let mut section = Section {
            name: name.to_string(),
            paths: BTreeMap::new(),
            programs,
            runs: BTreeMap::new(),
            path_lists: BTreeMap::new(),
            value_lists: BTreeMap::new(),
            values: BTreeMap::new(),
            host_commands: BTreeMap::new(),
            project_files: BTreeMap::new(),
            egress_hosts: BTreeMap::new(),
            missing: missing_programs,
        };
        for key in &path_keys {
            let key = key.as_str();
            let path = match resolve(PATHS, key)? {
                Some(Resolved::Text(parts)) => Some(PathBuf::from(command::render_parts(&parts))),
                _ => None,
            };
            section.paths.insert(key.to_string(), path);
        }
        let set = self.sections.get(name).into_iter().flatten();
        for (key, setting) in set {
            match &setting.value {
                Entry::Path(_) | Entry::Program(_) => {}
                Entry::Run(_) => {
                    let run = match resolve(RUNS, key)? {
                        Some(Resolved::List(words)) => Some(template(key, words)?),
                        _ => None,
                    };
                    section.runs.insert(key.clone(), run);
                }
                Entry::PathList(_) => {
                    let list = match resolve(PATH_LISTS, key)? {
                        Some(Resolved::List(paths)) => Some(render_list(paths)),
                        _ => None,
                    };
                    section.path_lists.insert(key.clone(), list);
                }
                Entry::ValueList(_) => {
                    let list = match resolve(VALUE_LISTS, key)? {
                        Some(Resolved::List(items)) => {
                            Some(items.iter().map(|p| command::render_parts(p)).collect())
                        }
                        _ => None,
                    };
                    section.value_lists.insert(key.clone(), list);
                }
                Entry::Value(ScalarValue::String(_)) => {
                    let value = match resolve(VALUES, key)? {
                        Some(Resolved::Text(parts)) => {
                            ScalarValue::String(command::render_parts(&parts))
                        }
                        _ => ScalarValue::Null,
                    };
                    section.values.insert(key.clone(), value);
                }
                Entry::Value(value) => {
                    section.values.insert(key.clone(), value.clone());
                }
                Entry::Simple(_, None) => {}
                Entry::Simple(group, Some(check)) => {
                    let checks = if *group == EGRESS_HOSTS {
                        &mut section.egress_hosts
                    } else {
                        &mut section.project_files
                    };
                    checks.insert(key.clone(), check.clone());
                }
                Entry::HostCommand(None) => {}
                Entry::HostCommand(Some(check)) => {
                    let locations = expander
                        .check_locations(name, key, check)
                        .map_err(|f| failure(f, HOST_COMMANDS, key))?;
                    let mut commands: BTreeMap<String, Option<PathBuf>> = check
                        .commands
                        .iter()
                        .map(|command| (command.clone(), self.find_program(command)))
                        .collect();
                    for (command, program) in locations.programs {
                        let found = program.and_then(|p| self.find_program(&path_text(&p)));
                        commands.insert(command, found);
                    }
                    section.host_commands.insert(
                        key.clone(),
                        Check {
                            action: check.action,
                            reason: check.reason.clone(),
                            commands,
                            name_patterns: check.name_patterns.clone(),
                            options: check.options.clone(),
                            env: check.env.clone(),
                            paths: locations.paths,
                            overrides: check.overrides.clone(),
                        },
                    );
                }
            }
        }
        for (key, raw) in defaults.runs.iter().filter(|(key, _)| !is_set(key)) {
            let origin = Some((name.to_string(), group_key(RUNS, key)));
            let mut stack = vec![(name.to_string(), key.to_string())];
            let run =
                match expander.words(name, &RunValue::Line(raw.to_string()), &origin, &mut stack) {
                    Ok(words) => Some(template(key, words)?),
                    Err(f) if f.no_value() => None,
                    Err(f) => return Err(failure(f, RUNS, key)),
                };
            section.runs.insert(key.to_string(), run);
        }
        Ok(section)
    }
}

/// Where a problem is: (section, `GROUP.KEY`, possibly with an index).
type Origin = Option<(String, String)>;

/// Why a value couldn't be expanded.
#[derive(Debug)]
struct Failure {
    /// The value whose text has the problem; `None` for the text being expanded.
    origin: Origin,
    message: String,
    kind: FailureKind,
}

/// What kind of failure: most are errors, but a value that needs something
/// this run doesn't have just has no value.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum FailureKind {
    Error,
    /// It needs a program that isn't there.
    MissingProgram,
    /// It needs a `scoter` location this run doesn't have, such as
    /// `project-dir` without a project.
    UnsetLocation,
}

impl Failure {
    /// The value has no value, rather than an error.
    fn no_value(&self) -> bool {
        self.kind != FailureKind::Error
    }
}

impl Failure {
    fn at(section: &str, group: &str, key: &str, message: String) -> Self {
        Failure {
            kind: FailureKind::Error,
            origin: Some((section.to_string(), group_key(group, key))),
            message,
        }
    }
}

/// An expanded value.
#[derive(Debug)]
enum Resolved {
    /// `paths` and `values`.
    Text(Vec<Part>),
    /// `runs` and `path-lists`: one entry per argument or path.
    List(Vec<Vec<Part>>),
}

/// A check's `command-map` programs (`None`: no value this run) and
/// `paths`, expanded.
struct CheckLocations {
    programs: BTreeMap<String, Option<PathBuf>>,
    paths: Vec<PathBuf>,
}

/// Expands `${...}` references.
struct Expander<'a> {
    sections: &'a Sections,
    /// `paths` values used in place of the configured ones.
    overrides: &'a Values,
    /// Entries for keys the configuration doesn't set.
    fallbacks: &'a Fallbacks,
    /// Executables that aren't there.
    missing: &'a Missing,
    /// For resolving file locations.
    env: &'a Env,
    project_dir: Option<&'a Path>,
}

impl Expander<'_> {
    fn entry(&self, section: &str, key: &str) -> Option<Entry> {
        let set = self
            .sections
            .get(section)
            .and_then(|s| s.get(key))
            .map(|s| s.value.clone());
        set.or_else(|| {
            self.fallbacks
                .get(&(section.to_string(), key.to_string()))
                .cloned()
        })
    }

    /// The value of `section:key`.  `referrer` is where the reference to it
    /// is; `stack` holds the values being expanded, to find loops.
    fn resolve(
        &self,
        section: &str,
        key: &str,
        referrer: &Origin,
        stack: &mut Vec<(String, String)>,
    ) -> Result<Resolved, Failure> {
        let id = (section.to_string(), key.to_string());
        if self.missing.contains(&id) {
            return Err(Failure {
                origin: referrer.clone(),
                message: format!("the program for `${{{section}:{key}}}` isn't there"),
                kind: FailureKind::MissingProgram,
            });
        }
        if let Some(value) = self.overrides.get(&id) {
            return Ok(Resolved::Text(vec![Part::Text(value.clone())]));
        }
        if let Some(start) = stack.iter().position(|s| *s == id) {
            return Err(self.loop_failure(&stack[start..]));
        }
        let Some(entry) = self.entry(section, key) else {
            if is_location(section, key) {
                return Err(Failure {
                    kind: FailureKind::UnsetLocation,
                    origin: referrer.clone(),
                    message: unset_location(section, key),
                });
            }
            return Err(Failure {
                kind: FailureKind::Error,
                origin: referrer.clone(),
                message: format!("unknown reference `${{{section}:{key}}}`"),
            });
        };
        stack.push(id);
        let result = self.resolve_entry(section, key, &entry, referrer, stack);
        stack.pop();
        result
    }

    fn resolve_entry(
        &self,
        section: &str,
        key: &str,
        entry: &Entry,
        referrer: &Origin,
        stack: &mut Vec<(String, String)>,
    ) -> Result<Resolved, Failure> {
        let origin = Some((section.to_string(), group_key(entry.group(), key)));
        match entry {
            Entry::Path(raw) | Entry::Program(raw) => {
                let parts = self.expand_text(section, raw, &origin, stack)?;
                if parts.iter().any(|p| matches!(p, Part::External(_))) {
                    let kind = if matches!(entry, Entry::Path(_)) {
                        "paths are file locations"
                    } else {
                        "programs are files"
                    };
                    return Err(Failure {
                        kind: FailureKind::Error,
                        origin,
                        message: format!("{kind}; they can't use `{{x:...}}` values"),
                    });
                }
                let path = resolve_path(&command::render_parts(&parts), self.env, self.project_dir);
                Ok(Resolved::Text(vec![Part::Text(path_text(&path))]))
            }
            Entry::Value(ScalarValue::String(raw)) => Ok(Resolved::Text(
                self.expand_text(section, raw, &origin, stack)?,
            )),
            Entry::Value(value) => match value.text() {
                Some(text) => Ok(Resolved::Text(vec![Part::Text(text)])),
                None => Err(Failure {
                    kind: FailureKind::Error,
                    origin: referrer.clone(),
                    message: format!("`${{{section}:{key}}}` is null, so it can't be used here"),
                }),
            },
            Entry::Run(raw) => Ok(Resolved::List(self.words(section, raw, &origin, stack)?)),
            Entry::HostCommand(_) | Entry::Simple(..) => Err(Failure {
                kind: FailureKind::Error,
                origin: referrer.clone(),
                message: check_reference(section, key),
            }),
            Entry::PathList(entries) => {
                let mut paths = Vec::new();
                for (i, entry) in entries.iter().enumerate() {
                    let found = self.path_list_entry(section, key, i, entry, stack)?;
                    paths.extend(found.iter().map(|p| vec![Part::Text(path_text(p))]));
                }
                Ok(Resolved::List(paths))
            }
            Entry::ValueList(entries) => {
                let mut items = Vec::new();
                for (i, entry) in entries.iter().enumerate() {
                    let found = self.value_list_entry(section, key, i, entry, stack)?;
                    items.extend(found.into_iter().map(|text| vec![Part::Text(text)]));
                }
                Ok(Resolved::List(items))
            }
        }
    }

    /// A `runs` command's words, expanded, from `origin`.
    fn words(
        &self,
        section: &str,
        raw: &RunValue,
        origin: &Origin,
        stack: &mut Vec<(String, String)>,
    ) -> Result<Vec<Vec<Part>>, Failure> {
        let split = match raw {
            RunValue::Line(line) => command::split_words(line).map_err(|message| Failure {
                kind: FailureKind::Error,
                origin: origin.clone(),
                message,
            })?,
            RunValue::Args(args) => args.clone(),
        };
        let mut words = Vec::new();
        for word in split {
            words.extend(self.expand_word(section, &word, origin, stack)?);
        }
        Ok(words)
    }

    /// Entry `i` of `section:key`'s `path-lists` list: its file locations.
    fn path_list_entry(
        &self,
        section: &str,
        key: &str,
        i: usize,
        entry: &str,
        stack: &mut Vec<(String, String)>,
    ) -> Result<Vec<PathBuf>, Failure> {
        let origin = Some((
            section.to_string(),
            index_key(&group_key(PATH_LISTS, key), i),
        ));
        self.location_entry(section, &origin, entry, stack)
    }

    /// Entry `i` of `section:key`'s `value-lists` list: its values (more than
    /// one when it's a whole reference to a list).  An entry that needs a
    /// `scoter` location this run doesn't have is left out.
    fn value_list_entry(
        &self,
        section: &str,
        key: &str,
        i: usize,
        entry: &str,
        stack: &mut Vec<(String, String)>,
    ) -> Result<Vec<String>, Failure> {
        let origin = Some((
            section.to_string(),
            index_key(&group_key(VALUE_LISTS, key), i),
        ));
        let words = match self.expand_word(section, entry, &origin, stack) {
            Err(f) if f.kind == FailureKind::UnsetLocation => return Ok(Vec::new()),
            words => words?,
        };
        let mut items = Vec::new();
        for parts in words {
            if parts.iter().any(|p| matches!(p, Part::External(_))) {
                return Err(Failure {
                    kind: FailureKind::Error,
                    origin,
                    message: "value lists can't use `{x:...}` values".to_string(),
                });
            }
            items.push(command::render_parts(&parts));
        }
        Ok(items)
    }

    /// The `host-commands` check `section:key`'s `command-map` programs and
    /// `paths`, expanded.  A program with no value this run (it refers to a
    /// program that isn't there, say) is `None`, and a path with none is
    /// left out.
    fn check_locations(
        &self,
        section: &str,
        key: &str,
        check: &CheckRecord,
    ) -> Result<CheckLocations, Failure> {
        let at = group_key(HOST_COMMANDS, key);
        let mut stack = vec![(section.to_string(), key.to_string())];
        let mut programs = BTreeMap::new();
        for (name, raw) in &check.command_map {
            let origin = Some((section.to_string(), format!("{at}.command-map.{name}")));
            let program = match self.expand_text(section, raw, &origin, &mut stack) {
                Ok(parts) if parts.iter().any(|p| matches!(p, Part::External(_))) => {
                    return Err(Failure {
                        kind: FailureKind::Error,
                        origin,
                        message: "programs are files; they can't use `{x:...}` values".to_string(),
                    });
                }
                Ok(parts) => Some(resolve_path(
                    &command::render_parts(&parts),
                    self.env,
                    self.project_dir,
                )),
                Err(f) if f.no_value() => None,
                Err(f) => return Err(f),
            };
            programs.insert(name.clone(), program);
        }
        let mut paths = Vec::new();
        for (i, entry) in check.paths.iter().enumerate() {
            let origin = Some((section.to_string(), index_key(&format!("{at}.paths"), i)));
            match self.location_entry(section, &origin, entry, &mut stack) {
                Ok(found) => paths.extend(found),
                Err(f) if f.no_value() => {}
                Err(f) => return Err(f),
            }
        }
        Ok(CheckLocations { programs, paths })
    }

    /// A list entry that's a file location, at `origin`: its file locations
    /// (more than one when it's a whole reference to a list).
    fn location_entry(
        &self,
        section: &str,
        origin: &Origin,
        entry: &str,
        stack: &mut Vec<(String, String)>,
    ) -> Result<Vec<PathBuf>, Failure> {
        let origin = origin.clone();
        let words = match self.expand_word(section, entry, &origin, stack) {
            // Like a `./` entry without a project.
            Err(f) if f.kind == FailureKind::UnsetLocation => return Ok(Vec::new()),
            words => words?,
        };
        let mut paths = Vec::new();
        for parts in words {
            if parts.iter().any(|p| matches!(p, Part::External(_))) {
                return Err(Failure {
                    kind: FailureKind::Error,
                    origin,
                    message: "paths can't use `{x:...}` values".to_string(),
                });
            }
            let text = command::render_parts(&parts);
            // Without a project, `./` entries are left out.
            if self.project_dir.is_none() && is_project_relative(&text) {
                continue;
            }
            paths.push(resolve_path(&text, self.env, self.project_dir));
        }
        Ok(paths)
    }

    /// One word of a list: a whole `${...}` reference to a list splices it in.
    fn expand_word(
        &self,
        section: &str,
        word: &str,
        origin: &Origin,
        stack: &mut Vec<(String, String)>,
    ) -> Result<Vec<Vec<Part>>, Failure> {
        if let Some(reference) = whole_reference(word) {
            let (ref_section, ref_key) =
                parse_reference(reference, section).map_err(|message| Failure {
                    kind: FailureKind::Error,
                    origin: origin.clone(),
                    message,
                })?;
            return Ok(match self.resolve(ref_section, ref_key, origin, stack)? {
                Resolved::Text(parts) => vec![parts],
                Resolved::List(words) => words,
            });
        }
        Ok(vec![self.expand_text(section, word, origin, stack)?])
    }

    /// Expand `text`, found in `section` at `origin`.
    fn expand_text(
        &self,
        section: &str,
        text: &str,
        origin: &Origin,
        stack: &mut Vec<(String, String)>,
    ) -> Result<Vec<Part>, Failure> {
        let here = |message: String| Failure {
            kind: FailureKind::Error,
            origin: origin.clone(),
            message,
        };
        let mut parts = Vec::new();
        let mut rest = text;
        while !rest.is_empty() {
            if let Some(after) = rest.strip_prefix("$${") {
                command::push_text(&mut parts, "${");
                rest = after;
            } else if let Some(after) = rest.strip_prefix("${") {
                let Some(end) = after.find('}') else {
                    return Err(here(format!("`${{{after}` is missing its `}}`")));
                };
                let reference = &after[..end];
                rest = &after[end + 1..];
                let (ref_section, ref_key) = parse_reference(reference, section).map_err(here)?;
                match self.resolve(ref_section, ref_key, origin, stack)? {
                    Resolved::Text(value) => command::extend_parts(&mut parts, value),
                    Resolved::List(_) => {
                        return Err(here(format!(
                            "`${{{ref_section}:{ref_key}}}` is a list; use it as a whole argument \
                             in `runs` or a whole entry in `path-lists` or `value-lists`"
                        )));
                    }
                }
            } else if let Some(after) = rest.strip_prefix("{x:") {
                let Some(end) = after.find('}') else {
                    return Err(here(format!("`{{x:{after}` is missing its `}}`")));
                };
                let name = &after[..end];
                if name.is_empty() {
                    return Err(here("`{x:}` needs a name".to_string()));
                }
                parts.push(Part::External(name.to_string()));
                rest = &after[end + 1..];
            } else {
                let len = rest.chars().next().map_or(1, char::len_utf8);
                command::push_text(&mut parts, &rest[..len]);
                rest = &rest[len..];
            }
        }
        Ok(parts)
    }

    /// The error for values referring to each other in a loop.  It's always
    /// reported for the same value, whichever one the expansion started from.
    fn loop_failure(&self, cycle: &[(String, String)]) -> Failure {
        let start = (0..cycle.len()).min_by_key(|&i| &cycle[i]).unwrap_or(0);
        let mut names: Vec<String> = cycle[start..]
            .iter()
            .chain(&cycle[..start])
            .map(|(s, k)| format!("${{{s}:{k}}}"))
            .collect();
        names.push(names[0].clone());
        let (section, key) = &cycle[start];
        let group = self.entry(section, key).map_or(PATHS, |e| e.group());
        Failure::at(
            section,
            group,
            key,
            format!(
                "values refer to each other in a loop: {}",
                names.join(" -> ")
            ),
        )
    }
}

/// The reference in `word`, when the whole word is one `${...}`.
/// The message for a reference to a `host-commands` check.
fn check_reference(section: &str, key: &str) -> String {
    format!("`${{{section}:{key}}}` is a `{HOST_COMMANDS}` check; nothing can refer to one")
}

fn whole_reference(word: &str) -> Option<&str> {
    let inner = word.strip_prefix("${")?.strip_suffix('}')?;
    (!inner.contains(['$', '{', '}'])).then_some(inner)
}

/// `(section, key)` from a reference (without its `${` and `}`) found in
/// `section`.
fn parse_reference<'a>(reference: &'a str, section: &'a str) -> Result<(&'a str, &'a str), String> {
    let (ref_section, ref_key) = reference.split_once(':').unwrap_or((section, reference));
    if ref_key.is_empty() || (reference.contains(':') && ref_section.is_empty()) {
        return Err(format!("`${{{reference}}}` is not a valid reference"));
    }
    if ref_section.is_empty() {
        return Err(format!(
            "`${{{reference}}}` needs a section here, as `${{section:{reference}}}`"
        ));
    }
    Ok((ref_section, ref_key))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::config::testing::{TempDir, load_setup};

    fn messages(errors: Vec<ConfigError>) -> Vec<String> {
        errors
            .into_iter()
            .map(|e| format!("{}: {}", e.key.unwrap_or_default(), e.message))
            .collect()
    }

    /// Load a single setup file, as the user's.
    fn load_one(text: &str) -> Result<SystemConfig, Vec<String>> {
        let dir = TempDir::new();
        let file = dir.write("setup.yaml", text);
        load_setup(&file, None, None, Env::default()).map_err(messages)
    }

    fn one_error(text: &str) -> String {
        let errors = load_one(text).unwrap_err();
        assert_eq!(errors.len(), 1, "{errors:?}");
        errors.into_iter().next().unwrap()
    }

    /// A section whose executables are all there.
    fn found(section: Section) -> Section {
        assert!(section.missing.is_empty(), "{:?}", section.missing);
        section
    }

    // ---- discovery ----

    #[test]
    fn discovery_order() {
        let dir = TempDir::new();
        let user = dir.write(
            "home/.config/surf-scoter.setup.yml",
            "a:\n  paths:\n    k: user\n    u: user\n",
        );
        let dist = dir.write(
            "dist/setup.yaml",
            &format!(
                "a:\n  paths:\n    k: dist\n    u: dist\n    d: dist\nscoter:\n  paths:\n    distribution-dir: {}/other\n",
                dir.path().display()
            ),
        );
        dir.write("other/setup.yaml", "a:\n  paths:\n    o: other\n");
        let env = Env {
            home: Some(dir.path().join("home")),
            ..Env::default()
        };
        let config = load(Some(&dir.path().join("dist")), None, env).unwrap();
        assert_eq!(config.files(), [user.clone(), dist]);
        assert_eq!(config.get("a", "k").as_deref(), Some("user"));
        assert_eq!(config.get("a", "u").as_deref(), Some("user"));
        assert_eq!(config.get("a", "d").as_deref(), Some("dist"));
        assert_eq!(config.get("a", "o"), None);
        // The one given wins.
        assert_eq!(
            config.get_path(SCOTER_SECTION, DISTRIBUTION_DIR),
            Some(dir.path().join("dist"))
        );
        assert_eq!(
            config.get(SCOTER_SECTION, USER_SETUP_FILE),
            Some(user.display().to_string())
        );
        // Found as ~/.config/surf-scoter.setup.yml: no user directory.
        assert_eq!(config.get(SCOTER_SECTION, USER_DIR), None);
    }

    #[test]
    fn discovery_user_locations() {
        let dir = TempDir::new();
        let home = dir.path().join("home");
        let scoter_home = dir.path().join("scoter-home");
        let in_d = dir.write("home/.config/surf-scoter.d/setup.yaml", "");
        let env = Env {
            home: Some(home.clone()),
            scoter_home: Some(scoter_home.clone()),
            ..Env::default()
        };
        let config = load(None, None, env.clone()).unwrap();
        assert_eq!(config.files(), [in_d]);
        assert_eq!(
            config.get_path(SCOTER_SECTION, USER_DIR),
            Some(home.join(".config/surf-scoter.d"))
        );

        let in_home = dir.write("scoter-home/setup.yml", "");
        let config = load(None, None, env).unwrap();
        assert_eq!(config.files(), [in_home]);
        assert_eq!(config.get_path(SCOTER_SECTION, USER_DIR), Some(scoter_home));
    }

    #[test]
    fn discovery_xdg_config_home() {
        let dir = TempDir::new();
        let home = dir.path().join("home");
        let xdg = dir.path().join("xdg");
        dir.write("home/.config/surf-scoter.setup.yaml", "");
        let env = Env {
            home: Some(home),
            config_home: Some(xdg.clone()),
            ..Env::default()
        };
        // `~/.config` isn't looked at.
        let config = load(None, None, env.clone()).unwrap();
        assert!(config.files().is_empty());

        let in_d = dir.write("xdg/surf-scoter.d/setup.yaml", "");
        let config = load(None, None, env.clone()).unwrap();
        assert_eq!(config.files(), [in_d]);
        assert_eq!(
            config.get_path(SCOTER_SECTION, USER_DIR),
            Some(xdg.join("surf-scoter.d"))
        );

        let flat = dir.write("xdg/surf-scoter.setup.yaml", "");
        let config = load(None, None, env.clone()).unwrap();
        assert_eq!(config.files(), [flat]);
        assert_eq!(config.get(SCOTER_SECTION, USER_DIR), None);

        // `SURF_SCOTER_HOME` comes first.
        let in_home = dir.write("scoter-home/setup.yaml", "");
        let env = Env {
            scoter_home: Some(dir.path().join("scoter-home")),
            ..env
        };
        let config = load(None, None, env).unwrap();
        assert_eq!(config.files(), [in_home]);
    }

    #[test]
    fn config_dir() {
        let mut env = Env::default();
        assert_eq!(env.config_dir(), None);
        env.home = Some(PathBuf::from("/home/me"));
        assert_eq!(env.config_dir(), Some(PathBuf::from("/home/me/.config")));
        env.config_home = Some(PathBuf::from("/xdg"));
        assert_eq!(env.config_dir(), Some(PathBuf::from("/xdg")));
    }

    #[test]
    fn discovery_nothing_found() {
        let dir = TempDir::new();
        let env = Env {
            home: Some(dir.path().to_path_buf()),
            ..Env::default()
        };
        let config = load(None, None, env).unwrap();
        assert!(config.files().is_empty());
        assert_eq!(config.get(SCOTER_SECTION, DISTRIBUTION_DIR), None);
        // The tool's own section is always there.
        assert_eq!(config.sections().collect::<Vec<_>>(), [SCOTER_SECTION]);
    }

    #[test]
    fn discovery_errors() {
        let dir = TempDir::new();
        dir.write("home/.config/surf-scoter.d/setup.yaml", "");
        dir.write("home/.config/surf-scoter.d/setup.yml", "");
        dir.write("dist/setup.yaml", "");
        dir.write("dist/setup.yml", "");
        let env = Env {
            home: Some(dir.path().join("home")),
            ..Env::default()
        };
        let errors = load(Some(&dir.path().join("dist")), None, env).unwrap_err();
        let errors: Vec<String> = errors.iter().map(ToString::to_string).collect();
        assert_eq!(errors.len(), 2, "{errors:?}");
        assert!(
            errors[0]
                .ends_with("surf-scoter.d: both setup.yaml and setup.yml exist; keep only one"),
            "{errors:?}"
        );
        assert!(
            errors[1].ends_with("dist: both setup.yaml and setup.yml exist; keep only one"),
            "{errors:?}"
        );
    }

    #[test]
    fn no_setup_file_in_project() {
        let dir = TempDir::new();
        dir.write("proj/dist/setup.yaml", "a:\n  paths:\n    k: v\n");
        let project_dir = dir.path().join("proj");
        let project_config = project_dir.join(".surf-scoter.d/project.yaml");
        let workspace = dir.path().join("ws");
        dir.write("ws/dist/setup.yaml", "a:\n  paths:\n    k: v\n");
        let errors = |setting: &str, project: bool| {
            dir.write(
                "setup.yaml",
                &format!(
                    "scoter:\n  paths:\n    distribution-dir: {setting}\n  values:\n    project-files-dir: .surf-scoter.d\n    project-file-name: project.yaml\n"
                ),
            );
            let options = SetupOptions {
                project_dir: project.then_some(project_dir.as_path()),
                project_config: project.then_some(project_config.as_path()),
                project_clone_dir: project.then_some(workspace.as_path()),
                ..SetupOptions::default()
            };
            let env = Env {
                cwd: Some(project_dir.clone()),
                scoter_home: Some(dir.path().to_path_buf()),
                ..Env::default()
            };
            load_with(&options, env).map_err(messages).unwrap_err()
        };
        let one = |setting: &str, project: bool| {
            let errors = errors(setting, project);
            assert_eq!(errors.len(), 1, "{errors:?}");
            errors.into_iter().next().unwrap()
        };
        assert!(one("./dist", true).contains("is in the project (`scoter:project-dir`)"));
        assert!(one("${scoter:project-dir}/dist", true).contains("is in the project"));
        assert!(
            one(&format!("{}/dist", workspace.display()), true)
                .contains("(`scoter:project-clone-dir`)")
        );
        // Without a project, `./` would be wherever the tool runs.
        assert!(one("./dist", false).contains("must be absolute"));
        // Nor can it say so, directly or not; the file isn't looked for.
        for setting in [
            "${scoter:cwd}/dist",
            "${scoter:here}/dist\n    here: ${scoter:cwd}",
        ] {
            for project in [true, false] {
                assert!(
                    one(setting, project).contains("can't refer to `${scoter:cwd}`"),
                    "{setting}"
                );
            }
        }

        // Given, it's the caller's choice.
        let given = load(
            Some(&project_dir.join("dist")),
            Some(&project_dir),
            Env::default(),
        );
        assert!(given.is_ok());
    }

    /// The setup for a project file in `.surf-scoter.d`, with nothing else.
    const PROJECT_FILES_SETUP: &str = "scoter:\n  values:\n    project-files-dir: .surf-scoter.d\n    project-file-name: project.yaml\n";

    #[test]
    fn environment_id_matches_store() {
        // The same as the scripts' `store.environment_id` gives for these.
        for (dir, id) in [
            (
                "/no/such/My Project!! (copy)",
                "my-project-copy-c593c7ec2d55",
            ),
            (
                "/no/such/xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx-y",
                "xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx-06211fd163da",
            ),
            ("/no/such/!!!", "project-b94ae0807dcf"),
        ] {
            assert_eq!(environment_id(Path::new(dir)), id, "{dir}");
        }
    }

    #[test]
    fn path_part_is_one_safe_name() {
        for (branch, part) in [
            ("main", Some("main")),
            ("feature/x", Some("feature-x")),
            ("../../.ssh", Some("ssh")),
            ("..", None),
            (".", None),
            ("", None),
            ("a\nb", Some("a-b")),
            ("${scoter:state-dir}", Some("scoter-state-dir")),
            ("detached at 0123456789ab", Some("detached-at-0123456789ab")),
            ("release-1.2_rc", Some("release-1.2_rc")),
        ] {
            assert_eq!(path_part(branch).as_deref(), part, "{branch:?}");
        }
        assert_eq!(path_part(&"b".repeat(100)).unwrap().len(), 64);
    }

    #[test]
    fn no_user_setup_from_project() {
        let dir = TempDir::new();
        // A repository, with the project in `client`.
        std::fs::create_dir_all(dir.path().join("repo/.git")).unwrap();
        let repo = dir.path().join("repo");
        let project_dir = dir.path().join("repo/client");
        let project_config = project_dir.join(".surf-scoter.d/project.yaml");
        let workspace = dir.path().join("ws");
        let options = SetupOptions {
            project_dir: Some(project_dir.as_path()),
            project_config: Some(project_config.as_path()),
            vcs_root: Some(repo.as_path()),
            project_clone_dir: Some(workspace.as_path()),
            ..SetupOptions::default()
        };
        let load_from = |home: &str| {
            dir.write(&format!("{home}/setup.yaml"), PROJECT_FILES_SETUP);
            let env = Env {
                home: Some(dir.path().join("me")),
                scoter_home: Some(dir.path().join(home)),
                ..Env::default()
            };
            load_with(&options, env).map_err(messages)
        };
        for (home, location) in [
            ("repo/client/conf", "scoter:project-dir"),
            ("repo/client/.surf-scoter.d", "scoter:project-dir"),
            ("repo/server/conf", "scoter:vcs-root"),
            ("repo", "scoter:vcs-root"),
            ("ws/conf", "scoter:project-clone-dir"),
        ] {
            assert_eq!(
                load_from(home).unwrap_err(),
                [format!(
                    ": `SURF_SCOTER_HOME` is in the project (`{location}`); the user's setup file is never read from a project"
                )],
                "{home}"
            );
        }
        assert!(load_from("elsewhere").is_ok());
        // Even without a setup file there.
        let env = Env {
            scoter_home: Some(dir.path().join("repo/empty")),
            ..Env::default()
        };
        assert_eq!(
            load_with(&options, env)
                .map_err(messages)
                .unwrap_err()
                .len(),
            1
        );

        // A `distribution-dir` elsewhere in the repository is in the project too.
        dir.write(
            "elsewhere/setup.yaml",
            &format!(
                "{PROJECT_FILES_SETUP}  paths:\n    distribution-dir: {}\n",
                dir.path().join("repo/tools").display()
            ),
        );
        dir.write("repo/tools/setup.yaml", "");
        let env = Env {
            scoter_home: Some(dir.path().join("elsewhere")),
            ..Env::default()
        };
        let errors = load_with(&options, env).map_err(messages).unwrap_err();
        assert_eq!(errors.len(), 1, "{errors:?}");
        assert!(
            errors[0].contains("is in the project (`scoter:vcs-root`)"),
            "{errors:?}"
        );
    }

    #[test]
    fn no_user_setup_found_earlier() {
        // Found before the project was known, the file is in the record.
        let dir = TempDir::new();
        let file = dir.write("proj/conf/setup.yaml", PROJECT_FILES_SETUP);
        let first = load_setup(&file, None, None, Env::default()).unwrap();
        let record = first.setup_record().clone();
        let origin = dir.path().join("first.json");
        let project_dir = dir.path().join("proj");
        let project_config = project_dir.join(".surf-scoter.d/project.yaml");
        let options = SetupOptions {
            project_dir: Some(project_dir.as_path()),
            project_config: Some(project_config.as_path()),
            no_search: true,
            base: Some(base(&record, &origin)),
            ..SetupOptions::default()
        };
        let errors = load_with(&options, Env::default())
            .map_err(messages)
            .unwrap_err();
        assert_eq!(
            errors,
            [
                ": the user's setup file is in the project (`scoter:project-dir`); it's never read from a project"
            ]
        );
    }

    #[test]
    fn verified_dir_outside_project() {
        let dir = TempDir::new();
        std::fs::create_dir_all(dir.path().join("repo/.git")).unwrap();
        let repo = dir.path().join("repo");
        let project_dir = dir.path().join("repo/client");
        let project_config = project_dir.join(".surf-scoter.d/project.yaml");
        let options = SetupOptions {
            project_dir: Some(project_dir.as_path()),
            project_config: Some(project_config.as_path()),
            vcs_root: Some(repo.as_path()),
            ..SetupOptions::default()
        };
        let load_with_verified = |verified: &str| {
            dir.write(
                "home/setup.yaml",
                &format!("{PROJECT_FILES_SETUP}verification:\n  paths:\n    store: {verified}\n"),
            );
            let env = Env {
                scoter_home: Some(dir.path().join("home")),
                ..Env::default()
            };
            load_with(&options, env).map_err(messages)
        };
        let outside = dir.path().join("me/verified").display().to_string();
        assert_eq!(
            load_with_verified(&outside)
                .unwrap()
                .get_path(VERIFICATION_SECTION, VERIFIED_STORE),
            Some(PathBuf::from(&outside))
        );
        for (verified, location) in [
            ("./verified", "scoter:project-dir"),
            ("${scoter:project-dir}/../server/v", "scoter:vcs-root"),
        ] {
            let errors = load_with_verified(verified).unwrap_err();
            assert_eq!(errors.len(), 1, "{verified}: {errors:?}");
            assert!(
                errors[0].ends_with(&format!(
                    "is in the project (`{location}`); the accepted project settings are never kept in a project"
                )),
                "{verified}: {errors:?}"
            );
        }
        let errors = load_with_verified("relative/v").unwrap_err();
        assert_eq!(
            errors,
            [": `verification:store` relative/v must be an absolute path"]
        );

        // The state directory is checked the same way.
        let load_with_state = |state: &str| {
            dir.write(
                "home/setup.yaml",
                &format!("{PROJECT_FILES_SETUP}  paths:\n    state-dir: {state}\n"),
            );
            let env = Env {
                scoter_home: Some(dir.path().join("home")),
                ..Env::default()
            };
            load_with(&options, env).map_err(messages)
        };
        assert!(load_with_state(&outside).is_ok());
        assert_eq!(
            load_with_state("./state").unwrap_err(),
            [format!(
                ": `scoter:state-dir` {} is in the project (`scoter:project-dir`); the tools' state is never kept in a project",
                project_dir.join("state").display()
            )]
        );
        assert_eq!(
            load_with_state("relative/s").unwrap_err(),
            [": `scoter:state-dir` relative/s must be an absolute path"]
        );
    }

    #[test]
    fn resolve_partly_fills_missing() {
        let dir = TempDir::new();
        std::fs::create_dir_all(dir.path().join("repo/client")).unwrap();
        let at = |p: &str| dir.path().join(p);
        assert_eq!(resolve_partly(&at("repo/client")), at("repo/client"));
        assert_eq!(
            resolve_partly(&at("repo/client/../server/v")),
            at("repo/server/v")
        );
        assert_eq!(resolve_partly(&at("elsewhere/../repo/x")), at("repo/x"));
        assert_eq!(resolve_partly(&at("missing/./a/../b")), at("missing/b"));
        #[cfg(unix)]
        {
            // A symlink in the part that exists is followed.
            std::os::unix::fs::symlink(at("repo"), at("link")).unwrap();
            assert_eq!(resolve_partly(&at("link/new/v")), at("repo/new/v"));
        }
    }

    #[test]
    fn distribution_dir_given() {
        let dir = TempDir::new();
        let dist = dir.write("dist/setup.yaml", "a:\n  paths:\n    k: v\n");
        let config = load(Some(&dir.path().join("dist")), None, Env::default()).unwrap();
        assert_eq!(config.files(), [dist]);
        assert_eq!(
            config.get_path(SCOTER_SECTION, DISTRIBUTION_DIR),
            Some(dir.path().join("dist"))
        );
    }

    #[test]
    fn distribution_dir_from_a_file() {
        let dir = TempDir::new();
        let cli = dir.write(
            "home-setup/setup.yaml",
            "scoter:\n  paths:\n    distribution-dir: ~/dist\n",
        );
        let dist = dir.write("home/dist/setup.yaml", "a:\n  paths:\n    k: v\n");
        let other = dir.write("other/setup.yaml", "a:\n  paths:\n    k: other\n");
        let env = Env {
            home: Some(dir.path().join("home")),
            ..Env::default()
        };
        let config = load_setup(&cli, None, None, env.clone()).unwrap();
        assert_eq!(config.files(), [cli.clone(), dist]);
        assert_eq!(config.get("a", "k").as_deref(), Some("v"));
        assert_eq!(
            config.get_path(SCOTER_SECTION, DISTRIBUTION_DIR),
            Some(dir.path().join("home/dist"))
        );

        // The one given wins over the file's.
        let config = load_setup(&cli, Some(&dir.path().join("other")), None, env.clone()).unwrap();
        assert_eq!(config.files(), [cli.clone(), other]);
        assert_eq!(config.get("a", "k").as_deref(), Some("other"));

        // Without the search, the user's file isn't read, nor the
        // distribution directory's it names.
        let options = SetupOptions {
            no_search: true,
            ..SetupOptions::default()
        };
        let env = crate::config::testing::home_env(cli.parent().unwrap(), env);
        let config = load_with(&options, env).unwrap();
        assert!(config.files().is_empty());
        assert_eq!(config.get_path(SCOTER_SECTION, DISTRIBUTION_DIR), None);
    }

    #[test]
    fn tool_keys_cant_be_set() {
        for key in TOOL_ONLY_KEYS {
            assert_eq!(
                one_error(&format!("scoter:\n  paths:\n    {key}: /x\n")),
                format!("scoter.paths.{key}: is set by the tool; remove it")
            );
        }
        // Only in the `scoter` section.
        assert!(load_one("a:\n  paths:\n    user-dir: /x\n").is_ok());
    }

    #[test]
    fn tool_set_locations() {
        let dir = TempDir::new();
        let setup = dir.write(
            "setup.yaml",
            "a:\n  paths:\n    p: ${scoter:project-dir}/p\n    c: ${scoter:project-config}\n    w: ${scoter:cwd}/w\nscoter:\n  values:\n    project-files-dir: .surf-scoter.d\n    project-file-name: project.yaml\n",
        );
        let options = SetupOptions {
            project_dir: Some(Path::new("/proj")),
            project_config: Some(Path::new("/proj/.surf-scoter.d/project.yaml")),
            ..SetupOptions::default()
        };
        let env = Env {
            scoter_home: Some(dir.path().to_path_buf()),
            cwd: Some(PathBuf::from("/work")),
            ..Env::default()
        };
        let config = load_with(&options, env).unwrap();
        assert_eq!(config.get("a", "p").as_deref(), Some("/proj/p"));
        assert_eq!(
            config.get("a", "c").as_deref(),
            Some("/proj/.surf-scoter.d/project.yaml")
        );
        assert_eq!(config.get("a", "w").as_deref(), Some("/work/w"));

        // Unset, they're not an error: what needs them just has no value.
        let config = load_setup(&setup, None, None, Env::default()).unwrap();
        assert_eq!(config.get("a", "p"), None);
        let section = config.section("a", &SectionDefaults::default()).unwrap();
        assert_eq!(
            section.paths,
            BTreeMap::from([
                ("c".to_string(), None),
                ("p".to_string(), None),
                ("w".to_string(), None),
            ])
        );
    }

    #[test]
    fn unset_locations() {
        let config = load_one(concat!(
            "scoter:\n  path-lists:\n",
            // Entries needing a location this run doesn't have are left out.
            "    search: ['${scoter:project-dir}/a', /b, '${scoter:distribution-dir}', '${a:dirs}']\n",
            "a:\n",
            "  paths:\n    p: ${scoter:project-dir}/p\n    plain: /plain\n",
            "  path-lists:\n    dirs: ['${p}', /c]\n",
            "  runs:\n    go: tool ${scoter:project-dir}\n    ok: tool ${plain}\n",
            "  values:\n    v: in ${scoter:cwd}\n",
        ))
        .unwrap();
        assert_eq!(
            config.path_list(SCOTER_SECTION, "search"),
            [PathBuf::from("/b"), PathBuf::from("/c")]
        );
        let section = config.section("a", &SectionDefaults::default()).unwrap();
        assert_eq!(section.paths["p"], None);
        assert_eq!(section.paths["plain"], Some(PathBuf::from("/plain")));
        assert_eq!(section.path_lists["dirs"], Some(vec![PathBuf::from("/c")]));
        assert_eq!(section.runs["go"], None);
        assert!(section.runs["ok"].is_some());
        assert_eq!(section.values["v"], ScalarValue::Null);
        // Not programs that are missing.
        assert!(section.missing.is_empty());
        assert_eq!(
            config.scalar_text(SCOTER_SECTION, PROJECT_DIR),
            Err("`${scoter:project-dir}` isn't set for this run".to_string())
        );

        // Other unknown references are still errors.
        assert_eq!(
            one_error("a:\n  path-lists:\n    l: ['${scoter:project-dirr}']\n"),
            "a.path-lists.l[0]: unknown reference `${scoter:project-dirr}`"
        );
    }

    #[test]
    fn dev_container_path_set_later() {
        let dir = TempDir::new();
        let setup = dir.write(
            "setup.yaml",
            "a:\n  paths:\n    x: ${scoter:dev-container-path}/x\n",
        );
        // Without a dev container, it's not set: no value.
        let config = load_setup(&setup, None, None, Env::default()).unwrap();
        assert_eq!(config.get("a", "x"), None);

        let options = SetupOptions {
            dev_container: true,
            ..SetupOptions::default()
        };
        let env = crate::config::testing::home_env(dir.path(), Env::default());
        let mut config = load_with(&options, env).unwrap();
        assert_eq!(config.get("a", "x"), None);
        config.set_dev_container_path(Path::new("/dev"));
        assert_eq!(config.get("a", "x").as_deref(), Some("/dev/x"));
        assert_eq!(
            config.get(SCOTER_SECTION, DEV_CONTAINER_PATH).as_deref(),
            Some("/dev")
        );
    }

    // ---- file shape ----

    #[test]
    fn shape_values() {
        let config = load_one(concat!(
            "a:\n  paths:\n    s: text\n    n: 5\n    f: 1.5\n    b: true\n",
            "  runs:\n    go: go now\n",
            "empty:\n",
            "empty-groups:\n  paths:\n  runs:\n  path-lists:\n  values:\n",
            "l:\n  path-lists:\n    list: [/a, /b]\n    empty: []\n",
        ))
        .unwrap();
        assert_eq!(config.get("a", "s").as_deref(), Some("text"));
        assert_eq!(config.get("a", "n").as_deref(), Some("5"));
        assert_eq!(config.get("a", "f").as_deref(), Some("1.5"));
        assert_eq!(config.get("a", "b").as_deref(), Some("true"));
        assert_eq!(config.get("a", "missing"), None);
        // Only `paths` values can be referred to.
        assert_eq!(config.get("a", "go"), None);
        assert_eq!(config.run_keys("a").collect::<Vec<_>>(), ["go"]);
        assert_eq!(config.path_keys("empty").count(), 0);
        assert_eq!(
            config.sections().collect::<Vec<_>>(),
            ["a", "empty", "empty-groups", "l", "scoter"]
        );
        assert_eq!(
            config.path_list("l", "list"),
            [PathBuf::from("/a"), PathBuf::from("/b")]
        );
        assert_eq!(config.path_list("l", "empty"), [] as [PathBuf; 0]);
        assert_eq!(config.path_list("l", "other"), [] as [PathBuf; 0]);
        assert_eq!(config.path_list("other", "list"), [] as [PathBuf; 0]);
    }

    #[test]
    fn shape_errors_are_gathered() {
        let errors = load_one(concat!(
            "a:\n  paths:\n    list: [1]\n    map: {x: y}\n    none:\n    ok: fine\n",
            "  runs: text\n",
            "  other: {k: v}\n",
            "  path-lists:\n    c: /not/a/list\n    d: [/ok, [nested], 5]\n",
            "  values:\n    list: [1]\n    map: {x: y}\n    nan: .nan\n    inf: -.inf\n",
            "b: text\n",
            "old-paths: [/a]\n",
        ))
        .unwrap_err();
        assert_eq!(
            errors,
            [
                "a.paths.list: must be a single value, not a list or mapping",
                "a.paths.map: must be a single value, not a list or mapping",
                "a.paths.none: has no value",
                "a.runs: must be a mapping of names to values",
                "a.other: unknown key; a section has `paths`, `programs`, `runs`, `path-lists`, `value-lists`, `values`, `host-commands`, `project-files` and `egress-hosts`",
                "a.path-lists.c: must be a list of paths",
                "a.path-lists.d[1]: must be a path",
                "a.path-lists.d[2]: must be a path",
                "a.values.list: must be a single value: text, a number, true/false or null",
                "a.values.map: must be a single value: text, a number, true/false or null",
                "a.values.nan: must be a finite number",
                "a.values.inf: must be a finite number",
                "b: must be a section, with `paths`, `programs`, `runs`, `path-lists`, `value-lists`, `values`, `host-commands`, `project-files` and `egress-hosts` mappings",
                "old-paths: must be a section; put lists of paths in a section's `path-lists`",
            ]
        );
    }

    #[test]
    fn old_flat_sections_refused() {
        assert_eq!(
            one_error("ssh:\n  home: ~/.ssh\n"),
            "ssh.home: unknown key; a section has `paths`, `programs`, `runs`, `path-lists`, `value-lists`, `values`, `host-commands`, `project-files` and `egress-hosts`"
        );
    }

    #[test]
    fn shape_top_level() {
        assert_eq!(one_error("[a, b]\n"), ": must be a mapping of sections");
        assert!(one_error("a: [\n").starts_with(": invalid YAML: "));
        assert!(load_one("").unwrap().files().len() == 1);
    }

    #[test]
    fn shape_rejects_tags() {
        let errors = load_one(
            "a:\n  paths:\n    k: !include other.yaml\n  path-lists:\n    b: [!env HOME]\n",
        )
        .unwrap_err();
        assert_eq!(errors.len(), 2, "{errors:?}");
        assert!(
            errors[0].starts_with("a.paths.k: YAML tags such as `!include`"),
            "{errors:?}"
        );
        assert!(errors[1].starts_with("a.path-lists.b[0]: "), "{errors:?}");
    }

    #[test]
    fn bad_file_doesnt_hide_others() {
        let dir = TempDir::new();
        let cli = dir.write("home-setup/setup.yaml", "a: [\n");
        dir.write("dist/setup.yaml", "b: text\n");
        let errors =
            load_setup(&cli, Some(&dir.path().join("dist")), None, Env::default()).unwrap_err();
        assert_eq!(errors.len(), 2, "{errors:?}");
    }

    // ---- merging ----

    #[test]
    fn lists_replace_sections_merge() {
        let dir = TempDir::new();
        let cli = dir.write(
            "home-setup/setup.yaml",
            "a:\n  paths:\n    x: high\n  runs:\n    r: high\n  path-lists:\n    l: [/high]\n  values:\n    v: high\n",
        );
        dir.write(
            "dist/setup.yaml",
            "a:\n  paths:\n    x: low\n    y: low\n  runs:\n    r: low\n    s: low\n  path-lists:\n    l: [/low1, /low2]\n    m: [/m]\n  values:\n    v: low\n    w: 2\n",
        );
        let config =
            load_setup(&cli, Some(&dir.path().join("dist")), None, Env::default()).unwrap();
        assert_eq!(config.get("a", "x").as_deref(), Some("high"));
        assert_eq!(config.get("a", "y").as_deref(), Some("low"));
        let section = found(config.section("a", &SectionDefaults::default()).unwrap());
        let render = |key: &str| {
            section.runs[key]
                .as_ref()
                .unwrap()
                .render(&HashMap::new())
                .unwrap()
        };
        assert_eq!(render("r"), ["high"]);
        assert_eq!(render("s"), ["low"]);
        // The highest-priority file's list replaces the whole list.
        assert_eq!(config.path_list("a", "l"), [PathBuf::from("/high")]);
        assert_eq!(config.path_list("a", "m"), [PathBuf::from("/m")]);
        assert_eq!(section.path_lists.len(), 2);
        assert_eq!(
            config.value("a", "v"),
            Some(ScalarValue::String("high".to_string()))
        );
        assert_eq!(config.value("a", "w"), Some(ScalarValue::Int(2)));
    }

    // ---- expansion ----

    #[test]
    fn expand_references() {
        let dir = TempDir::new();
        let file = dir.write(
            "setup.yaml",
            concat!(
                "ssh:\n  paths:\n    home: ~/.ssh\n    config-file: ${ssh:home}/config\n    key-file: ${home}/key\n",
                "other:\n  paths:\n    x: ${ssh:key-file}!\n    escaped: $${home} $$ a$b ${ssh:home}\n",
            ),
        );
        let env = Env {
            home: Some(PathBuf::from("/home/me")),
            ..Env::default()
        };
        let config = load_setup(&file, None, None, env).unwrap();
        assert_eq!(config.get("ssh", "home").as_deref(), Some("/home/me/.ssh"));
        assert_eq!(
            config.get("ssh", "config-file").as_deref(),
            Some("/home/me/.ssh/config")
        );
        assert_eq!(
            config.get("ssh", "key-file").as_deref(),
            Some("/home/me/.ssh/key")
        );
        assert_eq!(
            config.get("other", "x").as_deref(),
            Some("/home/me/.ssh/key!")
        );
        assert_eq!(
            config.get("other", "escaped").as_deref(),
            Some("${home} $$ a$b /home/me/.ssh")
        );
    }

    #[test]
    fn paths_are_file_locations() {
        let dir = TempDir::new();
        let file = dir.write(
            "setup.yaml",
            concat!(
                "s:\n  paths:\n    home: ~/x\n    tilde: '~'\n    mid: /a/~/b\n    local: ./local\n    dot: .\n",
                "  path-lists:\n    l: ['~/a', '${home}/y', '${s:home}/z', './c']\n",
            ),
        );
        let env = Env {
            home: Some(PathBuf::from("/home/me")),
            ..Env::default()
        };
        let config = load_setup(&file, None, Some(Path::new("/proj")), env.clone()).unwrap();
        assert_eq!(
            config.get_path("s", "home"),
            Some(PathBuf::from("/home/me/x"))
        );
        assert_eq!(
            config.get_path("s", "tilde"),
            Some(PathBuf::from("/home/me"))
        );
        assert_eq!(config.get_path("s", "mid"), Some(PathBuf::from("/a/~/b")));
        assert_eq!(
            config.get_path("s", "local"),
            Some(PathBuf::from("/proj/local"))
        );
        assert_eq!(config.get_path("s", "dot"), Some(PathBuf::from("/proj")));
        assert_eq!(
            config.path_list("s", "l"),
            [
                PathBuf::from("/home/me/a"),
                PathBuf::from("/home/me/x/y"),
                PathBuf::from("/home/me/x/z"),
                PathBuf::from("/proj/c"),
            ]
        );
        // Without a project, `./` paths stay as written, and `./` list entries are left out.
        let config = load_setup(&file, None, None, env).unwrap();
        assert_eq!(config.get("s", "local").as_deref(), Some("./local"));
        assert_eq!(
            config.path_list("s", "l"),
            [
                PathBuf::from("/home/me/a"),
                PathBuf::from("/home/me/x/y"),
                PathBuf::from("/home/me/x/z"),
            ]
        );
    }

    #[test]
    fn values_are_used_as_written() {
        let config = load_one(concat!(
            "v:\n  paths:\n    home: /h\n",
            "  values:\n",
            "    neg: -3\n    big: 18446744073709551615\n    f: 2.5e3\n    t: true\n    n: ~\n",
            "    tilde: ~/x\n    text: '${home}/x $${y}'\n    quoted: '5'\n",
        ))
        .unwrap();
        let value = |key: &str| config.value("v", key);
        assert_eq!(value("neg"), Some(ScalarValue::Int(-3)));
        assert_eq!(value("big"), Some(ScalarValue::UInt(u64::MAX)));
        assert_eq!(value("f"), Some(ScalarValue::Float(2500.0)));
        assert_eq!(value("t"), Some(ScalarValue::Bool(true)));
        assert_eq!(value("n"), Some(ScalarValue::Null));
        // Not a file location: `~` stays.
        assert_eq!(value("tilde"), Some(ScalarValue::String("~/x".to_string())));
        assert_eq!(
            value("text"),
            Some(ScalarValue::String("/h/x ${y}".to_string()))
        );
        assert_eq!(value("quoted"), Some(ScalarValue::String("5".to_string())));
        assert_eq!(value("missing"), None);
        // Values can be referred to, as text.
        let config =
            load_one("v:\n  values:\n    a: 1\n    f: false\n  paths:\n    p: /x/${a}-${f}\n")
                .unwrap();
        assert_eq!(config.get("v", "p").as_deref(), Some("/x/1-false"));
    }

    #[test]
    fn key_has_one_kind_of_value() {
        let errors = load_one(concat!(
            "a:\n",
            "  paths:\n    x: /p\n    y: /q\n",
            "  values:\n    x: 1\n",
            "  runs:\n    y: go\n",
        ))
        .unwrap_err();
        assert_eq!(
            errors,
            [
                "a.values.x: is also set in `paths`; a key has only one kind of value",
                "a.runs.y: is also set in `paths`; a key has only one kind of value",
            ]
        );
    }

    #[test]
    fn key_keeps_kind_across_files() {
        let dir = TempDir::new();
        let cli = dir.write(
            "home-setup/setup.yaml",
            "a:\n  values:\n    x: 5\n  paths:\n    y: /high\n",
        );
        let dist = dir.write(
            "dist/setup.yaml",
            "a:\n  paths:\n    x: /low\n    y: /low\n",
        );
        let errors =
            load_setup(&cli, Some(&dir.path().join("dist")), None, Env::default()).unwrap_err();
        // The change is reported where it's made; changing only the value is fine.
        assert_eq!(
            errors,
            [ConfigError::new(
                Some(&cli),
                "a.values.x",
                format!(
                    "is set in `paths` by {}; a key's kind of value can't change",
                    dist.display()
                ),
            ),]
        );
    }

    #[test]
    fn references_to_any_kind() {
        let config = load_one(concat!(
            "a:\n",
            "  paths:\n    home: /h\n",
            "  values:\n    port: 22\n    name: ${home}-n\n",
            "  runs:\n",
            "    base: [tool, '--port=${port}', '${name}']\n",
            "    more: ${base} extra '${home} x'\n",
            "    search: tool ${dirs} --end\n",
            "  path-lists:\n",
            "    dirs: ['${home}/a', '${b:dirs}', ./c]\n",
            "b:\n  path-lists:\n    dirs: [/b1, /b2]\n",
        ))
        .unwrap();
        let section = found(config.section("a", &SectionDefaults::default()).unwrap());
        let render = |key: &str| {
            section.runs[key]
                .as_ref()
                .unwrap()
                .render(&HashMap::new())
                .unwrap()
        };
        // A whole-argument reference to a command splices its arguments in.
        assert_eq!(
            render("more"),
            ["tool", "--port=22", "/h-n", "extra", "/h x"]
        );
        // And a list of paths, too; `./c` is left out without a project.
        assert_eq!(render("search"), ["tool", "/h/a", "/b1", "/b2", "--end"]);
        // Kept whether or not they're there.
        assert_eq!(
            section.path_lists["dirs"],
            Some(["/h/a", "/b1", "/b2"].map(PathBuf::from).to_vec())
        );
        assert_eq!(
            config.path_list("a", "dirs"),
            ["/h/a", "/b1", "/b2"].map(PathBuf::from)
        );
        assert_eq!(
            section.values["name"],
            ScalarValue::String("/h-n".to_string())
        );
    }

    #[test]
    fn reference_errors_by_kind() {
        let errors = load_one(concat!(
            "a:\n",
            "  values:\n    nothing: null\n    in-text: x${cmd}y\n",
            "  runs:\n    cmd: go\n    uses-null: go ${nothing}\n    loop: go ${loop-list}\n",
            "  path-lists:\n    loop-list: ['${loop}']\n    dirs: [/d]\n",
            "  paths:\n    p: /p/${dirs}\n",
        ))
        .unwrap_err();
        assert_eq!(
            errors,
            [
                "a.values.in-text: `${a:cmd}` is a list; use it as a whole argument in `runs` or a whole entry in `path-lists` or `value-lists`",
                "a.runs.loop: values refer to each other in a loop: ${a:loop} -> ${a:loop-list} -> ${a:loop}",
                "a.paths.p: `${a:dirs}` is a list; use it as a whole argument in `runs` or a whole entry in `path-lists` or `value-lists`",
                "a.runs.uses-null: `${a:nothing}` is null, so it can't be used here",
            ]
        );
    }

    #[test]
    fn expand_errors_are_gathered() {
        let errors = load_one(concat!(
            "a:\n",
            "  paths:\n",
            "    unknown: ${nope}\n",
            "    unknown-section: ${zz:top}\n",
            "    uses-unknown: x${a:unknown}\n",
            "    open: ${a:open\n",
            "    empty: ${}\n",
            "    empty-section: ${:k}\n",
            "    open-x: '{x:tag'\n",
            "    empty-x: '{x:}'\n",
            "    has-x: '/a/{x:tag}'\n",
            "  runs:\n",
            "    refers-to-run: ${a:cmd}\n",
            "    cmd: go\n",
            "  path-lists:\n    l: ['${top}', '{x:tag}', '${a:unknown}/x', '${a:open}']\n",
            "  values:\n    v: '${nope2}'\n    ok: 5\n",
        ))
        .unwrap_err();
        assert_eq!(
            errors,
            [
                // In key order, whatever kind of value each key has.
                "a.paths.empty: `${}` is not a valid reference",
                "a.paths.empty-section: `${:k}` is not a valid reference",
                "a.paths.empty-x: `{x:}` needs a name",
                "a.paths.has-x: paths are file locations; they can't use `{x:...}` values",
                // `${top}` is in the list's own section.
                "a.path-lists.l[0]: unknown reference `${a:top}`",
                "a.path-lists.l[1]: paths can't use `{x:...}` values",
                // `l[2]` and `l[3]` fail because of values reported elsewhere.
                "a.paths.unknown: unknown reference `${a:nope}`",
                "a.paths.open: `${a:open` is missing its `}`",
                "a.paths.open-x: `{x:tag` is missing its `}`",
                "a.paths.unknown-section: unknown reference `${zz:top}`",
                // `a.paths.uses-unknown` fails because of `a.paths.unknown`, already reported,
                // and `a.runs.refers-to-run` splices in the `cmd` command.
                "a.values.v: unknown reference `${a:nope2}`",
            ]
        );
    }

    #[test]
    fn expand_loops() {
        let errors = load_one(
            "a:\n  paths:\n    x: ${b:y}\n    self: ${self}\nb:\n  paths:\n    y: ${a:x}\n    z: ${a:x}\n",
        )
        .unwrap_err();
        assert_eq!(
            errors,
            [
                "a.paths.self: values refer to each other in a loop: ${a:self} -> ${a:self}",
                "a.paths.x: values refer to each other in a loop: ${a:x} -> ${b:y} -> ${a:x}",
            ]
        );
    }

    #[test]
    fn errors_name_the_setting_file() {
        let dir = TempDir::new();
        let cli = dir.write("home-setup/setup.yaml", "a:\n  paths:\n    x: ${a:bad}\n");
        dir.write(
            "dist/setup.yaml",
            "b:\n  paths:\n    y: ${a:x}\n  runs:\n    z: go ${a:x}\n",
        );
        let errors =
            load_setup(&cli, Some(&dir.path().join("dist")), None, Env::default()).unwrap_err();
        assert_eq!(
            errors,
            [ConfigError::new(
                Some(&cli),
                "a.paths.x",
                "unknown reference `${a:bad}`"
            )]
        );
    }

    // ---- sections ----

    #[cfg(unix)]
    fn tool(dir: &TempDir, name: &str) -> PathBuf {
        use std::os::unix::fs::PermissionsExt;
        let path = dir.write(name, "#!/bin/sh\n");
        std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o755)).unwrap();
        path
    }

    #[cfg(unix)]
    #[test]
    fn section_with_exec() {
        let dir = TempDir::new();
        let podman = tool(&dir, "bin dir/podman");
        let file = dir.write(
            "setup.yaml",
            concat!(
                "docker:\n",
                "  programs:\n    exec: podman\n",
                "  paths:\n    socket: /run/podman.sock\n",
                "  runs:\n    build: ${exec} build -t {x:tag} '{x:dir}/a b'\n    label: \"${docker:exec}\"\n",
            ),
        );
        let env = Env {
            path: Some(dir.path().join("bin dir").into_os_string()),
            ..Env::default()
        };
        let config = load_setup(&file, None, None, env).unwrap();
        assert_eq!(config.program_keys("docker").collect::<Vec<_>>(), ["exec"]);
        assert_eq!(config.path_keys("docker").collect::<Vec<_>>(), ["socket"]);
        let defaults = SectionDefaults {
            runs: &[("extra", "${exec} --version --socket ${socket}")],
            ..SectionDefaults::default()
        };
        let section = found(config.section("docker", &defaults).unwrap());
        assert_eq!(
            section.programs,
            BTreeMap::from([("exec".to_string(), Some(podman.clone()))])
        );
        // Kept, though it isn't there.
        assert_eq!(
            section.paths,
            BTreeMap::from([(
                "socket".to_string(),
                Some(PathBuf::from("/run/podman.sock"))
            )])
        );
        assert_eq!(
            section.runs.keys().collect::<Vec<_>>(),
            ["build", "extra", "label"]
        );
        let values = HashMap::from([("tag", "t:1"), ("dir", "/d")]);
        let podman = podman.display().to_string();
        // The path with a space stays one argument.
        assert_eq!(
            section.runs["build"]
                .as_ref()
                .unwrap()
                .render(&values)
                .unwrap(),
            [podman.as_str(), "build", "-t", "t:1", "/d/a b"]
        );
        assert_eq!(
            section.runs["extra"]
                .as_ref()
                .unwrap()
                .render(&values)
                .unwrap(),
            [podman.as_str(), "--version", "--socket", "/run/podman.sock"]
        );
        assert_eq!(
            section.runs["label"]
                .as_ref()
                .unwrap()
                .render(&values)
                .unwrap(),
            [podman.as_str()]
        );
    }

    #[cfg(unix)]
    #[test]
    fn section_exec_with_home() {
        let dir = TempDir::new();
        let found_tool = tool(&dir, "home/bin/tool");
        dir.write("home/my conf", "");
        let file = dir.write(
            "setup.yaml",
            "t:\n  programs:\n    exec: ~/bin/tool\n  paths:\n    conf: ~/my conf\n  runs:\n    go: ${exec} -F ${conf}\n",
        );
        let home = dir.path().join("home");
        let env = Env {
            home: Some(home.clone()),
            ..Env::default()
        };
        let config = load_setup(&file, None, None, env).unwrap();
        let section = found(config.section("t", &SectionDefaults::default()).unwrap());
        assert_eq!(section.programs["exec"], Some(found_tool.clone()));
        assert_eq!(section.paths["conf"], Some(home.join("my conf")));
        assert_eq!(
            section.runs["go"]
                .as_ref()
                .unwrap()
                .render(&HashMap::new())
                .unwrap(),
            [
                found_tool.display().to_string(),
                "-F".to_string(),
                home.join("my conf").display().to_string()
            ]
        );
    }

    #[cfg(unix)]
    #[test]
    fn section_programs_by_any_name() {
        let dir = TempDir::new();
        let keygen = tool(&dir, "bin/ssh-keygen");
        let ssh = tool(&dir, "bin/ssh");
        let file = dir.write(
            "setup.yaml",
            concat!(
                "ssh:\n",
                "  programs:\n    client: ssh\n    keygen: ssh-keygen\n",
                "  paths:\n    home: /h\n    exec: plain\n    keygen-cmd: also-plain\n",
                "  runs:\n    new-key: ${keygen} -f ${home}/key\n",
                "other:\n  programs:\n    helper: no-such-helper\n  paths:\n    home: /o\n",
            ),
        );
        let env = Env {
            path: Some(dir.path().join("bin").into_os_string()),
            ..Env::default()
        };
        let config = load_setup(&file, None, None, env).unwrap();
        let section = found(config.section("ssh", &SectionDefaults::default()).unwrap());
        assert_eq!(
            section.programs,
            BTreeMap::from([
                ("client".to_string(), Some(ssh)),
                ("keygen".to_string(), Some(keygen.clone())),
            ])
        );
        // A path's name doesn't make it a program.
        assert_eq!(section.paths["exec"], Some(PathBuf::from("plain")));
        assert_eq!(
            section.paths["keygen-cmd"],
            Some(PathBuf::from("also-plain"))
        );
        assert_eq!(
            section.runs["new-key"]
                .as_ref()
                .unwrap()
                .render(&HashMap::new())
                .unwrap(),
            [
                keygen.display().to_string(),
                "-f".to_string(),
                "/h/key".to_string()
            ]
        );
        // A missing program has no value; the rest of the section is there.
        let other = config
            .section("other", &SectionDefaults::default())
            .unwrap();
        assert_eq!(other.programs["helper"], None);
        assert_eq!(other.paths["home"], Some(PathBuf::from("/o")));
        assert_eq!(
            other.missing,
            BTreeMap::from([("helper".to_string(), "no-such-helper".to_string())])
        );
    }

    #[cfg(unix)]
    #[test]
    fn section_uses_others_programs() {
        let dir = TempDir::new();
        let python = tool(&dir, "bin/python3");
        let file = dir.write(
            "setup.yaml",
            concat!(
                "python:\n  programs:\n    exec: python3\n    gone: no-such-tool\n",
                "vcs:\n",
                "  runs:\n",
                "    find-root: ['${python:exec}', -E, script.py, '{x:start}']\n",
                "    broken: ${python:gone} x\n",
                "    plain: tool x\n",
            ),
        );
        let env = Env {
            path: Some(dir.path().join("bin").into_os_string()),
            ..Env::default()
        };
        let config = load_setup(&file, None, None, env).unwrap();
        let section = config.section("vcs", &SectionDefaults::default()).unwrap();
        // Another section's program is the program found, as its own are.
        assert_eq!(
            section.runs["find-root"]
                .as_ref()
                .unwrap()
                .render(&HashMap::from([("start", "/s")]))
                .unwrap(),
            [
                python.display().to_string(),
                "-E".into(),
                "script.py".into(),
                "/s".into()
            ]
        );
        // One that isn't there has no value, nor has what refers to it; the
        // section's own `missing` only lists its own programs.
        assert_eq!(section.runs["broken"], None);
        assert!(section.runs["plain"].is_some());
        assert!(section.missing.is_empty());
        assert!(section.programs.is_empty());
    }

    #[test]
    fn programs_and_paths_differ() {
        let dir = TempDir::new();
        let cli = dir.write(
            "home-setup/setup.yaml",
            "t:\n  paths:\n    exec: /usr/bin/tool\n",
        );
        dir.write("dist/setup.yaml", "t:\n  programs:\n    exec: tool\n");
        let errors =
            load_setup(&cli, Some(&dir.path().join("dist")), None, Env::default()).unwrap_err();
        let errors: Vec<String> = errors.iter().map(ToString::to_string).collect();
        assert_eq!(errors.len(), 1, "{errors:?}");
        assert!(
            errors[0].ends_with(
                "t.paths.exec: is set in `programs` by {}; a key's kind of value can't change"
                    .replace(
                        "{}",
                        &dir.path().join("dist/setup.yaml").display().to_string()
                    )
                    .as_str()
            ),
            "{errors:?}"
        );
        assert_eq!(
            one_error("t:\n  programs:\n    exec: [a, b]\n"),
            "t.programs.exec: must be a single value, not a list or mapping"
        );
        assert_eq!(
            one_error("t:\n  paths:\n    exec: a\n  programs:\n    exec: b\n"),
            "t.programs.exec: is also set in `paths`; a key has only one kind of value"
        );
    }

    #[test]
    fn run_as_a_list() {
        let config = load_one(concat!(
            "t:\n",
            "  paths:\n    home: /h\n",
            "  runs:\n",
            "    line: go 'a b' \"c d\"\n",
            "    list: [go, 'a b', \"'kept'\", '\"too\"', '${home}/x', '--tag={x:tag}', '', 5, true]\n",
            "    block:\n      - go\n      - with spaces\n",
        ))
        .unwrap();
        let section = found(config.section("t", &SectionDefaults::default()).unwrap());
        let values = HashMap::from([("tag", "v 1")]);
        let render = |key: &str| section.runs[key].as_ref().unwrap().render(&values).unwrap();
        assert_eq!(render("line"), ["go", "a b", "c d"]);
        // List entries are used as is: no splitting, and quotes are kept.
        assert_eq!(
            render("list"),
            [
                "go",
                "a b",
                "'kept'",
                "\"too\"",
                "/h/x",
                "--tag=v 1",
                "",
                "5",
                "true"
            ]
        );
        assert_eq!(render("block"), ["go", "with spaces"]);
    }

    #[test]
    fn run_list_errors() {
        let errors = load_one(concat!(
            "t:\n  runs:\n",
            "    nested: [go, [a], {b: c}, ~]\n",
            "    empty: []\n",
            "    unknown: [go, '${nope}']\n",
            "    map: {a: b}\n",
        ))
        .unwrap_err();
        assert_eq!(
            errors,
            [
                "t.runs.nested[1]: must be a single value, not a list or mapping",
                "t.runs.nested[2]: must be a single value, not a list or mapping",
                "t.runs.nested[3]: has no value",
                "t.runs.map: must be a single value, not a list or mapping",
            ]
        );
        let errors =
            load_one("t:\n  runs:\n    empty: []\n    unknown: [go, '${nope}']\n").unwrap_err();
        assert_eq!(
            errors,
            [
                "t.runs.empty: the command is empty",
                "t.runs.unknown: unknown reference `${t:nope}`",
            ]
        );
    }

    #[test]
    fn run_list_and_line_merge() {
        let dir = TempDir::new();
        let cli = dir.write(
            "home-setup/setup.yaml",
            "t:\n  runs:\n    go: [high, 'a b']\n",
        );
        dir.write(
            "dist/setup.yaml",
            "t:\n  runs:\n    go: low line\n    other: [low]\n",
        );
        let config =
            load_setup(&cli, Some(&dir.path().join("dist")), None, Env::default()).unwrap();
        let section = found(config.section("t", &SectionDefaults::default()).unwrap());
        let render = |key: &str| {
            section.runs[key]
                .as_ref()
                .unwrap()
                .render(&HashMap::new())
                .unwrap()
        };
        assert_eq!(render("go"), ["high", "a b"]);
        assert_eq!(render("other"), ["low"]);
    }

    #[cfg(unix)]
    #[test]
    fn executable_files_required() {
        let dir = TempDir::new();
        let plain = dir.write("plain-file", "#!/bin/sh\n");
        std::fs::create_dir_all(dir.path().join("a-dir")).unwrap();
        let ok = tool(&dir, "bin/ok");
        let file = dir.write(
            "setup.yaml",
            &format!(
                "t:\n  programs:\n    exec: {}\n    dir: {}\n    name: no-such-program\n    ok: {}\n  paths:\n    data: {}\n  runs:\n    go: ${{exec}} x\n    run-ok: ${{ok}} y\n",
                plain.display(),
                dir.path().join("a-dir").display(),
                ok.display(),
                dir.path().join("missing-data").display(),
            ),
        );
        let config = load_setup(&file, None, None, Env::default()).unwrap();
        let section = config.section("t", &SectionDefaults::default()).unwrap();
        // Not executable, a directory, and not on the `PATH`: no value.
        assert_eq!(section.programs["exec"], None);
        assert_eq!(section.programs["dir"], None);
        assert_eq!(section.programs["name"], None);
        assert_eq!(section.programs["ok"], Some(ok.clone()));
        // Paths are kept, whether or not they're there.
        assert_eq!(section.paths["data"], Some(dir.path().join("missing-data")));
        assert_eq!(section.runs["go"], None);
        assert!(section.runs["run-ok"].is_some());
        assert_eq!(
            section.missing.keys().collect::<Vec<_>>(),
            ["dir", "exec", "name"]
        );
    }

    #[test]
    fn section_without_exec() {
        let config =
            load_one("ssh:\n  paths:\n    home: /h/.ssh\n    config-file: ${home}/config\n")
                .unwrap();
        assert_eq!(config.program_keys("ssh").count(), 0);
        let section = found(config.section("ssh", &SectionDefaults::default()).unwrap());
        assert!(section.programs.is_empty());
        // Kept, though it isn't there: whoever uses it checks.
        assert_eq!(
            section.paths["config-file"],
            Some(PathBuf::from("/h/.ssh/config"))
        );
        assert_eq!(
            config.get("ssh", "config-file").as_deref(),
            Some("/h/.ssh/config")
        );
        assert!(section.runs.is_empty());
        // A section nobody set is empty.
        let section = found(config.section("none", &SectionDefaults::default()).unwrap());
        assert!(section.paths.is_empty() && section.programs.is_empty() && section.runs.is_empty());
    }

    #[test]
    fn section_defaults_not_found() {
        let config = load_one("").unwrap();
        let defaults = SectionDefaults {
            programs: &[("exec", "no-such-docker")],
            runs: &[("go", "${exec} x")],
            ..SectionDefaults::default()
        };
        let section = config.section("docker", &defaults).unwrap();
        assert_eq!(section.programs["exec"], None);
        assert!(section.paths.is_empty());
        assert_eq!(section.runs["go"], None);
        assert_eq!(
            section.missing,
            BTreeMap::from([("exec".to_string(), "no-such-docker".to_string())])
        );
        let defaults = SectionDefaults {
            runs: &[("go", "${exec} x")],
            ..SectionDefaults::default()
        };
        let missing = config.section("docker", &defaults).unwrap_err();
        assert_eq!(
            missing.to_string(),
            "docker.runs.go: unknown reference `${docker:exec}`"
        );
    }

    #[cfg(unix)]
    #[test]
    fn section_bad_default() {
        let dir = TempDir::new();
        tool(&dir, "bin/sh-tool");
        let file = dir.write("setup.yaml", "t:\n  programs:\n    exec: sh-tool\n");
        let env = Env {
            path: Some(dir.path().join("bin").into_os_string()),
            ..Env::default()
        };
        let config = load_setup(&file, None, None, env).unwrap();
        let defaults = SectionDefaults {
            runs: &[("go", "${exec} 'x")],
            ..SectionDefaults::default()
        };
        let error = config.section("t", &defaults).unwrap_err();
        assert_eq!(error.to_string(), "t.runs.go: unmatched ' quote");
    }

    #[test]
    fn commands_checked_on_load() {
        let errors = load_one(concat!(
            "t:\n",
            "  programs:\n    exec: tool {x:no}\n",
            "  runs:\n",
            "    quote: ${exec} 'x\n",
            "    empty: '  '\n",
            "    unknown: ${exec} ${nope}\n",
            "    ok: ${exec} {x:fine}\n",
            "no-exec:\n  runs:\n    go: ${exec} now\n",
        ))
        .unwrap_err();
        assert_eq!(
            errors,
            [
                "no-exec.runs.go: unknown reference `${no-exec:exec}`",
                // In key order, whatever kind of value each key has.
                "t.runs.empty: the command is empty",
                "t.programs.exec: programs are files; they can't use `{x:...}` values",
                "t.runs.quote: unmatched ' quote",
                "t.runs.unknown: unknown reference `${t:nope}`",
            ]
        );
    }

    // ---- starting from a setup record ----

    #[test]
    fn record_expands_without_reads() {
        let dir = TempDir::new();
        let file = dir.write(
            "setup.yaml",
            concat!(
                "a:\n",
                "  paths:\n    local: ./local\n    home: /h\n",
                "  runs:\n    go: [tool, '${local}']\n    line: tool 'a b'\n",
                "  path-lists:\n    dirs: [./d, '${home}/e']\n",
                "  values:\n    n: 3\n    t: ${home}/t\n",
            ),
        );
        let first = load_setup(&file, None, None, Env::default()).unwrap();
        // Without a project, `./` stays as written, and `./` list entries are left out.
        assert_eq!(first.get("a", "local").as_deref(), Some("./local"));
        assert_eq!(first.path_list("a", "dirs"), [PathBuf::from("/h/e")]);
        let record = first.setup_record().clone();
        assert_eq!(record.files.len(), 1);
        assert_eq!(record.files[0].source, Source::User);

        // The second pass doesn't read the file, so it can be gone.
        std::fs::remove_file(&file).unwrap();
        let origin = dir.path().join("first.json");
        let options = SetupOptions {
            project_dir: Some(Path::new("/proj")),
            no_search: true,
            base: Some(base(&record, &origin)),
            ..SetupOptions::default()
        };
        let second = load_with(&options, Env::default()).unwrap();
        assert_eq!(second.files(), [file]);
        assert_eq!(second.get("a", "local").as_deref(), Some("/proj/local"));
        assert_eq!(
            second.path_list("a", "dirs"),
            [PathBuf::from("/proj/d"), PathBuf::from("/h/e")]
        );
        assert_eq!(second.value("a", "n"), Some(ScalarValue::Int(3)));
        let section = second.section("a", &SectionDefaults::default()).unwrap();
        let render = |key: &str| {
            section.runs[key]
                .as_ref()
                .unwrap()
                .render(&HashMap::new())
                .unwrap()
        };
        assert_eq!(render("go"), ["tool", "/proj/local"]);
        assert_eq!(render("line"), ["tool", "a b"]);
        // And the record carries over as it was.
        assert_eq!(second.setup_record(), &record);
    }

    #[test]
    fn record_is_search_base_layer() {
        let dir = TempDir::new();
        let cli = dir.write(
            "home-setup/setup.yaml",
            "a:\n  paths:\n    x: /cli\n    y: /cli\n",
        );
        let dist = dir.write(
            "dist/setup.yaml",
            "a:\n  paths:\n    x: /dist\n    z: /dist\n",
        );
        let first = load_setup(&cli, Some(&dir.path().join("dist")), None, Env::default()).unwrap();
        let record = first.setup_record().clone();
        assert_eq!(
            first.loaded_from().distribution_dir,
            [dir.path().join("dist").display().to_string()]
        );

        // Changed on disk since: the record's copy is used, not read again.
        dir.write("dist/setup.yaml", "a:\n  paths:\n    x: /changed\n");
        // Found by the search now: another user file, which goes before the
        // record's.
        let user = dir.write(
            "home/.config/surf-scoter.setup.yaml",
            "a:\n  paths:\n    x: /user\n    y: /user\n    w: /user\n",
        );
        let env = Env {
            home: Some(dir.path().join("home")),
            ..Env::default()
        };
        let origin = dir.path().join("first.json");
        let options = SetupOptions {
            base: Some(base(&record, &origin)),
            ..SetupOptions::default()
        };
        let config = load_with(&options, env.clone()).unwrap();
        assert_eq!(config.files(), [user.clone(), cli.clone(), dist.clone()]);
        assert_eq!(config.get("a", "x").as_deref(), Some("/user"));
        assert_eq!(config.get("a", "y").as_deref(), Some("/user"));
        assert_eq!(config.get("a", "z").as_deref(), Some("/dist"));
        assert_eq!(config.get("a", "w").as_deref(), Some("/user"));
        assert_eq!(
            config.get(SCOTER_SECTION, USER_SETUP_FILE),
            Some(user.display().to_string())
        );

        // Without the search, only the record.
        let options = SetupOptions {
            no_search: true,
            ..options
        };
        let config = load_with(&options, env).unwrap();
        assert_eq!(config.files(), [cli, dist]);
        assert_eq!(config.get("a", "w"), None);
    }

    const NO_INPUTS: &SetupInputs = &SetupInputs {
        user_setup_file: None,
        user_dir: None,
        distribution_dir: None,
        distribution_bin_dir: None,
    };

    const NO_HISTORY: &LoadedFrom = &LoadedFrom {
        user_setup_file: Vec::new(),
        user_dir: Vec::new(),
        distribution_dir: Vec::new(),
        project_config: Vec::new(),
    };

    /// A base with no inputs or history of its own.
    fn base<'a>(record: &'a SetupRecord, origin: &'a Path) -> SetupBase<'a> {
        SetupBase {
            record,
            loaded_from: NO_HISTORY,
            origin,
            inputs: NO_INPUTS,
        }
    }

    #[test]
    fn record_keeps_input_history() {
        let dir = TempDir::new();
        dir.write("dist1/setup.yaml", "");
        dir.write("dist2/setup.yaml", "");
        let dist1 = dir.path().join("dist1");
        let dist2 = dir.path().join("dist2");
        let text = |p: &Path| p.display().to_string();
        let origin = dir.path().join("u.json");
        let record = SetupRecord::default();
        let run = |history: Option<&LoadedFrom>, dist: Option<&Path>, env: Env| {
            let options = SetupOptions {
                distribution_dir: dist,
                base: history.map(|loaded_from| SetupBase {
                    loaded_from,
                    ..base(&record, &origin)
                }),
                ..SetupOptions::default()
            };
            load_with(&options, env).unwrap().loaded_from().clone()
        };
        let no_user = Env::default();

        let first = run(None, Some(&dist1), no_user.clone());
        assert_eq!(first.distribution_dir, [text(&dist1)]);
        let second = run(Some(&first), Some(&dist2), no_user.clone());
        assert_eq!(second.distribution_dir, [text(&dist2), text(&dist1)]);
        // Given again: it moves to the front, without a duplicate.
        let third = run(Some(&second), Some(&dist1), no_user.clone());
        assert_eq!(third.distribution_dir, [text(&dist1), text(&dist2)]);
        // None given leaves the history be.
        let fourth = run(Some(&third), None, no_user);
        assert_eq!(fourth.distribution_dir, third.distribution_dir);
        assert!(fourth.user_setup_file.is_empty() && fourth.user_dir.is_empty());

        // Only the user directories actually found are kept.
        let in_d = dir.write("home/.config/surf-scoter.d/setup.yaml", "");
        let env = Env {
            home: Some(dir.path().join("home")),
            ..Env::default()
        };
        let fifth = run(Some(&fourth), None, env.clone());
        assert_eq!(fifth.user_setup_file, [text(&in_d)]);
        assert_eq!(
            fifth.user_dir,
            [text(&dir.path().join("home/.config/surf-scoter.d"))]
        );
        std::fs::remove_file(&in_d).unwrap();
        let flat = dir.write("home/.config/surf-scoter.setup.yaml", "");
        let sixth = run(Some(&fifth), None, env);
        assert_eq!(sixth.user_setup_file, [text(&flat), text(&in_d)]);
        // Found outside a user directory: none added.
        assert_eq!(sixth.user_dir, fifth.user_dir);
    }

    #[test]
    fn record_duplicates_removed() {
        let record = SetupRecord::default();
        let history = LoadedFrom {
            distribution_dir: vec!["/a".to_string(), "/b".to_string(), "/a".to_string()],
            project_config: vec!["/p".to_string(), "/p".to_string()],
            ..LoadedFrom::default()
        };
        let origin = Path::new("/u.json");
        let options = SetupOptions {
            no_search: true,
            base: Some(SetupBase {
                loaded_from: &history,
                ..base(&record, origin)
            }),
            ..SetupOptions::default()
        };
        let config = load_with(&options, Env::default()).unwrap();
        assert_eq!(config.loaded_from().distribution_dir, ["/a", "/b"]);
        assert_eq!(config.loaded_from().project_config, ["/p"]);
    }

    #[test]
    fn base_inputs_are_used() {
        let record = SetupRecord::default();
        let origin = Path::new("/u.json");
        let inputs = SetupInputs {
            user_setup_file: Some("/home/me/.config/surf-scoter.d/setup.yaml".to_string()),
            user_dir: Some("/home/me/.config/surf-scoter.d".to_string()),
            distribution_dir: Some("/opt/scoter".to_string()),
            // As another program changed it.
            distribution_bin_dir: Some("/opt/other-bin".to_string()),
        };
        let options = SetupOptions {
            no_search: true,
            base: Some(SetupBase {
                inputs: &inputs,
                ..base(&record, origin)
            }),
            ..SetupOptions::default()
        };
        let config = load_with(&options, Env::default()).unwrap();
        assert_eq!(
            config.get(SCOTER_SECTION, USER_SETUP_FILE).as_deref(),
            Some("/home/me/.config/surf-scoter.d/setup.yaml")
        );
        assert_eq!(
            config.get(SCOTER_SECTION, USER_DIR).as_deref(),
            Some("/home/me/.config/surf-scoter.d")
        );
        assert_eq!(
            config.get(SCOTER_SECTION, DISTRIBUTION_DIR).as_deref(),
            Some("/opt/scoter")
        );
        assert_eq!(
            config.get(SCOTER_SECTION, DISTRIBUTION_BIN_DIR).as_deref(),
            Some("/opt/other-bin")
        );
        assert_eq!(config.setup_inputs(), &inputs);
        // Only a record: the history isn't changed by them.
        assert!(config.loaded_from().user_setup_file.is_empty());

        // Another distribution directory has its own `bin`.
        let options = SetupOptions {
            distribution_dir: Some(Path::new("/opt/new")),
            ..options
        };
        let config = load_with(&options, Env::default()).unwrap();
        assert_eq!(
            config.get(SCOTER_SECTION, DISTRIBUTION_BIN_DIR).as_deref(),
            Some("/opt/new/bin")
        );
    }

    #[test]
    fn distribution_bin_dir() {
        // Without a distribution directory, it isn't set.
        let config = load_with(&SetupOptions::default(), Env::default()).unwrap();
        assert_eq!(config.get(SCOTER_SECTION, DISTRIBUTION_BIN_DIR), None);

        let options = SetupOptions {
            distribution_dir: Some(Path::new("/opt/scoter")),
            no_search: true,
            ..SetupOptions::default()
        };
        let config = load_with(&options, Env::default()).unwrap();
        assert_eq!(
            config.get(SCOTER_SECTION, DISTRIBUTION_BIN_DIR).as_deref(),
            Some("/opt/scoter/bin")
        );
        assert_eq!(
            config.setup_inputs().distribution_bin_dir.as_deref(),
            Some("/opt/scoter/bin")
        );

        // From a setup file's distribution directory, too, and usable there.
        let config = load_one(
            "scoter:\n  paths:\n    distribution-dir: /dist\na:\n  paths:\n    x: ${scoter:distribution-bin-dir}/x\n",
        )
        .unwrap();
        assert_eq!(config.get("a", "x").as_deref(), Some("/dist/bin/x"));
    }

    #[test]
    fn record_errors() {
        let record: SetupRecord = serde_json::from_str(
            r#"{"files": [{"file": "/s.yaml", "source": "user", "sections": {
                "a": {"paths": {"x": "/p"}, "values": {"x": 1}},
                "scoter": {"paths": {"user-dir": "/u"}}
            }}]}"#,
        )
        .unwrap();
        let origin = Path::new("/first.json");
        let options = SetupOptions {
            no_search: true,
            base: Some(base(&record, origin)),
            ..SetupOptions::default()
        };
        let errors = load_with(&options, Env::default()).unwrap_err();
        let errors: Vec<String> = errors.iter().map(ToString::to_string).collect();
        assert_eq!(
            errors,
            [
                "/first.json: setup.files[0].sections.a.values.x: is also set in `paths`; a key has only one kind of value",
                "/first.json: setup.files[0].sections.scoter.paths.user-dir: is set by the tool; remove it",
            ]
        );
        // Unknown fields are refused.
        assert!(serde_json::from_str::<SetupRecord>(r#"{"files": [], "extra": 1}"#).is_err());
    }

    #[test]
    fn find_var_case() {
        let vars = HashMap::from([
            ("Path".to_string(), "C:\\bin".to_string()),
            ("HOME".to_string(), "/h".to_string()),
        ]);
        assert_eq!(find_var(&vars, "Path", false), Some("C:\\bin"));
        assert_eq!(find_var(&vars, "PATH", false), None);
        // As on Windows.
        assert_eq!(find_var(&vars, "PATH", true), Some("C:\\bin"));
        assert_eq!(find_var(&vars, "home", true), Some("/h"));
        assert_eq!(find_var(&vars, "other", true), None);
    }

    #[test]
    fn env_expand_home() {
        let env = Env {
            home: Some(PathBuf::from("/h")),
            ..Env::default()
        };
        assert_eq!(env.expand_home("~"), PathBuf::from("/h"));
        assert_eq!(env.expand_home("~/a"), PathBuf::from("/h/a"));
        assert_eq!(env.expand_home("~user/a"), PathBuf::from("~user/a"));
        assert_eq!(env.expand_home("/a/~"), PathBuf::from("/a/~"));
        assert_eq!(Env::default().expand_home("~/a"), PathBuf::from("~/a"));
    }

    // ---- host-commands ----

    #[test]
    fn host_checks_not_referable() {
        let check = "  host-commands:\n    c: {action: warn, reason: r, commands: [x]}\n";
        assert_eq!(
            one_error(&format!("a:\n  values:\n    v: 'is ${{a:c}}'\n{check}")),
            "a.values.v: `${a:c}` is a `host-commands` check; nothing can refer to one"
        );
        assert_eq!(
            one_error(&format!("a:\n  path-lists:\n    l: ['${{c}}']\n{check}")),
            "a.path-lists.l[0]: `${a:c}` is a `host-commands` check; nothing can refer to one"
        );
        let config = load_one(&format!("a:\n{check}")).unwrap();
        assert_eq!(
            config.scalar_text("a", "c").unwrap_err(),
            "`${a:c}` is a `host-commands` check; nothing can refer to one"
        );
        assert_eq!(config.get("a", "c"), None);
    }

    #[test]
    fn host_commands_errors() {
        let errors = load_one(concat!(
            "a:\n  host-commands:\n",
            "    bad: {action: warn, reason: r, commands: [/bin/sh]}\n",
            "    ref: {action: warn, reason: r, commands: [x]}\n",
            "    list: [sh]\n",
            "  values:\n    ref: 1\n",
        ))
        .unwrap_err();
        assert_eq!(
            errors,
            [
                "a.host-commands.bad.commands[0]: '/bin/sh' is a command name, not a path; map the name to a program with `command-map`",
                "a.host-commands.list: must be a check (a mapping with `action`, `reason` and what it matches), or null to remove it",
                "a.values.ref: is also set in `host-commands`; a key has only one kind of value",
            ]
        );
        assert_eq!(
            one_error(
                "a:\n  host-commands:\n    ext: {action: warn, reason: r, command-map: {d: '{x:d}'}}\n"
            ),
            "a.host-commands.ext.command-map.d: programs are files; they can't use `{x:...}` values"
        );
        assert_eq!(
            one_error(
                "a:\n  host-commands:\n    ref: {action: warn, reason: r, paths: ['${a:nope}/x']}\n"
            ),
            "a.host-commands.ref.paths[0]: unknown reference `${a:nope}`"
        );
    }

    #[test]
    fn host_commands_kind_fixed() {
        let dir = TempDir::new();
        let dist = dir.write(
            "dist/setup.yaml",
            "a:\n  host-commands:\n    c: {action: warn, reason: r, commands: [x]}\n",
        );
        let user = dir.write("home/setup.yaml", "a:\n  values:\n    c: 1\n");
        let errors = load_setup(&user, dist.parent(), None, Env::default())
            .map_err(messages)
            .unwrap_err();
        assert_eq!(
            errors,
            [format!(
                "a.values.c: is set in `host-commands` by {}; a key's kind of value can't change",
                dist.display()
            )]
        );
    }

    #[cfg(unix)]
    #[test]
    fn host_commands_expanded() {
        use std::os::unix::fs::PermissionsExt;
        let dir = TempDir::new();
        let tool = |name: &str| {
            let path = dir.write(name, "#!/bin/sh\n");
            std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o755)).unwrap();
            path
        };
        let podman = tool("bin/podman");
        let git = tool("bin/git");
        let dist = dir.write(
            "dist/setup.yaml",
            concat!(
                "a:\n  host-commands:\n",
                "    gone: {action: refuse, reason: r, commands: [x]}\n",
                "    kept: {action: refuse, reason: r, commands: [y]}\n",
            ),
        );
        let user = dir.write(
            "home/setup.yaml",
            concat!(
                "a:\n  programs:\n    exec: podman\n    missing: not-there\n",
                "  path-lists:\n    shims: [/s1, /s2]\n",
                "  host-commands:\n",
                "    gone: null\n",
                "    c:\n      action: warn\n      reason: why\n      commands: [git, nope]\n",
                "      command-map: {docker: '${exec}', other: '${missing}'}\n",
                "      options: [-c]\n",
                "    p: {action: refuse, reason: r, paths: ['~/shims', '${shims}']}\n",
            ),
        );
        let env = Env {
            home: Some(PathBuf::from("/home/u")),
            path: Some(dir.path().join("bin").into_os_string()),
            ..Env::default()
        };
        let config = load_setup(&user, dist.parent(), None, env).unwrap();
        let section = config.section("a", &SectionDefaults::default()).unwrap();
        assert_eq!(
            section.host_commands.keys().collect::<Vec<_>>(),
            ["c", "kept", "p"]
        );
        let c = &section.host_commands["c"];
        assert_eq!(c.action, host_commands::Action::Warn);
        assert_eq!(
            c.commands,
            BTreeMap::from([
                ("docker".to_string(), Some(podman)),
                ("git".to_string(), Some(git)),
                ("nope".to_string(), None),
                ("other".to_string(), None),
            ])
        );
        assert_eq!(c.options, ["-c"]);
        assert_eq!(
            section.host_commands["p"].paths,
            [
                PathBuf::from("/home/u/shims"),
                PathBuf::from("/s1"),
                PathBuf::from("/s2")
            ]
        );
        assert_eq!(config.host_command_file("a", "kept"), Some(dist.as_path()));
        assert_eq!(config.host_command_file("a", "c"), Some(user.as_path()));
    }

    // ---- value-lists ----

    #[test]
    fn value_lists_expanded() {
        let config = load_one(concat!(
            "a:\n  values:\n    v: one\n  paths:\n    p: /p\n",
            "  value-lists:\n",
            "    base: ['${v}', two, 3, true]\n",
            "    more: ['${base}', 'x-${a:p}', '$${kept}', '${scoter:project-dir}/gone']\n",
            "    empty: []\n",
        ))
        .unwrap();
        assert_eq!(config.value_list("a", "base"), ["one", "two", "3", "true"]);
        // A whole reference splices the list in; a location this run
        // doesn't have leaves its entry out.
        assert_eq!(
            config.value_list("a", "more"),
            ["one", "two", "3", "true", "x-/p", "${kept}"]
        );
        assert!(config.value_list("a", "empty").is_empty());
        assert!(config.value_list("a", "missing").is_empty());
        let section = config.section("a", &SectionDefaults::default()).unwrap();
        assert_eq!(
            section.value_lists["base"].as_deref(),
            Some(&["one", "two", "3", "true"].map(String::from)[..])
        );
    }

    #[test]
    fn value_lists_errors() {
        let errors = load_one(concat!(
            "a:\n  value-lists:\n",
            "    not-list: text\n",
            "    nested: [ok, [no], {m: 1}]\n",
        ))
        .unwrap_err();
        assert_eq!(
            errors,
            [
                "a.value-lists.not-list: must be a list of values",
                "a.value-lists.nested[1]: must be a single value, not a list or mapping",
                "a.value-lists.nested[2]: must be a single value, not a list or mapping",
            ]
        );
        assert_eq!(
            one_error("a:\n  value-lists:\n    l: [ok, '{x:no}']\n"),
            "a.value-lists.l[1]: value lists can't use `{x:...}` values"
        );
        assert_eq!(
            one_error("a:\n  value-lists:\n    l: ['${a:nope}']\n"),
            "a.value-lists.l[0]: unknown reference `${a:nope}`"
        );
        assert_eq!(
            one_error("a:\n  value-lists:\n    l: [x]\n  values:\n    v: 'in ${l}'\n"),
            "a.values.v: `${a:l}` is a list; use it as a whole argument in `runs` or a whole entry in `path-lists` or `value-lists`"
        );
    }

    #[test]
    fn value_lists_replace_whole() {
        let dir = TempDir::new();
        let dist = dir.write(
            "dist/setup.yaml",
            "a:\n  value-lists:\n    l: [d1, d2]\n    k: [k]\n",
        );
        let user = dir.write("home/setup.yaml", "a:\n  value-lists:\n    l: [u]\n");
        let config = load_setup(&user, dist.parent(), None, Env::default()).unwrap();
        assert_eq!(config.value_list("a", "l"), ["u"]);
        assert_eq!(config.value_list("a", "k"), ["k"]);
        let record = &config.setup_record().files[0].sections["a"];
        assert_eq!(record.value_lists["l"], ["u"]);
    }
}
