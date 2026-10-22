//! The accepted settings: what the user accepted for a project, resolved,
//! from the verified store (`accepted.json`).
//!
//! ```json
//! {
//!     "$schema": "surf-scoter-accepted-settings-1",
//!     "project-dir": "/home/user/mono/client",
//!     "expires-at": "2026-11-19T15:04:05+00:00",
//!     "vcs": "git",
//!     "gateway": null,
//!     "egress": [{"host": "registry.npmjs.org", "protocol": "tls", "port": 443, "methods": []}],
//!     "commands": [
//!         {"command": "aws", "program": "/usr/local/bin/aws", "args": ["s3", "ls"],
//!          "env": {"allow": [], "drop": [], "error": [], "set": {}, "default": {}}}
//!     ]
//! }
//! ```
//!
//! `commands` have the shape of the universal configuration file's
//! `host-brokered` entries: `program` is the absolute executable the review
//! resolved, and `env` the whole environment plan, with `@NAME@` left for
//! `scoter-host`.  So `host-brokered` is built from them, and only them.
//!
//! The generator reads the file it's given (`--accepted-settings`), checks it,
//! and keeps it in the universal configuration file's `accepted-settings`.
//! `scoter-host` checks, when it starts, that the store's file still says
//! the same, and hasn't expired (`check_for_run`).

use std::collections::BTreeMap;
use std::path::{Path, PathBuf};

use serde_json::{Map, Value as Json};

use super::egress::{self, EgressRule};
use super::environment::EnvPlan;
use super::project::{self, Commands};
use super::read;
use super::system::{SCOTER_SECTION, SystemConfig, VERIFICATION_SECTION, VERIFIED_STORE};
use super::{ConfigError, child_key, index_key};

pub const SCHEMA: &str = "surf-scoter-accepted-settings-1";
/// The setup section and `values` key naming the network gateway.
pub const GATEWAY_SECTION: &str = "dev-env";
pub const GATEWAY_NAME: &str = "network-gateway-name";
const KEYS: [&str; 9] = [
    "$schema",
    "project-dir",
    "expires-at",
    "vcs",
    "gateway",
    "egress",
    "commands",
    "dev-container",
    "overrides",
];

/// The accepted settings, checked.
#[derive(Debug, PartialEq)]
pub struct AcceptedSettings {
    /// The store's file they came from.
    pub file: PathBuf,
    /// The file's contents, as written.
    pub contents: Json,
    pub project_dir: PathBuf,
    /// When they expire: seconds since the Unix epoch.
    pub expires_at: i64,
    pub vcs: Option<String>,
    pub gateway: Option<String>,
    pub egress: Vec<EgressRule>,
    pub commands: Commands,
    /// Each of `commands` as written.
    pub raw_commands: Vec<Json>,
    /// The dev container the user accepted: the project's (`None`), or one of
    /// theirs, by name.
    pub container_name: Option<String>,
    /// Each image the build starts from, and what to build from instead.
    pub images: BTreeMap<String, String>,
    /// The setup checks the user overrode, by command index and by egress
    /// rule index.
    pub command_overrides: BTreeMap<usize, Vec<String>>,
    pub egress_overrides: BTreeMap<usize, Vec<String>>,
}

/// Read the accepted settings from the store's `file`.
pub fn read(file: &Path) -> Result<AcceptedSettings, Vec<ConfigError>> {
    let text = read::read_regular_file_to(file, read::MAX_UNIVERSAL_FILE_BYTES)
        .map_err(|m| vec![ConfigError::new(Some(file), "", m)])?;
    let contents: Json = serde_json::from_str(&text).map_err(|e| {
        vec![ConfigError::new(
            Some(file),
            "",
            format!("not an accepted settings file: {e}"),
        )]
    })?;
    parse(contents, file)
}

