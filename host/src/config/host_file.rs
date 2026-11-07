//! The host file: where each host command's program is on this computer, for
//! one configuration (`host.json`, beside the configuration's `files/`).
//!
//! ```json
//! {
//!     "$schema": "surf-scoter-host-file-1",
//!     "programs": {
//!         "aws": {"path": "/usr/local/bin/aws", "real": "/usr/local/aws-cli/v2/current/bin/aws",
//!                 "origin": "resolved", "sha256": null}
//!     }
//! }
//! ```
//!
//! `programs` is keyed by the program as the project file names it: a
//! command's `program`, or its `command` without one.  Commands that run the
//! same program share its entry.  Each entry is the absolute
//! `path` to run, the `real` file it was when it was written (symlinks
//! resolved), and its `origin`: `resolved` by the tools, through the path
//! checks, or written by the developer (`user`), which an approval keeps.
//! `sha256`, when given, is the program's contents' hash.
//!
//! The generator reads the file it's given (`--host-file`), checks it
//! against the project's commands and this computer, and keeps it in the
//! universal configuration file's `host-file`.  `host-brokered` is then the
//! project's commands with each `program` the host file's `path`: nothing is
//! looked up on the `PATH` (BRIDGE-11).  `scoter-host` checks the programs
//! again when it starts (`check_for_run`).

use std::collections::BTreeMap;
use std::path::{Path, PathBuf};

use serde_json::{Map, Value as Json};
use sha2::{Digest, Sha256};

use super::project::ProjectConfig;
use super::read;
use super::system::SystemConfig;
use super::{ConfigError, child_key};

pub const SCHEMA: &str = "surf-scoter-host-file-1";
const KEYS: [&str; 2] = ["$schema", "programs"];
const ENTRY_KEYS: [&str; 4] = ["path", "real", "origin", "sha256"];
/// The largest program `check_program` hashes.
const MAX_HASHED_BYTES: u64 = 1024 * 1024 * 1024;

/// Where an entry's path came from.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Origin {
    /// Found by the tools, through the path checks.
    Resolved,
    /// Written by the developer.
    User,
}

/// One program's entry.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct HostProgram {
    pub path: PathBuf,
    pub real: PathBuf,
    pub origin: Origin,
    pub sha256: Option<String>,
}

/// The host file, checked.
#[derive(Debug, PartialEq)]
pub struct HostFile {
    /// The file it came from.
    pub file: PathBuf,
    /// The file's contents, as written.
    pub contents: Json,
    pub programs: BTreeMap<String, HostProgram>,
}

/// Read the host file `file`.
pub fn read(file: &Path) -> Result<HostFile, Vec<ConfigError>> {
    let text = read::read_regular_file_to(file, read::MAX_UNIVERSAL_FILE_BYTES)
        .map_err(|m| vec![ConfigError::new(Some(file), "", m)])?;
    let contents: Json = serde_json::from_str(&text).map_err(|e| {
        vec![ConfigError::new(
            Some(file),
            "",
            format!("not a host file: {e}"),
        )]
    })?;
    parse(contents, file)
}

/// Check host file `contents`, from `file`.
pub fn parse(contents: Json, file: &Path) -> Result<HostFile, Vec<ConfigError>> {
    let error = |key: &str, message: String| ConfigError::new(Some(file), key, message);
    let Json::Object(map) = &contents else {
        return Err(vec![error("", "must be a JSON object".to_string())]);
    };
    if map.get("$schema").and_then(Json::as_str) != Some(SCHEMA) {
        return Err(vec![error(
            "$schema",
            format!("must be `{SCHEMA}`; this isn't a host file this version reads"),
        )]);
    }
    let mut errors = Vec::new();
    for key in map.keys().filter(|k| !KEYS.contains(&k.as_str())) {
        errors.push(error(key, "unknown key".to_string()));
    }
    let mut programs = BTreeMap::new();
    match map.get("programs") {
        None | Some(Json::Null) => {}
        Some(Json::Object(entries)) => {
            for (name, entry) in entries {
                let at = child_key("programs", name);
                if name.is_empty() {
                    errors.push(error(
                        &at,
                        "must be the program as a command names it".to_string(),
                    ));
                    continue;
                }
                match program(entry, &at) {
                    Ok(found) => {
                        programs.insert(name.clone(), found);
                    }
                    Err(found) => errors.extend(found.into_iter().map(|(k, m)| error(&k, m))),
                }
            }
        }
        Some(_) => errors.push(error("programs", "must be a mapping".to_string())),
    }
    if errors.is_empty() {
        Ok(HostFile {
            file: file.to_path_buf(),
            contents,
            programs,
        })
    } else {
        Err(errors)
    }
}

