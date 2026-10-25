//! The `host-commands` checks: what the setup files say about the commands a
//! project may run on the host.  See `docs/system-config-file.md`.
//!
//! Any section can have `host-commands`.  Each key is one check:
//!
//! ```yaml
//! verification:
//!   host-commands:
//!     shells:
//!       action: refuse
//!       reason: A shell runs whatever command it's given.
//!       commands: [sh, bash]
//! ```
//!
//!   * `action`: `refuse`, `warn` or `allow`, from most to least restrictive.
//!   * `reason`: the explanation shown when the check matches.
//!   * What it matches; with more than one, all must match:
//!       * `commands`: names, each found on the `PATH` as a `programs` value
//!         is (short for mapping the name to itself in `command-map`);
//!       * `command-map`: names, each mapped to the host program it runs,
//!         expanded as a `programs` value is;
//!       * `name-patterns`: name patterns (`*`, `?` and `[...]`), matched
//!         against the name and the base name of the executable it really is;
//!       * `options`: options that run code;
//!       * `env`: environment variable names, matched exactly against a
//!         command's `env` `allow` entries;
//!       * `paths`: directories, expanded as `path-lists` entries are; an
//!         executable in one matches.
//!   * `overrides`: the checks (`section:key`) this one replaces, for the
//!     commands it names only.
//!
//! A null check removes a lower priority file's check of the same name.
//! Nothing can refer to a check with `${...}`.  Only `command-map` values and
//! `paths` are expanded; `${` anywhere else is an error.
//!
//! The loader (`system`) parses and expands each section's checks;
//! `combine` puts every section's together for the universal configuration
//! file.

use std::collections::BTreeMap;
use std::path::{Path, PathBuf};

use serde::{Deserialize, Serialize};
use yaml_serde::Value;

use super::system::{HOST_COMMANDS, Section, SetupRecord, SystemConfig};
use super::{ConfigError, child_key, index_key};

/// What a check does with what it matches, from least to most restrictive.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash, Serialize, Deserialize)]
#[serde(rename_all = "kebab-case")]
pub enum Action {
    Allow,
    Warn,
    Refuse,
}

impl Action {
    fn name(self) -> &'static str {
        match self {
            Action::Allow => "allow",
            Action::Warn => "warn",
            Action::Refuse => "refuse",
        }
    }
}

/// A check, as written.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "kebab-case", deny_unknown_fields)]
pub struct CheckRecord {
    pub action: Action,
    pub reason: String,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub commands: Vec<String>,
    #[serde(default, skip_serializing_if = "BTreeMap::is_empty")]
    pub command_map: BTreeMap<String, String>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub name_patterns: Vec<String>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub options: Vec<String>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub env: Vec<String>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub paths: Vec<String>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub overrides: Vec<String>,
}

/// A `host-commands` entry at `at`, as written: `None` for null, which
/// removes the check.  Problems are (key, message) pairs.
pub fn parse(value: Value, at: &str) -> Result<Option<CheckRecord>, Vec<(String, String)>> {
    match value {
        Value::Null => Ok(None),
        Value::Mapping(_) => {
            let record: CheckRecord =
                yaml_serde::from_value(value).map_err(|e| vec![(at.to_string(), e.to_string())])?;
            let problems = record.problems(at);
            if problems.is_empty() {
                Ok(Some(record))
            } else {
                Err(problems)
            }
        }
        _ => Err(vec![(
            at.to_string(),
            "must be a check (a mapping with `action`, `reason` and what it matches), or null to remove it"
                .to_string(),
        )]),
    }
}