/// Check accepted settings `contents`, from the store's `file`.
pub fn parse(contents: Json, file: &Path) -> Result<AcceptedSettings, Vec<ConfigError>> {
    let error = |key: &str, message: String| ConfigError::new(Some(file), key, message);
    let Json::Object(map) = &contents else {
        return Err(vec![error("", "must be a JSON object".to_string())]);
    };
    if map.get("$schema").and_then(Json::as_str) != Some(SCHEMA) {
        return Err(vec![error(
            "$schema",
            format!("must be `{SCHEMA}`; this isn't an accepted settings file this version reads"),
        )]);
    }
    let mut errors = Vec::new();
    for key in map.keys().filter(|k| !KEYS.contains(&k.as_str())) {
        errors.push(error(key, "unknown key".to_string()));
    }
    let text = |key: &str, errors: &mut Vec<ConfigError>| -> Option<String> {
        match map.get(key) {
            Some(Json::String(s)) if !s.is_empty() => Some(s.clone()),
            _ => {
                errors.push(error(key, "must be text".to_string()));
                None
            }
        }
    };
    let optional_text = |key: &str, errors: &mut Vec<ConfigError>| -> Option<String> {
        match map.get(key) {
            None | Some(Json::Null) => None,
            Some(Json::String(s)) if !s.is_empty() => Some(s.clone()),
            _ => {
                errors.push(error(key, "must be text or null".to_string()));
                None
            }
        }
    };
    let project_dir = text("project-dir", &mut errors).map(PathBuf::from);
    if let Some(dir) = &project_dir
        && !dir.is_absolute()
    {
        errors.push(error("project-dir", "must be an absolute path".to_string()));
    }
    let expires_at = text("expires-at", &mut errors).and_then(|t| {
        parse_time(&t)
            .map_err(|m| errors.push(error("expires-at", m)))
            .ok()
    });
    let vcs = optional_text("vcs", &mut errors);
    let gateway = optional_text("gateway", &mut errors);

    let as_yaml = |key: &str| {
        let value = map.get(key).cloned().unwrap_or(Json::Null);
        read::parse_yaml(&value.to_string())
    };
    let mut problems = Vec::new();
    let egress = match as_yaml("egress") {
        Ok(value) => egress::parse(value, &mut problems),
        Err(m) => {
            problems.push(("egress".to_string(), m));
            Vec::new()
        }
    };
    let raw_commands: Vec<Json> = match map.get("commands") {
        Some(Json::Array(items)) => items.clone(),
        None | Some(Json::Null) => Vec::new(),
        Some(_) => {
            problems.push(("commands".to_string(), "must be a list".to_string()));
            Vec::new()
        }
    };
    let mut commands = Commands::new();
    match read::parse_yaml(&Json::Array(raw_commands.clone()).to_string())
        .map_err(|m| vec![("commands".to_string(), m)])
        .and_then(|value| project::parse_command_list(value, "commands"))
    {
        Ok((mut parsed, _)) => {
            for (i, rule) in parsed.iter_mut().enumerate() {
                let key = index_key("commands", i);
                match rule.program.as_deref() {
                    Some(program) if Path::new(program).is_absolute() => {}
                    _ => problems.push((
                        child_key(&key, "program"),
                        "must be the absolute path of the program the review resolved".to_string(),
                    )),
                }
                rule.env_plan = EnvPlan::as_written(&rule.env);
            }
            commands = parsed;
        }
        Err(found) => problems.extend(found),
    }
    let (container_name, images) = dev_container(map.get("dev-container"), &mut problems);
    let command_overrides = overrides(map, "commands", commands.len(), &mut problems);
    let egress_overrides = overrides(map, "egress", egress.len(), &mut problems);
    errors.extend(
        problems
            .into_iter()
            .map(|(key, message)| error(&key, message)),
    );
    match (project_dir, expires_at) {
        (Some(project_dir), Some(expires_at)) if errors.is_empty() => Ok(AcceptedSettings {
            file: file.to_path_buf(),
            contents,
            project_dir,
            expires_at,
            vcs,
            gateway,
            egress,
            commands,
            raw_commands,
            container_name,
            images,
            command_overrides,
            egress_overrides,
        }),
        _ => Err(errors),
    }
}

