//! The filter's logic, apart from Envoy, so its tests run on the host.
//!
//! Nothing here ever puts a value into an error: the messages name secrets,
//! and describe a payload's shape, but never quote it.

use std::collections::BTreeMap;

/// What a placeholder looks like: `{{seekrit:NAME}}`, as the seekrit gateway
/// writes it, so a dev container works with either gateway.
pub const PREFIX: &str = "{{seekrit:";
pub const SUFFIX: &str = "}}";

/// The longest secret name (`surf_scoter_lib.gateway._SECRET_NAME`).
pub const MAX_NAME: usize = 64;
/// The largest value (`surf_scoter_lib.gateway_secrets.MAX_VALUE_BYTES`).
pub const MAX_VALUE_BYTES: usize = 64 * 1024;
/// The most secrets one delivery may hold.
pub const MAX_SECRETS: usize = 256;
/// The largest delivery the inject listener reads.
pub const MAX_PAYLOAD_BYTES: usize = 1024 * 1024;
/// Values shorter than this are not scrubbed from responses: too likely to
/// match by chance (as the seekrit gateway).
pub const MIN_SCRUB_BYTES: usize = 8;

/// The secrets, by name.
pub type Secrets = BTreeMap<String, String>;

/// Whether `name` is a secret's name: an upper-case letter, then up to 63
/// upper-case letters, digits or `_`.
pub fn name_is_valid(name: &str) -> bool {
    let b = name.as_bytes();
    !b.is_empty()
        && b.len() <= MAX_NAME
        && b[0].is_ascii_uppercase()
        && b[1..]
            .iter()
            .all(|c| c.is_ascii_uppercase() || c.is_ascii_digit() || *c == b'_')
}

/// The placeholder for `name`.
pub fn placeholder(name: &str) -> String {
    format!("{PREFIX}{name}{SUFFIX}")
}

/// Each well-formed placeholder in `text`: its byte range, and its name.
/// Anything else that starts like one is left as it is: it names no secret.
fn placeholders(text: &str) -> Vec<(usize, usize, &str)> {
    let mut found = Vec::new();
    let mut from = 0;
    while let Some(at) = text[from..].find(PREFIX) {
        let start = from + at;
        let rest = &text[start + PREFIX.len()..];
        if let Some(len) = rest.find(SUFFIX) {
            let name = &rest[..len];
            if name_is_valid(name) {
                let end = start + PREFIX.len() + len + SUFFIX.len();
                found.push((start, end, name));
                from = end;
                continue;
            }
        }
        from = start + 1;
    }
    found
}

/// Whether `text` holds a placeholder.
pub fn has_placeholder(text: &str) -> bool {
    !placeholders(text).is_empty()
}

/// Why a request can't have its placeholders filled.
#[derive(Debug, PartialEq, Eq)]
pub enum Refusal {
    /// The host's rules don't name this secret.
    Denied(String),
    /// The rules name it, but the delivery didn't have it.
    Unknown(String),
    /// The secrets haven't been delivered yet.
    Pending(String),
    /// A placeholder where only a header value may have one.
    Misplaced(String),
}

impl Refusal {
    /// The response's status and text: names only, never a value.
    pub fn response(&self) -> (u32, String) {
        match self {
            Refusal::Denied(n) => (403, format!("placeholder {} is not allowed toward this upstream\n", placeholder(n))),
            Refusal::Unknown(n) => (403, format!("placeholder {} references a secret that is not available\n", placeholder(n))),
            Refusal::Pending(n) => (503, format!("placeholder {} cannot be filled: secrets not loaded yet\n", placeholder(n))),
            Refusal::Misplaced(where_) => (
                403,
                format!("the Surf Scoter network gateway fills placeholders only into header values, not {where_}\n"),
            ),
        }
    }

    /// The reason, for the log.
    pub fn reason(&self) -> &'static str {
        match self {
            Refusal::Denied(_) => "not_allowed",
            Refusal::Unknown(_) => "unknown_secret",
            Refusal::Pending(_) => "secrets_pending",
            Refusal::Misplaced(_) => "misplaced_placeholder",
        }
    }
}