impl CheckRecord {
    /// The problems with the check at `at`, as (key, message) pairs.
    pub fn problems(&self, at: &str) -> Vec<(String, String)> {
        let mut out = Vec::new();
        let field = |name: &str| child_key(at, name);
        let mut add = |key: String, message: &str| out.push((key, message.to_string()));
        const AS_WRITTEN: &str = "is used as written; `${...}` isn't expanded here";

        if self.reason.trim().is_empty() {
            add(
                field("reason"),
                "must explain the check; it's shown when the check matches",
            );
        } else if self.reason.contains("${") {
            add(field("reason"), AS_WRITTEN);
        }
        let items = |name: &str, list: &[String]| -> Vec<(String, String)> {
            list.iter()
                .enumerate()
                .map(|(i, item)| (index_key(&field(name), i), item.clone()))
                .collect()
        };
        for (key, name) in items("commands", &self.commands) {
            if let Some(problem) = name_problem(&name, false) {
                add(key, &problem);
            }
        }
        for (name, program) in &self.command_map {
            let key = child_key(&field("command-map"), name);
            if let Some(problem) = name_problem(name, false) {
                add(key.clone(), &problem);
            }
            if self.commands.contains(name) {
                add(key.clone(), "is also in `commands`; give each name once");
            }
            if program.trim().is_empty() {
                add(key, "must name the program the command runs");
            }
        }
        for (key, pattern) in items("name-patterns", &self.name_patterns) {
            if let Some(problem) = name_problem(&pattern, true) {
                add(key, &problem);
            }
        }
        for (key, option) in items("options", &self.options) {
            if option.contains("${") {
                add(key, AS_WRITTEN);
            } else if option.len() < 2 || !option.starts_with('-') {
                add(key, "must be an option, starting with `-`");
            } else if option.contains('=') {
                add(key, "must be the option alone, without `=` and its value");
            } else if !plain_ascii(&option) {
                add(key, "must be ASCII, without spaces or control characters");
            }
        }
        for (key, name) in items("env", &self.env) {
            if name.contains("${") {
                add(key, AS_WRITTEN);
            } else if name.is_empty() || name.contains('=') || !plain_ascii(&name) {
                add(
                    key,
                    "must be an environment variable name: ASCII, without `=`, spaces or control characters",
                );
            } else if !self.overrides.is_empty() && is_pattern(&name) {
                add(key, "must be an exact name in a check with `overrides`");
            }
        }
        for (key, path) in items("paths", &self.paths) {
            if path.trim().is_empty() {
                add(key, "must be a directory");
            }
        }
        for (key, target) in items("overrides", &self.overrides) {
            let valid = target.split_once(':').is_some_and(|(section, name)| {
                !section.is_empty() && !name.is_empty() && !name.contains(':')
            });
            if target.contains("${") {
                add(key, AS_WRITTEN);
            } else if !valid || !plain_ascii(&target) {
                add(key, "must name a check as `section:key`");
            }
        }

        let names = !self.commands.is_empty() || !self.command_map.is_empty();
        let others =
            !self.name_patterns.is_empty() || !self.options.is_empty() || !self.env.is_empty();
        if !names && !others && self.paths.is_empty() {
            add(
                at.to_string(),
                "needs something to match: `commands`, `command-map`, `name-patterns`, `options`, `env` or `paths`",
            );
        }
        if !self.paths.is_empty() && (names || others) {
            add(
                field("paths"),
                "can't be combined with other matches; a check with `paths` matches every executable in them",
            );
        }
        if !self.options.is_empty() && !self.env.is_empty() {
            add(
                at.to_string(),
                "can't match both `options` and `env`; make them two checks",
            );
        }
        if !self.overrides.is_empty() {
            if !names {
                add(
                    field("overrides"),
                    "needs `commands` or `command-map`: an override names the commands it's for, exactly",
                );
            }
            if !self.name_patterns.is_empty() {
                add(
                    field("name-patterns"),
                    "can't be used with `overrides`; name the commands exactly",
                );
            }
        }
        out
    }
}

/// Why `name` isn't a command name (or, with `pattern`, a name pattern).
fn name_problem(name: &str, pattern: bool) -> Option<String> {
    let what = if pattern {
        "a name pattern"
    } else {
        "a command name"
    };
    if name.contains("${") {
        return Some("is used as written; `${...}` isn't expanded here".to_string());
    }
    if name.is_empty() || name == "." || name == ".." {
        return Some(format!("'{name}' is not {what}"));
    }
    if name.contains(['/', '\\']) {
        return Some(if pattern {
            format!("'{name}' is {what}, not a path")
        } else {
            format!("'{name}' is {what}, not a path; map the name to a program with `command-map`")
        });
    }
    if name.starts_with('-') {
        return Some(format!("'{name}' can't start with `-`"));
    }
    if !plain_ascii(name) {
        return Some(format!(
            "'{name}' must be ASCII, without spaces or control characters"
        ));
    }
    if pattern && !pattern_is_closed(name) {
        return Some(format!("'{name}' has a `[` without its `]`"));
    }
    if !pattern && let Some(problem) = windows_name_problem(name) {
        return Some(problem);
    }
    None
}

/// Why `name` can't be a command name, if it can't: the rules for a
/// project's `command` and `program`, which name a program rather than
/// give its path.  Not empty, `.` or `..`; no `/` or `\`; ASCII without
/// spaces or control characters; no leading `-`; and nothing Windows reads
/// as more than a file name (see `windows_name_problem`).
pub(crate) fn command_name_problem(name: &str) -> Option<String> {
    if name.is_empty() || name == "." || name == ".." {
        return Some(format!("'{name}' is not a command name"));
    }
    if name.contains(['/', '\\']) {
        return Some(format!(
            "'{name}' is a path; give the program's name, which the host finds by name (through your setup's `command-map`, or the `PATH`)"
        ));
    }
    if name.starts_with('-') {
        return Some(format!("'{name}' can't start with `-`"));
    }
    if !plain_ascii(name) {
        return Some(format!(
            "'{name}' must be ASCII, without spaces or control characters"
        ));
    }
    windows_name_problem(name)
}