/// `dev-container`: `use` (`"project"`, or `{"name": ...}`) and `images`.
fn dev_container(
    value: Option<&Json>,
    problems: &mut Vec<(String, String)>,
) -> (Option<String>, BTreeMap<String, String>) {
    let mut images = BTreeMap::new();
    let Some(value) = value.filter(|v| !v.is_null()) else {
        return (None, images);
    };
    let mut bad = |key: &str, message: &str| {
        problems.push((child_key("dev-container", key), message.to_string()));
    };
    let name = match value.get("use") {
        None | Some(Json::Null) => None,
        Some(Json::String(s)) if s == "project" => None,
        Some(Json::Object(choice)) => match choice.get("name").and_then(Json::as_str) {
            Some(name) if choice.len() == 1 && super::project::valid_name(name) => {
                Some(name.to_string())
            }
            _ => {
                bad(
                    "use",
                    "must be {\"name\": NAME}, with a single directory name",
                );
                None
            }
        },
        Some(_) => {
            bad("use", "must be \"project\", or {\"name\": NAME}");
            None
        }
    };
    match value.get("images") {
        None | Some(Json::Null) => {}
        Some(Json::Object(map)) => {
            for (from, to) in map {
                match to.as_str() {
                    Some(to) if !to.is_empty() => {
                        images.insert(from.clone(), to.to_string());
                    }
                    _ => bad(&format!("images.{from}"), "must be an image"),
                }
            }
        }
        Some(_) => bad("images", "must be a mapping"),
    }
    (name, images)
}

/// `overrides.LIST`: `[{"index": N, "checks": [...]}]`, each index in `0..len`.
fn overrides(
    map: &Map<String, Json>,
    list: &str,
    len: usize,
    problems: &mut Vec<(String, String)>,
) -> BTreeMap<usize, Vec<String>> {
    let key = format!("overrides.{list}");
    let mut found = BTreeMap::new();
    let entries = match map.get("overrides").and_then(|o| o.get(list)) {
        None | Some(Json::Null) => return found,
        Some(Json::Array(entries)) => entries,
        Some(_) => {
            problems.push((key, "must be a list".to_string()));
            return found;
        }
    };
    for (i, entry) in entries.iter().enumerate() {
        let index = entry
            .get("index")
            .and_then(Json::as_u64)
            .and_then(|n| usize::try_from(n).ok());
        let checks: Option<Vec<String>> = entry.get("checks").and_then(Json::as_array).map(|c| {
            c.iter()
                .filter_map(Json::as_str)
                .map(str::to_string)
                .collect()
        });
        match (index, checks) {
            (Some(index), Some(checks)) if index < len && !checks.is_empty() => {
                found.insert(index, checks);
            }
            _ => problems.push((
                index_key(&key, i),
                format!(
                    "must be {{\"index\": N, \"checks\": [...]}}, for one of the {len} entries"
                ),
            )),
        }
    }
    found
}

impl AcceptedSettings {
    /// The checks when the generator is given them: they're for this run's
    /// project, and each program is an executable outside the project.
    pub fn check(&self, system: &SystemConfig) -> Vec<ConfigError> {
        let error = |key: &str, message: String| ConfigError::new(Some(&self.file), key, message);
        let mut errors = Vec::new();
        let project_dir = system.get_path(SCOTER_SECTION, super::system::PROJECT_DIR);
        if project_dir.as_deref().map(canonical) != Some(canonical(&self.project_dir)) {
            errors.push(error(
                "project-dir",
                format!(
                    "is {}, not this run's project ({}); the settings were accepted for another project",
                    self.project_dir.display(),
                    project_dir.map_or_else(|| "none".to_string(), |p| p.display().to_string())
                ),
            ));
        }
        // The egress rules were accepted for one gateway; another enforces
        // them differently.
        let setup_gateway = system.get(GATEWAY_SECTION, GATEWAY_NAME);
        if let Some(accepted) = &self.gateway
            && setup_gateway.as_deref() != Some(accepted.as_str())
        {
            errors.push(error(
                "gateway",
                format!(
                    "the egress rules were accepted for the `{accepted}` gateway, but this setup's is `{}`; review the project again",
                    setup_gateway.as_deref().unwrap_or("none")
                ),
            ));
        }
        for (i, rule) in self.commands.iter().enumerate() {
            let key = child_key(&index_key("commands", i), "program");
            let Some(program) = rule.program.as_deref().map(Path::new) else {
                continue;
            };
            if super::command::find_executable(program, None, None).is_none() {
                errors.push(error(
                    &key,
                    format!(
                        "{} isn't an executable file any more; review the project again",
                        program.display()
                    ),
                ));
            } else if let Some(location) = system.project_location(program) {
                errors.push(error(
                    &key,
                    format!(
                        "{} is in the project (`{location}`); a host command never runs a program from the project",
                        program.display()
                    ),
                ));
            }
        }
        errors
    }

