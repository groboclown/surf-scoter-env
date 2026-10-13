//! The system setup files: settings for this computer.  See
//! `docs/system-config-file.md`.
//!
//! These files are read, highest priority first:
//!   1. the `--user-setup` file;
//!   2. the user's setup file, the first found of
//!      `$SURF_SCOTER_HOME/setup.{yaml,yml}`,
//!      `~/.config/surf-scoter.setup.{yaml,yml}` and
//!      `~/.config/surf-scoter.d/setup.{yaml,yml}`;
//!   3. `${scoter:distribution-dir}/setup.{yaml,yml}`, using the settings so
//!      far: `--distribution-dir`, or else what the files above set.
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
//! `dev-container-path`, `user-setup-file`, `user-dir`, `distribution-dir`
//! and `cwd`.  Setup files can set `distribution-dir` (the tool's value, when
//! it has one, wins), but none of the others.

use std::collections::{BTreeMap, HashMap, HashSet};
use std::ffi::OsString;
use std::path::{Path, PathBuf};

use serde::{Deserialize, Serialize};
use yaml_serde::Value;

use super::command::{self, CommandTemplate, Part};
use super::read::{parse_yaml, read_limited, tag_errors};
use super::{ConfigError, child_key, index_key};

pub const SCOTER_SECTION: &str = "scoter";
// The `scoter` section's `paths` the tool sets.
pub const PROJECT_CONFIG: &str = "project-config";
pub const PROJECT_DIR: &str = "project-dir";
pub const DEV_CONTAINER_PATH: &str = "dev-container-path";
pub const USER_SETUP_FILE: &str = "user-setup-file";
pub const USER_DIR: &str = "user-dir";
/// The one a setup file can also set.
pub const DISTRIBUTION_DIR: &str = "distribution-dir";
pub const CWD: &str = "cwd";
/// The `scoter` `paths` only the tool sets.
const TOOL_ONLY_KEYS: [&str; 6] = [
    PROJECT_CONFIG,
    PROJECT_DIR,
    DEV_CONTAINER_PATH,
    USER_SETUP_FILE,
    USER_DIR,
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
/// A section's plain values.
pub const VALUES: &str = "values";
const SETUP: &str = "setup";
const USER_SETUP: &str = "surf-scoter.setup";

/// What the process environment says about where things are.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct Env {
    /// The user's home directory.
    pub home: Option<PathBuf>,
    /// `SURF_SCOTER_HOME`.
    pub scoter_home: Option<PathBuf>,
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
            path: var("PATH"),
            path_ext: if cfg!(windows) { var("PATHEXT") } else { None },
            cwd: std::env::current_dir().ok(),
            vars: std::env::vars_os()
                .filter_map(|(k, v)| Some((k.into_string().ok()?, v.into_string().ok()?)))
                .collect(),
        }
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
    Value(ScalarValue),
}

impl Entry {
    /// The group the entry is written in.
    fn group(&self) -> &'static str {
        match self {
            Entry::Path(_) => PATHS,
            Entry::Program(_) => PROGRAMS,
            Entry::Run(_) => RUNS,
            Entry::PathList(_) => PATH_LISTS,
            Entry::Value(_) => VALUES,
        }
    }
}