/// The Windows device names: a file name that is one of these, with or
/// without an extension, names the device instead.
const WINDOWS_DEVICES: [&str; 6] = ["CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"];

/// Why Windows would read `name` as more than a file name, if it would: a
/// `:` (a drive, as in `C:aws`, or a stream, as in `aws:stream`), or a
/// device name (`NUL`, `COM1`, `nul.txt` and the like, in any case).
pub(crate) fn windows_name_problem(name: &str) -> Option<String> {
    if name.contains(':') {
        return Some(format!(
            "'{name}' can't have a `:`; Windows reads it as a drive or a stream"
        ));
    }
    let stem = name.split('.').next().unwrap_or(name).to_ascii_uppercase();
    let numbered = ["COM", "LPT"].iter().any(|prefix| {
        stem.strip_prefix(prefix)
            .is_some_and(|n| n.len() == 1 && n.as_bytes()[0].is_ascii_digit())
    });
    if numbered || WINDOWS_DEVICES.contains(&stem.as_str()) {
        return Some(format!("'{name}' is a Windows device name"));
    }
    None
}

/// ASCII, without spaces or control characters.
fn plain_ascii(text: &str) -> bool {
    text.chars().all(|c| c.is_ascii_graphic())
}

fn is_pattern(text: &str) -> bool {
    text.contains(['*', '?', '['])
}

// ---- name patterns ----

/// Whether `name` matches `pattern`: `*` matches any characters, `?` one
/// character, and `[...]` one of a set (`[!...]` or `[^...]`: one not in
/// it; `a-z`, a range).  Case sensitive.
pub fn pattern_matches(pattern: &str, name: &str) -> bool {
    let pattern: Vec<char> = pattern.chars().collect();
    let name: Vec<char> = name.chars().collect();
    matches_at(&pattern, &name)
}

fn matches_at(pattern: &[char], name: &[char]) -> bool {
    match pattern.first() {
        None => name.is_empty(),
        Some('*') => (0..=name.len()).any(|i| matches_at(&pattern[1..], &name[i..])),
        Some('?') => !name.is_empty() && matches_at(&pattern[1..], &name[1..]),
        Some('[') => match (name.first(), class(&pattern[1..])) {
            (Some(&c), Some((set, len))) => {
                set.contains(c) && matches_at(&pattern[1 + len..], &name[1..])
            }
            // An unclosed `[` is itself.
            (Some('['), None) => matches_at(&pattern[1..], &name[1..]),
            _ => false,
        },
        Some(&c) => name.first() == Some(&c) && matches_at(&pattern[1..], &name[1..]),
    }
}

/// A `[...]` set, after its `[`.
struct CharSet {
    negated: bool,
    ranges: Vec<(char, char)>,
}

impl CharSet {
    fn contains(&self, c: char) -> bool {
        self.ranges.iter().any(|&(lo, hi)| lo <= c && c <= hi) != self.negated
    }
}

/// The set starting `pattern` (just after a `[`), and how many characters it
/// takes, its `]` included; `None` when it has no `]`.
fn class(pattern: &[char]) -> Option<(CharSet, usize)> {
    let mut i = 0;
    let negated = matches!(pattern.first(), Some('!' | '^'));
    if negated {
        i += 1;
    }
    let mut ranges = Vec::new();
    let start = i;
    while i < pattern.len() {
        let c = pattern[i];
        // A `]` first in the set is part of it.
        if c == ']' && i > start {
            return Some((CharSet { negated, ranges }, i + 1));
        }
        if pattern.get(i + 1) == Some(&'-') && pattern.get(i + 2).is_some_and(|&e| e != ']') {
            ranges.push((c, pattern[i + 2]));
            i += 3;
        } else {
            ranges.push((c, c));
            i += 1;
        }
    }
    None
}

fn pattern_is_closed(pattern: &str) -> bool {
    let chars: Vec<char> = pattern.chars().collect();
    let mut i = 0;
    while i < chars.len() {
        if chars[i] == '[' {
            match class(&chars[i + 1..]) {
                Some((_, len)) => i += 1 + len,
                None => return false,
            }
        } else {
            i += 1;
        }
    }
    true
}

// ---- expanded checks ----

/// A check, expanded.
#[derive(Debug, Clone, PartialEq)]
pub struct Check {
    pub action: Action,
    pub reason: String,
    /// `commands` and `command-map` together: each name, and the executable
    /// it runs (`None` when the program isn't there).
    pub commands: BTreeMap<String, Option<PathBuf>>,
    pub name_patterns: Vec<String>,
    pub options: Vec<String>,
    pub env: Vec<String>,
    pub paths: Vec<PathBuf>,
    pub overrides: Vec<String>,
}

impl Check {
    /// Whether the check matches on names alone, so whether it applies to a
    /// command can be known without the command's arguments.
    fn names_only(&self) -> bool {
        self.options.is_empty() && self.env.is_empty() && self.paths.is_empty()
    }

