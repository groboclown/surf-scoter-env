//! The Public Suffix List (<https://publicsuffix.org/>), read from files the
//! setup names, for refusing egress wildcards (`*.DOMAIN`) on a domain where
//! anyone can have a name.
//!
//! The format: one rule a line, the first word of it; `//` starts a comment
//! line.  A rule is a domain (`co.uk`: a public suffix), `*.` and a domain
//! (`*.ck`: every name directly under it is a public suffix), or `!` and a
//! domain (`!www.ck`: an exception to a wildcard, not a public suffix).  The
//! list's private section (shared hosting such as `github.io`) is read the
//! same as the rest: for egress, a name anyone can register is the risk,
//! whoever runs the registry.
//!
//! Rules outside ASCII are skipped: an egress rule's host must be written in
//! its `xn--` form, and nothing here converts to it.  A suffix list's
//! internationalized entries are matched only if the file gives them as
//! `xn--` names.

use std::collections::HashSet;

/// A Public Suffix List's rules.
#[derive(Debug, Default, Clone, PartialEq, Eq)]
pub struct SuffixList {
    suffixes: HashSet<String>,
    /// The domains under which every name is a suffix (from `*.DOMAIN`).
    wildcards: HashSet<String>,
    exceptions: HashSet<String>,
}

impl SuffixList {
    /// The rules in `text`, a Public Suffix List file, added to these.
    pub fn add(&mut self, text: &str) {
        for line in text.lines() {
            let Some(rule) = line.split_whitespace().next() else {
                continue;
            };
            if rule.starts_with("//") || !rule.is_ascii() {
                continue;
            }
            let rule = rule.to_ascii_lowercase();
            if let Some(exception) = rule.strip_prefix('!') {
                self.exceptions.insert(exception.to_string());
            } else if let Some(domain) = rule.strip_prefix("*.") {
                self.wildcards.insert(domain.to_string());
            } else {
                self.suffixes.insert(rule);
            }
        }
    }

    /// Whether `domain` is a public suffix: a name anyone can have names under.
    pub fn is_public_suffix(&self, domain: &str) -> bool {
        let domain = domain.to_ascii_lowercase();
        if self.exceptions.contains(&domain) {
            return false;
        }
        if self.suffixes.contains(&domain) {
            return true;
        }
        domain
            .split_once('.')
            .is_some_and(|(_, parent)| self.wildcards.contains(parent))
    }

    /// Whether the list has no rules.
    pub fn is_empty(&self) -> bool {
        self.suffixes.is_empty() && self.wildcards.is_empty() && self.exceptions.is_empty()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const SAMPLE: &str = "\
// This Source Code Form is subject to the terms of the Mozilla Public License.

// ===BEGIN ICANN DOMAINS===
com
co.uk
// ck : https://en.wikipedia.org/wiki/.ck
*.ck
!www.ck
\u{516C}\u{53F8}.cn
xn--55qx5d.cn
// ===BEGIN PRIVATE DOMAINS===
github.io   // with a comment after
";

    #[test]
    fn rules() {
        let mut list = SuffixList::default();
        list.add(SAMPLE);
        for (domain, suffix) in [
            ("com", true),
            ("co.uk", true),
            ("CO.UK", true),
            ("github.io", true),
            ("example.co.uk", false),
            // Every name directly under `ck` is one, but for the exception.
            ("anything.ck", true),
            ("www.ck", false),
            ("ck", false),
            ("a.b.ck", false),
            // Only the `xn--` form matches.
            ("xn--55qx5d.cn", true),
            ("example.com", false),
        ] {
            assert_eq!(list.is_public_suffix(domain), suffix, "{domain}");
        }
        assert!(!list.is_empty());
        assert!(SuffixList::default().is_empty());
    }
}
