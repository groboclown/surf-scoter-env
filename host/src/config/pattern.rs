//! Workspace patterns: directory (and file) patterns relative to the
//! workspace, such as `infra/**`.
//!
//!   * `.` is the workspace itself, and a leading `./` is allowed.
//!   * `*` matches any characters within one name, and `?` matches one character.
//!   * `**`, as a whole name, matches any number of directories, including none.

use std::fmt;

use serde::de::{self, Visitor};
use serde::{Deserialize, Deserializer};

/// A directory pattern relative to the workspace, such as `infra/**`.
#[derive(Debug, PartialEq, Eq)]
pub struct WorkspacePattern {
    /// As written in the configuration.
    pub text: String,
    /// One part per directory name below the workspace.
    parts: Vec<PatternPart>,
}

#[derive(Debug, PartialEq, Eq)]
enum PatternPart {
    /// `**`: any number of directories, including none.
    AnyDirs,
    /// One directory name, which may use `*` and `?`.
    Name(String),
}

impl WorkspacePattern {
    pub fn parse(text: &str) -> Result<Self, String> {
        if text.is_empty() {
            return Err(
                "a workspace pattern must not be empty; use '.' for the workspace itself"
                    .to_string(),
            );
        }
        if text.starts_with('/') {
            return Err(format!(
                "workspace pattern '{text}' must be relative to the workspace; use '.' for the workspace itself"
            ));
        }
        if text.contains("{workspace}") {
            return Err(format!(
                "workspace pattern '{text}' is already relative to the workspace; leave out {{workspace}}"
            ));
        }
        let mut parts: Vec<PatternPart> = Vec::new();
        for name in text.split('/').filter(|n| !n.is_empty() && *n != ".") {
            let part = match name {
                ".." => return Err(format!("workspace pattern '{text}' must not contain '..'")),
                "**" => PatternPart::AnyDirs,
                _ if name.contains("**") => {
                    return Err(format!(
                        "workspace pattern '{text}': '**' must be a whole directory name"
                    ));
                }
                _ => PatternPart::Name(name.to_string()),
            };
            // Repeated `**` mean the same as one.
            if part == PatternPart::AnyDirs && parts.last() == Some(&PatternPart::AnyDirs) {
                continue;
            }
            parts.push(part);
        }
        Ok(WorkspacePattern {
            text: text.to_string(),
            parts,
        })
    }

    /// Whether a directory, given as its names below the workspace, matches.
    pub fn matches(&self, dirs: &[&str]) -> bool {
        match_parts(&self.parts, dirs)
    }
}

fn match_parts(parts: &[PatternPart], dirs: &[&str]) -> bool {
    match parts.split_first() {
        None => dirs.is_empty(),
        Some((PatternPart::AnyDirs, rest)) => {
            (0..=dirs.len()).any(|skip| match_parts(rest, &dirs[skip..]))
        }
        Some((PatternPart::Name(glob), rest)) => match dirs.split_first() {
            Some((dir, dirs)) => glob_matches(glob, dir) && match_parts(rest, dirs),
            None => false,
        },
    }
}

/// Match one name against a glob of `*` (any characters) and `?` (one character).
pub(crate) fn glob_matches(glob: &str, name: &str) -> bool {
    let glob: Vec<char> = glob.chars().collect();
    let name: Vec<char> = name.chars().collect();
    let (mut g, mut n) = (0, 0);
    // Where to resume after the last `*`: the glob position after it, and the name position it's covering up to.
    let mut star: Option<(usize, usize)> = None;
    while n < name.len() {
        if g < glob.len() && (glob[g] == '?' || glob[g] == name[n]) {
            g += 1;
            n += 1;
        } else if g < glob.len() && glob[g] == '*' {
            star = Some((g + 1, n));
            g += 1;
        } else if let Some((after_star, covered)) = star {
            // Let the `*` cover one more character.
            g = after_star;
            n = covered + 1;
            star = Some((after_star, covered + 1));
        } else {
            return false;
        }
    }
    glob[g..].iter().all(|&c| c == '*')
}

impl<'de> Deserialize<'de> for WorkspacePattern {
    fn deserialize<D: Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        struct PatternVisitor;