/// `value` with its placeholders filled in, and the names filled, in order;
/// `None` when it holds none.  `store` is `None` until the secrets arrive.
///
/// A name the host may not have is refused first, whatever the store holds,
/// so a refusal never says whether a secret exists.
pub fn fill(
    value: &str,
    allow: &[String],
    store: Option<&Secrets>,
) -> Result<Option<(String, Vec<String>)>, Refusal> {
    let found = placeholders(value);
    if found.is_empty() {
        return Ok(None);
    }
    let mut out = String::with_capacity(value.len());
    let mut names = Vec::new();
    let mut last = 0;
    for (start, end, name) in found {
        if !allow.iter().any(|a| a == name) {
            return Err(Refusal::Denied(name.to_string()));
        }
        let Some(store) = store else {
            return Err(Refusal::Pending(name.to_string()));
        };
        let Some(secret) = store.get(name) else {
            return Err(Refusal::Unknown(name.to_string()));
        };
        out.push_str(&value[last..start]);
        out.push_str(secret);
        last = end;
        if !names.iter().any(|n| n == name) {
            names.push(name.to_string());
        }
    }
    out.push_str(&value[last..]);
    Ok(Some((out, names)))
}

/// Whether a request header's name is one placeholders are never filled
/// into: the pseudo-headers (`:path` is logged, `:authority` and `:method`
/// are routed on), and `host`.
pub fn is_unfillable_header(name: &str) -> bool {
    name.starts_with(':') || name.eq_ignore_ascii_case("host")
}

// ---------------------------------------------------------------------
// The delivery

/// The secrets in an inject payload, `{"values": {"NAME": "value", ...}}`,
/// checked; or why not, describing its shape only.
pub fn parse_payload(data: &[u8]) -> Result<Secrets, &'static str> {
    if data.len() > MAX_PAYLOAD_BYTES {
        return Err("the payload is too large");
    }
    // serde_json's own error can quote the input: never pass it on.
    let doc: serde_json::Value =
        serde_json::from_slice(data).map_err(|_| "the payload isn't JSON")?;
    let serde_json::Value::Object(top) = doc else {
        return Err("the payload must be an object: {\"values\": {...}}");
    };
    if top.len() != 1 || !top.contains_key("values") {
        return Err("the payload must have only \"values\" (the Envoy gateway takes no token)");
    }
    let Some(serde_json::Value::Object(values)) = top.get("values") else {
        return Err("\"values\" must be an object of names to text");
    };
    if values.len() > MAX_SECRETS {
        return Err("the payload has too many secrets");
    }
    let mut secrets = Secrets::new();
    for (name, value) in values {
        if !name_is_valid(name) {
            return Err("a secret's name must be an upper-case letter, then upper-case letters, digits or _");
        }
        let serde_json::Value::String(value) = value else {
            return Err("each secret's value must be text");
        };
        if value.is_empty() {
            return Err("a secret's value is empty");
        }
        if value.len() > MAX_VALUE_BYTES {
            return Err("a secret's value is too large");
        }
        // A value goes into a header: no line breaks, NULs or other control
        // characters (tab is allowed, as in a header).
        if value.bytes().any(|b| (b < 0x20 && b != b'\t') || b == 0x7f) {
            return Err("a secret's value has a control character, which a header can't hold");
        }
        secrets.insert(name.clone(), value.clone());
    }
    Ok(secrets)
}

/// The shared data's value until the secrets arrive.
pub const PENDING: &[u8] = b"P";
const LOADED: u8 = b'L';

/// The secrets, as the shared data holds them: `L`, then each name and
/// value, each ended by a NUL (neither can hold one).
pub fn encode(secrets: &Secrets) -> Vec<u8> {
    let size = 1 + secrets
        .iter()
        .map(|(n, v)| n.len() + v.len() + 2)
        .sum::<usize>();
    let mut out = Vec::with_capacity(size);
    out.push(LOADED);
    for (name, value) in secrets {
        out.extend_from_slice(name.as_bytes());
        out.push(0);
        out.extend_from_slice(value.as_bytes());
        out.push(0);
    }
    out
}

