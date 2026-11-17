//! The project file's `egress`: the network destinations the dev container
//! needs, as rules that don't depend on the gateway.  See
//! `docs/project-configuration-file.md`.
//!
//! ```yaml
//! egress:
//!   - host: registry.npmjs.org        # port 443 by default
//!     protocol: tls
//!   - host: '*.pkg.dev'               # its subdomains, not pkg.dev itself
//!     protocol: tls
//!   - host: mirror.example.com
//!     port: 8080
//!     protocol: http
//!     methods: [GET, HEAD]
//!   - host: api.github.com            # a gateway that looks inside TLS
//!     protocol: https                 # fills in `{{seekrit:GITHUB_TOKEN}}`
//!     methods: [GET]
//!     secrets: [GITHUB_TOKEN]
//! ```
//!
//! A gateway enforces what it can of a rule; these checks are only about the
//! rule making sense, and being shown as what it does.

use serde::Deserialize;
use yaml_serde::Value;

use super::index_key;

/// The longest host name DNS allows (RFC 1035), without the trailing dot.
const MAX_HOST_NAME_CHARS: usize = 253;
/// The longest label (the parts between dots) DNS allows (RFC 1035).
const MAX_LABEL_CHARS: usize = 63;
/// The default port for TLS and HTTPS.
const HTTPS_PORT: u16 = 443;
/// The default port for plain HTTP.
const HTTP_PORT: u16 = 80;
/// The longest secret name.
const MAX_SECRET_NAME_CHARS: usize = 64;

/// How the dev container talks to the host.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Deserialize)]
#[serde(rename_all = "kebab-case")]
pub enum Protocol {
    /// A TLS connection the gateway only sees the host name of.
    Tls,
    /// Plain HTTP, which the gateway can read (and anyone in between).
    Http,
    /// HTTP over TLS that the gateway looks inside, so `methods` can be
    /// enforced; that needs a gateway that does so.
    Https,
}

impl Protocol {
    /// The protocol as the project file writes it.
    pub fn name(self) -> &'static str {
        match self {
            Protocol::Tls => "tls",
            Protocol::Http => "http",
            Protocol::Https => "https",
        }
    }

    fn default_port(self) -> u16 {
        match self {
            Protocol::Tls | Protocol::Https => HTTPS_PORT,
            Protocol::Http => HTTP_PORT,
        }
    }

    /// Whether the gateway can see the requests' methods.
    fn has_methods(self) -> bool {
        matches!(self, Protocol::Http | Protocol::Https)
    }
}

/// One egress rule.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct EgressRule {
    /// A host name, or `*.` and a host name for its subdomains.
    pub host: String,
    pub protocol: Protocol,
    pub port: u16,
    /// For `http` and `https`: the methods allowed; empty for any.
    pub methods: Vec<String>,
    /// For `https`: the names of the secrets the gateway may put into requests
    /// to this host (`{{seekrit:NAME}}` in a request becomes the user's value
    /// for `NAME`).  The values are the user's, never the project's.
    pub secrets: Vec<String>,
}

/// The setup section and `values` key naming the network gateway, which the
/// egress rules are enforced by.
pub const GATEWAY_SECTION: &str = "dev-env";
pub const GATEWAY_NAME: &str = "network-gateway-name";
/// The setup's `value-lists` key, in `GATEWAY_SECTION`, listing the ports
/// where the gateway may look inside TLS: a rule with `secrets` must be on one.
pub const GATEWAY_TLS_PORTS: &str = "network-gateway-tls-ports";

/// The ports a setup `value-lists` entry names; an entry that isn't a port
/// names none, so it can only refuse more.
pub fn ports_of(entries: &[String]) -> Vec<u16> {
    entries
        .iter()
        .filter_map(|entry| entry.parse::<u16>().ok().filter(|port| *port != 0))
        .collect()
}

impl EgressRule {
    /// The rule as the universal configuration file's `egress` holds it:
    /// `host`, `protocol`, `port`, `methods` and `secrets`, every one given.
    pub fn to_json(&self) -> serde_json::Value {
        serde_json::json!({
            "host": self.host,
            "protocol": self.protocol.name(),
            "port": self.port,
            "methods": self.methods,
            "secrets": self.secrets,
        })
    }

    /// The domain whose subdomains a wildcard rule allows (`pkg.dev` for
    /// `*.pkg.dev`), if it's one.
    pub fn wildcard_domain(&self) -> Option<&str> {
        self.host.strip_prefix("*.")
    }
}

