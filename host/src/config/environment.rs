//! The environment of the host processes that the project's commands run.
//! See `project` for where the `env` blocks go.
//!
//! ```yaml
//! env:
//!   # Container variables that may come through, as name patterns...
//!   allow: [LANG, 'LC_*']
//!   # ...except these.
//!   drop: [LC_IDENTIFICATION]
//!   # Container variables that refuse the request, as name patterns.
//!   error: ['AWS_SECRET_*']
//!   # Always set.
//!   set:
//!     HOME: '@HOME@'
//!   # Set when the container's value doesn't come through.
//!   default:
//!     AWS_REGION: us-east-1
//! ```
//!
//! A host process starts from an empty environment: the host's variables are
//! only there when `set` or `default` puts them there.  The container's
//! variables come through only when an `allow` pattern matches the name,
//! and no `drop` pattern does.
//! `default` then fills in what isn't there, and `set` always wins.  If the
//! container sends any variable matching an `error` pattern, allowed or not,
//! the request is refused.
//!
//! Patterns match the whole name: `*` matches any characters, and `?` one
//! character.  Quote patterns that start with `*`; YAML reads a bare `*...` as
//! an alias.
//!
//! `set` and `default` values can refer to the system configuration as
//! `${section:key}`: its `paths`, `programs` (the program found) and
//! `values`.  `$${` stands for `${`.  These are expanded when the
//! configuration is loaded (`EnvPlan::resolve`), so the universal
//! configuration file has them expanded.
//!
//! `@NAME@` is left until the program running the commands expands it with
//! its own environment (`EnvPlan::expand_host`): it's that program's `NAME`
//! environment variable (on Windows, matched
//! without regard to case, as Windows does).  When the host doesn't have it,
//! it's the system configuration's `host` section `NAME` key (matched
//! exactly), if there is one, and otherwise empty.  `NAME` is letters, digits,
//! `_`, `(` and `)`; any other `@` is kept as it is, and `@@` stands for `@`.
//! A value starting with `@` needs quotes in YAML.

use std::collections::BTreeMap;

use serde::{Deserialize, Serialize};

use super::pattern::glob_matches;
use super::system::SystemConfig;
use super::{child_key, index_key};

/// The system configuration section that `@NAME@` falls back to.
pub const HOST_SECTION: &str = "host";

/// An `env` block, as written.
#[derive(Debug, Clone, Default, PartialEq, Eq, Deserialize)]
#[serde(deny_unknown_fields, rename_all = "kebab-case")]
pub struct EnvRules {
    /// Patterns of the container variables that come through.
    #[serde(default)]
    pub allow: Vec<String>,
    /// Patterns of the `allow` container variables that don't come through.
    #[serde(default)]
    pub drop: Vec<String>,
    /// Patterns of the container variables that refuse the request.
    #[serde(default)]
    pub error: Vec<String>,
    /// Always set.
    #[serde(default)]
    pub set: BTreeMap<String, String>,
    /// Set when the container's value doesn't come through.
    #[serde(default)]
    pub default: BTreeMap<String, String>,
}

impl EnvRules {
    /// Check the names and patterns, adding (key, message) problems, with
    /// keys under `key`.
    pub(crate) fn validate(&self, key: &str, problems: &mut Vec<(String, String)>) {
        for (group, patterns) in [
            ("allow", &self.allow),
            ("drop", &self.drop),
            ("error", &self.error),
        ] {
            for (i, pattern) in patterns.iter().enumerate() {
                if let Err(message) = check_name(pattern) {
                    problems.push((child_key(key, &index_key(group, i)), message));
                }
            }
        }
        for (group, values) in [("set", &self.set), ("default", &self.default)] {
            for name in values.keys() {
                let message = match check_name(name) {
                    Err(message) => message,
                    Ok(()) if name.contains(['*', '?']) => {
                        "is a variable name, not a pattern".to_string()
                    }
                    Ok(()) => continue,
                };
                problems.push((child_key(key, &child_key(group, name)), message));
            }
        }
    }
}

/// A name, or a pattern of names: not empty, and without `=` or NUL.
fn check_name(name: &str) -> Result<(), String> {
    if name.is_empty() {
        Err("must not be empty".to_string())
    } else if name.contains(['=', '\0']) {
        Err(format!(
            "'{}' can't be a variable name: it has `=` or a NUL",
            name.escape_debug()
        ))
    } else {
        Ok(())
    }
}