/// One `programs` entry, at `at`.
fn program(entry: &Json, at: &str) -> Result<HostProgram, Vec<(String, String)>> {
    let Json::Object(map) = entry else {
        return Err(vec![(
            at.to_string(),
            "must be a mapping with `path`, `real` and `origin`".to_string(),
        )]);
    };
    let mut problems = Vec::new();
    for key in map.keys().filter(|k| !ENTRY_KEYS.contains(&k.as_str())) {
        problems.push((child_key(at, key), "unknown key".to_string()));
    }
    let mut absolute = |key: &str| match map.get(key).and_then(Json::as_str) {
        Some(p) if Path::new(p).is_absolute() => Some(PathBuf::from(p)),
        _ => {
            problems.push((child_key(at, key), "must be an absolute path".to_string()));
            None
        }
    };
    let path = absolute("path");
    let real = absolute("real");
    let origin = match map.get("origin").and_then(Json::as_str) {
        Some("resolved") => Some(Origin::Resolved),
        Some("user") => Some(Origin::User),
        _ => {
            problems.push((
                child_key(at, "origin"),
                "must be `resolved` or `user`".to_string(),
            ));
            None
        }
    };
    let sha256 = match map.get("sha256") {
        None | Some(Json::Null) => None,
        Some(Json::String(s)) if s.len() == 64 && s.bytes().all(|b| b.is_ascii_hexdigit()) => {
            Some(s.to_ascii_lowercase())
        }
        Some(_) => {
            problems.push((
                child_key(at, "sha256"),
                "must be a SHA-256, in hex, or null".to_string(),
            ));
            None
        }
    };
    match (path, real, origin) {
        (Some(path), Some(real), Some(origin)) if problems.is_empty() => Ok(HostProgram {
            path,
            real,
            origin,
            sha256,
        }),
        _ => Err(problems),
    }
}

impl HostFile {
    /// The checks when the generator is given it: it names exactly the
    /// project's programs, and each is where it says, outside the project.
    pub fn check(
        &self,
        system: &SystemConfig,
        project: Option<&ProjectConfig>,
    ) -> Vec<ConfigError> {
        let error = |key: &str, message: String| ConfigError::new(Some(&self.file), key, message);
        let mut errors = Vec::new();
        let wanted: Vec<String> = project
            .map(|p| {
                p.commands
                    .iter()
                    .map(|rule| rule.program_name().to_string())
                    .collect()
            })
            .unwrap_or_default();
        for name in &wanted {
            if !self.programs.contains_key(name) {
                errors.push(error(
                    "programs",
                    format!("has no `{name}`, which the configuration's commands run; approve the configuration again"),
                ));
            }
        }
        for (name, entry) in &self.programs {
            let key = child_key("programs", name);
            if !wanted.contains(name) {
                errors.push(error(
                    &key,
                    "isn't a program the configuration's commands run".to_string(),
                ));
                continue;
            }
            errors.extend(check_program(entry).into_iter().map(|m| error(&key, m)));
            for path in [&entry.path, &entry.real] {
                if let Some(location) = system.project_location(path) {
                    errors.push(error(
                        &key,
                        format!(
                            "{} is in the project (`{location}`); a host command never runs a program from the project",
                            path.display()
                        ),
                    ));
                    break;
                }
            }
        }
        errors
    }

    /// The program to run for a command whose program name is `name`.
    pub fn path_of(&self, name: &str) -> Option<&Path> {
        self.programs.get(name).map(|p| p.path.as_path())
    }

    /// The universal configuration file's `host-file`.
    pub fn to_universal(&self) -> Json {
        let mut map = Map::new();
        map.insert(
            "file".to_string(),
            Json::String(self.file.to_string_lossy().into_owned()),
        );
        map.insert("contents".to_string(), self.contents.clone());
        Json::Object(map)
    }
}

/// The problems with a program entry on this computer: it must still be an
/// executable, the same real file, with the same contents when there's a hash.
/// A missing or changed program is never looked for elsewhere.
pub fn check_program(entry: &HostProgram) -> Vec<String> {
    let path = &entry.path;
    if super::command::find_executable(path, None, None).is_none() {
        return vec![format!(
            "{} isn't an executable file any more; approve the configuration again",
            path.display()
        )];
    }
    let real = path.canonicalize().unwrap_or_else(|_| path.clone());
    if real != entry.real {
        return vec![format!(
            "{} is now {}, not {}; approve the configuration again",
            path.display(),
            real.display(),
            entry.real.display()
        )];
    }
    match (&entry.sha256, hash_file(&real)) {
        (None, _) => Vec::new(),
        (Some(want), Ok(found)) if *want == found => Vec::new(),
        (Some(_), Ok(_)) => vec![format!(
            "{} has changed since it was approved; approve the configuration again",
            path.display()
        )],
        (Some(_), Err(m)) => vec![format!("{}: {m}", path.display())],
    }
}

/// The SHA-256 of the file, in hex.
fn hash_file(path: &Path) -> Result<String, String> {
    use std::io::Read;
    let file = std::fs::File::open(path).map_err(|e| format!("can't be read: {e}"))?;
    let mut hasher = Sha256::new();
    let mut limited = file.take(MAX_HASHED_BYTES + 1);
    let mut buffer = vec![0u8; 64 * 1024];
    let mut total: u64 = 0;
    loop {
        let n = limited
            .read(&mut buffer)
            .map_err(|e| format!("can't be read: {e}"))?;
        if n == 0 {
            break;
        }
        total += n as u64;
        if total > MAX_HASHED_BYTES {
            return Err(format!("larger than the {MAX_HASHED_BYTES} bytes hashed"));
        }
        hasher.update(&buffer[..n]);
    }
    Ok(hasher
        .finalize()
        .iter()
        .map(|b| format!("{b:02x}"))
        .collect())
}