/// An egress rule, as written.
#[derive(Deserialize)]
#[serde(deny_unknown_fields, rename_all = "kebab-case")]
struct RuleRecord {
    host: String,
    protocol: Protocol,
    #[serde(default)]
    port: Option<u16>,
    #[serde(default)]
    methods: Option<Vec<String>>,
    #[serde(default)]
    secrets: Option<Vec<String>>,
}

/// The `egress` list; problems are (key, message).
pub fn parse(value: Value, problems: &mut Vec<(String, String)>) -> Vec<EgressRule> {
    const KEY: &str = "egress";
    let entries = match value {
        Value::Null => return Vec::new(),
        Value::Sequence(entries) => entries,
        _ => {
            problems.push((KEY.to_string(), "must be a list of rules".to_string()));
            return Vec::new();
        }
    };
    let mut rules = Vec::new();
    for (i, entry) in entries.into_iter().enumerate() {
        let key = index_key(KEY, i);
        let record = match yaml_serde::from_value::<RuleRecord>(entry) {
            Ok(record) => record,
            Err(e) => {
                problems.push((key, e.to_string()));
                continue;
            }
        };
        let before = problems.len();
        if let Some(problem) = host_problem(&record.host) {
            problems.push((format!("{key}.host"), problem));
        }
        if record.port == Some(0) {
            problems.push((format!("{key}.port"), "must be 1 to 65535".to_string()));
        }
        let methods = record.methods.unwrap_or_default();
        if !methods.is_empty() && !record.protocol.has_methods() {
            problems.push((
                format!("{key}.methods"),
                "only goes with `protocol: http` or `https`: the gateway can't see the methods inside `tls`".to_string(),
            ));
        }
        for (j, method) in methods.iter().enumerate() {
            if method.is_empty() || !method.bytes().all(|b| b.is_ascii_uppercase()) {
                problems.push((
                    index_key(&format!("{key}.methods"), j),
                    format!("'{method}' must be an upper-case method name, such as GET"),
                ));
            }
        }
        let secrets = record.secrets.unwrap_or_default();
        secrets_problems(&key, &record.host, record.protocol, &secrets, problems);
        if problems.len() == before {
            rules.push(EgressRule {
                host: record.host,
                port: record.port.unwrap_or(record.protocol.default_port()),
                protocol: record.protocol,
                methods,
                secrets,
            });
        }
    }
    rules
}

/// The problems with a rule's `secrets`, added to `problems`.
///
/// Only an `https` rule, which the gateway looks inside of, can carry a
/// secret: in `tls` the gateway never sees the request, and plain `http`
/// would send the value where anyone in between can read it.  Only an exact
/// host: a wildcard would hand the secret to every name under a domain.
fn secrets_problems(
    key: &str,
    host: &str,
    protocol: Protocol,
    secrets: &[String],
    problems: &mut Vec<(String, String)>,
) {
    if secrets.is_empty() {
        return;
    }
    let key = format!("{key}.secrets");
    if protocol != Protocol::Https {
        problems.push((
            key.clone(),
            "only goes with `protocol: https`: the gateway puts a secret only into a request it reads, and sends it only over TLS".to_string(),
        ));
    }
    if host.starts_with("*.") {
        problems.push((
            key.clone(),
            format!("'{host}' is a wildcard; a secret goes only to a host named exactly"),
        ));
    }
    for (j, name) in secrets.iter().enumerate() {
        if let Some(problem) = secret_name_problem(name) {
            problems.push((index_key(&key, j), problem));
        } else if secrets[..j].contains(name) {
            problems.push((index_key(&key, j), format!("'{name}' is listed twice")));
        }
    }
}

/// Why `name` can't be a secret's name, if it can't: an upper-case letter,
/// then upper-case letters, digits and `_`, as an environment variable is
/// usually named.
pub fn secret_name_problem(name: &str) -> Option<String> {
    let valid = name.len() <= MAX_SECRET_NAME_CHARS
        && name.bytes().next().is_some_and(|b| b.is_ascii_uppercase())
        && name
            .bytes()
            .all(|b| b.is_ascii_uppercase() || b.is_ascii_digit() || b == b'_');
    (!valid).then(|| {
        format!(
            "'{name}' must be an upper-case letter, then upper-case letters, digits and `_`, at most {MAX_SECRET_NAME_CHARS} in all, such as GITHUB_TOKEN"
        )
    })
}