/// `env` blocks worked out: values expanded, and a command's block added to
/// the project's.
#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize)]
pub struct EnvPlan {
    pub allow: Vec<String>,
    pub drop: Vec<String>,
    pub error: Vec<String>,
    pub set: BTreeMap<String, String>,
    pub default: BTreeMap<String, String>,
}

impl EnvPlan {
    /// `rules` with their `${section:key}` references expanded against
    /// `system`.  `@NAME@` is left for `expand_host`, by the program that
    /// runs the commands.  Problems are (key, message), with keys under
    /// `key`.
    pub fn resolve(
        rules: &EnvRules,
        system: &SystemConfig,
        key: &str,
    ) -> Result<Self, Vec<(String, String)>> {
        let mut problems = Vec::new();
        let expand = |text: &str| expand_references(text, system);
        let set = expand_each(&rules.set, key, "set", &mut problems, expand);
        let default = expand_each(&rules.default, key, "default", &mut problems, expand);
        if !problems.is_empty() {
            return Err(problems);
        }
        Ok(EnvPlan {
            allow: rules.allow.clone(),
            drop: rules.drop.clone(),
            error: rules.error.clone(),
            set,
            default,
        })
    }

    /// The plan `rules` already are, as `host-brokered` writes them: patterns
    /// and values used as they are, with `@NAME@` still to be expanded.
    pub fn as_written(rules: &EnvRules) -> Self {
        EnvPlan {
            allow: rules.allow.clone(),
            drop: rules.drop.clone(),
            error: rules.error.clone(),
            set: rules.set.clone(),
            default: rules.default.clone(),
        }
    }

    /// This plan with its `@NAME@` references replaced, using the
    /// environment of the program running the commands (see `SystemConfig`'s
    /// `Env`), and `@@` made `@`.  Problems are (key, message), with keys
    /// under `key`.
    pub fn expand_host(
        &self,
        system: &SystemConfig,
        key: &str,
    ) -> Result<Self, Vec<(String, String)>> {
        let mut problems = Vec::new();
        let expand = |text: &str| expand_host(text, system);
        let set = expand_each(&self.set, key, "set", &mut problems, expand);
        let default = expand_each(&self.default, key, "default", &mut problems, expand);
        if !problems.is_empty() {
            return Err(problems);
        }
        Ok(EnvPlan {
            set,
            default,
            ..self.clone()
        })
    }

    /// This plan with `over`'s added: the patterns of both, and `over`'s
    /// values winning.
    pub fn merged(&self, over: &EnvPlan) -> EnvPlan {
        let mut merged = self.clone();
        merged.allow.extend(over.allow.iter().cloned());
        merged.drop.extend(over.drop.iter().cloned());
        merged.error.extend(over.error.iter().cloned());
        merged.set.extend(over.set.clone());
        merged.default.extend(over.default.clone());
        merged
    }

    /// The process environment for a request whose container sent `client`,
    /// or why the request is refused.
    pub fn apply(&self, client: &[(String, String)]) -> Result<BTreeMap<String, String>, String> {
        for (name, _) in client {
            if let Some(pattern) = self.error.iter().find(|p| glob_matches(p, name)) {
                return Err(format!(
                    "the container may not set the environment variable {name} (it matches '{pattern}')"
                ));
            }
        }
        let matches =
            |patterns: &[String], name: &str| patterns.iter().any(|p| glob_matches(p, name));
        let mut env: BTreeMap<String, String> = client
            .iter()
            .filter(|(name, _)| matches(&self.allow, name) && !matches(&self.drop, name))
            .cloned()
            .collect();
        for (name, value) in &self.default {
            env.entry(name.clone()).or_insert_with(|| value.clone());
        }
        env.extend(self.set.clone());
        Ok(env)
    }
}

/// `values` with `expand` applied to each; problems are added with keys
/// under `key`'s `group`.
fn expand_each(
    values: &BTreeMap<String, String>,
    key: &str,
    group: &str,
    problems: &mut Vec<(String, String)>,
    expand: impl Fn(&str) -> Result<String, String>,
) -> BTreeMap<String, String> {
    let mut expanded = BTreeMap::new();
    for (name, value) in values {
        match expand(value) {
            Ok(value) => {
                expanded.insert(name.clone(), value);
            }
            Err(message) => problems.push((child_key(key, &child_key(group, name)), message)),
        }
    }
    expanded
}