    fn has_names(&self) -> bool {
        !self.commands.is_empty() || !self.name_patterns.is_empty()
    }

    /// Whether the check's names match the command `name`, whose executable
    /// is really `real`.
    fn matches_name(&self, name: &str, real: Option<&Path>) -> bool {
        if self.commands.contains_key(name) {
            return true;
        }
        if let Some(real) = real
            && self
                .commands
                .values()
                .flatten()
                .any(|program| real_path(program) == real)
        {
            return true;
        }
        let base = real.and_then(base_name);
        self.name_patterns.iter().any(|pattern| {
            pattern_matches(pattern, name)
                || base.as_deref().is_some_and(|b| pattern_matches(pattern, b))
        })
    }
}

/// The file `path` really is, with every symlink resolved.
fn real_path(path: &Path) -> PathBuf {
    path.canonicalize().unwrap_or_else(|_| path.to_path_buf())
}

/// The executable's base name; on Windows, without its extension.
fn base_name(path: &Path) -> Option<String> {
    let name = if cfg!(windows) {
        path.file_stem()
    } else {
        path.file_name()
    };
    name.and_then(|n| n.to_str()).map(str::to_string)
}

// ---- combined ----

/// Every section's checks, put together: the universal configuration file's
/// `host-command-checks`.
#[derive(Debug, Clone, Default, PartialEq, Serialize)]
#[serde(rename_all = "kebab-case")]
pub struct Combined {
    /// Each check, by `section:key`.  Removed checks aren't here.
    pub checks: BTreeMap<String, CombinedCheck>,
    /// Each command name the checks map, with the program it runs.
    pub command_map: BTreeMap<String, MappedCommand>,
    /// For each mapped command, what the checks that match on names alone
    /// make of it (see `Verdict`).  Checks with `options`, `env` or `paths`
    /// depend on the project's command, so they aren't counted here.
    pub commands: BTreeMap<String, Verdict>,
    /// Things the user should know about their checks, such as an override
    /// that does nothing, or a replaced check that drops commands.
    pub notices: Vec<Notice>,
}

/// One check, expanded, and the files that set it.
#[derive(Debug, Clone, PartialEq, Serialize)]
#[serde(rename_all = "kebab-case")]
pub struct CombinedCheck {
    /// Every setup file that sets this `section:key`, highest priority
    /// first.  The check is the first one's alone (a check's fields aren't
    /// merged); the others' versions are the ones it hides.
    pub files: Vec<String>,
    pub action: Action,
    pub reason: String,
    pub commands: BTreeMap<String, Option<String>>,
    pub name_patterns: Vec<String>,
    pub options: Vec<String>,
    pub env: Vec<String>,
    pub paths: Vec<String>,
    pub overrides: Vec<String>,
}

/// A command name, and the program it runs (`None` when it isn't there).
#[derive(Debug, Clone, PartialEq, Serialize)]
#[serde(rename_all = "kebab-case")]
pub struct MappedCommand {
    pub program: Option<String>,
    /// The checks that map it.
    pub checks: Vec<String>,
}

/// The action for a command.  Worked out in two steps: the checks without
/// `overrides` that match the command give the most restrictive action of
/// theirs; then each override for the command replaces the check it
/// overrides with itself, and the most restrictive action of what's left
/// is the action.
#[derive(Debug, Clone, PartialEq, Serialize)]
#[serde(rename_all = "kebab-case")]
pub struct Verdict {
    pub action: Action,
    /// The checks the action comes from, overrides included.
    pub checks: Vec<String>,
    /// The checks that matched, but that an override replaced.
    pub overridden: Vec<Overridden>,
}

/// A check an override replaced, for one command.
#[derive(Debug, Clone, PartialEq, Serialize)]
#[serde(rename_all = "kebab-case")]
pub struct Overridden {
    pub check: String,
    pub by: String,
}

/// Something the user should know about their checks.  `message` is the
/// text to show; `id` and `parameters` say the same, for translating it, and
/// `sources` are the setup files it's about.
#[derive(Debug, Clone, PartialEq, Serialize)]
#[serde(rename_all = "kebab-case")]
pub struct Notice {
    pub message: String,
    pub id: String,
    pub parameters: BTreeMap<String, NoticeValue>,
    pub sources: Vec<String>,
}

/// A notice parameter: text, or a list of texts.
#[derive(Debug, Clone, PartialEq, Serialize)]
#[serde(untagged)]
pub enum NoticeValue {
    Text(String),
    List(Vec<String>),
}

impl Notice {
    fn new(
        id: &str,
        message: String,
        parameters: &[(&str, NoticeValue)],
        sources: &[&str],
    ) -> Self {
        Notice {
            message,
            id: id.to_string(),
            parameters: parameters
                .iter()
                .map(|(k, v)| (k.to_string(), v.clone()))
                .collect(),
            sources: sources.iter().map(|s| s.to_string()).collect(),
        }
    }
}

