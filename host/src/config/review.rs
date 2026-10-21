//! The review file: the user's answer to a project's suggestion, as they
//! left it in their editor.  See step 12 of `docs/contributors/projects-v1.md`.
//!
//! ```yaml
//! review: "surf-scoter-review-1"
//! # result: reject
//! project-dir: "/home/user/mono/client"
//! expires-at: "2026-11-19T15:04:05+00:00"
//! egress:
//!   - {host: "registry.npmjs.org", protocol: "tls", port: 443}
//! env:
//!   set: {"HOME": "@HOME@"}
//! commands:
//!   - command: "aws"
//!     program: "/usr/local/bin/aws"
//!     args: ["s3", "ls"]
//!     $comment: "A note the user keeps."
//!     $override: ["host-bridge:shells"]
//! dev-container:
//!   use: "project"
//!   images: {"debian:bookworm": "debian@sha256:..."}
//! ```
//!
//! It's parsed as the project file is (step 7's rules: no tags, merge keys,
//! duplicate keys or control characters), and its `egress`, `env` and
//! `commands` are checked as the project file's are, with these differences:
//! each `program` is the absolute executable the user accepts; values are
//! as accepted (a `${...}` isn't expanded, and is refused, so nothing is
//! worked out again); and entries may carry `$comment` (a note the user
//! keeps) and `$override` (the setup checks the user sets aside for this
//! item).  `result: reject` says no, whatever else the file says.
//!
//! From it comes the accepted settings it implies, in `accepted.json`'s
//! shape, with each command's whole environment plan.  The checks library
//! (Python) judges them before anything is stored; this only reads them.

use std::collections::BTreeMap;
use std::path::{Path, PathBuf};

use serde_json::{Map, Value as Json};
use yaml_serde::{Mapping, Value};

use super::accepted::{self, SCHEMA};
use super::egress;
use super::environment::{EnvPlan, EnvRules};
use super::host_commands::command_name_problem;
use super::project::{self, Commands, valid_name};
use super::read::{parse_yaml, read_regular_file, tag_errors};
use super::{ConfigError, child_key, index_key};

/// The review file format this version reads and writes.
pub const FORMAT: &str = "surf-scoter-review-1";

const COMMENT: &str = "$comment";
const OVERRIDE: &str = "$override";
const KEYS: [&str; 9] = [
    "review",
    "result",
    "project-dir",
    "expires-at",
    "egress",
    "env",
    "commands",
    "dev-container",
    COMMENT,
];

/// Which dev container the user accepts.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ContainerUse {
    /// The project's own.
    Project,
    /// One of the user's, found by name in `dev-containers-search`.
    Name(String),
}

/// A review file, read.
#[derive(Debug, PartialEq)]
pub struct Review {
    pub file: PathBuf,
    /// `result: reject`: the user said no.  Nothing else is read.
    pub rejected: bool,
    pub project_dir: PathBuf,
    pub expires_at: i64,
    pub expires_text: String,
    /// Each egress rule, as written (without its `$` keys), its port worked out.
    pub egress: Vec<Json>,
    pub commands: Commands,
    /// Each command in `accepted.json`'s shape: `program`, and `env` as the
    /// whole plan.
    pub accepted_commands: Vec<Json>,
    /// Per command, and per egress rule: the checks the user overrides.
    pub command_overrides: BTreeMap<usize, Vec<String>>,
    pub egress_overrides: BTreeMap<usize, Vec<String>>,
    pub container_use: ContainerUse,
    /// Each image the build starts from, and what to build from instead.
    pub images: BTreeMap<String, String>,
}

/// Read the review file at `path`.
pub fn read(path: &Path) -> Result<Review, Vec<ConfigError>> {
    let text = read_regular_file(path).map_err(|m| vec![ConfigError::new(Some(path), "", m)])?;
    parse(&text, path)
}

