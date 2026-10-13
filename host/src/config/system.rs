//! The system setup files: settings for this computer.  See
//! `docs/system-config-file.md`.
//!
//! These files are read, highest priority first:
//!   1. the `--user-setup` file;
//!   2. the user's setup file, the first found of
//!      `$SURF_SCOTER_HOME/setup.{yaml,yml}`,
//!      `~/.config/surf-scoter.setup.{yaml,yml}` and
//!      `~/.config/surf-scoter.d/setup.{yaml,yml}`;
//!   3. `<distribution dir>/setup.{yaml,yml}`;
//!   4. `${scoter:install-dir}/setup.{yaml,yml}`, using the settings so far.
//!
//! Each top-level key is either a lookup path list (`NAME-paths`), which the
//! highest-priority file setting it replaces, or a section:
//!
//! ```yaml
//! docker:
//!   paths:
//!     exec: /usr/sbin/podman
//!   run:
//!     build: ${exec} build -t {x:tag} -f {x:dockerfile} {x:dirname}
//! ```
//!
//! `paths` holds file locations: a leading `~` is the home directory, and a
//! leading `./` is relative to the project file.  `run` holds command
//! templates (see `command`): each either one string, split into arguments
//! with the quoting rules, or a list with one entry per argument.  Sections
//! are merged key by key.
//!
//! Values refer to `paths` values as `${section:key}`, or `${key}` within the
//! same section; `$${` stands for `${`.  An unknown reference is an error.
//! `{x:name}` is left for the command that uses the value; `paths` values
//! can't have any.
//!
//! `paths` keys named `exec` or ending with `-cmd` are executables: a bare
//! name is found on the `PATH`, and references to them give the program
//! found.  A section with an executable that isn't there can't be used.
//!
//! The `scoter` section's `paths` also hold `install-dir` (by default, the
//! distribution directory), and `user-setup-file` and `user-dir`, which come
//! from where the user's setup file was found.

use std::collections::{BTreeMap, HashMap};
use std::ffi::OsString;
use std::path::{Path, PathBuf};

use yaml_serde::Value;

use super::command::{self, CommandTemplate, Part};
use super::read::{parse_yaml, read_limited, tag_errors};
use super::{ConfigError, child_key, index_key};

pub const SCOTER_SECTION: &str = "scoter";
pub const INSTALL_DIR: &str = "install-dir";
pub const USER_SETUP_FILE: &str = "user-setup-file";
pub const USER_DIR: &str = "user-dir";
/// The `paths` key naming a section's main program.
pub const EXEC: &str = "exec";
/// The suffix of other `paths` keys naming programs.
const CMD_SUFFIX: &str = "-cmd";
/// A section's file locations.
pub const PATHS: &str = "paths";
/// A section's command templates.
pub const RUN: &str = "run";
const PATHS_SUFFIX: &str = "-paths";
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
#[derive(Debug, Clone, PartialEq, Eq)]
struct Setting<T = String> {
    value: T,
    file: Option<PathBuf>,
}

/// A `run` command, as written.
#[derive(Debug, Clone, PartialEq, Eq)]
enum RunValue {
    /// One string, split into arguments with the quoting rules.
    Line(String),
    /// One entry per argument, used as is.
    Args(Vec<String>),
}

/// A section's merged values.
#[derive(Debug, Default)]
struct SectionSettings {
    paths: BTreeMap<String, Setting>,
    run: BTreeMap<String, Setting<RunValue>>,
}

type Sections = BTreeMap<String, SectionSettings>;

/// The merged system configuration.
#[derive(Debug)]
pub struct SystemConfig {
    /// The files read, highest priority first.
    files: Vec<PathBuf>,
    sections: Sections,
    /// Expanded lookup paths, by name without `-paths`.
    lookup: BTreeMap<String, Vec<PathBuf>>,
    env: Env,
    /// Where `./` paths start.
    project_dir: Option<PathBuf>,
}

/// A section, expanded.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Section {
    pub name: String,
    /// The file locations; executables (`exec`, `*-cmd`) are the programs found.
    pub paths: BTreeMap<String, PathBuf>,
    /// The commands; `${exec}` is the program found.
    pub run: BTreeMap<String, CommandTemplate>,
}