/// The checks `scoter-host` makes when it starts, before it runs anything:
/// the universal file decides what runs on the host, so it must be the
/// user's alone; and each program is still the one the host file names.
pub fn check_for_run(universal: &Path, host_file: Option<&HostFile>) -> Vec<ConfigError> {
    let mut errors = Vec::new();
    if let Err(m) = check_private(universal) {
        errors.push(ConfigError::new(Some(universal), "", m));
    }
    let Some(host_file) = host_file else {
        return errors;
    };
    errors.extend(host_file.programs.iter().flat_map(|(name, entry)| {
        let key = child_key("host-file.programs", name);
        check_program(entry)
            .into_iter()
            .map(move |m| ConfigError::new(Some(universal), &key, m))
    }));
    errors
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

#[cfg(test)]
mod tests {
    use super::*;

    fn contents(path: &str, real: &str) -> Json {
        serde_json::json!({
            "$schema": SCHEMA,
            "programs": {"aws": {"path": path, "real": real, "origin": "resolved", "sha256": null}},
        })
    }

    fn problems(json: Json) -> Vec<String> {
        parse(json, Path::new("/c/host.json"))
            .unwrap_err()
            .into_iter()
            .map(|e| format!("{}: {}", e.key.unwrap_or_default(), e.message))
            .collect()
    }

    #[test]
    fn parse_file() {
        let host = parse(contents("/bin/aws", "/opt/aws"), Path::new("/c/host.json")).unwrap();
        let aws = &host.programs["aws"];
        assert_eq!(aws.path, PathBuf::from("/bin/aws"));
        assert_eq!(aws.real, PathBuf::from("/opt/aws"));
        assert_eq!(aws.origin, Origin::Resolved);
        assert_eq!(host.path_of("aws"), Some(Path::new("/bin/aws")));
        assert_eq!(host.to_universal()["file"], "/c/host.json");
        assert_eq!(
            host.to_universal()["contents"],
            contents("/bin/aws", "/opt/aws")
        );
    }

    #[test]
    fn parse_errors() {
        let mut wrong = contents("/bin/aws", "/opt/aws");
        wrong["$schema"] = Json::String("other".to_string());
        assert_eq!(
            problems(wrong),
            [
                "$schema: must be `surf-scoter-host-file-1`; this isn't a host file this version reads"
            ]
        );
        let mut bad = contents("aws", "/opt/aws");
        bad["extra"] = Json::Bool(true);
        bad["programs"]["aws"]["origin"] = Json::String("project".to_string());
        bad["programs"]["aws"]["sha256"] = Json::String("abc".to_string());
        bad["programs"]["aws"]["more"] = Json::Bool(true);
        bad["programs"][""] = bad["programs"]["aws"].clone();
        assert_eq!(
            problems(bad),
            [
                "extra: unknown key",
                "programs: must be the program as a command names it",
                "programs.aws.more: unknown key",
                "programs.aws.path: must be an absolute path",
                "programs.aws.origin: must be `resolved` or `user`",
                "programs.aws.sha256: must be a SHA-256, in hex, or null",
            ]
        );
        assert_eq!(problems(Json::Array(vec![])), [": must be a JSON object"]);
    }

    #[cfg(unix)]
    #[test]
    fn programs_checked() {
        use std::os::unix::fs::PermissionsExt;
        let dir = crate::config::testing::TempDir::new();
        let program = dir.write("bin/tool", "#!/bin/sh\n");
        std::fs::set_permissions(&program, std::fs::Permissions::from_mode(0o755)).unwrap();
        let link = dir.path().join("bin/link");
        std::os::unix::fs::symlink(&program, &link).unwrap();
        let real = program.canonicalize().unwrap();
        let entry = |path: &Path, sha256: Option<String>| HostProgram {
            path: path.to_path_buf(),
            real: real.clone(),
            origin: Origin::User,
            sha256,
        };
        assert!(check_program(&entry(&program, None)).is_empty());
        assert!(check_program(&entry(&link, None)).is_empty());
        let hash = hash_file(&real).unwrap();
        assert!(check_program(&entry(&program, Some(hash.clone()))).is_empty());
        std::fs::write(&program, "#!/bin/sh\necho changed\n").unwrap();
        assert!(check_program(&entry(&program, Some(hash)))[0].contains("has changed"));
        // The link now points elsewhere: never followed to the new file.
        let other = dir.write("bin/other", "#!/bin/sh\n");
        std::fs::set_permissions(&other, std::fs::Permissions::from_mode(0o755)).unwrap();
        std::fs::remove_file(&link).unwrap();
        std::os::unix::fs::symlink(&other, &link).unwrap();
        assert!(check_program(&entry(&link, None))[0].contains("is now"));
        std::fs::remove_file(&program).unwrap();
        assert!(check_program(&entry(&program, None))[0].contains("isn't an executable"));
    }
}