/// Why `host` can't be an egress rule's host, if it can't.
///
/// A host name of two or more labels, each lower-case ASCII letters, digits
/// and `-` (not at either end), or `*.` and such a name.  No IP addresses,
/// no non-ASCII names (write the `xn--` form, which is what's shown), and no
/// wildcard on a single label (`*.com`).
pub fn host_problem(host: &str) -> Option<String> {
    if !host.is_ascii() {
        return Some(format!(
            "'{host}' isn't ASCII; write the name in its `xn--` (punycode) form, so the file says what the review shows"
        ));
    }
    if host == "*" {
        return Some("a bare `*` would allow every host".to_string());
    }
    let (wildcard, name) = match host.strip_prefix("*.") {
        Some(name) => (true, name),
        None => (false, host),
    };
    if name.contains('*') {
        return Some(format!(
            "'{host}' can only have a `*` as its first label, as `*.example.com`"
        ));
    }
    if name.len() > MAX_HOST_NAME_CHARS {
        return Some(format!("'{host}' is longer than a host name can be"));
    }
    let labels: Vec<&str> = name.split('.').collect();
    for label in &labels {
        let valid = !label.is_empty()
            && label.len() <= MAX_LABEL_CHARS
            && !label.starts_with('-')
            && !label.ends_with('-')
            && label
                .bytes()
                .all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || b == b'-');
        if !valid {
            return Some(format!(
                "'{host}' isn't a host name: each part must be lower-case letters, digits and `-` (not at either end)"
            ));
        }
    }
    if labels
        .iter()
        .all(|label| label.bytes().all(|b| b.is_ascii_digit()))
    {
        return Some(format!("'{host}' is an IP address; give a host name"));
    }
    if labels.len() < 2 {
        return Some(if wildcard {
            format!("'{host}' would allow every name under a top-level domain")
        } else {
            format!("'{host}' needs a domain, such as `{host}.example.com`")
        });
    }
    None
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::config::read::parse_yaml;

    fn rules(text: &str) -> Result<Vec<EgressRule>, Vec<String>> {
        let mut problems = Vec::new();
        let rules = parse(parse_yaml(text).unwrap(), &mut problems);
        if problems.is_empty() {
            Ok(rules)
        } else {
            Err(problems
                .into_iter()
                .map(|(k, m)| format!("{k}: {m}"))
                .collect())
        }
    }

    #[test]
    fn parse_rules() {
        let rules = rules(concat!(
            "- {host: registry.npmjs.org, protocol: tls}\n",
            "- {host: '*.pkg.dev', protocol: https, methods: [GET]}\n",
            "- {host: mirror.example.com, port: 8080, protocol: http, methods: [GET, HEAD]}\n",
            "- {host: plain.example.com, protocol: http}\n",
        ))
        .unwrap();
        assert_eq!(
            rules[0],
            EgressRule {
                host: "registry.npmjs.org".to_string(),
                protocol: Protocol::Tls,
                port: 443,
                methods: vec![],
                secrets: vec![],
            }
        );
        assert_eq!(rules[1].port, 443);
        assert_eq!(rules[1].wildcard_domain(), Some("pkg.dev"));
        assert_eq!(rules[0].wildcard_domain(), None);
        assert_eq!((rules[2].port, rules[2].methods.len()), (8080, 2));
        assert_eq!(rules[3].port, 80);
    }

    #[test]
    fn parse_errors() {
        assert_eq!(
            rules(concat!(
                "- {host: a.example.com, protocol: tls, methods: [GET]}\n",
                "- {host: b.example.com, protocol: http, methods: [get, '']}\n",
                "- {host: c.example.com, protocol: http, port: 0}\n",
            ))
            .unwrap_err(),
            [
                "egress[0].methods: only goes with `protocol: http` or `https`: the gateway can't see the methods inside `tls`",
                "egress[1].methods[0]: 'get' must be an upper-case method name, such as GET",
                "egress[1].methods[1]: '' must be an upper-case method name, such as GET",
                "egress[2].port: must be 1 to 65535",
            ]
        );
        let errors = rules("- {host: a.example.com, protocol: ssh}\n").unwrap_err();
        assert!(errors[0].contains("unknown variant `ssh`"), "{errors:?}");
        let errors = rules("- {host: a.example.com, protocol: tls, port: 70000}\n").unwrap_err();
        assert!(errors[0].starts_with("egress[0]: "), "{errors:?}");
        let errors = rules("- {host: a.example.com, protocol: tls, other: 1}\n").unwrap_err();
        assert!(errors[0].contains("unknown field `other`"), "{errors:?}");
        let errors = rules("- {protocol: tls}\n").unwrap_err();
        assert!(errors[0].contains("missing field `host`"), "{errors:?}");
        assert_eq!(
            rules("egress: no\n").unwrap_err(),
            ["egress: must be a list of rules"]
        );
    }

    #[test]
    fn secrets() {
        let parsed = rules(
            "- {host: api.github.com, protocol: https, methods: [GET], secrets: [GITHUB_TOKEN, NPM_TOKEN_2]}\n",
        )
        .unwrap();
        assert_eq!(parsed[0].secrets, ["GITHUB_TOKEN", "NPM_TOKEN_2"]);
        assert_eq!(
            parsed[0].to_json()["secrets"],
            serde_json::json!(["GITHUB_TOKEN", "NPM_TOKEN_2"])
        );
        assert_eq!(
            rules(concat!(
                "- {host: a.example.com, protocol: tls, secrets: [A]}\n",
                "- {host: b.example.com, protocol: http, secrets: [B]}\n",
                "- {host: '*.example.com', protocol: https, secrets: [C]}\n",
                "- {host: d.example.com, protocol: https, secrets: [lower, _X, 9X, A-B, '', D, D]}\n",
            ))
            .unwrap_err(),
            [
                "egress[0].secrets: only goes with `protocol: https`: the gateway puts a secret only into a request it reads, and sends it only over TLS",
                "egress[1].secrets: only goes with `protocol: https`: the gateway puts a secret only into a request it reads, and sends it only over TLS",
                "egress[2].secrets: '*.example.com' is a wildcard; a secret goes only to a host named exactly",
                "egress[3].secrets[0]: 'lower' must be an upper-case letter, then upper-case letters, digits and `_`, at most 64 in all, such as GITHUB_TOKEN",
                "egress[3].secrets[1]: '_X' must be an upper-case letter, then upper-case letters, digits and `_`, at most 64 in all, such as GITHUB_TOKEN",
                "egress[3].secrets[2]: '9X' must be an upper-case letter, then upper-case letters, digits and `_`, at most 64 in all, such as GITHUB_TOKEN",
                "egress[3].secrets[3]: 'A-B' must be an upper-case letter, then upper-case letters, digits and `_`, at most 64 in all, such as GITHUB_TOKEN",
                "egress[3].secrets[4]: '' must be an upper-case letter, then upper-case letters, digits and `_`, at most 64 in all, such as GITHUB_TOKEN",
                "egress[3].secrets[6]: 'D' is listed twice",
            ]
        );
        assert!(secret_name_problem(&"A".repeat(64)).is_none());
        assert!(secret_name_problem(&"A".repeat(65)).is_some());
        // An empty list is no secrets, as leaving it out is.
        assert!(rules("- {host: a.example.com, protocol: tls, secrets: []}\n").is_ok());
    }

    #[test]
    fn hosts() {
        for good in [
            "example.com",
            "a-b.example.co.uk",
            "*.example.com",
            "xn--bcher-kva.example",
            "1password.com",
        ] {
            assert_eq!(host_problem(good), None, "{good}");
        }
        let problem = |host: &str| host_problem(host).unwrap();
        assert!(problem("b\u{FC}cher.example").contains("isn't ASCII"));
        // A Cyrillic `o` (U+043E) in place of the Latin one.
        assert!(problem("github.c\u{43e}m").contains("isn't ASCII"));
        assert!(problem("*").contains("every host"));
        assert!(problem("*.com").contains("top-level domain"));
        assert!(problem("a.*.example.com").contains("first label"));
        assert!(problem("**.example.com").contains("first label"));
        assert!(problem("Example.com").contains("lower-case"));
        assert!(problem("-a.example.com").contains("lower-case"));
        assert!(problem("a..example.com").contains("lower-case"));
        assert!(problem("example.com.").contains("lower-case"));
        assert!(problem("a_b.example.com").contains("lower-case"));
        assert!(problem("10.0.0.1").contains("IP address"));
        assert!(problem("[::1]").contains("lower-case"));
        assert!(problem("localhost").contains("needs a domain"));
        assert!(problem(&format!("{}.com", "a".repeat(64))).contains("lower-case"));
    }
}
