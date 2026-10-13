//! Command templates: how the system setup files say to run an executable.
//!
//! ```yaml
//! docker:
//!     exec: /usr/sbin/podman
//!     run-build: ${exec} build -t {x:tag} -f {x:dockerfile} {x:dirname}
//! ```
//!
//! A template is split into words before `${...}` references are expanded, so
//! a value with spaces (such as `C:\Program Files\...`) stays one argument.
//! Words are separated by whitespace; `'...'` and `"..."` group text with
//! spaces into one word.  Inside `"..."`, `\"` and `\\` stand for `"` and `\`;
//! a backslash anywhere else is an ordinary character, for Windows paths.
//!
//! `{x:name}` is a value given when the command runs, such as the container
//! name.

use std::collections::HashMap;
use std::ffi::OsStr;
use std::path::{Path, PathBuf};

/// A piece of an expanded value.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Part {
    Text(String),
    /// `{x:name}`: given when the command runs.
    External(String),
}

/// Append text, joining it with any text before it.
pub(crate) fn push_text(parts: &mut Vec<Part>, text: &str) {
    if let Some(Part::Text(last)) = parts.last_mut() {
        last.push_str(text);
    } else if !text.is_empty() {
        parts.push(Part::Text(text.to_string()));
    }
}

/// Append parts, joining adjacent text.
pub(crate) fn extend_parts(parts: &mut Vec<Part>, more: Vec<Part>) {
    for part in more {
        match part {
            Part::Text(text) => push_text(parts, &text),
            external => parts.push(external),
        }
    }
}

/// The parts as text, with externals written as `{x:name}`.
pub fn render_parts(parts: &[Part]) -> String {
    parts
        .iter()
        .map(|part| match part {
            Part::Text(text) => text.clone(),
            Part::External(name) => format!("{{x:{name}}}"),
        })
        .collect()
}

/// A command, one entry per argument (the first is the program).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CommandTemplate {
    words: Vec<Vec<Part>>,
}

impl CommandTemplate {
    pub fn new(words: Vec<Vec<Part>>) -> Result<Self, String> {
        if words.is_empty() {
            return Err("the command is empty".to_string());
        }
        Ok(CommandTemplate { words })
    }

    pub fn words(&self) -> &[Vec<Part>] {
        &self.words
    }

    /// The `{x:name}` placeholders used, in order of first use.
    pub fn externals(&self) -> Vec<&str> {
        let mut names: Vec<&str> = Vec::new();
        for part in self.words.iter().flatten() {
            if let Part::External(name) = part
                && !names.contains(&name.as_str())
            {
                names.push(name);
            }
        }
        names
    }

    /// The argv, with each `{x:name}` replaced by `values[name]`.
    pub fn render(&self, values: &HashMap<&str, &str>) -> Result<Vec<String>, String> {
        self.words
            .iter()
            .map(|word| {
                word.iter()
                    .map(|part| match part {
                        Part::Text(text) => Ok(text.as_str()),
                        Part::External(name) => values
                            .get(name.as_str())
                            .copied()
                            .ok_or_else(|| format!("no value for `{{x:{name}}}`")),
                    })
                    .collect::<Result<String, String>>()
            })
            .collect()
    }
}

impl CommandTemplate {
    /// The arguments (the first is the program), quotes already removed, with
    /// `{x:name}` placeholders kept as text.
    pub fn to_args(&self) -> Vec<String> {
        self.words.iter().map(|word| render_parts(word)).collect()
    }
}

/// Split a template into words, removing the quotes.
pub fn split_words(text: &str) -> Result<Vec<String>, String> {
    let mut words = Vec::new();
    let mut word = String::new();
    let mut in_word = false;
    let mut chars = text.chars().peekable();
    while let Some(c) = chars.next() {
        match c {
            c if c.is_whitespace() => {
                if in_word {
                    words.push(std::mem::take(&mut word));
                    in_word = false;
                }
            }
            '\'' => {
                in_word = true;
                loop {
                    match chars.next() {
                        Some('\'') => break,
                        Some(c) => word.push(c),
                        None => return Err("unmatched ' quote".to_string()),
                    }
                }
            }
            '"' => {
                in_word = true;
                loop {
                    match chars.next() {
                        Some('"') => break,
                        Some('\\') if matches!(chars.peek(), Some('"' | '\\')) => {
                            word.extend(chars.next())
                        }
                        Some(c) => word.push(c),
                        None => return Err("unmatched \" quote".to_string()),
                    }
                }
            }
            c => {
                in_word = true;
                word.push(c);
            }
        }
    }
    if in_word {
        words.push(word);
    }
    Ok(words)
}

/// Find `program` the way a shell would.  A name with a directory part must
/// name an executable file; a bare name is searched for in `path_var` (the
/// `PATH`).  `path_ext` (Windows' `PATHEXT`, `;`-separated) gives extensions
/// to try as well.
pub fn find_executable(
    program: &Path,
    path_var: Option<&OsStr>,
    path_ext: Option<&OsStr>,
) -> Option<PathBuf> {
    if program.as_os_str().is_empty() {
        return None;
    }
    if program.is_absolute() || program.components().count() > 1 {
        return with_extensions(program, path_ext)
            .into_iter()
            .find(|p| is_executable(p));
    }
    std::env::split_paths(path_var?)
        .filter(|dir| !dir.as_os_str().is_empty())
        .flat_map(|dir| with_extensions(&dir.join(program), path_ext))
        .find(|p| is_executable(p))
}