        impl Visitor<'_> for PatternVisitor {
            type Value = WorkspacePattern;

            fn expecting(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
                f.write_str("a directory pattern relative to the workspace, such as infra/**")
            }

            fn visit_str<E: de::Error>(self, v: &str) -> Result<WorkspacePattern, E> {
                WorkspacePattern::parse(v).map_err(E::custom)
            }
        }

        deserializer.deserialize_str(PatternVisitor)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn pattern(text: &str) -> WorkspacePattern {
        WorkspacePattern::parse(text).unwrap()
    }

    fn matches(text: &str, dir: &str) -> bool {
        let dirs: Vec<&str> = dir.split('/').filter(|d| !d.is_empty()).collect();
        pattern(text).matches(&dirs)
    }

    #[test]
    fn pattern_syntax_errors() {
        let err = |text: &str| WorkspacePattern::parse(text).unwrap_err();
        assert_eq!(
            err(""),
            "a workspace pattern must not be empty; use '.' for the workspace itself"
        );
        assert_eq!(
            err("/services"),
            "workspace pattern '/services' must be relative to the workspace; use '.' for the workspace itself",
        );
        assert_eq!(
            err("../x"),
            "workspace pattern '../x' must not contain '..'"
        );
        assert_eq!(
            err("a/../b"),
            "workspace pattern 'a/../b' must not contain '..'"
        );
        assert_eq!(
            err("a**"),
            "workspace pattern 'a**': '**' must be a whole directory name"
        );
        assert!(err("{workspace}").contains("leave out {workspace}"));
    }

    #[test]
    fn pattern_workspace_root() {
        assert!(matches(".", ""));
        assert!(matches("./", ""));
        assert!(!matches(".", "a"));
        assert!(matches("**", ""));
        assert!(matches("**", "anything/at/all"));
    }

    #[test]
    fn pattern_literal_names() {
        assert!(matches("services/api", "services/api"));
        assert!(matches("./services/api", "services/api"));
        assert!(matches("services//./api/", "services/api"));
        assert!(!matches("services/api", "services"));
        assert!(!matches("services/api", "services/api/v2"));
        assert!(!matches("services/api", "Services/api"));
        // Braces are ordinary characters now.
        assert!(matches("{x}", "{x}"));
    }

    #[test]
    fn star_stays_within_a_name() {
        assert!(matches("services/*", "services/api"));
        assert!(matches("services/*", "services/.hidden"));
        assert!(!matches("services/*", "services"));
        assert!(!matches("services/*", "services/api/v2"));
        assert!(matches("*/api", "services/api"));
        assert!(!matches("*", ""));
        assert!(matches("svc-*", "svc-a"));
        assert!(matches("svc-*", "svc-"));
        assert!(!matches("svc-*", "svc"));
        assert!(matches("*-app-*", "my-app-v2"));
        assert!(!matches("*-app-*", "my-ap-v2"));
    }

    #[test]
    fn pattern_question_mark() {
        assert!(matches("v?", "v1"));
        assert!(!matches("v?", "v"));
        assert!(!matches("v?", "v10"));
        assert!(matches("??", "\u{FC}n"));
    }

    #[test]
    fn pattern_double_star() {
        let p = "infra/**";
        assert!(matches(p, "infra"));
        assert!(matches(p, "infra/a"));
        assert!(matches(p, "infra/a/b/c"));
        assert!(!matches(p, ""));
        assert!(!matches(p, "other/infra"));
        assert!(matches("**/deploy", "deploy"));
        assert!(matches("**/deploy", "a/b/deploy"));
        assert!(!matches("**/deploy", "a/b/deploy/x"));
        assert!(matches("**/svc-*/**", "x/svc-a/y/z"));
        assert!(matches("a/**/**/b", "a/b"));
    }

    #[test]
    fn glob_backtracking() {
        assert!(glob_matches("*a*b", "xaxxab"));
        assert!(glob_matches("a*a*a", "aaaaa"));
        assert!(!glob_matches("a*a*a", "aa"));
        assert!(glob_matches("*", ""));
        assert!(glob_matches("**x", "x"));
        assert!(!glob_matches("", "a"));
        assert!(glob_matches("", ""));
    }
}
