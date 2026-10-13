//! Reading configuration files: a size limit, and YAML without surprises.
//!
//! YAML has no way to include another file, and `yaml_serde` (with its
//! `libyaml-rs` parser) never touches the file system while parsing.  It
//! limits alias expansion and nesting depth, so "billion laughs" documents
//! fail instead of exhausting memory.  Custom tags such as `!include x` do
//! parse, though, as tagged values; `tag_errors` reports them so no file can
//! suggest a meaning the tools don't give it.

use std::fs::File;
use std::io::Read;
use std::path::Path;

use yaml_serde::Value;

use super::{child_key, index_key};

/// The largest configuration file the tools read.
pub const MAX_CONFIG_FILE_SIZE: u64 = 2 * 1024 * 1024;

/// Read a whole configuration file, refusing anything over `MAX_CONFIG_FILE_SIZE`.
/// The limit applies while reading, so special or growing files can't get around it.
pub fn read_limited(path: &Path) -> Result<String, String> {
    let file = File::open(path).map_err(|e| format!("cannot read the file: {e}"))?;
    let mut bytes = Vec::new();
    file.take(MAX_CONFIG_FILE_SIZE + 1)
        .read_to_end(&mut bytes)
        .map_err(|e| format!("cannot read the file: {e}"))?;
    if bytes.len() as u64 > MAX_CONFIG_FILE_SIZE {
        return Err(format!(
            "the file is larger than the {} MB limit",
            MAX_CONFIG_FILE_SIZE / (1024 * 1024)
        ));
    }
    String::from_utf8(bytes).map_err(|_| "the file is not valid UTF-8".to_string())
}

/// Parse YAML text.  An empty document is `Value::Null`.
pub fn parse_yaml(text: &str) -> Result<Value, String> {
    yaml_serde::from_str(text).map_err(|e| format!("invalid YAML: {e}"))
}

/// Every tagged value (`!tag ...`) under `value`, as (key path, message).
pub fn tag_errors(value: &Value) -> Vec<(String, String)> {
    let mut out = Vec::new();
    find_tags(value, "", &mut out);
    out
}

fn find_tags(value: &Value, key: &str, out: &mut Vec<(String, String)>) {
    match value {
        Value::Tagged(tagged) => out.push((
            key.to_string(),
            format!("YAML tags such as `{}` aren't supported", tagged.tag),
        )),
        Value::Sequence(items) => {
            for (i, item) in items.iter().enumerate() {
                find_tags(item, &index_key(key, i), out);
            }
        }
        Value::Mapping(map) => {
            for (k, v) in map {
                let name = match k {
                    Value::String(s) => s.clone(),
                    other => format!("{other:?}"),
                };
                find_tags(k, key, out);
                find_tags(v, &child_key(key, &name), out);
            }
        }
        Value::Null | Value::Bool(_) | Value::Number(_) | Value::String(_) => {}
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::config::testing::TempDir;
    use std::fs;

    #[test]
    fn read_limit() {
        let dir = TempDir::new();
        let at_limit = dir.path().join("at-limit.yaml");
        fs::write(&at_limit, vec![b'#'; MAX_CONFIG_FILE_SIZE as usize]).unwrap();
        assert_eq!(
            read_limited(&at_limit).unwrap().len(),
            MAX_CONFIG_FILE_SIZE as usize
        );

        let over = dir.path().join("over.yaml");
        fs::write(&over, vec![b'#'; MAX_CONFIG_FILE_SIZE as usize + 1]).unwrap();
        assert_eq!(
            read_limited(&over).unwrap_err(),
            "the file is larger than the 2 MB limit"
        );
    }

    #[test]
    fn read_errors() {
        let dir = TempDir::new();
        assert!(
            read_limited(&dir.path().join("missing.yaml"))
                .unwrap_err()
                .starts_with("cannot read the file: ")
        );
        let binary = dir.path().join("binary.yaml");
        fs::write(&binary, [0xff, 0xfe]).unwrap();
        assert_eq!(
            read_limited(&binary).unwrap_err(),
            "the file is not valid UTF-8"
        );
    }

    #[test]
    fn parse_empty_is_null() {
        assert_eq!(parse_yaml("").unwrap(), Value::Null);
        assert_eq!(parse_yaml("# only a comment\n").unwrap(), Value::Null);
    }

    #[test]
    fn tags_are_reported() {
        let value =
            parse_yaml("a: !include other.yaml\nb:\n  - x\n  - !!str y\n  - !env HOME\nc: plain\n")
                .unwrap();
        let errors = tag_errors(&value);
        assert_eq!(errors.len(), 2, "{errors:?}");
        assert_eq!(errors[0].0, "a");
        assert!(errors[0].1.contains("!include"), "{}", errors[0].1);
        assert_eq!(errors[1].0, "b[2]");
    }

    #[test]
    fn alias_bombs_fail() {
        let mut text = String::from("a0: &a0 [x, x, x, x, x, x, x, x, x, x]\n");
        for i in 1..12 {
            let prev = i - 1;
            text.push_str(&format!(
                "a{i}: &a{i} [*a{prev}, *a{prev}, *a{prev}, *a{prev}, *a{prev}, *a{prev}, *a{prev}, *a{prev}, *a{prev}, *a{prev}]\n"
            ));
        }
        assert!(parse_yaml(&text).is_err());
    }
}