fn with_extensions(path: &Path, path_ext: Option<&OsStr>) -> Vec<PathBuf> {
    let mut paths = vec![path.to_path_buf()];
    if let Some(exts) = path_ext.and_then(OsStr::to_str) {
        for ext in exts.split(';').filter(|e| !e.is_empty()) {
            let mut with = path.as_os_str().to_os_string();
            with.push(ext);
            paths.push(PathBuf::from(with));
        }
    }
    paths
}

fn is_executable(path: &Path) -> bool {
    let Ok(meta) = path.metadata() else {
        return false;
    };
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        meta.is_file() && meta.permissions().mode() & 0o111 != 0
    }
    #[cfg(not(unix))]
    {
        meta.is_file()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::config::testing::TempDir;

    fn words(text: &str) -> Vec<String> {
        split_words(text).unwrap()
    }

    #[test]
    fn split_plain() {
        assert_eq!(words("  a  b\tc\n"), ["a", "b", "c"]);
        assert_eq!(words(""), [] as [&str; 0]);
        assert_eq!(words("${exec} -t {x:tag}"), ["${exec}", "-t", "{x:tag}"]);
    }

    #[test]
    fn split_quotes() {
        assert_eq!(words("'a b' \"c d\" e'f g'h"), ["a b", "c d", "ef gh"]);
        assert_eq!(words("'' \"\""), ["", ""]);
        assert_eq!(words(r#""say \"hi\" \\ \n""#), [r#"say "hi" \ \n"#]);
        assert_eq!(words(r"'\'"), [r"\"]);
    }

    #[test]
    fn split_keeps_windows_backslashes() {
        assert_eq!(
            words(r"C:\tools\podman.exe exec"),
            [r"C:\tools\podman.exe", "exec"]
        );
    }

    #[test]
    fn split_unmatched_quotes() {
        assert_eq!(split_words("a 'b").unwrap_err(), "unmatched ' quote");
        assert_eq!(split_words("a \"b").unwrap_err(), "unmatched \" quote");
    }

    fn text(s: &str) -> Part {
        Part::Text(s.to_string())
    }

    fn ext(s: &str) -> Part {
        Part::External(s.to_string())
    }

    #[test]
    fn parts_join_text() {
        let mut parts = Vec::new();
        push_text(&mut parts, "");
        assert!(parts.is_empty());
        push_text(&mut parts, "a");
        extend_parts(&mut parts, vec![text("b"), ext("x"), text("c")]);
        push_text(&mut parts, "d");
        assert_eq!(parts, [text("ab"), ext("x"), text("cd")]);
        assert_eq!(render_parts(&parts), "ab{x:x}cd");
    }

    #[test]
    fn render_template() {
        let template = CommandTemplate::new(vec![
            vec![text("/usr/bin/docker")],
            vec![text("exec")],
            vec![ext("container")],
            vec![text("--name="), ext("container"), text("-x")],
            vec![ext("broker")],
        ])
        .unwrap();
        assert_eq!(template.externals(), ["container", "broker"]);
        let values = HashMap::from([("container", "dev 1"), ("broker", "/b")]);
        assert_eq!(
            template.render(&values).unwrap(),
            ["/usr/bin/docker", "exec", "dev 1", "--name=dev 1-x", "/b"]
        );
        let missing = HashMap::from([("container", "c")]);
        assert_eq!(
            template.render(&missing).unwrap_err(),
            "no value for `{x:broker}`"
        );
    }

    #[test]
    fn template_args() {
        let template = CommandTemplate::new(vec![
            vec![text("/opt/my tools/podman")],
            vec![text("build")],
            vec![text("--tag="), ext("tag")],
            vec![],
        ])
        .unwrap();
        assert_eq!(
            template.to_args(),
            ["/opt/my tools/podman", "build", "--tag={x:tag}", ""]
        );
    }

    #[test]
    fn empty_template() {
        assert_eq!(
            CommandTemplate::new(vec![]).unwrap_err(),
            "the command is empty"
        );
    }

    #[cfg(unix)]
    fn make_executable(path: &Path, executable: bool) {
        use std::os::unix::fs::PermissionsExt;
        std::fs::set_permissions(
            path,
            std::fs::Permissions::from_mode(if executable { 0o755 } else { 0o644 }),
        )
        .unwrap();
    }

    #[cfg(unix)]
    #[test]
    fn find_on_path() {
        let dir = TempDir::new();
        let tool = dir.write("b/tool", "#!/bin/sh\n");
        make_executable(&tool, true);
        let plain = dir.write("a/tool", "");
        make_executable(&plain, false);
        let path_var = std::env::join_paths([dir.path().join("a"), dir.path().join("b")]).unwrap();
        // Not executable in `a`; found in `b`.
        assert_eq!(
            find_executable(Path::new("tool"), Some(&path_var), None),
            Some(tool.clone())
        );
        assert_eq!(
            find_executable(Path::new("other"), Some(&path_var), None),
            None
        );
        assert_eq!(find_executable(Path::new("tool"), None, None), None);
        assert_eq!(find_executable(Path::new(""), Some(&path_var), None), None);
        // A path isn't searched for.
        assert_eq!(find_executable(&tool, None, None), Some(tool.clone()));
        assert_eq!(find_executable(&plain, None, None), None);
        assert_eq!(find_executable(&dir.path().join("b"), None, None), None);
    }

    #[test]
    fn find_with_extensions() {
        let dir = TempDir::new();
        let tool = dir.write("bin/tool.EXE", "");
        #[cfg(unix)]
        make_executable(&tool, true);
        let path_var = dir.path().join("bin").into_os_string();
        let exts = OsStr::new(".COM;.EXE");
        assert_eq!(
            find_executable(Path::new("tool"), Some(&path_var), Some(exts)),
            Some(tool.clone())
        );
        assert_eq!(
            find_executable(Path::new("tool"), Some(&path_var), None),
            None
        );
    }
}