/// The secrets from the shared data's value; `None` when they haven't
/// arrived (pending, absent or unreadable).
pub fn decode(data: &[u8]) -> Option<Secrets> {
    let (&first, rest) = data.split_first()?;
    if first != LOADED {
        return None;
    }
    let mut secrets = Secrets::new();
    let mut parts = rest.split(|b| *b == 0);
    loop {
        let name = parts.next()?;
        if name.is_empty() {
            // The end: `rest` finishes with a NUL, so the last part is empty.
            return parts.next().is_none().then_some(secrets);
        }
        let value = parts.next()?;
        secrets.insert(
            String::from_utf8(name.to_vec()).ok()?,
            String::from_utf8(value.to_vec()).ok()?,
        );
    }
}

/// Overwrite `data` with zeros, so a buffer freed after it held a secret
/// doesn't keep it.  Best effort: copies made elsewhere aren't reached.
pub fn zero(data: &mut [u8]) {
    for b in data.iter_mut() {
        // SAFETY: a valid, aligned, exclusive reference to the byte.
        unsafe { std::ptr::write_volatile(b, 0) };
    }
    std::sync::atomic::compiler_fence(std::sync::atomic::Ordering::SeqCst);
}

// ---------------------------------------------------------------------
// The response

/// Scrubs the values put into a request out of its response: in the
/// headers, and across the body's chunks, as written, percent-encoded
/// (either case) or JSON-escaped, putting the placeholder back in their place.
pub struct Scrubber {
    /// What to find, and what to put in its place; longest first.
    patterns: Vec<(Vec<u8>, Vec<u8>)>,
    /// The end of the last chunk, held back while it may be the start of a value.
    held: Vec<u8>,
}

fn percent_encoded(value: &str, upper: bool) -> String {
    let hex: &[u8; 16] = if upper {
        b"0123456789ABCDEF"
    } else {
        b"0123456789abcdef"
    };
    let mut out = String::with_capacity(value.len() * 3);
    for &b in value.as_bytes() {
        if b.is_ascii_alphanumeric() || b"-._~".contains(&b) {
            out.push(b as char);
        } else {
            out.push('%');
            out.push(hex[(b >> 4) as usize] as char);
            out.push(hex[(b & 15) as usize] as char);
        }
    }
    out
}

fn json_escaped(value: &str) -> String {
    let quoted = serde_json::Value::String(value.to_string()).to_string();
    quoted[1..quoted.len() - 1].to_string()
}

impl Scrubber {
    /// A scrubber for the `(name, value)` pairs put into a request; `None`
    /// when none is long enough to scrub.
    pub fn new(injected: &[(String, String)]) -> Option<Scrubber> {
        let mut patterns: Vec<(Vec<u8>, Vec<u8>)> = Vec::new();
        for (name, value) in injected {
            if value.len() < MIN_SCRUB_BYTES {
                continue;
            }
            let replacement = placeholder(name).into_bytes();
            for form in [
                value.clone(),
                percent_encoded(value, true),
                percent_encoded(value, false),
                json_escaped(value),
            ] {
                let form = form.into_bytes();
                if !patterns.iter().any(|(p, _)| *p == form) {
                    patterns.push((form, replacement.clone()));
                }
            }
        }
        if patterns.is_empty() {
            return None;
        }
        patterns.sort_by_key(|(p, _)| std::cmp::Reverse(p.len()));
        Some(Scrubber {
            patterns,
            held: Vec::new(),
        })
    }

    fn match_at(&self, data: &[u8], at: usize) -> Option<&(Vec<u8>, Vec<u8>)> {
        self.patterns
            .iter()
            .find(|(p, _)| data[at..].starts_with(p))
    }

    fn may_start(&self, tail: &[u8]) -> bool {
        self.patterns
            .iter()
            .any(|(p, _)| p.len() > tail.len() && p.starts_with(tail))
    }

    /// A header's value, scrubbed; `None` when it held no value.
    pub fn header(&self, value: &str) -> Option<String> {
        let data = value.as_bytes();
        let mut out = Vec::with_capacity(data.len());
        let mut changed = false;
        let mut at = 0;
        while at < data.len() {
            if let Some((p, r)) = self.match_at(data, at) {
                out.extend_from_slice(r);
                at += p.len();
                changed = true;
            } else {
                out.push(data[at]);
                at += 1;
            }
        }
        // Only ASCII placeholders replace whole UTF-8 values, so it stays UTF-8.
        changed.then(|| String::from_utf8(out).unwrap_or_default())
    }