/// Parse review file text; `path` is where it came from.
pub fn parse(text: &str, path: &Path) -> Result<Review, Vec<ConfigError>> {
    let error = |key: &str, message: String| ConfigError::new(Some(path), key, message);
    let errors_of = |problems: Vec<(String, String)>| {
        problems
            .into_iter()
            .map(|(key, message)| error(&key, message))
            .collect::<Vec<_>>()
    };
    let value = parse_yaml(text).map_err(|m| vec![error("", m)])?;
    let tags = tag_errors(&value);
    if !tags.is_empty() {
        return Err(errors_of(tags));
    }
    let mut problems = Vec::new();
    project::text_problems(&value, "", &mut problems);
    let Value::Mapping(mut map) = value else {
        return Err(vec![error("", "must be a mapping".to_string())]);
    };
    match map.get("review").and_then(Value::as_str) {
        Some(FORMAT) => {}
        _ => {
            return Err(vec![error(
                "review",
                format!("must be \"{FORMAT}\"; this isn't a review file this version reads"),
            )]);
        }
    }
    // The user said no: nothing else matters.
    match map.get("result") {
        None | Some(Value::Null) => {}
        Some(Value::String(r)) if r == "reject" => {
            return Ok(Review {
                file: path.to_path_buf(),
                rejected: true,
                project_dir: PathBuf::new(),
                expires_at: 0,
                expires_text: String::new(),
                egress: Vec::new(),
                commands: Commands::new(),
                accepted_commands: Vec::new(),
                command_overrides: BTreeMap::new(),
                egress_overrides: BTreeMap::new(),
                container_use: ContainerUse::Project,
                images: BTreeMap::new(),
            });
        }
        Some(_) => problems.push((
            "result".to_string(),
            "can only be `reject` (leave it commented out to accept)".to_string(),
        )),
    }
    for key in map.keys() {
        match key.as_str() {
            Some(k) if KEYS.contains(&k) => {}
            _ => problems.push((
                key.as_str()
                    .map_or_else(|| format!("{key:?}"), str::to_string),
                "unknown key".to_string(),
            )),
        }
    }
    if let Some(comment) = map.get(COMMENT) {
        comment_problem(comment, COMMENT, &mut problems);
    }

    let project_dir = match map.get("project-dir") {
        Some(Value::String(dir)) if Path::new(dir).is_absolute() => Some(PathBuf::from(dir)),
        _ => {
            problems.push((
                "project-dir".to_string(),
                "must be the project directory's absolute path, as the review gave it".to_string(),
            ));
            None
        }
    };
    let expires = match map.get("expires-at") {
        Some(Value::String(text)) => match accepted::parse_time(text) {
            Ok(t) => Some((t, text.clone())),
            Err(m) => {
                problems.push(("expires-at".to_string(), m));
                None
            }
        },
        _ => {
            problems.push((
                "expires-at".to_string(),
                "must be a time such as 2026-11-19T15:04:05+00:00".to_string(),
            ));
            None
        }
    };

    // Egress: the `$` keys come off, and the rules are checked as the project's are.
    let (egress_entries, egress_overrides) =
        strip_entries(map.remove("egress"), "egress", &mut problems);
    let rules = egress::parse(Value::Sequence(egress_entries), &mut problems);
    let egress: Vec<Json> = rules
        .iter()
        .map(|rule| {
            serde_json::json!({
                "host": rule.host,
                "protocol": match rule.protocol {
                    egress::Protocol::Tls => "tls",
                    egress::Protocol::Http => "http",
                    egress::Protocol::Https => "https",
                },
                "port": rule.port,
                "methods": rule.methods,
            })
        })
        .collect();

    let env = project::parse_env(map.remove("env").unwrap_or(Value::Null), &mut problems);
    frozen_problems(&env, "env", &mut problems);

    let (command_entries, command_overrides) =
        strip_entries(map.remove("commands"), "commands", &mut problems);
    let mut commands = Commands::new();
    let mut accepted_commands = Vec::new();
    match project::parse_command_list(Value::Sequence(command_entries.clone()), "commands") {
        Ok((mut parsed, raw)) => {
            let common = EnvPlan::as_written(&env);
            for (i, (rule, entry)) in parsed.iter_mut().zip(&raw).enumerate() {
                let at = index_key("commands", i);
                if let Some(problem) = command_name_problem(&rule.command) {
                    problems.push((child_key(&at, "command"), problem));
                }
                match rule.program.as_deref() {
                    Some(program) if Path::new(program).is_absolute() => {}
                    _ => problems.push((
                        child_key(&at, "program"),
                        "must be the absolute path of the program to run".to_string(),
                    )),
                }
                frozen_problems(&rule.env, &child_key(&at, "env"), &mut problems);
                rule.env_plan = common.merged(&EnvPlan::as_written(&rule.env));
                accepted_commands.push(accepted_command(entry, &rule.env_plan));
            }
            commands = parsed;
        }
        Err(found) => problems.extend(found),
    }

    let (container_use, images) = dev_container(
        map.remove("dev-container").unwrap_or(Value::Null),
        &mut problems,
    );

    match (project_dir, expires) {
        (Some(project_dir), Some((expires_at, expires_text))) if problems.is_empty() => {
            Ok(Review {
                file: path.to_path_buf(),
                rejected: false,
                project_dir,
                expires_at,
                expires_text,
                egress,
                commands,
                accepted_commands,
                command_overrides,
                egress_overrides,
                container_use,
                images,
            })
        }
        _ => Err(errors_of(problems)),
    }
}

