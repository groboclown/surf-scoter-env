//! Reading configuration files: a size limit, and YAML without surprises.
//!
//! YAML has no way to include another file, and `yaml_serde` (with its
//! `libyaml-rs` parser) never touches the file system while parsing.  It
//! limits alias expansion and nesting depth, so "billion laughs" documents
//! fail instead of exhausting memory.  Custom tags such as `!include x` do
//! parse, though, as tagged values; `tag_errors` reports them so no file can
//! suggest a meaning the tools don't give it.

use std::fs::{File, OpenOptions};
use std::io::Read;
use std::path::Path;

use yaml_serde::Value;

use super::{child_key, index_key};

/// The largest configuration file the tools read.
pub const MAX_CONFIG_FILE_SIZE: u64 = 2 * 1024 * 1024;

/// The largest universal configuration file the tools read (or write): it
/// holds the other files' contents, and more.
pub const MAX_UNIVERSAL_FILE_SIZE: u64 = 4 * 1024 * 1024;

/// Read a whole configuration file, refusing anything over `MAX_CONFIG_FILE_SIZE`.
pub fn read_limited(path: &Path) -> Result<String, String> {
    read_limited_to(path, MAX_CONFIG_FILE_SIZE)
}

/// Read a whole file, refusing anything over `max` bytes.  The limit applies
/// while reading, so special or growing files can't get around it.
pub fn read_limited_to(path: &Path, max: u64) -> Result<String, String> {
    let file = File::open(path).map_err(|e| format!("cannot read the file: {e}"))?;
    read_file(file, max)
}

/// Read a whole configuration file the project supplies, as `read_limited`
/// does, refusing anything but a regular file.  A symlink isn't followed (it
/// could point at a secret, or at a file the container writes), and a FIFO or
/// device is refused before anything is read from it, as reading one could
/// hang or have side effects.
pub fn read_regular_file(path: &Path) -> Result<String, String> {
    let file = open_regular(path)?;
    read_file(file, MAX_CONFIG_FILE_SIZE)
}

/// Open `path` without following a symlink in its last component, and
/// without blocking, then require a regular file.
#[cfg(unix)]
fn open_regular(path: &Path) -> Result<File, String> {
    use std::os::unix::fs::OpenOptionsExt;
    let file = OpenOptions::new()
        .read(true)
        .custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK)
        .open(path)
        .map_err(|e| match e.raw_os_error() {
            Some(libc::ELOOP) => "the file is a symlink; it must be a regular file".to_string(),
            _ => format!("cannot read the file: {e}"),
        })?;
    require_regular(file)
}

/// Open `path` without following a symlink or other reparse point, then
/// require a regular file.
#[cfg(windows)]
fn open_regular(path: &Path) -> Result<File, String> {
    use std::os::windows::fs::OpenOptionsExt;
    // FILE_FLAG_OPEN_REPARSE_POINT: open the link itself, not its target.
    const OPEN_REPARSE_POINT: u32 = 0x0020_0000;
    let file = OpenOptions::new()
        .read(true)
        .custom_flags(OPEN_REPARSE_POINT)
        .open(path)
        .map_err(|e| format!("cannot read the file: {e}"))?;
    let metadata = file
        .metadata()
        .map_err(|e| format!("cannot read the file: {e}"))?;
    if metadata.file_type().is_symlink() {
        return Err("the file is a symlink; it must be a regular file".to_string());
    }
    require_regular(file)
}

fn require_regular(file: File) -> Result<File, String> {
    let metadata = file
        .metadata()
        .map_err(|e| format!("cannot read the file: {e}"))?;
    if metadata.is_file() {
        Ok(file)
    } else {
        Err("not a regular file".to_string())
    }
}

/// Read all of `file`, refusing anything over `max` bytes.
fn read_file(file: File, max: u64) -> Result<String, String> {
    let mut bytes = Vec::new();
    file.take(max + 1)
        .read_to_end(&mut bytes)
        .map_err(|e| format!("cannot read the file: {e}"))?;
    if bytes.len() as u64 > max {
        return Err(size_error(max));
    }
    String::from_utf8(bytes).map_err(|_| "the file is not valid UTF-8".to_string())
}

/// The message for a file over `max` bytes.
pub fn size_error(max: u64) -> String {
    format!(
        "the file is larger than the {} MB limit",
        max / (1024 * 1024)
    )
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
        // The universal configuration file has a larger limit.
        assert!(read_limited_to(&over, MAX_UNIVERSAL_FILE_SIZE).is_ok());
        let universal_over = dir.path().join("over.json");
        fs::write(
            &universal_over,
            vec![b' '; MAX_UNIVERSAL_FILE_SIZE as usize + 1],
        )
        .unwrap();
        assert_eq!(
            read_limited_to(&universal_over, MAX_UNIVERSAL_FILE_SIZE).unwrap_err(),
            "the file is larger than the 4 MB limit"
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
    fn regular_file_reads() {
        let dir = TempDir::new();
        let file = dir.write("project.yaml", "a: 1\n");
        assert_eq!(read_regular_file(&file).unwrap(), "a: 1\n");

        let over = dir.path().join("over.yaml");
        fs::write(&over, vec![b'#'; MAX_CONFIG_FILE_SIZE as usize + 1]).unwrap();
        assert_eq!(
            read_regular_file(&over).unwrap_err(),
            "the file is larger than the 2 MB limit"
        );
        assert!(
            read_regular_file(&dir.path().join("missing.yaml"))
                .unwrap_err()
                .starts_with("cannot read the file: ")
        );
    }

    #[cfg(unix)]
    #[test]
    fn regular_file_refuses_others() {
        use std::ffi::CString;
        use std::os::unix::ffi::OsStrExt;

        let dir = TempDir::new();
        let target = dir.write("secret.env", "TOKEN=x\n");
        let link = dir.path().join("link.yaml");
        std::os::unix::fs::symlink(&target, &link).unwrap();
        assert_eq!(
            read_regular_file(&link).unwrap_err(),
            "the file is a symlink; it must be a regular file"
        );
        // A symlink to a directory, or a dangling one, is refused the same way.
        let dangling = dir.path().join("dangling.yaml");
        std::os::unix::fs::symlink(dir.path().join("nothing"), &dangling).unwrap();
        assert_eq!(
            read_regular_file(&dangling).unwrap_err(),
            "the file is a symlink; it must be a regular file"
        );

        fs::create_dir(dir.path().join("a-dir.yaml")).unwrap();
        assert_eq!(
            read_regular_file(&dir.path().join("a-dir.yaml")).unwrap_err(),
            "not a regular file"
        );

        // Nothing writes to the FIFO, so a blocking read would never return.
        let fifo = dir.path().join("fifo.yaml");
        let name = CString::new(fifo.as_os_str().as_bytes()).unwrap();
        assert_eq!(unsafe { libc::mkfifo(name.as_ptr(), 0o600) }, 0);
        assert_eq!(read_regular_file(&fifo).unwrap_err(), "not a regular file");
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