/// How a setup file was found, which sets its priority: earlier ones first.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Serialize, Deserialize)]
#[serde(rename_all = "kebab-case")]
pub enum Source {
    /// The `--user-setup` file.
    UserSetup,
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
    // A history of the runs' inputs, most recent first: they're only a record,
    // and aren't used to work out the configuration.  Duplicates are allowed,
    // but removed when written.
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

impl SetupRecord {
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
/// file and directory found, and the distribution directory given (which
/// wins over the setup files').
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct SetupInputs {
    pub user_setup_file: Option<String>,
    pub user_dir: Option<String>,
    pub distribution_dir: Option<String>,
}

/// Setup already worked out: its record, and the inputs that were in effect
/// (they aren't in any file).
#[derive(Debug, Clone, Copy)]
pub struct SetupBase<'a> {
    pub record: &'a SetupRecord,
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
    #[serde(default)]
    pub values: BTreeMap<String, ScalarValue>,
}

/// Where to find the system setup.
#[derive(Debug, Clone, Copy, Default)]
pub struct SetupOptions<'a> {
    /// The `--user-setup` file.
    pub user_setup: Option<&'a Path>,
    pub distribution_dir: Option<&'a Path>,
    /// Where `./` paths start: `scoter:project-dir`.
    pub project_dir: Option<&'a Path>,
    /// The project file in effect: `scoter:project-config`.
    pub project_config: Option<&'a Path>,
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
    /// The project file given this time, to record.
    pub given_project_config: Option<&'a Path>,
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
    /// The plain values.
    pub values: BTreeMap<String, ScalarValue>,
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
    user_setup: Option<&Path>,
    distribution_dir: Option<&Path>,
    project_dir: Option<&Path>,
    env: Env,
) -> Result<SystemConfig, Vec<ConfigError>> {
    let options = SetupOptions {
        user_setup,
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
        inputs = base.inputs.clone();
    }
    let distribution_dir = options.distribution_dir.map(super::absolute);
    if let Some(dir) = &distribution_dir {
        inputs.distribution_dir = Some(path_text(dir));
        record.distribution_dir.insert(0, path_text(dir));
    }
    if let Some(project) = options.given_project_config {
        record.project_config.insert(0, path_text(project));
    }

    let mut found: Vec<(PathBuf, Source)> = Vec::new();
    if let Some(path) = options.user_setup {
        if path.is_file() {
            found.push((path.to_path_buf(), Source::UserSetup));
        } else {
            errors.push(ConfigError::new(
                Some(path),
                "",
                "the --user-setup file doesn't exist",
            ));
        }
    }
    if !options.no_search {
        match find_user_setup(&env) {
            Ok(Some((file, dir))) => {
                inputs.user_setup_file = Some(path_text(&file));
                inputs.user_dir = dir.as_deref().map(path_text);
                record.user_setup_file.insert(0, path_text(&file));
                if let Some(dir) = &inputs.user_dir {
                    record.user_dir.insert(0, dir.clone());
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

    let mut tool_values: Vec<(&str, String)> = Vec::new();
    let mut add = |key, value: Option<String>| tool_values.extend(value.map(|v| (key, v)));
    add(PROJECT_CONFIG, options.project_config.map(path_text));
    add(PROJECT_DIR, options.project_dir.map(path_text));
    add(USER_SETUP_FILE, inputs.user_setup_file.clone());
    add(USER_DIR, inputs.user_dir.clone());
    add(DISTRIBUTION_DIR, inputs.distribution_dir.clone());
    add(CWD, env.cwd.as_deref().map(path_text));
    let merge = |files: &[SetupFile]| {
        SystemConfig::merge(files, &tool_values, env.clone(), options.project_dir)
    };
    let mut config = merge(&files);
    // The distribution directory's file comes last, as the files so far say
    // where it is.  A base's is already among its files, and a file already
    // read isn't read again.  Errors in `distribution-dir` itself are
    // reported with the rest, below.
    if options.distribution_dir.is_some() || !options.no_search {
        let distribution_dir = config.get_path(SCOTER_SECTION, DISTRIBUTION_DIR);
        if let Some(dir) = distribution_dir {
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

    errors.extend(kind_changes(&files));
    // Stands in for the dev container's directory, until it's found.
    let mut later = Fallbacks::new();
    if options.dev_container {
        later.insert(
            (SCOTER_SECTION.to_string(), DEV_CONTAINER_PATH.to_string()),
            Entry::Path(DEV_CONTAINER_PATH.to_string()),
        );
    }
    errors.extend(config.validate(&later));
    record.files = files.iter().map(SetupFile::record).collect();
    config.record = record.without_duplicates();
    config.inputs = inputs;
    if errors.is_empty() {
        Ok(config)
    } else {
        Err(errors)
    }
}

/// The user's setup file, and the user directory it's in (when it's in one).
fn find_user_setup(env: &Env) -> Result<Option<(PathBuf, Option<PathBuf>)>, ConfigError> {
    if let Some(dir) = env.scoter_home.as_deref().map(super::absolute)
        && let Some(file) = find_setup(&dir, SETUP)?
    {
        return Ok(Some((file, Some(dir))));
    }
    if let Some(home) = &env.home {
        let config = home.join(".config");
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
                    Entry::PathList(list) => {
                        section.path_lists.insert(key, list.clone());
                    }
                    Entry::Value(value) => {
                        section.values.insert(key, value.clone());
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
                    .values
                    .iter()
                    .map(|(k, v)| (k, Entry::Value(v.clone()))),
            );
        for (key, entry) in all {
            let at = format!(
                "setup.files[{i}].sections.{}",
                child_key(name, &group_key(entry.group(), key))
            );
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
                    "must be a section, with `paths`, `programs`, `runs`, `path-lists` and `values` mappings"
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
                Some(VALUES) => VALUES,
                _ => {
                    let shown = group
                        .as_str()
                        .map_or_else(|| format!("{group:?}"), str::to_string);
                    errors.push(error(
                        &child_key(name, &shown),
                        "unknown key; a section has `paths`, `programs`, `runs`, `path-lists` and `values`"
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
        tool_values: &[(&str, String)],
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
                    value: Entry::Path(value.clone()),
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
        self.sections
            .entry(SCOTER_SECTION.to_string())
            .or_default()
            .insert(
                DEV_CONTAINER_PATH.to_string(),
                Setting {
                    value: Entry::Path(path_text(dir)),
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
                    // Only text is expanded.
                    Entry::Value(value) if !matches!(value, ScalarValue::String(_)) => {}
                    entry => match expander.resolve(name, key, &None, &mut Vec::new()) {
                        Ok(Resolved::List(words))
                            if words.is_empty() && matches!(entry, Entry::Run(_)) =>
                        {
                            add(ConfigError::new(file, &at, "the command is empty"));
                        }
                        Ok(_) => {}
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
        let key = key.split_once('[').map_or(key, |(key, _)| key);
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
            None => Err(format!("unknown reference {reference}")),
            Some(Entry::Run(_) | Entry::PathList(_)) => {
                Err(format!("{reference} is a list; it can't be used here"))
            }
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

        let mut overrides = Values::new();
        let mut missing = Missing::new();
        let mut missing_programs = BTreeMap::new();
        let mut programs = BTreeMap::new();
        let no_values = Values::new();
        let no_missing = Missing::new();
        for key in &program_keys {
            let key = key.as_str();
            let program = self
                .expander(&no_values, &fallbacks, &no_missing)
                .resolve(name, key, &None, &mut Vec::new())
                .map_err(|f| failure(f, PROGRAMS, key))?;
            let program = match program {
                Resolved::Text(parts) => command::render_parts(&parts),
                Resolved::List(_) => unreachable!("programs are text"),
            };
            let id = (name.to_string(), key.to_string());
            let found = self.find_program(&program);
            match &found {
                Some(found) => {
                    overrides.insert(id, path_text(found));
                }
                None => {
                    missing.insert(id);
                    missing_programs.insert(key.to_string(), program);
                }
            }
            programs.insert(key.to_string(), found);
        }
        let expander = self.expander(&overrides, &fallbacks, &missing);
        // `None` when the value needs a program that isn't there.
        let resolve =
            |group: &str, key: &str| match expander.resolve(name, key, &None, &mut Vec::new()) {
                Ok(resolved) => Ok(Some(resolved)),
                Err(f) if f.missing => Ok(None),
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
            values: BTreeMap::new(),
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
            }
        }
        for (key, raw) in defaults.runs.iter().filter(|(key, _)| !is_set(key)) {
            let origin = Some((name.to_string(), group_key(RUNS, key)));
            let mut stack = vec![(name.to_string(), key.to_string())];
            let run =
                match expander.words(name, &RunValue::Line(raw.to_string()), &origin, &mut stack) {
                    Ok(words) => Some(template(key, words)?),
                    Err(f) if f.missing => None,
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
    /// The value refers to an executable that isn't there: it has no value,
    /// rather than an error.
    missing: bool,
}

impl Failure {
    fn at(section: &str, group: &str, key: &str, message: String) -> Self {
        Failure {
            missing: false,
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
                missing: true,
            });
        }
        if let Some(value) = self.overrides.get(&id) {
            return Ok(Resolved::Text(vec![Part::Text(value.clone())]));
        }
        if let Some(start) = stack.iter().position(|s| *s == id) {
            return Err(self.loop_failure(&stack[start..]));
        }
        let Some(entry) = self.entry(section, key) else {
            return Err(Failure {
                missing: false,
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
                        missing: false,
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
                    missing: false,
                    origin: referrer.clone(),
                    message: format!("`${{{section}:{key}}}` is null, so it can't be used here"),
                }),
            },
            Entry::Run(raw) => Ok(Resolved::List(self.words(section, raw, &origin, stack)?)),
            Entry::PathList(entries) => {
                let mut paths = Vec::new();
                for (i, entry) in entries.iter().enumerate() {
                    let found = self.path_list_entry(section, key, i, entry, stack)?;
                    paths.extend(found.iter().map(|p| vec![Part::Text(path_text(p))]));
                }
                Ok(Resolved::List(paths))
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
                missing: false,
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
        let mut paths = Vec::new();
        for parts in self.expand_word(section, entry, &origin, stack)? {
            if parts.iter().any(|p| matches!(p, Part::External(_))) {
                return Err(Failure {
                    missing: false,
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
                    missing: false,
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
            missing: false,
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
                             in `runs` or a whole entry in `path-lists`"
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
    use crate::config::testing::TempDir;

    fn messages(errors: Vec<ConfigError>) -> Vec<String> {
        errors
            .into_iter()
            .map(|e| format!("{}: {}", e.key.unwrap_or_default(), e.message))
            .collect()
    }

    /// Load a single `--user-setup` file.
    fn load_one(text: &str) -> Result<SystemConfig, Vec<String>> {
        let dir = TempDir::new();
        let file = dir.write("setup.yaml", text);
        load(Some(&file), None, None, Env::default()).map_err(messages)
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
        let cli = dir.write("cli.yaml", "a:\n  paths:\n    k: cli\n");
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
        let config = load(Some(&cli), Some(&dir.path().join("dist")), None, env).unwrap();
        assert_eq!(config.files(), [cli, user.clone(), dist]);
        assert_eq!(config.get("a", "k").as_deref(), Some("cli"));
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
        let config = load(None, None, None, env.clone()).unwrap();
        assert_eq!(config.files(), [in_d]);
        assert_eq!(
            config.get_path(SCOTER_SECTION, USER_DIR),
            Some(home.join(".config/surf-scoter.d"))
        );

        let in_home = dir.write("scoter-home/setup.yml", "");
        let config = load(None, None, None, env).unwrap();
        assert_eq!(config.files(), [in_home]);
        assert_eq!(config.get_path(SCOTER_SECTION, USER_DIR), Some(scoter_home));
    }

    #[test]
    fn discovery_nothing_found() {
        let dir = TempDir::new();
        let env = Env {
            home: Some(dir.path().to_path_buf()),
            ..Env::default()
        };
        let config = load(None, None, None, env).unwrap();
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
        let errors = load(
            Some(&dir.path().join("missing.yaml")),
            Some(&dir.path().join("dist")),
            None,
            env,
        )
        .unwrap_err();
        let errors: Vec<String> = errors.iter().map(ToString::to_string).collect();
        assert_eq!(errors.len(), 3, "{errors:?}");
        assert!(
            errors[0].ends_with("missing.yaml: the --user-setup file doesn't exist"),
            "{errors:?}"
        );
        assert!(
            errors[1]
                .ends_with("surf-scoter.d: both setup.yaml and setup.yml exist; keep only one"),
            "{errors:?}"
        );
        assert!(
            errors[2].ends_with("dist: both setup.yaml and setup.yml exist; keep only one"),
            "{errors:?}"
        );
    }

    #[test]
    fn distribution_dir_given() {
        let dir = TempDir::new();
        let dist = dir.write("dist/setup.yaml", "a:\n  paths:\n    k: v\n");
        let config = load(None, Some(&dir.path().join("dist")), None, Env::default()).unwrap();
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
            "cli.yaml",
            "scoter:\n  paths:\n    distribution-dir: ~/dist\n",
        );
        let dist = dir.write("home/dist/setup.yaml", "a:\n  paths:\n    k: v\n");
        let other = dir.write("other/setup.yaml", "a:\n  paths:\n    k: other\n");
        let env = Env {
            home: Some(dir.path().join("home")),
            ..Env::default()
        };
        let config = load(Some(&cli), None, None, env.clone()).unwrap();
        assert_eq!(config.files(), [cli.clone(), dist]);
        assert_eq!(config.get("a", "k").as_deref(), Some("v"));
        assert_eq!(
            config.get_path(SCOTER_SECTION, DISTRIBUTION_DIR),
            Some(dir.path().join("home/dist"))
        );

        // The one given wins over the file's.
        let config = load(
            Some(&cli),
            Some(&dir.path().join("other")),
            None,
            env.clone(),
        )
        .unwrap();
        assert_eq!(config.files(), [cli.clone(), other]);
        assert_eq!(config.get("a", "k").as_deref(), Some("other"));

        // Without the search, only a given one's file is read.
        let options = SetupOptions {
            user_setup: Some(&cli),
            no_search: true,
            ..SetupOptions::default()
        };
        let config = load_with(&options, env).unwrap();
        assert_eq!(config.files(), [cli]);
        assert_eq!(
            config.get_path(SCOTER_SECTION, DISTRIBUTION_DIR),
            Some(dir.path().join("home/dist"))
        );
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
            "a:\n  paths:\n    p: ${scoter:project-dir}/p\n    c: ${scoter:project-config}\n    w: ${scoter:cwd}/w\n",
        );
        let options = SetupOptions {
            user_setup: Some(&setup),
            project_dir: Some(Path::new("/proj")),
            project_config: Some(Path::new("/proj/.scoter/scoter.yaml")),
            ..SetupOptions::default()
        };
        let env = Env {
            cwd: Some(PathBuf::from("/work")),
            ..Env::default()
        };
        let config = load_with(&options, env).unwrap();
        assert_eq!(config.get("a", "p").as_deref(), Some("/proj/p"));
        assert_eq!(
            config.get("a", "c").as_deref(),
            Some("/proj/.scoter/scoter.yaml")
        );
        assert_eq!(config.get("a", "w").as_deref(), Some("/work/w"));

        // Unset, they can't be referred to.
        let errors = load(Some(&setup), None, None, Env::default()).unwrap_err();
        assert_eq!(errors.len(), 3, "{errors:?}");
    }

    #[test]
    fn dev_container_path_is_set_later() {
        let dir = TempDir::new();
        let setup = dir.write(
            "setup.yaml",
            "a:\n  paths:\n    x: ${scoter:dev-container-path}/x\n",
        );
        let errors = messages(load(Some(&setup), None, None, Env::default()).unwrap_err());
        assert_eq!(
            errors,
            ["a.paths.x: unknown reference `${scoter:dev-container-path}`"]
        );

        let options = SetupOptions {
            user_setup: Some(&setup),
            dev_container: true,
            ..SetupOptions::default()
        };
        let mut config = load_with(&options, Env::default()).unwrap();
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
                "a.other: unknown key; a section has `paths`, `programs`, `runs`, `path-lists` and `values`",
                "a.path-lists.c: must be a list of paths",
                "a.path-lists.d[1]: must be a path",
                "a.path-lists.d[2]: must be a path",
                "a.values.list: must be a single value: text, a number, true/false or null",
                "a.values.map: must be a single value: text, a number, true/false or null",
                "a.values.nan: must be a finite number",
                "a.values.inf: must be a finite number",
                "b: must be a section, with `paths`, `programs`, `runs`, `path-lists` and `values` mappings",
                "old-paths: must be a section; put lists of paths in a section's `path-lists`",
            ]
        );
    }

    #[test]
    fn shape_old_flat_sections_are_refused() {
        assert_eq!(
            one_error("ssh:\n  home: ~/.ssh\n"),
            "ssh.home: unknown key; a section has `paths`, `programs`, `runs`, `path-lists` and `values`"
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
    fn a_bad_file_doesnt_hide_the_others() {
        let dir = TempDir::new();
        let cli = dir.write("cli.yaml", "a: [\n");
        dir.write("dist/setup.yaml", "b: text\n");
        let errors = load(
            Some(&cli),
            Some(&dir.path().join("dist")),
            None,
            Env::default(),
        )
        .unwrap_err();
        assert_eq!(errors.len(), 2, "{errors:?}");
    }

    // ---- merging ----

    #[test]
    fn lists_are_replaced_sections_merged() {
        let dir = TempDir::new();
        let cli = dir.write(
            "cli.yaml",
            "a:\n  paths:\n    x: high\n  runs:\n    r: high\n  path-lists:\n    l: [/high]\n  values:\n    v: high\n",
        );
        dir.write(
            "dist/setup.yaml",
            "a:\n  paths:\n    x: low\n    y: low\n  runs:\n    r: low\n    s: low\n  path-lists:\n    l: [/low1, /low2]\n    m: [/m]\n  values:\n    v: low\n    w: 2\n",
        );
        let config = load(
            Some(&cli),
            Some(&dir.path().join("dist")),
            None,
            Env::default(),
        )
        .unwrap();
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
        let config = load(Some(&file), None, None, env).unwrap();
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
        let config = load(Some(&file), None, Some(Path::new("/proj")), env.clone()).unwrap();
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
        let config = load(Some(&file), None, None, env).unwrap();
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
    fn a_key_has_one_kind_of_value() {
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
    fn a_key_keeps_its_kind_across_files() {
        let dir = TempDir::new();
        let cli = dir.write(
            "cli.yaml",
            "a:\n  values:\n    x: 5\n  paths:\n    y: /high\n",
        );
        let user = dir.write(
            "home/.config/surf-scoter.setup.yaml",
            "a:\n  runs:\n    x: go\n",
        );
        let dist = dir.write(
            "dist/setup.yaml",
            "a:\n  paths:\n    x: /low\n    y: /low\n",
        );
        let env = Env {
            home: Some(dir.path().join("home")),
            ..Env::default()
        };
        let errors = load(Some(&cli), Some(&dir.path().join("dist")), None, env).unwrap_err();
        // Each change is reported where it's made; changing only the value is fine.
        assert_eq!(
            errors,
            [
                ConfigError::new(
                    Some(&user),
                    "a.runs.x",
                    format!(
                        "is set in `paths` by {}; a key's kind of value can't change",
                        dist.display()
                    ),
                ),
                ConfigError::new(
                    Some(&cli),
                    "a.values.x",
                    format!(
                        "is set in `paths` by {}; a key's kind of value can't change",
                        dist.display()
                    ),
                ),
            ]
        );
    }

    #[test]
    fn references_to_any_kind_of_value() {
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
                "a.values.in-text: `${a:cmd}` is a list; use it as a whole argument in `runs` or a whole entry in `path-lists`",
                "a.runs.loop: values refer to each other in a loop: ${a:loop} -> ${a:loop-list} -> ${a:loop}",
                "a.paths.p: `${a:dirs}` is a list; use it as a whole argument in `runs` or a whole entry in `path-lists`",
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
    fn errors_name_the_file_that_set_the_value() {
        let dir = TempDir::new();
        let cli = dir.write("cli.yaml", "a:\n  paths:\n    x: ${a:bad}\n");
        dir.write(
            "dist/setup.yaml",
            "b:\n  paths:\n    y: ${a:x}\n  runs:\n    z: go ${a:x}\n",
        );
        let errors = load(
            Some(&cli),
            Some(&dir.path().join("dist")),
            None,
            Env::default(),
        )
        .unwrap_err();
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
        let config = load(Some(&file), None, None, env).unwrap();
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
        let config = load(Some(&file), None, None, env).unwrap();
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
        let config = load(Some(&file), None, None, env).unwrap();
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

    #[test]
    fn programs_and_paths_are_different_kinds() {
        let dir = TempDir::new();
        let cli = dir.write("cli.yaml", "t:\n  paths:\n    exec: /usr/bin/tool\n");
        dir.write("dist/setup.yaml", "t:\n  programs:\n    exec: tool\n");
        let errors = load(
            Some(&cli),
            Some(&dir.path().join("dist")),
            None,
            Env::default(),
        )
        .unwrap_err();
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
        let cli = dir.write("cli.yaml", "t:\n  runs:\n    go: [high, 'a b']\n");
        dir.write(
            "dist/setup.yaml",
            "t:\n  runs:\n    go: low line\n    other: [low]\n",
        );
        let config = load(
            Some(&cli),
            Some(&dir.path().join("dist")),
            None,
            Env::default(),
        )
        .unwrap();
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
    fn executables_must_be_executable_files() {
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
        let config = load(Some(&file), None, None, Env::default()).unwrap();
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
    fn section_defaults_and_not_found() {
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
        let config = load(Some(&file), None, None, env).unwrap();
        let defaults = SectionDefaults {
            runs: &[("go", "${exec} 'x")],
            ..SectionDefaults::default()
        };
        let error = config.section("t", &defaults).unwrap_err();
        assert_eq!(error.to_string(), "t.runs.go: unmatched ' quote");
    }

    #[test]
    fn commands_are_checked_when_loading() {
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
    fn record_is_expanded_again_without_reading_the_files() {
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
        let first = load(Some(&file), None, None, Env::default()).unwrap();
        // Without a project, `./` stays as written, and `./` list entries are left out.
        assert_eq!(first.get("a", "local").as_deref(), Some("./local"));
        assert_eq!(first.path_list("a", "dirs"), [PathBuf::from("/h/e")]);
        let record = first.setup_record().clone();
        assert_eq!(record.files.len(), 1);
        assert_eq!(record.files[0].source, Source::UserSetup);

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
    fn record_is_a_base_layer_for_the_search() {
        let dir = TempDir::new();
        let cli = dir.write("cli.yaml", "a:\n  paths:\n    x: /cli\n    y: /cli\n");
        let dist = dir.write(
            "dist/setup.yaml",
            "a:\n  paths:\n    x: /dist\n    z: /dist\n",
        );
        let record = load(
            Some(&cli),
            Some(&dir.path().join("dist")),
            None,
            Env::default(),
        )
        .unwrap()
        .setup_record()
        .clone();
        assert_eq!(
            record.distribution_dir,
            [dir.path().join("dist").display().to_string()]
        );

        // Changed on disk since: the record's copy is used, not read again.
        dir.write("dist/setup.yaml", "a:\n  paths:\n    x: /changed\n");
        // Found by the search now: a user file, between the two in priority.
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
        assert_eq!(config.files(), [cli.clone(), user.clone(), dist.clone()]);
        assert_eq!(config.get("a", "x").as_deref(), Some("/cli"));
        assert_eq!(config.get("a", "y").as_deref(), Some("/cli"));
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
    };

    /// A base with no inputs of its own.
    fn base<'a>(record: &'a SetupRecord, origin: &'a Path) -> SetupBase<'a> {
        SetupBase {
            record,
            origin,
            inputs: NO_INPUTS,
        }
    }

    #[test]
    fn record_keeps_a_history_of_inputs() {
        let dir = TempDir::new();
        dir.write("dist1/setup.yaml", "");
        dir.write("dist2/setup.yaml", "");
        let dist1 = dir.path().join("dist1");
        let dist2 = dir.path().join("dist2");
        let text = |p: &Path| p.display().to_string();
        let origin = dir.path().join("u.json");
        let run = |record: Option<&SetupRecord>, dist: Option<&Path>, env: Env| {
            let options = SetupOptions {
                distribution_dir: dist,
                base: record.map(|r| base(r, &origin)),
                ..SetupOptions::default()
            };
            load_with(&options, env).unwrap().setup_record().clone()
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
    fn record_duplicates_are_allowed_and_removed() {
        let record = SetupRecord {
            distribution_dir: vec!["/a".to_string(), "/b".to_string(), "/a".to_string()],
            project_config: vec!["/p".to_string(), "/p".to_string()],
            ..SetupRecord::default()
        };
        let origin = Path::new("/u.json");
        let options = SetupOptions {
            no_search: true,
            base: Some(base(&record, origin)),
            ..SetupOptions::default()
        };
        let config = load_with(&options, Env::default()).unwrap();
        assert_eq!(config.setup_record().distribution_dir, ["/a", "/b"]);
        assert_eq!(config.setup_record().project_config, ["/p"]);
    }

    #[test]
    fn base_inputs_are_used() {
        let record = SetupRecord::default();
        let origin = Path::new("/u.json");
        let inputs = SetupInputs {
            user_setup_file: Some("/home/me/.config/surf-scoter.d/setup.yaml".to_string()),
            user_dir: Some("/home/me/.config/surf-scoter.d".to_string()),
            distribution_dir: Some("/opt/scoter".to_string()),
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
        assert_eq!(config.setup_inputs(), &inputs);
        // Only a record: the history isn't changed by them.
        assert!(config.setup_record().user_setup_file.is_empty());
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
}