fn comment_problem(value: &Value, key: &str, problems: &mut Vec<(String, String)>) {
    if !matches!(value, Value::String(_)) {
        problems.push((key.to_string(), "must be text".to_string()));
    }
}

/// The entries of a list (`egress` or `commands`), without their `$comment`
/// and `$override` keys, and each entry's overrides by its index.
fn strip_entries(
    value: Option<Value>,
    list_key: &str,
    problems: &mut Vec<(String, String)>,
) -> (Vec<Value>, BTreeMap<usize, Vec<String>>) {
    let entries = match value {
        None | Some(Value::Null) => return (Vec::new(), BTreeMap::new()),
        Some(Value::Sequence(entries)) => entries,
        Some(_) => {
            problems.push((list_key.to_string(), "must be a list".to_string()));
            return (Vec::new(), BTreeMap::new());
        }
    };
    let mut overrides = BTreeMap::new();
    let stripped = entries
        .into_iter()
        .enumerate()
        .map(|(i, entry)| {
            let Value::Mapping(mut map) = entry else {
                return entry;
            };
            let at = index_key(list_key, i);
            if let Some(comment) = map.remove(COMMENT) {
                comment_problem(&comment, &child_key(&at, COMMENT), problems);
            }
            if let Some(names) = map.remove(OVERRIDE)
                && let Some(names) = override_names(&names, &child_key(&at, OVERRIDE), problems)
            {
                overrides.insert(i, names);
            }
            Value::Mapping(map)
        })
        .collect();
    (stripped, overrides)
}

/// An `$override`'s check names: a non-empty list of `section:key`.
fn override_names(
    value: &Value,
    key: &str,
    problems: &mut Vec<(String, String)>,
) -> Option<Vec<String>> {
    let names: Option<Vec<String>> = match value {
        Value::Sequence(items) if !items.is_empty() => items
            .iter()
            .map(|item| match item {
                Value::String(name)
                    if name.split_once(':').is_some_and(|(section, check)| {
                        !section.is_empty() && !check.is_empty() && !check.contains(':')
                    }) =>
                {
                    Some(name.clone())
                }
                _ => None,
            })
            .collect(),
        _ => None,
    };
    if names.is_none() {
        problems.push((
            key.to_string(),
            "must list the setup checks it overrides, as `section:key`".to_string(),
        ));
    }
    names
}

/// The review's values are as accepted: a `${...}` would mean something
/// worked out again, so it's refused.
fn frozen_problems(rules: &EnvRules, key: &str, problems: &mut Vec<(String, String)>) {
    for (part, values) in [("set", &rules.set), ("default", &rules.default)] {
        for (name, value) in values {
            if value.contains("${") && !value.contains("$${") {
                problems.push((
                    format!("{key}.{part}.{name}"),
                    "holds a `${...}` reference; the review gives values as they're accepted"
                        .to_string(),
                ));
            }
        }
    }
}

/// A command entry in `accepted.json`'s shape: as written, with `env` as
/// its whole plan.
fn accepted_command(entry: &Value, plan: &EnvPlan) -> Json {
    let mut command = match serde_json::to_value(entry) {
        Ok(Json::Object(map)) => map,
        _ => Map::new(),
    };
    command.insert(
        "env".to_string(),
        serde_json::to_value(plan).expect("only text is stored"),
    );
    Json::Object(command)
}