    /// The universal configuration file's `accepted-settings`.
    pub fn to_universal(&self) -> Json {
        let mut map = Map::new();
        map.insert(
            "file".to_string(),
            Json::String(self.file.to_string_lossy().into_owned()),
        );
        map.insert("contents".to_string(), self.contents.clone());
        Json::Object(map)
    }

    /// The universal configuration file's `egress`.
    pub fn egress_universal(&self) -> Json {
        let rules: Vec<Json> = match self.contents.get("egress") {
            Some(Json::Array(rules)) => rules.clone(),
            _ => Vec::new(),
        };
        let mut map = Map::new();
        map.insert(
            "gateway".to_string(),
            self.gateway.clone().map_or(Json::Null, Json::String),
        );
        map.insert("rules".to_string(), Json::Array(rules));
        Json::Object(map)
    }
}

/// The checks `scoter-host` makes when it starts, before it runs anything.
///
/// The universal file (`universal`) decides what runs on the host, so it
/// must be the user's alone.  Its accepted settings must still be the
/// store's: a revoked, replaced or edited acceptance no longer matches.
/// They mustn't have expired by now (`now`, in seconds since the Unix
/// epoch); a session running when they expire carries on.  And the project
/// and dev container must still be where it says.
pub fn check_for_run(
    universal: &Path,
    accepted: Option<&AcceptedSettings>,
    system: &SystemConfig,
    now: i64,
) -> Vec<ConfigError> {
    let mut errors = Vec::new();
    if let Err(m) = check_private(universal) {
        errors.push(ConfigError::new(Some(universal), "", m));
    }
    let Some(accepted) = accepted else {
        return errors;
    };
    let error = |key: &str, message: String| ConfigError::new(Some(universal), key, message);
    let at = "accepted-settings";
    match system.get_path(VERIFICATION_SECTION, VERIFIED_STORE) {
        Some(verified) if canonical(&accepted.file).starts_with(canonical(&verified)) => {
            match check_private(&accepted.file).and_then(|()| {
                let text =
                    read::read_regular_file_to(&accepted.file, read::MAX_UNIVERSAL_FILE_BYTES)?;
                serde_json::from_str::<Json>(&text).map_err(|e| e.to_string())
            }) {
                Ok(stored) if stored == accepted.contents => {}
                Ok(_) => errors.push(error(
                    at,
                    format!(
                        "{} no longer says what this file does: the acceptance was revoked or changed; run `scoter` again",
                        accepted.file.display()
                    ),
                )),
                Err(m) => errors.push(error(
                    at,
                    format!("{}: {m}; run `scoter` again", accepted.file.display()),
                )),
            }
        }
        _ => errors.push(error(
            &child_key(at, "file"),
            format!(
                "{} isn't in the verified store (`{VERIFICATION_SECTION}:{VERIFIED_STORE}`)",
                accepted.file.display()
            ),
        )),
    }
    if accepted.expires_at <= now {
        errors.push(error(
            at,
            "the acceptance has expired; review the project again".to_string(),
        ));
    }
    let location = |key| system.get_path(SCOTER_SECTION, key);
    match location(super::system::PROJECT_DIR) {
        Some(dir) if dir.is_dir() && canonical(&dir) == canonical(&accepted.project_dir) => {}
        _ => errors.push(error(
            "active-config-locations.project-dir",
            format!(
                "isn't the accepted project directory, {}, or it's gone",
                accepted.project_dir.display()
            ),
        )),
    }
    if !location(super::system::DEV_CONTAINER_PATH).is_some_and(|dir| dir.is_dir()) {
        errors.push(error(
            "active-config-locations.dev-container-path",
            "the dev container directory is gone".to_string(),
        ));
    }
    errors
}

/// The path with symlinks resolved, where it exists.
fn canonical(path: &Path) -> PathBuf {
    path.canonicalize().unwrap_or_else(|_| path.to_path_buf())
}

/// Refuse a file another user owns, or that others can use.
#[cfg(unix)]
fn check_private(path: &Path) -> Result<(), String> {
    use std::os::unix::fs::MetadataExt;
    let metadata = path
        .symlink_metadata()
        .map_err(|e| format!("cannot read the file: {e}"))?;
    if !metadata.is_file() {
        return Err("not a regular file".to_string());
    }
    // SAFETY: getuid has no preconditions and can't fail.
    let uid = unsafe { libc::getuid() };
    if metadata.uid() != uid {
        return Err("belongs to another user".to_string());
    }
    if metadata.mode() & 0o077 != 0 {
        return Err(format!(
            "others can use it (mode {:o}); it must be private to you",
            metadata.mode() & 0o777
        ));
    }
    Ok(())
}