    /// The next chunk of the body, scrubbed.  The end of a chunk that may be
    /// the start of a value is held back for the next; `end` gives it all.
    pub fn chunk(&mut self, data: &[u8], end: bool) -> Vec<u8> {
        let mut buf = std::mem::take(&mut self.held);
        buf.extend_from_slice(data);
        let mut out = Vec::with_capacity(buf.len());
        let mut at = 0;
        while at < buf.len() {
            if let Some((p, r)) = self.match_at(&buf, at) {
                out.extend_from_slice(r);
                at += p.len();
            } else if !end && self.may_start(&buf[at..]) {
                self.held = buf[at..].to_vec();
                break;
            } else {
                out.push(buf[at]);
                at += 1;
            }
        }
        zero(&mut buf);
        out
    }

    /// Whether the end of the body is held back.
    pub fn holds(&self) -> bool {
        !self.held.is_empty()
    }
}

impl Drop for Scrubber {
    fn drop(&mut self) {
        zero(&mut self.held);
        for (p, _) in &mut self.patterns {
            zero(p);
        }
    }
}

// ---------------------------------------------------------------------
// The configuration

/// What a filter instance does, from its plugin configuration.
#[derive(Debug, PartialEq, Eq)]
pub enum Mode {
    /// `{"mode": "inject"}`: take the delivery.
    Inject,
    /// `{"mode": "fill", "allow": ["NAME", ...]}`: fill these secrets in.
    Fill(Vec<String>),
}