/// `dev-container`: `use` (`"project"`, or `{name: ...}`) and `images`.
fn dev_container(
    value: Value,
    problems: &mut Vec<(String, String)>,
) -> (ContainerUse, BTreeMap<String, String>) {
    const KEY: &str = "dev-container";
    let map = match value {
        Value::Null => return (ContainerUse::Project, BTreeMap::new()),
        Value::Mapping(map) => map,
        _ => {
            problems.push((KEY.to_string(), "must be a mapping".to_string()));
            return (ContainerUse::Project, BTreeMap::new());
        }
    };
    for key in map.keys() {
        match key.as_str() {
            Some("use" | "images" | COMMENT) => {}
            _ => problems.push((
                child_key(KEY, key.as_str().unwrap_or("?")),
                "unknown key".to_string(),
            )),
        }
    }
    if let Some(comment) = map.get(COMMENT) {
        comment_problem(comment, &child_key(KEY, COMMENT), problems);
    }
    let at = child_key(KEY, "use");
    let container_use = match map.get("use") {
        None | Some(Value::Null) => ContainerUse::Project,
        Some(Value::String(s)) if s == "project" => ContainerUse::Project,
        Some(Value::Mapping(choice)) => match name_of(choice) {
            Some(name) if valid_name(&name) => ContainerUse::Name(name),
            _ => {
                problems.push((
                    at,
                    "`{name: ...}` needs the name of one of your containers (a single directory name)"
                        .to_string(),
                ));
                ContainerUse::Project
            }
        },
        Some(_) => {
            problems.push((
                at,
                "must be \"project\", or `{name: ...}` for one of your containers".to_string(),
            ));
            ContainerUse::Project
        }
    };
    let mut images = BTreeMap::new();
    match map.get("images") {
        None | Some(Value::Null) => {}
        Some(Value::Mapping(entries)) => {
            for (from, to) in entries {
                match (from.as_str(), to.as_str()) {
                    (Some(from), Some(to)) if image_ref_ok(from) && image_ref_ok(to) => {
                        images.insert(from.to_string(), to.to_string());
                    }
                    _ => problems.push((
                        child_key(&child_key(KEY, "images"), from.as_str().unwrap_or("?")),
                        "must map an image to the image to build from instead, both without spaces"
                            .to_string(),
                    )),
                }
            }
        }
        Some(_) => problems.push((
            child_key(KEY, "images"),
            "must be a mapping of images".to_string(),
        )),
    }
    (container_use, images)
}

fn name_of(choice: &Mapping) -> Option<String> {
    if choice.len() != 1 {
        return None;
    }
    choice
        .get("name")
        .and_then(Value::as_str)
        .map(str::to_string)
}

/// An image reference: text a container tool reads as one, with nothing
/// that could be taken for another argument or a second reference.
fn image_ref_ok(text: &str) -> bool {
    !text.is_empty()
        && !text.starts_with('-')
        && text.chars().all(|c| c.is_ascii_graphic() && c != '=')
}