/// `text` with its `${section:key}` references replaced.  `@NAME@` and
/// `@@` are kept for `expand_host`, and an `@` in a replaced value is
/// written as `@@`, so it stays as it is.
fn expand_references(text: &str, system: &SystemConfig) -> Result<String, String> {
    let mut out = String::new();
    let mut rest = text;
    while let Some(at) = rest.find('$') {
        out.push_str(&rest[..at]);
        rest = &rest[at..];
        if let Some(after) = rest.strip_prefix("$${") {
            out.push_str("${");
            rest = after;
        } else if let Some(after) = rest.strip_prefix("${") {
            let Some(end) = after.find('}') else {
                return Err(format!("`${{{after}` is missing its `}}`"));
            };
            let reference = &after[..end];
            rest = &after[end + 1..];
            let (section, key) = reference
                .split_once(':')
                .filter(|(section, key)| !section.is_empty() && !key.is_empty())
                .ok_or_else(|| {
                    format!("`${{{reference}}}` needs a section, as `${{section:key}}`")
                })?;
            out.push_str(&system.scalar_text(section, key)?.replace('@', "@@"));
        } else {
            out.push('$');
            rest = &rest[1..];
        }
    }
    out.push_str(rest);
    Ok(out)
}

/// `text` with its `@NAME@` references replaced, and `@@` made `@`.
fn expand_host(text: &str, system: &SystemConfig) -> Result<String, String> {
    let mut out = String::new();
    let mut rest = text;
    while let Some(at) = rest.find('@') {
        out.push_str(&rest[..at]);
        let after = &rest[at + 1..];
        if let Some(after) = after.strip_prefix('@') {
            out.push('@');
            rest = after;
            continue;
        }
        let name_len = after
            .find(|c: char| !is_host_name_char(c))
            .unwrap_or(after.len());
        if name_len > 0 && after[name_len..].starts_with('@') {
            out.push_str(&host_value(&after[..name_len], system)?);
            rest = &after[name_len + 1..];
        } else {
            out.push('@');
            rest = after;
        }
    }
    out.push_str(rest);
    Ok(out)
}

/// Whether `c` can be in the `NAME` of `@NAME@`.
fn is_host_name_char(c: char) -> bool {
    c.is_ascii_alphanumeric() || matches!(c, '_' | '(' | ')')
}

