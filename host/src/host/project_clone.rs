//! The project clone directory: where it is on the host and in the container.

use std::fs;
use std::path::{Path, PathBuf};

/// The project clone directory, shared between the host and the container: the
/// universal configuration's `project-clone-dir`, mounted at the
/// dev container's `mounts: project-clone`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct ProjectClone {
    /// Canonical path of the directory on the host.
    pub host: PathBuf,
    /// Absolute path where it's mounted in the container, without a trailing '/'.
    pub container: String,
}

impl ProjectClone {
    /// The project clone at `host`, mounted at `container`.  The host directory
    /// must exist.
    pub(crate) fn new(host: &Path, container: &str) -> Result<Self, String> {
        let container = normalize_container_path(container)?;
        let shown = host.display();
        let host_dir =
            fs::canonicalize(host).map_err(|e| format!("host directory '{shown}': {e}"))?;
        if !host_dir.is_dir() {
            return Err(format!("host directory '{shown}' is not a directory"));
        }
        Ok(ProjectClone {
            host: host_dir,
            container,
        })
    }
}

/// Check that `path` is an absolute container path, and drop empty and `.` components.
fn normalize_container_path(path: &str) -> Result<String, String> {
    if !path.starts_with('/') {
        return Err(format!("container path '{path}' must be absolute"));
    }
    let parts: Vec<&str> = path
        .split('/')
        .filter(|p| !p.is_empty() && *p != ".")
        .collect();
    if parts.contains(&"..") {
        return Err(format!("container path '{path}' must not contain '..'"));
    }
    if parts.is_empty() {
        return Err("the container path can't be '/'".to_string());
    }
    Ok(format!("/{}", parts.join("/")))
}

#[cfg(test)]
mod tests {
    use super::*;

    /// A canonical, existing directory to use as the host side.
    fn host_dir() -> PathBuf {
        std::env::temp_dir().canonicalize().unwrap()
    }

    fn workspace_error(host: &Path, container: &str) -> String {
        ProjectClone::new(host, container).unwrap_err()
    }

    #[test]
    fn workspace_new() {
        let host = host_dir();
        assert_eq!(
            ProjectClone::new(&host, "/workspace"),
            Ok(ProjectClone {
                host: host.clone(),
                container: "/workspace".to_string()
            })
        );
    }

    #[test]
    fn workspace_host_is_canonical() {
        let host = host_dir();
        assert_eq!(
            ProjectClone::new(&host.join("."), "/workspace")
                .unwrap()
                .host,
            host
        );
    }

    #[test]
    fn container_path_is_normalized() {
        let host = host_dir();
        for (given, expected) in [
            ("/workspace/", "/workspace"),
            ("//a/./b//", "/a/b"),
            ("/a", "/a"),
        ] {
            assert_eq!(
                ProjectClone::new(&host, given).unwrap().container,
                expected,
                "{given}"
            );
        }
    }

    #[test]
    fn container_path_errors() {
        let host = host_dir();
        for (given, expected) in [
            ("", "container path '' must be absolute"),
            ("workspace", "container path 'workspace' must be absolute"),
            ("/a/../b", "container path '/a/../b' must not contain '..'"),
            ("/", "the container path can't be '/'"),
            ("/./", "the container path can't be '/'"),
        ] {
            assert_eq!(workspace_error(&host, given), expected, "{given}");
        }
    }

    #[test]
    fn host_must_be_existing_dir() {
        assert!(
            workspace_error(Path::new("/nonexistent/scoter-ws"), "/workspace")
                .starts_with("host directory '/nonexistent/scoter-ws': ")
        );
        let file = std::env::temp_dir().join(format!("scoter-host-ws-file-{}", std::process::id()));
        fs::write(&file, b"").unwrap();
        let err = workspace_error(&file, "/workspace");
        fs::remove_file(&file).unwrap();
        assert!(err.ends_with("is not a directory"), "{err}");
    }
}