impl Review {
    /// The accepted settings this review gives: `accepted.json`'s contents,
    /// for the project's `vcs` and the setup's gateway.
    pub fn accepted(&self, vcs: Option<&str>, gateway: Option<&str>) -> Json {
        let overrides = |by_index: &BTreeMap<usize, Vec<String>>| -> Json {
            Json::Array(
                by_index
                    .iter()
                    .map(|(i, checks)| serde_json::json!({"index": i, "checks": checks}))
                    .collect(),
            )
        };
        serde_json::json!({
            "$schema": SCHEMA,
            "project-dir": self.project_dir.to_string_lossy(),
            "expires-at": self.expires_text,
            "vcs": vcs,
            "gateway": gateway,
            "egress": self.egress,
            "commands": self.accepted_commands,
            "dev-container": {
                "use": match &self.container_use {
                    ContainerUse::Project => serde_json::json!("project"),
                    ContainerUse::Name(name) => serde_json::json!({"name": name}),
                },
                "images": self.images,
            },
            "overrides": {
                "commands": overrides(&self.command_overrides),
                "egress": overrides(&self.egress_overrides),
            },
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const HEAD: &str = "review: \"surf-scoter-review-1\"\nproject-dir: \"/p\"\nexpires-at: \"2030-01-01T00:00:00Z\"\n";

    fn review(body: &str) -> Result<Review, Vec<String>> {
        parse(&format!("{HEAD}{body}"), Path::new("/r/review.yaml")).map_err(|errors| {
            errors
                .into_iter()
                .map(|e| format!("{}: {}", e.key.unwrap_or_default(), e.message))
                .collect()
        })
    }

    #[test]
    fn parse_review() {
        let review = review(concat!(
            "$comment: \"the whole review\"\n",
            "egress:\n  - {host: \"registry.npmjs.org\", protocol: \"tls\", $comment: \"why\"}\n",
            "env:\n  allow: [\"LANG\"]\n  set: {\"HOME\": \"@HOME@\"}\n",
            "commands:\n",
            "  - command: \"sh\"\n    program: \"/usr/bin/sh\"\n    args: [\"-c\", {any: {}}]\n",
            "    env: {allow: [\"AWS_*\"]}\n",
            "    $override: [\"host-bridge:shells\"]\n",
            "dev-container:\n  use: {name: \"my-env\"}\n  images: {\"debian:bookworm\": \"debian@sha256:abc\"}\n",
        ))
        .unwrap();
        assert!(!review.rejected);
        assert_eq!(review.project_dir, PathBuf::from("/p"));
        assert_eq!(review.egress[0]["port"], 443);
        assert_eq!(review.commands.len(), 1);
        assert_eq!(review.commands[0].env_plan.allow, ["LANG", "AWS_*"]);
        assert_eq!(review.commands[0].env_plan.set["HOME"], "@HOME@");
        assert_eq!(
            review.command_overrides,
            BTreeMap::from([(0, vec!["host-bridge:shells".to_string()])])
        );
        assert_eq!(
            review.container_use,
            ContainerUse::Name("my-env".to_string())
        );
        assert_eq!(review.images["debian:bookworm"], "debian@sha256:abc");
        let accepted = review.accepted(Some("git"), Some("squid"));
        assert_eq!(accepted["$schema"], SCHEMA);
        assert_eq!(accepted["gateway"], "squid");
        assert_eq!(accepted["commands"][0]["program"], "/usr/bin/sh");
        assert_eq!(accepted["commands"][0]["env"]["allow"][1], "AWS_*");
        assert!(accepted["commands"][0].get("$override").is_none());
        assert_eq!(
            accepted["overrides"]["commands"][0]["checks"][0],
            "host-bridge:shells"
        );
        assert_eq!(accepted["dev-container"]["use"]["name"], "my-env");
        // What it gives is what the generator reads back.
        let parsed = accepted::parse(accepted, Path::new("/v/accepted.json")).unwrap();
        assert_eq!(parsed.commands.len(), 1);
    }

    #[test]
    fn reject_wins() {
        let rejected = parse(
            "review: \"surf-scoter-review-1\"\nresult: reject\ncommands: \"broken\"\n",
            Path::new("/r/review.yaml"),
        )
        .unwrap();
        assert!(rejected.rejected);
        assert_eq!(
            review("result: accept\n").unwrap_err(),
            ["result: can only be `reject` (leave it commented out to accept)"]
        );
    }

    #[test]
    fn review_errors() {
        assert_eq!(
            parse("review: \"other\"\n", Path::new("/r")).unwrap_err()[0].message,
            "must be \"surf-scoter-review-1\"; this isn't a review file this version reads"
        );
        let errors = review(concat!(
            "extra: 1\n",
            "commands:\n",
            "  - {command: \"aws\", program: \"aws\", args: []}\n",
            "  - {command: \"NUL\", program: \"/bin/x\", args: [], $override: \"one\"}\n",
            "  - {command: \"x\", program: \"/bin/x\", args: [], env: {set: {\"A\": \"${ssh:home}\"}}}\n",
            "dev-container:\n  use: \"mine\"\n  images: {\"a\": \"--privileged\"}\n",
        ))
        .unwrap_err();
        assert_eq!(
            errors,
            [
                "extra: unknown key",
                "commands[1].$override: must list the setup checks it overrides, as `section:key`",
                "commands[0].program: must be the absolute path of the program to run",
                "commands[1].command: 'NUL' is a Windows device name",
                "commands[2].env.set.A: holds a `${...}` reference; the review gives values as they're accepted",
                "dev-container.use: must be \"project\", or `{name: ...}` for one of your containers",
                "dev-container.images.a: must map an image to the image to build from instead, both without spaces",
            ]
        );
        // Step 7's rules: merge keys, duplicate keys, control characters.
        let errors = review("env:\n  set:\n    <<: {A: b}\n").unwrap_err();
        assert!(errors[0].contains("merge key"), "{errors:?}");
        let errors = review("expires-at: \"2031-01-01T00:00:00Z\"\n").unwrap_err();
        assert!(errors[0].contains("duplicate entry"), "{errors:?}");
        let errors = review("$comment: \"line\\nbreak\"\n").unwrap_err();
        assert!(errors[0].contains("control character"), "{errors:?}");
        let errors =
            review("commands:\n  - {command: \"a\", program: \"/bin/a\", args: [], shell: true}\n")
                .unwrap_err();
        assert!(errors[0].contains("unknown field `shell`"), "{errors:?}");
    }
}
