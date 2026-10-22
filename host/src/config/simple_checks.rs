//! The setup's simple checks: `project-files` and `egress-hosts`.
//!
//! Like `host-commands` (see `host_commands.rs`), each key is a check with
//! an `action` (`refuse`, `warn` or `allow`) and a `reason`, merged key by
//! key across the setup files (`null` removes a lower priority one); but
//! each matches one thing:
//!
//! ```yaml
//! verification:
//!     project-files:
//!         secret-files:
//!             action: warn
//!             reason: It looks like a secret, and goes into the image.
//!             patterns: [.env, '*.pem', .aws/]
//!     egress-hosts:
//!         code-hosts:
//!             action: warn
//!             reason: Anyone can upload to it.
//!             hosts: [github.com, '*.githubusercontent.com']
//!         my-company-git:
//!             action: allow
//!             reason: Our own code host.
//!             hosts: [git.example.com]
//!             overrides: [verification:code-hosts]
//! ```
//!
//! The generator checks them and writes them to the universal configuration
//! file's resolved sections, as written; the review's checks (the scripts'
//! `checks.py`) apply them: the most restrictive action of the checks that
//! match wins, then each override takes the place of the checks it names,
//! for what it matches.

use serde::{Deserialize, Serialize};
use yaml_serde::Value;

use super::egress;
use super::host_commands::Action;
use super::{child_key, index_key};

/// The group of checks on the files in a project's container directories.
pub const PROJECT_FILES: &str = "project-files";
/// The group of checks on a project's egress rules' hosts.
pub const EGRESS_HOSTS: &str = "egress-hosts";

/// A simple check, as written.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "kebab-case", deny_unknown_fields)]
pub struct SimpleCheck {
    pub action: Action,
    pub reason: String,
    /// `project-files`: names of files (and, ending in `/`, directories),
    /// with `*`, `?` and `[...]`.
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub patterns: Vec<String>,
    /// `egress-hosts`: host names, or `*.` and a domain for its subdomains.
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub hosts: Vec<String>,
    /// The checks (`section:key`, of the same group) this one takes the
    /// place of, for what it matches.
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub overrides: Vec<String>,
}

/// A `group` entry at `at`, as written: `None` for null, which removes the
/// check.  Problems are (key, message) pairs.
pub fn parse(
    value: Value,
    group: &str,
    at: &str,
) -> Result<Option<SimpleCheck>, Vec<(String, String)>> {
    match value {
        Value::Null => Ok(None),
        Value::Mapping(_) => {
            let check: SimpleCheck =
                yaml_serde::from_value(value).map_err(|e| vec![(at.to_string(), e.to_string())])?;
            let problems = check.problems(group, at);
            if problems.is_empty() {
                Ok(Some(check))
            } else {
                Err(problems)
            }
        }
        _ => Err(vec![(
            at.to_string(),
            format!(
                "must be a check (a mapping with `action`, `reason` and its `{}`), or null to remove it",
                match_field(group)
            ),
        )]),
    }
}

/// The field a group's checks match with.
pub fn match_field(group: &str) -> &'static str {
    if group == EGRESS_HOSTS {
        "hosts"
    } else {
        "patterns"
    }
}

impl SimpleCheck {
    /// What the check matches: its `patterns` or its `hosts`.
    pub fn matches(&self) -> &[String] {
        if self.patterns.is_empty() {
            &self.hosts
        } else {
            &self.patterns
        }
    }