/// The filter instance's mode, from its plugin configuration; `None` when
/// it's not one (Envoy then refuses the configuration).
pub fn parse_mode(data: &[u8]) -> Option<Mode> {
    let serde_json::Value::Object(cfg) = serde_json::from_slice(data).ok()? else {
        return None;
    };
    match (cfg.get("mode")?.as_str()?, cfg.len()) {
        ("inject", 1) => Some(Mode::Inject),
        ("fill", 2) => {
            let allow: Vec<String> = cfg
                .get("allow")?
                .as_array()?
                .iter()
                .map(|v| v.as_str().filter(|n| name_is_valid(n)).map(str::to_string))
                .collect::<Option<_>>()?;
            (!allow.is_empty()).then_some(Mode::Fill(allow))
        }
        _ => None,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn store(pairs: &[(&str, &str)]) -> Secrets {
        pairs
            .iter()
            .map(|(n, v)| (n.to_string(), v.to_string()))
            .collect()
    }

    fn allow(names: &[&str]) -> Vec<String> {
        names.iter().map(|n| n.to_string()).collect()
    }

    #[test]
    fn names() {
        assert!(name_is_valid("A"));
        assert!(name_is_valid("GITHUB_TOKEN2"));
        assert!(name_is_valid(&"A".repeat(64)));
        assert!(!name_is_valid(&"A".repeat(65)));
        for bad in ["", "a", "_A", "1A", "A-B", "A B", "Ä"] {
            assert!(!name_is_valid(bad), "{bad:?}");
        }
    }

    #[test]
    fn fills_header_values() {
        let s = store(&[("TOKEN", "s3cr3t-value"), ("OTHER", "o")]);
        let got = fill("Bearer {{seekrit:TOKEN}}", &allow(&["TOKEN"]), Some(&s))
            .unwrap()
            .unwrap();
        assert_eq!(
            got,
            ("Bearer s3cr3t-value".to_string(), vec!["TOKEN".to_string()])
        );
        let got = fill(
            "{{seekrit:TOKEN}}:{{seekrit:OTHER}}:{{seekrit:TOKEN}}",
            &allow(&["TOKEN", "OTHER"]),
            Some(&s),
        )
        .unwrap()
        .unwrap();
        assert_eq!(got.0, "s3cr3t-value:o:s3cr3t-value");
        assert_eq!(got.1, vec!["TOKEN".to_string(), "OTHER".to_string()]);
    }

    #[test]
    fn leaves_what_isnt_a_placeholder() {
        let s = store(&[("TOKEN", "v")]);
        for text in [
            "plain",
            "{{seekrit:}}",
            "{{seekrit:lower}}",
            "{{seekrit:TOKEN}",
            "{seekrit:TOKEN}}",
            "{{SEEKRIT:TOKEN}}",
        ] {
            assert_eq!(
                fill(text, &allow(&["TOKEN"]), Some(&s)),
                Ok(None),
                "{text:?}"
            );
            assert!(!has_placeholder(text));
        }
        // A broken one before a good one doesn't hide it.
        let got = fill("{{seekrit:x{{seekrit:TOKEN}}", &allow(&["TOKEN"]), Some(&s))
            .unwrap()
            .unwrap();
        assert_eq!(got.0, "{{seekrit:xv");
    }

    #[test]
    fn refusals_in_order() {
        let s = store(&[("TOKEN", "v")]);
        // Not allowed: refused whether or not it exists, loaded or not.
        assert_eq!(
            fill("{{seekrit:TOKEN}}", &allow(&["OTHER"]), Some(&s)),
            Err(Refusal::Denied("TOKEN".into()))
        );
        assert_eq!(
            fill("{{seekrit:NOPE}}", &allow(&["OTHER"]), None),
            Err(Refusal::Denied("NOPE".into()))
        );
        assert_eq!(
            fill("{{seekrit:TOKEN}}", &allow(&["TOKEN"]), None),
            Err(Refusal::Pending("TOKEN".into()))
        );
        assert_eq!(
            fill("{{seekrit:NOPE}}", &allow(&["NOPE"]), Some(&s)),
            Err(Refusal::Unknown("NOPE".into()))
        );
        // A later refused one refuses the whole value.
        assert!(fill(
            "{{seekrit:TOKEN}} {{seekrit:OTHER}}",
            &allow(&["TOKEN"]),
            Some(&s)
        )
        .is_err());
        assert_eq!(Refusal::Pending("A".into()).response().0, 503);
        assert_eq!(Refusal::Denied("A".into()).response().0, 403);
    }

    #[test]
    fn unfillable_headers() {
        for name in [":path", ":authority", ":method", ":scheme", "host", "Host"] {
            assert!(is_unfillable_header(name), "{name}");
        }
        for name in ["authorization", "x-api-key", "cookie"] {
            assert!(!is_unfillable_header(name), "{name}");
        }
    }

    #[test]
    fn payloads() {
        let got = parse_payload(br#"{"values": {"A": "one", "B_2": "two\tthree"}}"#).unwrap();
        assert_eq!(got, store(&[("A", "one"), ("B_2", "two\tthree")]));
        assert_eq!(parse_payload(br#"{"values": {}}"#).unwrap(), Secrets::new());
        for bad in [
            &b"not json"[..],
            br#"[]"#,
            br#"{"token": "skt_x"}"#,
            br#"{"values": {}, "token": "x"}"#,
            br#"{"values": []}"#,
            br#"{"values": {"lower": "x"}}"#,
            br#"{"values": {"A": 1}}"#,
            br#"{"values": {"A": ""}}"#,
            br#"{"values": {"A": "x\r\nInjected: y"}}"#,
            br#"{"values": {"A": "x\u0000"}}"#,
        ] {
            assert!(
                parse_payload(bad).is_err(),
                "{}",
                String::from_utf8_lossy(bad)
            );
        }
        let big = format!(
            r#"{{"values": {{"A": "{}"}}}}"#,
            "x".repeat(MAX_VALUE_BYTES + 1)
        );
        assert!(parse_payload(big.as_bytes()).is_err());
    }

    #[test]
    fn payload_errors_never_quote_it() {
        let secret = "hunter2-very-secret";
        for bad in [
            format!(r#"{{"values": {{"A": "{secret}\n"}}}}"#),
            format!(r#"{{"values": {{"a": "{secret}"}}}}"#),
            format!(r#"{{"values": {{"A": "{secret}"#),
            format!(r#"{{"{secret}": 1}}"#),
        ] {
            let why = parse_payload(bad.as_bytes()).unwrap_err();
            assert!(!why.contains("hunter2"), "{why}");
        }
    }

    #[test]
    fn encoding_round_trips() {
        let s = store(&[("A", "one"), ("B", "two words\t")]);
        assert_eq!(decode(&encode(&s)), Some(s));
        assert_eq!(decode(&encode(&Secrets::new())), Some(Secrets::new()));
        assert_eq!(decode(PENDING), None);
        assert_eq!(decode(b""), None);
        assert_eq!(decode(b"LA\0"), None);
    }

    #[test]
    fn modes() {
        assert_eq!(parse_mode(br#"{"mode": "inject"}"#), Some(Mode::Inject));
        assert_eq!(
            parse_mode(br#"{"mode": "fill", "allow": ["A", "B"]}"#),
            Some(Mode::Fill(allow(&["A", "B"])))
        );
        for bad in [
            &br#"{"mode": "fill"}"#[..],
            br#"{"mode": "fill", "allow": []}"#,
            br#"{"mode": "fill", "allow": ["a"]}"#,
            br#"{"mode": "inject", "allow": ["A"]}"#,
            br#"{"mode": "other"}"#,
            br#""inject""#,
        ] {
            assert_eq!(parse_mode(bad), None, "{}", String::from_utf8_lossy(bad));
        }
    }

    fn pairs(p: &[(&str, &str)]) -> Vec<(String, String)> {
        p.iter()
            .map(|(n, v)| (n.to_string(), v.to_string()))
            .collect()
    }

    #[test]
    fn scrubs_headers() {
        let s = Scrubber::new(&pairs(&[("TOKEN", "abc/def+ghi")])).unwrap();
        assert_eq!(
            s.header("got abc/def+ghi!").as_deref(),
            Some("got {{seekrit:TOKEN}}!")
        );
        assert_eq!(
            s.header("q=abc%2Fdef%2Bghi").as_deref(),
            Some("q={{seekrit:TOKEN}}")
        );
        assert_eq!(
            s.header("q=abc%2fdef%2bghi").as_deref(),
            Some("q={{seekrit:TOKEN}}")
        );
        assert_eq!(s.header("nothing here"), None);
        let s = Scrubber::new(&pairs(&[("TOKEN", "quote\"back\\slash")])).unwrap();
        assert_eq!(
            s.header(r#"{"v":"quote\"back\\slash"}"#).as_deref(),
            Some(r#"{"v":"{{seekrit:TOKEN}}"}"#)
        );
    }

    #[test]
    fn short_values_arent_scrubbed() {
        assert!(Scrubber::new(&pairs(&[("A", "1234567")])).is_none());
        assert!(Scrubber::new(&pairs(&[("A", "12345678")])).is_some());
    }

    #[test]
    fn scrubs_across_chunks() {
        let body = b"start s3cr3t-value middle s3cr3t-value end".to_vec();
        for split in 0..=body.len() {
            for split2 in split..=body.len() {
                let mut s = Scrubber::new(&pairs(&[("T", "s3cr3t-value")])).unwrap();
                let mut out = s.chunk(&body[..split], false);
                out.extend(s.chunk(&body[split..split2], false));
                out.extend(s.chunk(&body[split2..], true));
                assert_eq!(
                    out,
                    b"start {{seekrit:T}} middle {{seekrit:T}} end".to_vec(),
                    "{split} {split2}"
                );
                assert!(!s.holds());
            }
        }
    }

    #[test]
    fn holds_only_a_possible_start() {
        let mut s = Scrubber::new(&pairs(&[("T", "s3cr3t-value")])).unwrap();
        assert_eq!(s.chunk(b"plain text", false), b"plain text".to_vec());
        assert!(!s.holds());
        assert_eq!(s.chunk(b"ends with s3cr", false), b"ends with ".to_vec());
        assert!(s.holds());
        // Not the value after all: given back.
        assert_eq!(s.chunk(b"ew", false), b"s3crew".to_vec());
        assert_eq!(s.chunk(b"s3c", true), b"s3c".to_vec());
    }
}