/// The text `@name@` stands for: the host's variable, or else the `host`
/// section's key, or else nothing.
fn host_value(name: &str, system: &SystemConfig) -> Result<String, String> {
    if let Some(value) = system.host_var(name) {
        return Ok(value.to_string());
    }
    if system.is_set(HOST_SECTION, name) {
        return system.scalar_text(HOST_SECTION, name);
    }
    Ok(String::new())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::config::system::Env;
    use crate::config::testing::{TempDir, load_setup};
    use std::collections::HashMap;

    fn system_with(setup: &str, vars: &[(&str, &str)]) -> SystemConfig {
        let dir = TempDir::new();
        let file = dir.write("setup.yaml", setup);
        let env = Env {
            vars: vars
                .iter()
                .map(|(k, v)| (k.to_string(), v.to_string()))
                .collect::<HashMap<_, _>>(),
            ..Env::default()
        };
        load_setup(&file, None, None, env).unwrap()
    }

    fn rules(yaml: &str) -> EnvRules {
        yaml_serde::from_str(yaml).unwrap()
    }

    fn pairs(items: &[(&str, &str)]) -> Vec<(String, String)> {
        items
            .iter()
            .map(|(k, v)| (k.to_string(), v.to_string()))
            .collect()
    }

    #[test]
    fn nothing_comes_through_by_default() {
        let plan = EnvPlan::default();
        let env = plan
            .apply(&pairs(&[("LD_PRELOAD", "/tmp/evil.so"), ("PATH", "/bin")]))
            .unwrap();
        assert!(env.is_empty(), "{env:?}");
    }

    #[test]
    fn allow_default_and_set() {
        let plan = EnvPlan {
            allow: vec!["LANG".to_string(), "LC_*".to_string(), "A?".to_string()],
            drop: vec!["LC_*_SECRET".to_string()],
            set: BTreeMap::from([("LANG".to_string(), "C".to_string())]),
            default: BTreeMap::from([
                ("LC_ALL".to_string(), "C.UTF-8".to_string()),
                ("REGION".to_string(), "us-east-1".to_string()),
            ]),
            ..EnvPlan::default()
        };
        let env = plan
            .apply(&pairs(&[
                ("LANG", "en_US"),
                ("LC_ALL", "en_US.UTF-8"),
                ("AB", "1"),
                ("ABC", "no"),
                ("LC_X_SECRET", "dropped"),
                ("HOME", "/home/container"),
            ]))
            .unwrap();
        assert_eq!(
            env,
            BTreeMap::from([
                ("AB".to_string(), "1".to_string()),
                // `set` wins over the container.
                ("LANG".to_string(), "C".to_string()),
                // `default` doesn't.
                ("LC_ALL".to_string(), "en_US.UTF-8".to_string()),
                ("REGION".to_string(), "us-east-1".to_string()),
            ])
        );
    }

    #[test]
    fn error_refuses_even_when_not_allowed() {
        let plan = EnvPlan {
            error: vec!["AWS_SECRET_*".to_string()],
            ..EnvPlan::default()
        };
        assert_eq!(
            plan.apply(&pairs(&[("AWS_SECRET_ACCESS_KEY", "x")])),
            Err(
                "the container may not set the environment variable AWS_SECRET_ACCESS_KEY (it matches 'AWS_SECRET_*')"
                    .to_string()
            )
        );
        assert!(plan.apply(&pairs(&[("AWS_REGION", "x")])).is_ok());
    }

    #[test]
    fn merged_adds_patterns_and_overrides_values() {
        let project = EnvPlan {
            allow: vec!["LANG".to_string()],
            drop: vec!["LANG_X".to_string()],
            error: vec!["SECRET".to_string()],
            set: BTreeMap::from([
                ("A".to_string(), "project".to_string()),
                ("B".to_string(), "project".to_string()),
            ]),
            default: BTreeMap::from([("D".to_string(), "project".to_string())]),
        };
        let command = EnvPlan {
            allow: vec!["TERM".to_string()],
            drop: vec!["TERM_Y".to_string()],
            set: BTreeMap::from([("A".to_string(), "command".to_string())]),
            ..EnvPlan::default()
        };
        let merged = project.merged(&command);
        assert_eq!(merged.allow, ["LANG", "TERM"]);
        assert_eq!(merged.drop, ["LANG_X", "TERM_Y"]);
        assert_eq!(merged.error, ["SECRET"]);
        assert_eq!(merged.set["A"], "command");
        assert_eq!(merged.set["B"], "project");
        assert_eq!(merged.default["D"], "project");
    }

    #[test]
    fn values_are_expanded() {
        let system = system_with(
            concat!(
                "a:\n  paths:\n    dir: /opt/a\n  values:\n    n: 3\n    s: text\n    at: me@B@x\n",
                "host:\n  values:\n    FALLBACK: from-setup\n    HOME: from-setup\n",
            ),
            &[("HOME", "/home/me"), ("EMPTY", "")],
        );
        let plan = EnvPlan::resolve(
            &rules(concat!(
                "set:\n",
                "  P: ${a:dir}/bin\n",
                "  N: n=${a:n}\n",
                "  S: ${a:s}\n",
                "  HOME: '@HOME@'\n",
                "  F: '@FALLBACK@'\n",
                "  LOWER: '@fallback@'\n",
                "  E: '[@EMPTY@]'\n",
                "  M: '[@MISSING@]'\n",
                "  SECTION: ${host:HOME}\n",
                "  ESC: $${a:dir} costs $5\n",
                "  AT: '@@HOME@ and me@example.com, @ @ @'\n",
                "  BOTH: '@HOME@@EMPTY@/x'\n",
                "  REF_AT: ${a:at}\n",
            )),
            &system,
            "env",
        )
        .unwrap();
        // References are expanded; `@NAME@` is left for the program running
        // the commands, and a reference's `@` is kept from looking like one.
        assert_eq!(plan.set["HOME"], "@HOME@");
        assert_eq!(plan.set["AT"], "@@HOME@ and me@example.com, @ @ @");
        assert_eq!(plan.set["P"], "/opt/a/bin");
        assert_eq!(plan.set["REF_AT"], "me@@B@@x");

        let plan = plan.expand_host(&system, "env").unwrap();
        let get = |name: &str| plan.set[name].as_str();
        assert_eq!(get("P"), "/opt/a/bin");
        assert_eq!(get("REF_AT"), "me@B@x");
        assert_eq!(get("N"), "n=3");
        assert_eq!(get("S"), "text");
        // The host's own variable wins over the `host` section.
        assert_eq!(get("HOME"), "/home/me");
        assert_eq!(get("F"), "from-setup");
        // The `host` section is matched exactly.
        assert_eq!(get("LOWER"), "");
        assert_eq!(get("E"), "[]");
        assert_eq!(get("M"), "[]");
        // `${host:...}` is the section, as any other.
        assert_eq!(get("SECTION"), "from-setup");
        assert_eq!(get("ESC"), "${a:dir} costs $5");
        assert_eq!(get("AT"), "@HOME@ and me@example.com, @ @ @");
        assert_eq!(get("BOTH"), "/home/me/x");
    }

    #[cfg(unix)]
    #[test]
    fn programs_are_the_program_found() {
        use std::os::unix::fs::PermissionsExt;
        let dir = TempDir::new();
        let tool = dir.write("bin/tool", "#!/bin/sh\n");
        std::fs::set_permissions(&tool, std::fs::Permissions::from_mode(0o755)).unwrap();
        let file = dir.write(
            "setup.yaml",
            "t:\n  programs:\n    exec: tool\n    gone: no-such-tool\n",
        );
        let env = Env {
            path: Some(dir.path().join("bin").into_os_string()),
            ..Env::default()
        };
        let system = load_setup(&file, None, None, env).unwrap();
        let plan = EnvPlan::resolve(&rules("set:\n  T: ${t:exec}\n"), &system, "env").unwrap();
        assert_eq!(plan.set["T"], tool.display().to_string());
        let errors = EnvPlan::resolve(&rules("set:\n  G: ${t:gone}\n"), &system, "env");
        assert_eq!(
            errors,
            Err(vec![(
                "env.set.G".to_string(),
                "the program for `${t:gone}` isn't there".to_string()
            )])
        );
    }

    #[test]
    fn expansion_errors() {
        let system = system_with(
            "a:\n  runs:\n    go: go now\n  values:\n    nothing: null\n",
            &[],
        );
        let errors = EnvPlan::resolve(
            &rules(concat!(
                "set:\n",
                "  A: ${a:missing}\n",
                "  B: ${nosection}\n",
                "  C: ${a:go}\n",
                "  D: ${a:nothing}\n",
                "default:\n",
                "  E: ${a:open\n",
            )),
            &system,
            "commands[0].env",
        )
        .unwrap_err();
        assert_eq!(
            errors,
            [
                (
                    "commands[0].env.set.A".to_string(),
                    "unknown reference `${a:missing}`".to_string()
                ),
                (
                    "commands[0].env.set.B".to_string(),
                    "`${nosection}` needs a section, as `${section:key}`".to_string()
                ),
                (
                    "commands[0].env.set.C".to_string(),
                    "`${a:go}` is a list; it can't be used here".to_string()
                ),
                (
                    "commands[0].env.set.D".to_string(),
                    "`${a:nothing}` is null, so it can't be used here".to_string()
                ),
                (
                    "commands[0].env.default.E".to_string(),
                    "`${a:open` is missing its `}`".to_string()
                ),
            ]
        );
    }

    #[test]
    fn rule_checks() {
        let mut problems = Vec::new();
        rules(concat!(
            "allow: ['', 'A=B']\n",
            "drop: ['ok*', '']\n",
            "error: ['OK_*']\n",
            "set: {'X*': v, 'GOOD': v}\n",
            "default: {'': v}\n",
        ))
        .validate("env", &mut problems);
        assert_eq!(
            problems,
            [
                ("env.allow[0]".to_string(), "must not be empty".to_string()),
                (
                    "env.allow[1]".to_string(),
                    "'A=B' can't be a variable name: it has `=` or a NUL".to_string()
                ),
                ("env.drop[1]".to_string(), "must not be empty".to_string()),
                (
                    "env.set.X*".to_string(),
                    "is a variable name, not a pattern".to_string()
                ),
                ("env.default".to_string(), "must not be empty".to_string()),
            ]
        );
        assert!(yaml_serde::from_str::<EnvRules>("pass: ['*']\n").is_err());
    }
}