/// Windows has no mode bits; the user profile's ACLs protect the files.
#[cfg(not(unix))]
fn check_private(path: &Path) -> Result<(), String> {
    match path.symlink_metadata() {
        Ok(m) if m.is_file() => Ok(()),
        Ok(_) => Err("not a regular file".to_string()),
        Err(e) => Err(format!("cannot read the file: {e}")),
    }
}

/// An example time, as `parse_time` takes it, for messages.
pub const TIME_EXAMPLE: &str = "2026-11-19T15:04:05+00:00";

const SECONDS_PER_MINUTE: i64 = 60;
const SECONDS_PER_HOUR: i64 = 60 * SECONDS_PER_MINUTE;
const SECONDS_PER_DAY: i64 = 24 * SECONDS_PER_HOUR;

/// An RFC 3339 time with seconds and an offset (`2026-11-19T15:04:05+00:00`,
/// or `...Z`), as seconds since the Unix epoch.
pub fn parse_time(text: &str) -> Result<i64, String> {
    let bad = || format!("'{text}' must be a time such as {TIME_EXAMPLE}");
    let bytes = text.as_bytes();
    if bytes.len() < 20 || bytes[4] != b'-' || bytes[7] != b'-' || bytes[10] != b'T' {
        return Err(bad());
    }
    let number = |range: std::ops::Range<usize>| -> Result<i64, String> {
        let part = text.get(range).ok_or_else(bad)?;
        if part.bytes().all(|b| b.is_ascii_digit()) {
            part.parse().map_err(|_| bad())
        } else {
            Err(bad())
        }
    };
    let (year, month, day) = (number(0..4)?, number(5..7)?, number(8..10)?);
    if bytes[13] != b':' || bytes[16] != b':' {
        return Err(bad());
    }
    let (hour, minute, second) = (number(11..13)?, number(14..16)?, number(17..19)?);
    // Fractions of a second don't matter here.
    let mut rest = &text[19..];
    if let Some(fraction) = rest.strip_prefix('.') {
        let digits = fraction.bytes().take_while(u8::is_ascii_digit).count();
        if digits == 0 {
            return Err(bad());
        }
        rest = &fraction[digits..];
    }
    let offset = match rest {
        "Z" => 0,
        _ => {
            let sign = match rest.as_bytes().first() {
                Some(b'+') => 1,
                Some(b'-') => -1,
                _ => return Err(bad()),
            };
            let r = rest.as_bytes();
            if rest.len() != 6 || r[3] != b':' {
                return Err(bad());
            }
            let hours: i64 = rest[1..3].parse().map_err(|_| bad())?;
            let minutes: i64 = rest[4..6].parse().map_err(|_| bad())?;
            sign * (hours * SECONDS_PER_HOUR + minutes * SECONDS_PER_MINUTE)
        }
    };
    let days_in_month = match month {
        1 | 3 | 5 | 7 | 8 | 10 | 12 => 31,
        4 | 6 | 9 | 11 => 30,
        2 if year % 4 == 0 && (year % 100 != 0 || year % 400 == 0) => 29,
        2 => 28,
        _ => return Err(bad()),
    };
    if day < 1 || day > days_in_month || hour > 23 || minute > 59 || second > 60 {
        return Err(bad());
    }
    Ok(days_from_civil(year, month, day) * SECONDS_PER_DAY
        + hour * SECONDS_PER_HOUR
        + minute * SECONDS_PER_MINUTE
        + second
        - offset)
}

/// Days since 1970-01-01 for a date (proleptic Gregorian).
fn days_from_civil(year: i64, month: i64, day: i64) -> i64 {
    let y = if month <= 2 { year - 1 } else { year };
    let era = y.div_euclid(400);
    let yoe = y - era * 400;
    let mp = (month + 9) % 12;
    let doy = (153 * mp + 2) / 5 + day - 1;
    let doe = yoe * 365 + yoe / 4 - yoe / 100 + doy;
    era * 146_097 + doe - 719_468
}