fn text(value: &str) -> NoticeValue {
    NoticeValue::Text(value.to_string())
}

/// One check, with where it's from.
struct Found<'a> {
    id: String,
    section: &'a str,
    key: &'a str,
    file: Option<&'a Path>,
    /// The file's place among the setup files, highest priority first.
    priority: usize,
    check: &'a Check,
}

impl Found<'_> {
    fn at(&self) -> String {
        child_key(self.section, &format!("{HOST_COMMANDS}.{}", self.key))
    }

    fn error(&self, message: String) -> ConfigError {
        ConfigError::new(self.file, &self.at(), message)
    }

    fn file_text(&self) -> String {
        self.file.map(path_text).unwrap_or_default()
    }

    fn is_override(&self) -> bool {
        !self.check.overrides.is_empty()
    }
}

/// An override of a check for a command: the override, and the options or
/// `env` names it's limited to (none: all of them).
type Claim<'a> = (&'a str, &'a [String]);

/// A setup file, and the check it sets (`None`: it removes the check).
type FileCheck<'a> = (&'a str, Option<&'a CheckRecord>);

/// Each `section:key` check in the setup files: every file's version,
/// highest priority first.
type Versions<'a> = BTreeMap<(&'a str, &'a str), Vec<FileCheck<'a>>>;

fn versions(record: &SetupRecord) -> Versions<'_> {
    let mut settings = Versions::new();
    for file in &record.files {
        for (section, groups) in &file.sections {
            for (key, check) in &groups.host_commands {
                settings
                    .entry((section.as_str(), key.as_str()))
                    .or_default()
                    .push((file.file.as_str(), check.as_ref()));
            }
        }
    }
    settings
}