impl Section {
    /// The section's program, if it has one.
    pub fn exec(&self) -> Option<&Path> {
        self.paths.get(EXEC).map(PathBuf::as_path)
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum SectionLookup {
    Found(Section),
    /// The program executable `key` names (after expansion) isn't there.
    MissingExecutable {
        key: String,
        program: String,
    },
}

/// Whether a `paths` key names a program: `exec`, or `NAME-cmd`.
pub fn is_executable_key(key: &str) -> bool {
    key == EXEC || (key.len() > CMD_SUFFIX.len() && key.ends_with(CMD_SUFFIX))
}

/// Values for keys no setup file sets, as (key, value) pairs.
#[derive(Debug, Clone, Copy, Default)]
pub struct SectionDefaults<'a> {
    pub paths: &'a [(&'a str, &'a str)],
    pub run: &'a [(&'a str, &'a str)],
}

/// A file location: a leading `./` (or `.`) is relative to `project_dir`, when
/// there is one, and a leading `~` is the home directory.
fn resolve_path(text: &str, env: &Env, project_dir: Option<&Path>) -> PathBuf {
    match project_dir {
        Some(dir) if text == "." || text.starts_with("./") => {
            dir.join(text.trim_start_matches('.').trim_start_matches('/'))
        }
        _ => env.expand_home(text),
    }
}

/// Read the system configuration.  `project_dir` is where `./` paths start;
/// without it, `./` lookup paths are left out, and `./` `paths` values are
/// kept as written.
pub fn load(
    user_setup: Option<&Path>,
    distribution_dir: Option<&Path>,
    project_dir: Option<&Path>,
    env: Env,
) -> Result<SystemConfig, Vec<ConfigError>> {
    let mut errors = Vec::new();
    let mut tool_values: Vec<(&str, String)> = Vec::new();
    let mut found: Vec<PathBuf> = Vec::new();

    if let Some(path) = user_setup {
        if path.is_file() {
            found.push(path.to_path_buf());
        } else {
            errors.push(ConfigError::new(
                Some(path),
                "",
                "the --user-setup file doesn't exist",
            ));
        }
    }
    match find_user_setup(&env) {
        Ok(Some((file, user_dir))) => {
            tool_values.push((USER_SETUP_FILE, path_text(&file)));
            if let Some(dir) = user_dir {
                tool_values.push((USER_DIR, path_text(&dir)));
            }
            found.push(file);
        }
        Ok(None) => {}
        Err(e) => errors.push(e),
    }
    if let Some(dir) = distribution_dir {
        match find_setup(dir, SETUP) {
            Ok(file) => found.extend(file),
            Err(e) => errors.push(e),
        }
    }
    let mut files = Vec::new();
    let mut tried: Vec<PathBuf> = Vec::new();
    for path in found {
        if !tried.iter().any(|t| same_file(t, &path)) {
            match load_file(&path) {
                Ok(file) => files.push(file),
                Err(e) => errors.extend(e),
            }
            tried.push(path);
        }
    }

    let install_default = distribution_dir.map(path_text);
    let mut config = SystemConfig::merge(
        &files,
        &tool_values,
        install_default.as_deref(),
        env.clone(),
        project_dir,
    );
    // Errors in `install-dir` itself are reported with the rest, below.
    // The distribution directory was already searched.
    let install_dir = config
        .get_path(SCOTER_SECTION, INSTALL_DIR)
        .filter(|dir| !distribution_dir.is_some_and(|dist| same_file(dist, dir)));
    if let Some(install_dir) = install_dir {
        match find_setup(&install_dir, SETUP) {
            Ok(Some(path)) if !tried.iter().any(|t| same_file(t, &path)) => {
                match load_file(&path) {
                    Ok(file) => {
                        files.push(file);
                        config = SystemConfig::merge(
                            &files,
                            &tool_values,
                            install_default.as_deref(),
                            env,
                            project_dir,
                        );
                    }
                    Err(e) => errors.extend(e),
                }
            }
            Ok(_) => {}
            Err(e) => errors.push(e),
        }
    }

    errors.extend(config.validate(&files));
    if errors.is_empty() {
        Ok(config)
    } else {
        Err(errors)
    }
}

/// The user's setup file, and the user directory it's in (when it's in one).
fn find_user_setup(env: &Env) -> Result<Option<(PathBuf, Option<PathBuf>)>, ConfigError> {
    if let Some(dir) = &env.scoter_home
        && let Some(file) = find_setup(dir, SETUP)?
    {
        return Ok(Some((file, Some(dir.clone()))));
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

fn same_file(a: &Path, b: &Path) -> bool {
    match (a.canonicalize(), b.canonicalize()) {
        (Ok(a), Ok(b)) => a == b,
        _ => a == b,
    }
}

fn path_text(path: &Path) -> String {
    path.to_string_lossy().into_owned()
}

/// `paths.key` or `run.key`: where a value is within its section.
fn group_key(group: &str, key: &str) -> String {
    format!("{group}.{key}")
}

/// One section of one setup file.
#[derive(Debug, Default)]
struct RawSection {
    paths: BTreeMap<String, String>,
    run: BTreeMap<String, RunValue>,
}

/// One setup file's contents.
#[derive(Debug)]
struct SetupFile {
    path: PathBuf,
    sections: BTreeMap<String, RawSection>,
    lookup: BTreeMap<String, Vec<String>>,
}

fn load_file(path: &Path) -> Result<SetupFile, Vec<ConfigError>> {
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
        sections: BTreeMap::new(),
        lookup: BTreeMap::new(),
    };
    let map = match value {
        Value::Null => return Ok(file),
        Value::Mapping(map) => map,
        _ => {
            return Err(vec![error(
                "",
                "must be a mapping of sections and lookup paths".to_string(),
            )]);
        }
    };
    for (key, value) in map {
        let Some(name) = key.as_str() else {
            errors.push(error("", format!("top-level key {key:?} must be a string")));
            continue;
        };
        if name.ends_with(PATHS_SUFFIX) && name.len() > PATHS_SUFFIX.len() {
            let items = match value {
                Value::Sequence(items) => items,
                _ => {
                    errors.push(error(name, "must be a list of paths".to_string()));
                    continue;
                }
            };
            let mut list = Vec::new();
            for (i, item) in items.iter().enumerate() {
                match item {
                    Value::String(s) => list.push(s.clone()),
                    _ => errors.push(error(&index_key(name, i), "must be a path".to_string())),
                }
            }
            file.lookup.insert(name.to_string(), list);
            continue;
        }
        let groups = match value {
            Value::Null => Default::default(),
            Value::Mapping(groups) => groups,
            _ => {
                errors.push(error(
                    name,
                    "must be a section, with `paths` and `run` mappings".to_string(),
                ));
                continue;
            }
        };
        let section = file.sections.entry(name.to_string()).or_default();
        for (group, entries) in groups {
            let group = match group.as_str() {
                Some(PATHS) => PATHS,
                Some(RUN) => RUN,
                _ => {
                    let shown = group
                        .as_str()
                        .map_or_else(|| format!("{group:?}"), str::to_string);
                    errors.push(error(
                        &child_key(name, &shown),
                        "unknown key; a section has `paths` and `run`".to_string(),
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
                if name == SCOTER_SECTION
                    && group == PATHS
                    && (key == USER_SETUP_FILE || key == USER_DIR)
                {
                    errors.push(error(&at, "is set by the tool; remove it".to_string()));
                    continue;
                }
                if group == RUN
                    && let Value::Sequence(items) = &value
                {
                    let mut args = Vec::new();
                    for (i, item) in items.iter().enumerate() {
                        match scalar_text(item) {
                            Ok(arg) => args.push(arg),
                            Err(message) => {
                                errors.push(error(&index_key(&at, i), message.to_string()))
                            }
                        }
                    }
                    section.run.insert(key.to_string(), RunValue::Args(args));
                    continue;
                }
                match scalar_text(&value) {
                    Ok(text) if group == RUN => {
                        section.run.insert(key.to_string(), RunValue::Line(text));
                    }
                    Ok(text) => {
                        section.paths.insert(key.to_string(), text);
                    }
                    Err(message) => errors.push(error(&at, message.to_string())),
                }
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

type Values = HashMap<(String, String), String>;

impl SystemConfig {
    /// Merge the files (highest priority first) with the values the tool sets.
    fn merge(
        files: &[SetupFile],
        tool_values: &[(&str, String)],
        install_default: Option<&str>,
        env: Env,
        project_dir: Option<&Path>,
    ) -> Self {
        let mut sections = Sections::new();
        for file in files {
            for (name, raw) in &file.sections {
                let section = sections.entry(name.clone()).or_default();
                for (key, value) in &raw.paths {
                    section.paths.entry(key.clone()).or_insert_with(|| Setting {
                        value: value.clone(),
                        file: Some(file.path.clone()),
                    });
                }
                for (key, value) in &raw.run {
                    section.run.entry(key.clone()).or_insert_with(|| Setting {
                        value: value.clone(),
                        file: Some(file.path.clone()),
                    });
                }
            }
        }
        let scoter = &mut sections
            .entry(SCOTER_SECTION.to_string())
            .or_default()
            .paths;
        for (key, value) in tool_values {
            scoter.insert(
                key.to_string(),
                Setting {
                    value: value.clone(),
                    file: None,
                },
            );
        }
        if let Some(dir) = install_default {
            scoter
                .entry(INSTALL_DIR.to_string())
                .or_insert_with(|| Setting {
                    value: dir.to_string(),
                    file: None,
                });
        }
        SystemConfig {
            files: files.iter().map(|f| f.path.clone()).collect(),
            sections,
            lookup: BTreeMap::new(),
            env,
            project_dir: project_dir.map(Path::to_path_buf),
        }
    }

    fn expander<'a>(&'a self, overrides: &'a Values, fallbacks: &'a Values) -> Expander<'a> {
        Expander {
            sections: &self.sections,
            overrides,
            fallbacks,
            env: &self.env,
            project_dir: self.project_dir.as_deref(),
        }
    }

    /// Expand every value, recording every problem, and fill in the lookup paths.
    fn validate(&mut self, files: &[SetupFile]) -> Vec<ConfigError> {
        let mut errors: Vec<ConfigError> = Vec::new();
        let mut add = |error: ConfigError| {
            if !errors.contains(&error) {
                errors.push(error);
            }
        };

        let no_values = Values::new();
        for (name, section) in &self.sections {
            for (key, setting) in &section.paths {
                let result = self.expander(&no_values, &no_values).expand_key(
                    name,
                    key,
                    &None,
                    &mut Vec::new(),
                );
                if let Err(failure) = result {
                    let at = child_key(name, &group_key(PATHS, key));
                    add(self.failure_error(failure, setting.file.as_deref(), &at));
                }
            }
            // Executables were checked above; commands get stand-ins for them.
            let stand_in: Values = section
                .paths
                .keys()
                .filter(|key| is_executable_key(key))
                .map(|key| ((name.clone(), key.clone()), key.clone()))
                .collect();
            for (key, setting) in &section.run {
                let result =
                    self.expander(&stand_in, &no_values)
                        .template(name, key, &setting.value);
                if let Err(failure) = result {
                    let at = child_key(name, &group_key(RUN, key));
                    add(self.failure_error(failure, setting.file.as_deref(), &at));
                }
            }
        }

        // The highest-priority file setting a list wins.
        let mut lists: BTreeMap<&str, (&[String], &Path)> = BTreeMap::new();
        for file in files {
            for (name, list) in &file.lookup {
                lists.entry(name.as_str()).or_insert((list, &file.path));
            }
        }
        let expander = self.expander(&no_values, &no_values);
        let mut expanded = BTreeMap::new();
        for (name, (list, file)) in lists {
            let mut paths = Vec::new();
            for (i, entry) in list.iter().enumerate() {
                let key = index_key(name, i);
                let parts = match expander.expand_text("", entry, &None, &mut Vec::new()) {
                    Ok(parts) => parts,
                    Err(failure) => {
                        add(self.failure_error(failure, Some(file), &key));
                        continue;
                    }
                };
                if parts.iter().any(|p| matches!(p, Part::External(_))) {
                    add(ConfigError::new(
                        Some(file),
                        &key,
                        "paths can't use `{x:...}` values",
                    ));
                    continue;
                }
                let text = command::render_parts(&parts);
                // Without a project, `./` entries are left out.
                let project_dir = self.project_dir.as_deref();
                if project_dir.is_some() || !(text == "." || text.starts_with("./")) {
                    paths.push(resolve_path(&text, &self.env, project_dir));
                }
            }
            let name = name.strip_suffix(PATHS_SUFFIX).unwrap_or(name);
            expanded.insert(name.to_string(), paths);
        }
        self.lookup = expanded;
        errors
    }

    /// The file that set `key_path` (`paths.KEY` or `run.KEY`) in `section`.
    fn setting_file(&self, section: &str, key_path: &str) -> Option<&Path> {
        let section = self.sections.get(section)?;
        let file = match key_path.split_once('.') {
            Some((PATHS, key)) => &section.paths.get(key)?.file,
            Some((RUN, key)) => &section.run.get(key)?.file,
            _ => return None,
        };
        file.as_deref()
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
        self.sections
            .get(section)
            .into_iter()
            .flat_map(|s| s.paths.keys().map(String::as_str))
    }

    /// The `run` keys set in a section.
    pub fn run_keys(&self, section: &str) -> impl Iterator<Item = &str> {
        self.sections
            .get(section)
            .into_iter()
            .flat_map(|s| s.run.keys().map(String::as_str))
    }

    /// The sections with executables (`exec` or `*-cmd` paths).
    pub fn exec_sections(&self) -> impl Iterator<Item = &str> {
        self.sections
            .iter()
            .filter(|(_, s)| s.paths.keys().any(|k| is_executable_key(k)))
            .map(|(name, _)| name.as_str())
    }

    /// Find a program the way executables are found: a leading `~` is expanded,
    /// and a bare name is searched for on the `PATH`.
    pub fn find_program(&self, program: &str) -> Option<PathBuf> {
        command::find_executable(
            &self.env.expand_home(program),
            self.env.path.as_deref(),
            self.env.path_ext.as_deref(),
        )
    }

    /// A `paths` value, expanded as a file location.  (Executables are as
    /// configured, not looked up on the `PATH`.)
    pub fn get(&self, section: &str, key: &str) -> Option<String> {
        let no_values = Values::new();
        // `load` already checked every value expands.
        self.expander(&no_values, &no_values)
            .expand_key(section, key, &None, &mut Vec::new())
            .ok()
            .map(|parts| command::render_parts(&parts))
    }

    /// `get`, as a path.
    pub fn get_path(&self, section: &str, key: &str) -> Option<PathBuf> {
        self.get(section, key).map(PathBuf::from)
    }

    /// A lookup path list (`NAME-paths`, given as `NAME`); empty if unset.
    pub fn lookup_path(&self, name: &str) -> &[PathBuf] {
        self.lookup.get(name).map_or(&[], Vec::as_slice)
    }

    /// Every lookup path list, by name without `-paths`.
    pub fn lookup_paths(&self) -> &BTreeMap<String, Vec<PathBuf>> {
        &self.lookup
    }

    /// The section `name`, expanded, with its executables (`exec`, `*-cmd`)
    /// looked up on the `PATH`.  `defaults` gives values for keys no file sets.
    pub fn section(
        &self,
        name: &str,
        defaults: &SectionDefaults,
    ) -> Result<SectionLookup, ConfigError> {
        let fallbacks: Values = defaults
            .paths
            .iter()
            .map(|(key, value)| ((name.to_string(), key.to_string()), value.to_string()))
            .collect();
        let settings = self.sections.get(name);
        let failure = |failure: Failure, group: &str, key: &str| {
            let key_path = group_key(group, key);
            let file = self.setting_file(name, &key_path);
            self.failure_error(failure, file, &child_key(name, &key_path))
        };

        let mut keys: Vec<&str> = self.path_keys(name).collect();
        keys.extend(defaults.paths.iter().map(|(key, _)| *key));
        keys.sort_unstable();
        keys.dedup();

        let mut overrides = Values::new();
        let no_values = Values::new();
        for key in keys.iter().copied().filter(|key| is_executable_key(key)) {
            let program = self
                .expander(&no_values, &fallbacks)
                .expand_key(name, key, &None, &mut Vec::new())
                .map_err(|f| failure(f, PATHS, key))?;
            let program = command::render_parts(&program);
            let Some(found) = self.find_program(&program) else {
                return Ok(SectionLookup::MissingExecutable {
                    key: key.to_string(),
                    program,
                });
            };
            overrides.insert((name.to_string(), key.to_string()), path_text(&found));
        }
        let expander = self.expander(&overrides, &fallbacks);

        let mut paths = BTreeMap::new();
        for key in keys {
            let path = expander
                .expand_key(name, key, &None, &mut Vec::new())
                .map_err(|f| failure(f, PATHS, key))?;
            paths.insert(key.to_string(), PathBuf::from(command::render_parts(&path)));
        }

        let mut keys: Vec<&str> = self.run_keys(name).collect();
        keys.extend(defaults.run.iter().map(|(key, _)| *key));
        keys.sort_unstable();
        keys.dedup();
        let mut run = BTreeMap::new();
        for key in keys {
            let raw = settings
                .and_then(|s| s.run.get(key))
                .map(|s| s.value.clone())
                .or_else(|| {
                    defaults
                        .run
                        .iter()
                        .find(|(k, _)| *k == key)
                        .map(|(_, v)| RunValue::Line(v.to_string()))
                })
                .unwrap_or(RunValue::Args(Vec::new()));
            let template = expander
                .template(name, key, &raw)
                .map_err(|f| failure(f, RUN, key))?;
            run.insert(key.to_string(), template);
        }
        Ok(SectionLookup::Found(Section {
            name: name.to_string(),
            paths,
            run,
        }))
    }
}

/// Where a problem is: (section, `paths.KEY` or `run.KEY`).
type Origin = Option<(String, String)>;

/// Why a value couldn't be expanded.
#[derive(Debug)]
struct Failure {
    /// The value whose text has the problem; `None` for the text being expanded.
    origin: Origin,
    message: String,
}

/// Expands `${...}` references to `paths` values.
struct Expander<'a> {
    sections: &'a Sections,
    /// `paths` values used in place of the configured ones.
    overrides: &'a Values,
    /// `paths` values used for keys the configuration doesn't set.
    fallbacks: &'a Values,
    /// For resolving file locations.
    env: &'a Env,
    project_dir: Option<&'a Path>,
}

impl Expander<'_> {
    /// The `run` command `key`: a line split into words, or a list of
    /// words, then each word expanded.
    fn template(
        &self,
        section: &str,
        key: &str,
        raw: &RunValue,
    ) -> Result<CommandTemplate, Failure> {
        let origin = Some((section.to_string(), group_key(RUN, key)));
        let here = |message: String| Failure {
            origin: origin.clone(),
            message,
        };
        let split = match raw {
            RunValue::Line(line) => command::split_words(line).map_err(here)?,
            RunValue::Args(args) => args.clone(),
        };
        let mut words = Vec::new();
        for word in split {
            words.push(self.expand_text(section, &word, &origin, &mut Vec::new())?);
        }
        CommandTemplate::new(words).map_err(here)
    }

    /// The `paths` value `section:key`, as a file location.  `referrer` is
    /// where the reference to it is; `stack` holds the values being expanded,
    /// to find loops.
    fn expand_key(
        &self,
        section: &str,
        key: &str,
        referrer: &Origin,
        stack: &mut Vec<(String, String)>,
    ) -> Result<Vec<Part>, Failure> {
        let id = (section.to_string(), key.to_string());
        if let Some(value) = self.overrides.get(&id) {
            return Ok(vec![Part::Text(value.clone())]);
        }
        if let Some(start) = stack.iter().position(|s| *s == id) {
            return Err(loop_failure(&stack[start..]));
        }
        let raw = self
            .sections
            .get(section)
            .and_then(|s| s.paths.get(key))
            .map(|s| s.value.as_str());
        let Some(raw) = raw.or_else(|| self.fallbacks.get(&id).map(String::as_str)) else {
            return Err(Failure {
                origin: referrer.clone(),
                message: format!("unknown reference `${{{section}:{key}}}`"),
            });
        };
        let origin = Some((section.to_string(), group_key(PATHS, key)));
        stack.push(id);
        let parts = self.expand_text(section, raw, &origin, stack);
        stack.pop();
        let parts = parts?;
        if parts.iter().any(|p| matches!(p, Part::External(_))) {
            return Err(Failure {
                origin,
                message: "paths are file locations; they can't use `{x:...}` values".to_string(),
            });
        }
        let path = resolve_path(&command::render_parts(&parts), self.env, self.project_dir);
        Ok(vec![Part::Text(path_text(&path))])
    }

    /// Expand `text`, found in `section` (empty outside any section) at `origin`.
    fn expand_text(
        &self,
        section: &str,
        text: &str,
        origin: &Origin,
        stack: &mut Vec<(String, String)>,
    ) -> Result<Vec<Part>, Failure> {
        let here = |message: String| Failure {
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
                let (ref_section, ref_key) =
                    reference.split_once(':').unwrap_or((section, reference));
                if ref_key.is_empty() || (reference.contains(':') && ref_section.is_empty()) {
                    return Err(here(format!("`${{{reference}}}` is not a valid reference")));
                }
                if ref_section.is_empty() {
                    return Err(here(format!(
                        "`${{{reference}}}` needs a section here, as `${{section:{reference}}}`"
                    )));
                }
                let value = self.expand_key(ref_section, ref_key, origin, stack)?;
                command::extend_parts(&mut parts, value);
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
}

/// The error for values referring to each other in a loop.  It's always
/// reported for the same value, whichever one the expansion started from.
fn loop_failure(cycle: &[(String, String)]) -> Failure {
    let start = (0..cycle.len()).min_by_key(|&i| &cycle[i]).unwrap_or(0);
    let mut names: Vec<String> = cycle[start..]
        .iter()
        .chain(&cycle[..start])
        .map(|(s, k)| format!("${{{s}:{k}}}"))
        .collect();
    names.push(names[0].clone());
    let (section, key) = &cycle[start];
    Failure {
        origin: Some((section.clone(), group_key(PATHS, key))),
        message: format!(
            "values refer to each other in a loop: {}",
            names.join(" -> ")
        ),
    }
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

    fn found(lookup: SectionLookup) -> Section {
        match lookup {
            SectionLookup::Found(section) => section,
            other => panic!("not found: {other:?}"),
        }
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
                "a:\n  paths:\n    k: dist\n    u: dist\n    d: dist\nscoter:\n  paths:\n    install-dir: {}/inst\n",
                dir.path().display()
            ),
        );
        let inst = dir.write(
            "inst/setup.yaml",
            "a:\n  paths:\n    k: inst\n    i: inst\n",
        );
        let env = Env {
            home: Some(dir.path().join("home")),
            ..Env::default()
        };
        let config = load(Some(&cli), Some(&dir.path().join("dist")), None, env).unwrap();
        assert_eq!(config.files(), [cli, user.clone(), dist, inst]);
        assert_eq!(config.get("a", "k").as_deref(), Some("cli"));
        assert_eq!(config.get("a", "u").as_deref(), Some("user"));
        assert_eq!(config.get("a", "d").as_deref(), Some("dist"));
        assert_eq!(config.get("a", "i").as_deref(), Some("inst"));
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
        assert_eq!(config.get(SCOTER_SECTION, INSTALL_DIR), None);
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
    fn install_dir_defaults_to_distribution() {
        let dir = TempDir::new();
        let dist = dir.write("dist/setup.yaml", "a:\n  paths:\n    k: v\n");
        let config = load(None, Some(&dir.path().join("dist")), None, Env::default()).unwrap();
        // Read once, though it's also the install directory's file.
        assert_eq!(config.files(), [dist]);
        assert_eq!(
            config.get_path(SCOTER_SECTION, INSTALL_DIR),
            Some(dir.path().join("dist"))
        );
    }

    #[test]
    fn install_dir_from_a_file() {
        let dir = TempDir::new();
        let cli = dir.write("cli.yaml", "scoter:\n  paths:\n    install-dir: ~/inst\n");
        let inst = dir.write("home/inst/setup.yaml", "a:\n  paths:\n    k: v\n");
        let env = Env {
            home: Some(dir.path().join("home")),
            ..Env::default()
        };
        let config = load(Some(&cli), None, None, env).unwrap();
        assert_eq!(config.files(), [cli, inst]);
        assert_eq!(config.get("a", "k").as_deref(), Some("v"));
    }

    #[test]
    fn tool_keys_cant_be_set() {
        assert_eq!(
            one_error("scoter:\n  paths:\n    user-dir: /x\n"),
            "scoter.paths.user-dir: is set by the tool; remove it"
        );
        assert_eq!(
            one_error("scoter:\n  paths:\n    user-setup-file: /x\n"),
            "scoter.paths.user-setup-file: is set by the tool; remove it"
        );
    }

    // ---- file shape ----

    #[test]
    fn shape_values() {
        let config = load_one(concat!(
            "a:\n  paths:\n    s: text\n    n: 5\n    f: 1.5\n    b: true\n",
            "  run:\n    go: go now\n",
            "empty:\n",
            "empty-groups:\n  paths:\n  run:\n",
            "list-paths: [/a, /b]\n",
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
            ["a", "empty", "empty-groups", "scoter"]
        );
        assert_eq!(
            config.lookup_path("list"),
            [PathBuf::from("/a"), PathBuf::from("/b")]
        );
        assert_eq!(config.lookup_path("other"), [] as [PathBuf; 0]);
    }

    #[test]
    fn shape_errors_are_gathered() {
        let errors = load_one(concat!(
            "a:\n  paths:\n    list: [1]\n    map: {x: y}\n    none:\n    ok: fine\n",
            "  run: text\n",
            "  other: {k: v}\n",
            "b: text\n",
            "c-paths: /not/a/list\n",
            "d-paths: [/ok, [nested], 5]\n",
            "-paths:\n  paths:\n    k: v\n",
        ))
        .unwrap_err();
        assert_eq!(
            errors,
            [
                "a.paths.list: must be a single value, not a list or mapping",
                "a.paths.map: must be a single value, not a list or mapping",
                "a.paths.none: has no value",
                "a.run: must be a mapping of names to values",
                "a.other: unknown key; a section has `paths` and `run`",
                "b: must be a section, with `paths` and `run` mappings",
                "c-paths: must be a list of paths",
                "d-paths[1]: must be a path",
                "d-paths[2]: must be a path",
            ]
        );
    }

    #[test]
    fn shape_old_flat_sections_are_refused() {
        assert_eq!(
            one_error("ssh:\n  home: ~/.ssh\n"),
            "ssh.home: unknown key; a section has `paths` and `run`"
        );
    }

    #[test]
    fn shape_top_level() {
        assert_eq!(
            one_error("[a, b]\n"),
            ": must be a mapping of sections and lookup paths"
        );
        assert!(one_error("a: [\n").starts_with(": invalid YAML: "));
        assert!(load_one("").unwrap().files().len() == 1);
    }

    #[test]
    fn shape_rejects_tags() {
        let errors = load_one("a:\n  paths:\n    k: !include other.yaml\nb-paths: [!env HOME]\n")
            .unwrap_err();
        assert_eq!(errors.len(), 2, "{errors:?}");
        assert!(
            errors[0].starts_with("a.paths.k: YAML tags such as `!include`"),
            "{errors:?}"
        );
        assert!(errors[1].starts_with("b-paths[0]: "), "{errors:?}");
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
            "a:\n  paths:\n    x: high\n  run:\n    r: high\nl-paths: [/high]\n",
        );
        dir.write(
            "dist/setup.yaml",
            "a:\n  paths:\n    x: low\n    y: low\n  run:\n    r: low\n    s: low\nl-paths: [/low1, /low2]\nm-paths: [/m]\n",
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
        let render = |key: &str| section.run[key].render(&HashMap::new()).unwrap();
        assert_eq!(render("r"), ["high"]);
        assert_eq!(render("s"), ["low"]);
        assert_eq!(config.lookup_path("l"), [PathBuf::from("/high")]);
        assert_eq!(config.lookup_path("m"), [PathBuf::from("/m")]);
        assert_eq!(config.lookup_paths().len(), 2);
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
                "l-paths: ['~/a', '${s:home}/y', './c']\n",
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
            config.lookup_path("l"),
            [
                PathBuf::from("/home/me/a"),
                PathBuf::from("/home/me/x/y"),
                PathBuf::from("/proj/c"),
            ]
        );
        // Without a project, `./` paths stay as written, and `./` lookup paths are left out.
        let config = load(Some(&file), None, None, env).unwrap();
        assert_eq!(config.get("s", "local").as_deref(), Some("./local"));
        assert_eq!(
            config.lookup_path("l"),
            [PathBuf::from("/home/me/a"), PathBuf::from("/home/me/x/y")]
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
            "  run:\n",
            "    refers-to-run: ${a:cmd}\n",
            "    cmd: go\n",
            "l-paths: ['${top}', '{x:tag}', '${a:nope}']\n",
        ))
        .unwrap_err();
        assert_eq!(
            errors,
            [
                "a.paths.empty: `${}` is not a valid reference",
                "a.paths.empty-section: `${:k}` is not a valid reference",
                "a.paths.empty-x: `{x:}` needs a name",
                "a.paths.has-x: paths are file locations; they can't use `{x:...}` values",
                "a.paths.open: `${a:open` is missing its `}`",
                "a.paths.open-x: `{x:tag` is missing its `}`",
                "a.paths.unknown: unknown reference `${a:nope}`",
                "a.paths.unknown-section: unknown reference `${zz:top}`",
                // `a.paths.uses-unknown` fails because of `a.paths.unknown`, already reported.
                // `run` values can't be referred to.
                "a.run.refers-to-run: unknown reference `${a:cmd}`",
                "l-paths[0]: `${top}` needs a section here, as `${section:top}`",
                "l-paths[1]: paths can't use `{x:...}` values",
                "l-paths[2]: unknown reference `${a:nope}`",
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
            "b:\n  paths:\n    y: ${a:x}\n  run:\n    z: go ${a:x}\n",
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
                "  paths:\n    exec: podman\n    socket: /run/podman.sock\n",
                "  run:\n    build: ${exec} build -t {x:tag} '{x:dir}/a b'\n    label: \"${docker:exec}\"\n",
            ),
        );
        let env = Env {
            path: Some(dir.path().join("bin dir").into_os_string()),
            ..Env::default()
        };
        let config = load(Some(&file), None, None, env).unwrap();
        assert_eq!(config.exec_sections().collect::<Vec<_>>(), ["docker"]);
        let defaults = SectionDefaults {
            run: &[("extra", "${exec} --version --socket ${socket}")],
            ..SectionDefaults::default()
        };
        let section = found(config.section("docker", &defaults).unwrap());
        assert_eq!(section.exec(), Some(podman.as_path()));
        assert_eq!(
            section.paths,
            BTreeMap::from([
                ("exec".to_string(), podman.clone()),
                ("socket".to_string(), PathBuf::from("/run/podman.sock")),
            ])
        );
        assert_eq!(
            section.run.keys().collect::<Vec<_>>(),
            ["build", "extra", "label"]
        );
        let values = HashMap::from([("tag", "t:1"), ("dir", "/d")]);
        let podman = podman.display().to_string();
        // The path with a space stays one argument.
        assert_eq!(
            section.run["build"].render(&values).unwrap(),
            [podman.as_str(), "build", "-t", "t:1", "/d/a b"]
        );
        assert_eq!(
            section.run["extra"].render(&values).unwrap(),
            [podman.as_str(), "--version", "--socket", "/run/podman.sock"]
        );
        assert_eq!(
            section.run["label"].render(&values).unwrap(),
            [podman.as_str()]
        );
    }

    #[cfg(unix)]
    #[test]
    fn section_exec_with_home() {
        let dir = TempDir::new();
        let found_tool = tool(&dir, "home/bin/tool");
        let file = dir.write(
            "setup.yaml",
            "t:\n  paths:\n    exec: ~/bin/tool\n    conf: ~/my conf\n  run:\n    go: ${exec} -F ${conf}\n",
        );
        let home = dir.path().join("home");
        let env = Env {
            home: Some(home.clone()),
            ..Env::default()
        };
        let config = load(Some(&file), None, None, env).unwrap();
        let section = found(config.section("t", &SectionDefaults::default()).unwrap());
        assert_eq!(section.exec(), Some(found_tool.as_path()));
        assert_eq!(section.paths["conf"], home.join("my conf"));
        assert_eq!(
            section.run["go"].render(&HashMap::new()).unwrap(),
            [
                found_tool.display().to_string(),
                "-F".to_string(),
                home.join("my conf").display().to_string()
            ]
        );
    }

    #[cfg(unix)]
    #[test]
    fn section_cmd_executables() {
        let dir = TempDir::new();
        let keygen = tool(&dir, "bin/ssh-keygen");
        let ssh = tool(&dir, "bin/ssh");
        let file = dir.write(
            "setup.yaml",
            concat!(
                "ssh:\n",
                "  paths:\n    exec: ssh\n    keygen-cmd: ssh-keygen\n    home: /h\n    -cmd: plain\n",
                "  run:\n    new-key: ${keygen-cmd} -f ${home}/key\n",
                "other:\n  paths:\n    helper-cmd: no-such-helper\n    home: /o\n",
            ),
        );
        let env = Env {
            path: Some(dir.path().join("bin").into_os_string()),
            ..Env::default()
        };
        let config = load(Some(&file), None, None, env).unwrap();
        assert_eq!(config.exec_sections().collect::<Vec<_>>(), ["other", "ssh"]);
        let section = found(config.section("ssh", &SectionDefaults::default()).unwrap());
        assert_eq!(section.exec(), Some(ssh.as_path()));
        assert_eq!(section.paths["keygen-cmd"], keygen);
        // `-cmd` alone is an ordinary path.
        assert_eq!(section.paths["-cmd"], PathBuf::from("plain"));
        assert_eq!(
            section.run["new-key"].render(&HashMap::new()).unwrap(),
            [
                keygen.display().to_string(),
                "-f".to_string(),
                "/h/key".to_string()
            ]
        );
        // Any missing executable makes the section unusable.
        assert_eq!(
            config
                .section("other", &SectionDefaults::default())
                .unwrap(),
            SectionLookup::MissingExecutable {
                key: "helper-cmd".to_string(),
                program: "no-such-helper".to_string()
            }
        );
        assert!(is_executable_key("exec"));
        assert!(is_executable_key("a-cmd"));
        assert!(!is_executable_key("-cmd"));
        assert!(!is_executable_key("execs"));
    }

    #[test]
    fn run_as_a_list() {
        let config = load_one(concat!(
            "t:\n",
            "  paths:\n    home: /h\n",
            "  run:\n",
            "    line: go 'a b' \"c d\"\n",
            "    list: [go, 'a b', \"'kept'\", '\"too\"', '${home}/x', '--tag={x:tag}', '', 5, true]\n",
            "    block:\n      - go\n      - with spaces\n",
        ))
        .unwrap();
        let section = found(config.section("t", &SectionDefaults::default()).unwrap());
        let values = HashMap::from([("tag", "v 1")]);
        let render = |key: &str| section.run[key].render(&values).unwrap();
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
            "t:\n  run:\n",
            "    nested: [go, [a], {b: c}, ~]\n",
            "    empty: []\n",
            "    unknown: [go, '${nope}']\n",
            "    map: {a: b}\n",
        ))
        .unwrap_err();
        assert_eq!(
            errors,
            [
                "t.run.nested[1]: must be a single value, not a list or mapping",
                "t.run.nested[2]: must be a single value, not a list or mapping",
                "t.run.nested[3]: has no value",
                "t.run.map: must be a single value, not a list or mapping",
            ]
        );
        let errors =
            load_one("t:\n  run:\n    empty: []\n    unknown: [go, '${nope}']\n").unwrap_err();
        assert_eq!(
            errors,
            [
                "t.run.empty: the command is empty",
                "t.run.unknown: unknown reference `${t:nope}`",
            ]
        );
    }

    #[test]
    fn run_list_and_line_merge() {
        let dir = TempDir::new();
        let cli = dir.write("cli.yaml", "t:\n  run:\n    go: [high, 'a b']\n");
        dir.write(
            "dist/setup.yaml",
            "t:\n  run:\n    go: low line\n    other: [low]\n",
        );
        let config = load(
            Some(&cli),
            Some(&dir.path().join("dist")),
            None,
            Env::default(),
        )
        .unwrap();
        let section = found(config.section("t", &SectionDefaults::default()).unwrap());
        let render = |key: &str| section.run[key].render(&HashMap::new()).unwrap();
        assert_eq!(render("go"), ["high", "a b"]);
        assert_eq!(render("other"), ["low"]);
    }

    #[test]
    fn section_without_exec() {
        let config =
            load_one("ssh:\n  paths:\n    home: /h/.ssh\n    config-file: ${home}/config\n")
                .unwrap();
        assert_eq!(config.exec_sections().count(), 0);
        let section = found(config.section("ssh", &SectionDefaults::default()).unwrap());
        assert_eq!(section.exec(), None);
        assert_eq!(
            section.paths["config-file"],
            PathBuf::from("/h/.ssh/config")
        );
        assert!(section.run.is_empty());
        // A section nobody set is empty.
        let section = found(config.section("none", &SectionDefaults::default()).unwrap());
        assert!(section.paths.is_empty() && section.run.is_empty());
    }

    #[test]
    fn section_defaults_and_not_found() {
        let config = load_one("").unwrap();
        let defaults = SectionDefaults {
            paths: &[("exec", "no-such-docker")],
            run: &[("go", "${exec} x")],
        };
        assert_eq!(
            config.section("docker", &defaults).unwrap(),
            SectionLookup::MissingExecutable {
                key: "exec".to_string(),
                program: "no-such-docker".to_string()
            },
        );
        let defaults = SectionDefaults {
            run: &[("go", "${exec} x")],
            ..SectionDefaults::default()
        };
        let missing = config.section("docker", &defaults).unwrap_err();
        assert_eq!(
            missing.to_string(),
            "docker.run.go: unknown reference `${docker:exec}`"
        );
    }

    #[cfg(unix)]
    #[test]
    fn section_bad_default() {
        let dir = TempDir::new();
        tool(&dir, "bin/sh-tool");
        let file = dir.write("setup.yaml", "t:\n  paths:\n    exec: sh-tool\n");
        let env = Env {
            path: Some(dir.path().join("bin").into_os_string()),
            ..Env::default()
        };
        let config = load(Some(&file), None, None, env).unwrap();
        let defaults = SectionDefaults {
            run: &[("go", "${exec} 'x")],
            ..SectionDefaults::default()
        };
        let error = config.section("t", &defaults).unwrap_err();
        assert_eq!(error.to_string(), "t.run.go: unmatched ' quote");
    }

    #[test]
    fn commands_are_checked_when_loading() {
        let errors = load_one(concat!(
            "t:\n",
            "  paths:\n    exec: tool {x:no}\n",
            "  run:\n",
            "    quote: ${exec} 'x\n",
            "    empty: '  '\n",
            "    unknown: ${exec} ${nope}\n",
            "    ok: ${exec} {x:fine}\n",
            "no-exec:\n  run:\n    go: ${exec} now\n",
        ))
        .unwrap_err();
        assert_eq!(
            errors,
            [
                "no-exec.run.go: unknown reference `${no-exec:exec}`",
                "t.paths.exec: paths are file locations; they can't use `{x:...}` values",
                "t.run.empty: the command is empty",
                "t.run.quote: unmatched ' quote",
                "t.run.unknown: unknown reference `${t:nope}`",
            ]
        );
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