    /// The problems with the `group` check at `at`, as (key, message) pairs.
    pub fn problems(&self, group: &str, at: &str) -> Vec<(String, String)> {
        let mut out = Vec::new();
        let field = |name: &str| child_key(at, name);
        const AS_WRITTEN: &str = "is used as written; `${...}` isn't expanded here";

        if self.reason.trim().is_empty() {
            out.push((
                field("reason"),
                "must explain the check; it's shown when the check matches".to_string(),
            ));
        } else if self.reason.contains("${") {
            out.push((field("reason"), AS_WRITTEN.to_string()));
        }
        let (wanted, other, other_name) = if group == EGRESS_HOSTS {
            (&self.hosts, &self.patterns, "patterns")
        } else {
            (&self.patterns, &self.hosts, "hosts")
        };
        let name = match_field(group);
        if !other.is_empty() {
            out.push((
                field(other_name),
                format!("isn't a field of `{group}` checks, which match with `{name}`"),
            ));
        }
        if wanted.is_empty() {
            out.push((field(name), "must name at least one".to_string()));
        }
        for (i, item) in wanted.iter().enumerate() {
            let key = index_key(&field(name), i);
            let problem = if item.contains("${") {
                Some(AS_WRITTEN.to_string())
            } else if group == EGRESS_HOSTS {
                egress::host_problem(item)
            } else {
                pattern_problem(item)
            };
            if let Some(message) = problem {
                out.push((key, message));
            }
        }
        for (i, name) in self.overrides.iter().enumerate() {
            let valid = name.split_once(':').is_some_and(|(section, key)| {
                !section.is_empty() && !key.is_empty() && !key.contains(':')
            });
            if !valid {
                out.push((
                    index_key(&field("overrides"), i),
                    "must name a check as `section:key`".to_string(),
                ));
            }
        }
        out
    }
}

/// Why a `project-files` pattern can't be right, if it can't: one name (a
/// directory ends in `/`), with no control characters.
fn pattern_problem(pattern: &str) -> Option<String> {
    let name = pattern.strip_suffix('/').unwrap_or(pattern);
    if name.is_empty() {
        Some("must be a name, or a name ending in `/` for a directory".to_string())
    } else if name.contains('/') || name.contains('\\') {
        Some("must be one name, matched against each file's and directory's own name".to_string())
    } else if name.chars().any(char::is_control) {
        Some("can't hold control characters".to_string())
    } else {
        None
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn parsed(group: &str, yaml: &str) -> Result<Option<SimpleCheck>, Vec<(String, String)>> {
        parse(yaml_serde::from_str(yaml).unwrap(), group, "v.c")
    }

    fn problems(group: &str, yaml: &str) -> Vec<String> {
        parsed(group, yaml)
            .unwrap_err()
            .into_iter()
            .map(|(key, message)| format!("{key}: {message}"))
            .collect()
    }

    #[test]
    fn checks_as_written() {
        let check = parsed(
            PROJECT_FILES,
            "{action: warn, reason: secrets, patterns: [.env, '*.pem', .aws/]}",
        )
        .unwrap()
        .unwrap();
        assert_eq!(check.action, Action::Warn);
        assert_eq!(check.matches(), [".env", "*.pem", ".aws/"]);
        let check = parsed(
            EGRESS_HOSTS,
            "{action: allow, reason: ours, hosts: [git.example.com], overrides: ['verification:code-hosts']}",
        )
        .unwrap()
        .unwrap();
        assert_eq!(check.overrides, ["verification:code-hosts"]);
        assert_eq!(parsed(EGRESS_HOSTS, "null").unwrap(), None);
    }

    #[test]
    fn problems_found() {
        assert_eq!(
            problems(
                PROJECT_FILES,
                "{action: warn, reason: ' ', patterns: [a/b, '', ok/]}"
            ),
            [
                "v.c.reason: must explain the check; it's shown when the check matches",
                "v.c.patterns[0]: must be one name, matched against each file's and directory's own name",
                "v.c.patterns[1]: must be a name, or a name ending in `/` for a directory",
            ]
        );
        assert_eq!(
            problems(EGRESS_HOSTS, "{action: refuse, reason: r, patterns: [x]}"),
            [
                "v.c.patterns: isn't a field of `egress-hosts` checks, which match with `hosts`",
                "v.c.hosts: must name at least one",
            ]
        );
        assert!(
            problems(
                EGRESS_HOSTS,
                "{action: refuse, reason: r, hosts: ['not a host'], overrides: [nope]}"
            )
            .len()
                == 2
        );
        assert!(!problems(PROJECT_FILES, "{action: never, reason: r, patterns: [x]}").is_empty());
        assert!(!problems(PROJECT_FILES, "[x]").is_empty());
        assert_eq!(
            problems(
                PROJECT_FILES,
                "{action: warn, reason: '${a:b}', patterns: ['${x:y}']}"
            ),
            [
                "v.c.reason: is used as written; `${...}` isn't expanded here",
                "v.c.patterns[0]: is used as written; `${...}` isn't expanded here",
            ]
        );
    }
}