/// Put the checks of `sections` (all of `system`'s, expanded) together.
pub fn combine(system: &SystemConfig, sections: &[Section]) -> Result<Combined, Vec<ConfigError>> {
    let files = system.files();
    let mut found = Vec::new();
    for section in sections {
        for (key, check) in &section.host_commands {
            let file = system.host_command_file(&section.name, key);
            let priority = file
                .and_then(|f| files.iter().position(|p| p == f))
                .unwrap_or(files.len());
            found.push(Found {
                id: format!("{}:{key}", section.name),
                section: &section.name,
                key,
                file,
                priority,
                check,
            });
        }
    }
    let versions = versions(system.setup_record());
    let mut errors = Vec::new();
    let mut notices = Vec::new();

    // A host command never runs a program from the project.
    for f in &found {
        for (name, program) in &f.check.commands {
            if let Some(program) = program
                && let Some(location) = system.project_location(program)
            {
                errors.push(f.error(format!(
                    "`{name}` runs {}, which is in the project (`{location}`); a host command never runs a program from the project",
                    program.display()
                )));
            }
        }
    }

    // One program per command.
    let mut command_map: BTreeMap<String, MappedCommand> = BTreeMap::new();
    for f in &found {
        for (name, program) in &f.check.commands {
            let program = program.as_deref().map(path_text);
            match command_map.get_mut(name) {
                Some(mapped) => {
                    match (&mapped.program, &program) {
                        (Some(a), Some(b)) if a != b => errors.push(f.error(format!(
                            "maps `{name}` to {b}, but `{}` maps it to {a}; a command runs one program, so change or override one of them",
                            mapped.checks.join("`, `")
                        ))),
                        (None, Some(_)) => mapped.program = program,
                        _ => {}
                    }
                    mapped.checks.push(f.id.clone());
                }
                None => {
                    command_map.insert(
                        name.clone(),
                        MappedCommand {
                            program,
                            checks: vec![f.id.clone()],
                        },
                    );
                }
            }
        }
    }

    // Overrides.
    let by_id: BTreeMap<&str, &Found> = found.iter().map(|f| (f.id.as_str(), f)).collect();
    // (target, command) -> the overrides for it so far.
    let mut claimed: BTreeMap<(&str, &str), Vec<Claim>> = BTreeMap::new();
    for f in &found {
        for target_id in &f.check.overrides {
            if *target_id == f.id {
                errors.push(f.error("can't override itself".to_string()));
                continue;
            }
            let Some(target) = by_id.get(target_id.as_str()) else {
                notices.push(Notice::new(
                    "override-target-unset",
                    format!(
                        "`{}` overrides `{target_id}`, which isn't set; the override does nothing",
                        f.id
                    ),
                    &[("check", text(&f.id)), ("target", text(target_id))],
                    &[&f.file_text()],
                ));
                continue;
            };
            if target.is_override() {
                errors.push(f.error(format!(
                    "overrides `{target_id}`, which is an override itself; an override can only replace checks without `overrides`"
                )));
                continue;
            }
            if f.priority > target.priority {
                errors.push(f.error(format!(
                    "overrides `{target_id}`, from a higher priority setup file ({}); a check can only override checks from its own file or a lower priority one",
                    target.file_text()
                )));
                continue;
            }
            let t = target.check;
            let not_matched = |kind: &str, what: &str, name: &str| {
                Notice::new(
                    "override-not-matched",
                    format!(
                        "`{}` overrides `{target_id}` for the {what} `{name}`, which `{target_id}` doesn't match; the override does nothing for it",
                        f.id
                    ),
                    &[
                        ("check", text(&f.id)),
                        ("target", text(target_id)),
                        ("kind", text(kind)),
                        ("name", text(name)),
                    ],
                    &[&f.file_text(), &target.file_text()],
                )
            };
            // Overrides name their commands exactly.
            for name in f.check.commands.keys() {
                if t.has_names() && !t.matches_name(name, None) {
                    notices.push(not_matched("command", "command", name));
                }
            }
            let items: &[String] = if f.check.options.is_empty() {
                &f.check.env
            } else {
                &f.check.options
            };
            for (kind, what, own, theirs) in [
                ("option", "option", &f.check.options, &t.options),
                ("env", "environment variable", &f.check.env, &t.env),
            ] {
                for item in own.iter().filter(|item| !theirs.contains(item)) {
                    notices.push(not_matched(kind, what, item));
                }
            }
            // Two overrides of one check for the same command can't both
            // say what it becomes.
            for name in f.check.commands.keys() {
                let others = claimed.entry((target_id, name)).or_default();
                for (other, other_items) in others.iter() {
                    let overlap = items.is_empty()
                        || other_items.is_empty()
                        || items.iter().any(|i| other_items.contains(i));
                    if overlap {
                        errors.push(f.error(format!(
                            "overrides `{target_id}` for `{name}`, as `{other}` does; only one override can say what a check becomes for a command"
                        )));
                    }
                }
                others.push((&f.id, items));
            }
        }
    }

    notices.extend(replacement_notices(&versions));

    // What the checks that match on names make of each mapped command:
    // first the checks without overrides, then the overrides.
    let mut commands = BTreeMap::new();
    for (name, mapped) in &command_map {
        let real = mapped.program.as_deref().map(|p| real_path(Path::new(p)));
        let matching: Vec<&Found> = found
            .iter()
            .filter(|f| {
                !f.is_override()
                    && f.check.names_only()
                    && f.check.matches_name(name, real.as_deref())
            })
            .collect();
        if matching.is_empty() {
            continue;
        }
        let mut applying: Vec<(&str, Action)> = matching
            .iter()
            .map(|f| (f.id.as_str(), f.check.action))
            .collect();
        let mut overridden = Vec::new();
        let overrides = found.iter().filter(|o| {
            o.is_override() && o.check.names_only() && o.check.commands.contains_key(name)
        });
        for o in overrides {
            for target in &o.check.overrides {
                let Some(at) = applying.iter().position(|(id, _)| id == target) else {
                    continue;
                };
                applying.remove(at);
                overridden.push(Overridden {
                    check: target.clone(),
                    by: o.id.clone(),
                });
                if !applying.iter().any(|(id, _)| *id == o.id) {
                    applying.push((&o.id, o.check.action));
                }
            }
        }
        // Never empty: an override takes the place of the check it removes.
        let action = applying
            .iter()
            .map(|(_, a)| *a)
            .max()
            .expect("a command some checks match has an action");
        let mut checks: Vec<String> = applying.iter().map(|(id, _)| id.to_string()).collect();
        checks.sort();
        commands.insert(
            name.clone(),
            Verdict {
                action,
                checks,
                overridden,
            },
        );
    }

    if !errors.is_empty() {
        return Err(errors);
    }
    let checks = found
        .iter()
        .map(|f| {
            let c = f.check;
            let files = versions
                .get(&(f.section, f.key))
                .map(|v| v.iter().map(|(file, _)| file.to_string()).collect())
                .unwrap_or_default();
            let combined = CombinedCheck {
                files,
                action: c.action,
                reason: c.reason.clone(),
                commands: c
                    .commands
                    .iter()
                    .map(|(name, p)| (name.clone(), p.as_deref().map(path_text)))
                    .collect(),
                name_patterns: c.name_patterns.clone(),
                options: c.options.clone(),
                env: c.env.clone(),
                paths: c.paths.iter().map(|p| path_text(p)).collect(),
                overrides: c.overrides.clone(),
            };
            (f.id.clone(), combined)
        })
        .collect();
    Ok(Combined {
        checks,
        command_map,
        commands,
        notices,
    })
}