/// Now, in seconds since the Unix epoch.
pub fn now() -> i64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map_or(0, |d| d.as_secs() as i64)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn contents(extra: &str) -> Json {
        serde_json::from_str(&format!(
            r#"{{"$schema": "{SCHEMA}", "project-dir": "/p", "expires-at": "2026-11-19T15:04:05+00:00",
                "vcs": "git", "gateway": null,
                "egress": [{{"host": "registry.npmjs.org", "protocol": "tls", "port": 443, "methods": []}}],
                "commands": [{{"command": "aws", "program": "/usr/local/bin/aws", "args": ["s3", "ls"],
                    "env": {{"allow": [], "drop": [], "error": [], "set": {{"HOME": "@HOME@"}}, "default": {{}}}}}}]
                {extra}}}"#
        ))
        .unwrap()
    }

    fn problems(json: Json) -> Vec<String> {
        parse(json, Path::new("/s/accepted.json"))
            .unwrap_err()
            .into_iter()
            .map(|e| format!("{}: {}", e.key.unwrap_or_default(), e.message))
            .collect()
    }

    #[test]
    fn parse_settings() {
        let accepted = parse(contents(""), Path::new("/s/accepted.json")).unwrap();
        assert_eq!(accepted.project_dir, PathBuf::from("/p"));
        assert_eq!(
            accepted.expires_at,
            parse_time("2026-11-19T15:04:05Z").unwrap()
        );
        assert_eq!(accepted.vcs.as_deref(), Some("git"));
        assert_eq!(accepted.gateway, None);
        assert_eq!(accepted.egress.len(), 1);
        assert_eq!(accepted.commands.len(), 1);
        assert_eq!(accepted.commands[0].env_plan.set["HOME"], "@HOME@");
        assert_eq!(accepted.raw_commands.len(), 1);
        let egress = accepted.egress_universal();
        assert_eq!(egress["gateway"], Json::Null);
        assert_eq!(egress["rules"][0]["host"], "registry.npmjs.org");
        assert_eq!(accepted.to_universal()["file"], "/s/accepted.json");
    }

    #[test]
    fn parse_errors() {
        let mut wrong = contents("");
        wrong["$schema"] = Json::String("other".to_string());
        assert_eq!(
            problems(wrong),
            [
                "$schema: must be `surf-scoter-accepted-settings-1`; this isn't an accepted settings file this version reads"
            ]
        );
        let mut bad = contents(r#", "extra": 1"#);
        bad["project-dir"] = Json::String("relative".to_string());
        bad["expires-at"] = Json::String("tomorrow".to_string());
        bad["commands"][0]["program"] = Json::String("aws".to_string());
        bad["egress"][0]["methods"] = serde_json::json!(["GET"]);
        assert_eq!(
            problems(bad),
            [
                "extra: unknown key",
                "project-dir: must be an absolute path",
                "expires-at: 'tomorrow' must be a time such as 2026-11-19T15:04:05+00:00",
                "egress[0].methods: only goes with `protocol: http` or `https`: the gateway can't see the methods inside `tls`",
                "commands[0].program: must be the absolute path of the program the review resolved",
            ]
        );
        assert_eq!(problems(Json::Array(vec![])), [": must be a JSON object"]);
    }

    #[test]
    fn times() {
        assert_eq!(parse_time("1970-01-01T00:00:00Z"), Ok(0));
        assert_eq!(parse_time("1970-01-02T00:00:00+00:00"), Ok(86_400));
        assert_eq!(parse_time("1970-01-01T01:00:00+01:00"), Ok(0));
        assert_eq!(parse_time("1969-12-31T23:00:00-01:00"), Ok(0));
        assert_eq!(parse_time("2000-03-01T00:00:00.123Z"), Ok(951_868_800));
        assert_eq!(parse_time("2024-02-29T12:00:00Z"), Ok(1_709_208_000));
        for bad in [
            "2023-02-29T00:00:00Z",
            "2026-13-01T00:00:00Z",
            "2026-11-19 15:04:05Z",
            "2026-11-19T15:04:05",
            "2026-11-19T25:00:00Z",
            "2026-11-19T15:04:05+0000",
            "x",
        ] {
            assert!(parse_time(bad).is_err(), "{bad}");
        }
    }
}