/// For each check that a setup file replaces or removes, what the lower
/// priority file's check had that's now gone.  A replacement freezes the
/// check: what the distribution adds to it later never applies.
fn replacement_notices(versions: &Versions) -> Vec<Notice> {
    let mut notices = Vec::new();
    for ((section, key), versions) in versions {
        let [(file, winner), (lower_file, Some(lower)), ..] = versions[..] else {
            continue;
        };
        let id = format!("{section}:{key}");
        let sources = [file, lower_file];
        let Some(winner) = winner else {
            notices.push(Notice::new(
                "check-removed",
                format!(
                    "{file} removes `{id}`, so {lower_file}'s check no longer applies, nor will what it adds later"
                ),
                &[("check", text(&id))],
                &sources,
            ));
            continue;
        };
        let names = |c: &CheckRecord| -> Vec<String> {
            c.commands
                .iter()
                .chain(c.command_map.keys())
                .cloned()
                .collect()
        };
        for (field, theirs, ours) in [
            ("commands", names(lower), names(winner)),
            (
                "name-patterns",
                lower.name_patterns.clone(),
                winner.name_patterns.clone(),
            ),
            ("options", lower.options.clone(), winner.options.clone()),
            ("env", lower.env.clone(), winner.env.clone()),
            ("paths", lower.paths.clone(), winner.paths.clone()),
        ] {
            let missing: Vec<String> = theirs
                .iter()
                .filter(|item| !ours.contains(item))
                .cloned()
                .collect();
            if !missing.is_empty() {
                let shown: Vec<String> = missing.iter().map(|m| format!("`{m}`")).collect();
                notices.push(Notice::new(
                    "check-replaced-without",
                    format!(
                        "{file} replaces `{id}` from {lower_file} without its {field} {}; what {lower_file} adds to it later won't apply (`overrides` relaxes a check without replacing it)",
                        shown.join(", ")
                    ),
                    &[
                        ("check", text(&id)),
                        ("field", text(field)),
                        ("entries", NoticeValue::List(missing)),
                    ],
                    &sources,
                ));
            }
        }
        if winner.action < lower.action {
            notices.push(Notice::new(
                "check-replaced-less-restrictive",
                format!(
                    "{file} replaces `{id}` from {lower_file} with `{}` in place of `{}`",
                    winner.action.name(),
                    lower.action.name()
                ),
                &[
                    ("check", text(&id)),
                    ("action", text(winner.action.name())),
                    ("replaced-action", text(lower.action.name())),
                ],
                &sources,
            ));
        }
    }
    notices
}

fn path_text(path: &Path) -> String {
    path.to_string_lossy().into_owned()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn record(yaml: &str) -> Result<Option<CheckRecord>, Vec<String>> {
        let value: Value = yaml_serde::from_str(yaml).unwrap();
        parse(value, "s.host-commands.c")
            .map_err(|p| p.into_iter().map(|(k, m)| format!("{k}: {m}")).collect())
    }

    fn problems(yaml: &str) -> Vec<String> {
        record(yaml).unwrap_err()
    }

    #[test]
    fn parse_check() {
        let check = record(
            "action: warn\nreason: why\ncommands: [git]\noptions: [-c]\ncommand-map: {docker: podman}\n",
        )
        .unwrap()
        .unwrap();
        assert_eq!(check.action, Action::Warn);
        assert_eq!(check.commands, ["git"]);
        assert_eq!(check.command_map["docker"], "podman");
        assert_eq!(record("~").unwrap(), None);
    }

    #[test]
    fn parse_structure_errors() {
        assert_eq!(
            problems("[a]"),
            [
                "s.host-commands.c: must be a check (a mapping with `action`, `reason` and what it matches), or null to remove it"
            ]
        );
        let unknown = problems("action: warn\nreason: why\nprograms: [sh]\n");
        assert!(
            unknown[0].contains("unknown field `programs`"),
            "{unknown:?}"
        );
        let missing = problems("action: warn\ncommands: [sh]\n");
        assert!(missing[0].contains("missing field `reason`"), "{missing:?}");
        let action = problems("action: deny\nreason: why\ncommands: [sh]\n");
        assert!(action[0].contains("unknown variant `deny`"), "{action:?}");
    }

    #[test]
    fn parse_field_errors() {
        assert_eq!(
            problems(
                "action: refuse\nreason: ' '\ncommands: [/bin/sh, -x, '', 'a b', ok]\ncommand-map: {ok: x, y: ''}\n"
            ),
            [
                "s.host-commands.c.reason: must explain the check; it's shown when the check matches",
                "s.host-commands.c.commands[0]: '/bin/sh' is a command name, not a path; map the name to a program with `command-map`",
                "s.host-commands.c.commands[1]: '-x' can't start with `-`",
                "s.host-commands.c.commands[2]: '' is not a command name",
                "s.host-commands.c.commands[3]: 'a b' must be ASCII, without spaces or control characters",
                "s.host-commands.c.command-map.ok: is also in `commands`; give each name once",
                "s.host-commands.c.command-map.y: must name the program the command runs",
            ]
        );
        assert_eq!(
            problems(
                "action: warn\nreason: '${x}'\nname-patterns: ['py[3']\noptions: [c, --eval=1, '${o}']\n"
            ),
            [
                "s.host-commands.c.reason: is used as written; `${...}` isn't expanded here",
                "s.host-commands.c.name-patterns[0]: 'py[3' has a `[` without its `]`",
                "s.host-commands.c.options[0]: must be an option, starting with `-`",
                "s.host-commands.c.options[1]: must be the option alone, without `=` and its value",
                "s.host-commands.c.options[2]: is used as written; `${...}` isn't expanded here",
            ]
        );
        assert_eq!(
            problems(
                "action: warn\nreason: r\nenv: ['A=B']\noverrides: [nocolon]\ncommands: [a]\n"
            ),
            [
                "s.host-commands.c.env[0]: must be an environment variable name: ASCII, without `=`, spaces or control characters",
                "s.host-commands.c.overrides[0]: must name a check as `section:key`",
            ]
        );
    }

    #[test]
    fn parse_combination_errors() {
        assert_eq!(
            problems("action: warn\nreason: r\n"),
            [
                "s.host-commands.c: needs something to match: `commands`, `command-map`, `name-patterns`, `options`, `env` or `paths`"
            ]
        );
        assert_eq!(
            problems("action: warn\nreason: r\npaths: [/a]\ncommands: [a]\n"),
            [
                "s.host-commands.c.paths: can't be combined with other matches; a check with `paths` matches every executable in them"
            ]
        );
        assert_eq!(
            problems("action: warn\nreason: r\noptions: [-c]\nenv: [A]\n"),
            ["s.host-commands.c: can't match both `options` and `env`; make them two checks"]
        );
        assert_eq!(
            problems("action: warn\nreason: r\noptions: [-c]\noverrides: ['a:b']\n"),
            [
                "s.host-commands.c.overrides: needs `commands` or `command-map`: an override names the commands it's for, exactly"
            ]
        );
        assert_eq!(
            problems(
                "action: warn\nreason: r\ncommands: [a]\nname-patterns: [b*]\nenv: [GIT_*]\noverrides: ['a:b']\n"
            ),
            [
                "s.host-commands.c.env[0]: must be an exact name in a check with `overrides`",
                "s.host-commands.c.name-patterns: can't be used with `overrides`; name the commands exactly",
            ]
        );
        // `options` and `env` alone apply to every command; `paths` alone is fine.
        assert!(record("action: warn\nreason: r\noptions: [-c]\n").is_ok());
        assert!(record("action: refuse\nreason: r\npaths: [~/shims]\n").is_ok());
    }

    #[test]
    fn patterns() {
        assert!(pattern_matches("python*", "python3.12"));
        assert!(pattern_matches("python*", "python"));
        assert!(!pattern_matches("python*", "ipython"));
        assert!(pattern_matches("?sh", "zsh"));
        assert!(!pattern_matches("?sh", "sh"));
        assert!(pattern_matches("perl[0-9]*", "perl5.36"));
        assert!(!pattern_matches("perl[0-9]*", "perldoc"));
        assert!(pattern_matches("[!a]b", "cb"));
        assert!(!pattern_matches("[!a]b", "ab"));
        assert!(pattern_matches("[]x]", "]"));
        // Case sensitive.
        assert!(!pattern_matches("python*", "Python"));
        assert!(pattern_is_closed("a[bc]d"));
        assert!(!pattern_is_closed("a[bc"));
    }

    #[test]
    fn command_names() {
        for good in ["aws", "aws-build", "python3.12", "a_b", "x.exe"] {
            assert_eq!(command_name_problem(good), None, "{good}");
        }
        let problem = |name: &str| command_name_problem(name).unwrap();
        assert!(problem("").contains("is not a command name"));
        assert!(problem(".").contains("is not a command name"));
        assert!(problem("..").contains("is not a command name"));
        assert!(problem("/usr/bin/aws").contains("is a path"));
        assert!(problem("bin\\aws").contains("is a path"));
        assert!(problem("-rf").contains("can't start with `-`"));
        assert!(problem("a b").contains("must be ASCII"));
        assert!(problem("caf\u{e9}").contains("must be ASCII"));
        assert!(problem("a\tb").contains("must be ASCII"));
        assert!(problem("C:aws").contains("can't have a `:`"));
        assert!(problem("aws:stream").contains("can't have a `:`"));
        for device in ["NUL", "nul", "con.txt", "COM1", "lpt9.exe", "AUX", "CONIN$"] {
            assert!(
                problem(device).contains("is a Windows device name"),
                "{device}"
            );
        }
        // Not devices: longer names, and COM without a digit.
        for good in ["console", "COM", "COM10", "nullable", "auxiliary"] {
            assert_eq!(command_name_problem(good), None, "{good}");
        }
    }

    #[test]
    fn action_order() {
        assert!(Action::Refuse > Action::Warn && Action::Warn > Action::Allow);
    }
}
