//! The dev container: a directory holding its `Dockerfile` and a
//! `dev-env.yaml` describing how to run it.  See `docs/project-dockerfile.md`.
//!
//! ```yaml
//! parents:
//!     - dir: ../base-env
//!       tag: my-base:latest
//!     - file: ../published/dev-env.yaml
//!     - name: dev-env
//!       tag: local-surf-scoter-env/dev-env:build
//! user: ubuntu
//! mounts:
//!     project-clone: /project
//!     persistent-store: /persistent-store
//!     setup: /setup
//! broker-bin: /usr/local/bin/scoter-container
//! sshd-port: 22022
//! sshd-port-env: SCOTER_SSHD_PORT
//! ```
//!
//! A file may build on parents: their values come first, in the order
//! listed (a later parent's win), and the file's own override them.  Mappings
//! merge key by key; anything else is replaced, `null` included.  A `dir`
//! parent is a local container directory, built with `tag` before this one;
//! without a `dev-env.yaml` of its own, it adds no values and has no parents
//! to build.  A `name` parent is a `dir` parent found by name: the first
//! `dev-containers-search` directory holding `NAME/Dockerfile`.  A `file`
//! parent is only another `dev-env.yaml`, for an image pulled from a
//! repository.  Parents may have parents, so long as none is its own.
//!
//! Loading resolves all that.  The resulting `DevEnv::raw` holds the merged
//! values, with every key the tools use, and `parent-builds` in place of
//! `parents`: the local parents to build, as `{dir, tag}`, in build order.
//!
//! Other keys are allowed, and kept in `DevEnv::raw`.
//!
//! The mount points, `user` and `broker-bin` are written into host files and
//! command lines (the compose `.env` file, `~/.ssh/config`), so each is
//! limited to characters that mean nothing there (DEV-1, DEV-2).
//!
//! The dev container, and every parent `dir`, `name` and `file`, must resolve
//! (following symlinks) to somewhere inside the project files directory (the
//! one holding the project file) or one of the system configuration's
//! `dev-containers-search` directories.
//! Those are the only places the tools build from, so a project can't make
//! them read, or use as a build context, anything else on the host.

use std::collections::BTreeMap;
use std::path::{Path, PathBuf};

use yaml_serde::{Mapping, Value};

use super::project::{ContainerRef, valid_name};
use super::read::{parse_yaml, read_limited, tag_errors};
use super::{ConfigError, child_key, index_key};

pub const DOCKERFILE: &str = "Dockerfile";
pub const DEV_ENV_FILE: &str = "dev-env.yaml";

/// The parents a `dev-env.yaml` builds on, as written.
const PARENTS: &str = "parents";
/// The local parents to build, as loading resolves them.
const PARENT_BUILDS: &str = "parent-builds";
const SSHD_PORT: &str = "sshd-port";
const SSHD_PORT_ENV: &str = "sshd-port-env";

/// A dev container directory.
#[derive(Debug, Clone, PartialEq)]
pub struct DevContainer {
    pub dir: PathBuf,
    pub env: DevEnv,
}

impl DevContainer {
    pub fn dockerfile(&self) -> PathBuf {
        self.dir.join(DOCKERFILE)
    }

    pub fn env_file(&self) -> PathBuf {
        self.dir.join(DEV_ENV_FILE)
    }
}

/// The contents of `dev-env.yaml`, with its parents' merged in.
#[derive(Debug, Clone, PartialEq)]
pub struct DevEnv {
    /// The user the container runs as, and that `sshd` accepts.
    pub user: String,
    pub mounts: Mounts,
    /// Absolute path of the broker executable inside the container.
    pub broker_bin: String,
    /// The port `sshd` listens on, on the container's loopback interface;
    /// `None` when the container picks it as it starts.
    pub sshd_port: Option<u16>,
    /// The environment variable given to the container holding the `sshd`
    /// port.  Always set when `sshd_port` isn't.
    pub sshd_port_env: Option<String>,
    /// The local parent images to build before this one, lowest first.
    pub parent_builds: Vec<ParentBuild>,
    /// The merged values, with every key above.
    pub raw: Value,
}

/// Where things are mounted in the container.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Mounts {
    /// The clone of the project, read-write.
    pub project_clone: String,
    /// The container's own volume, kept between runs.
    pub persistent_store: String,
    /// The settings directory, read-only.
    pub setup: String,
}

impl Mounts {
    /// Each mount's key and container path.
    pub fn named(&self) -> [(&'static str, &str); 3] {
        [
            (PROJECT_CLONE, &self.project_clone),
            (PERSISTENT_STORE, &self.persistent_store),
            (SETUP, &self.setup),
        ]
    }
}

/// The `mounts` keys.
pub const PROJECT_CLONE: &str = "project-clone";
pub const PERSISTENT_STORE: &str = "persistent-store";
pub const SETUP: &str = "setup";

/// A local parent image: its container directory, and the tag to build it
/// with (the tag its children's `FROM` names).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ParentBuild {
    pub dir: PathBuf,
    pub tag: String,
}

/// Find the project's dev container.  `project` is the project file, which
/// `dev-container-path` is relative to; `lookup` holds the directories to
/// search for a `dev-container` name.  The container and its parents must be
/// inside the project file's directory or a `lookup` directory.
pub fn find(
    reference: &ContainerRef,
    project: &Path,
    lookup: &[PathBuf],
) -> Result<DevContainer, Vec<ConfigError>> {
    let base = project.parent().unwrap_or(Path::new(""));
    // The search list normally holds the project files directory too.
    let mut roots: Vec<PathBuf> = vec![base.to_path_buf()];
    for dir in lookup {
        if !roots.contains(dir) {
            roots.push(dir.clone());
        }
    }
    let outside = |key: &str, dir: &Path| {
        vec![ConfigError::new(
            Some(project),
            key,
            outside_message(dir, &roots),
        )]
    };
    match reference {
        ContainerRef::Path(path) => {
            let dir = base.join(path);
            if !within(&dir, &roots) {
                return Err(outside("container.dev-container-path", &dir));
            }
            load(&dir, &roots, lookup)
        }
        ContainerRef::Name(name) => {
            for dir in lookup {
                let candidate = dir.join(name);
                if !candidate.join(DOCKERFILE).is_file() || !candidate.join(DEV_ENV_FILE).is_file()
                {
                    continue;
                }
                if !within(&candidate, &roots) {
                    return Err(outside("container.dev-container", &candidate));
                }
                return load(&candidate, &roots, lookup);
            }
            let message = if lookup.is_empty() {
                format!(
                    "can't find dev container '{name}': the system configuration sets no `scoter` `path-lists` `dev-containers-search`"
                )
            } else {
                let searched: Vec<String> =
                    lookup.iter().map(|d| d.display().to_string()).collect();
                format!(
                    "can't find dev container '{name}' (a directory with {DOCKERFILE} and {DEV_ENV_FILE}) in: {}",
                    searched.join(", ")
                )
            };
            Err(vec![ConfigError::new(
                Some(project),
                "container.dev-container",
                message,
            )])
        }
    }
}

/// Whether `path` resolves (following symlinks) to somewhere inside one of
/// `roots`.  A path that doesn't exist is reported elsewhere, so it passes.
fn within(path: &Path, roots: &[PathBuf]) -> bool {
    let Ok(path) = path.canonicalize() else {
        return true;
    };
    roots
        .iter()
        .filter_map(|root| root.canonicalize().ok())
        .any(|root| path.starts_with(root))
}

/// The message for a container or parent that isn't inside one of `roots`.
fn outside_message(path: &Path, roots: &[PathBuf]) -> String {
    let roots: Vec<String> = roots.iter().map(|r| r.display().to_string()).collect();
    format!(
        "{} is outside the project files directory and the `dev-containers-search` directories: {}",
        path.display(),
        roots.join(", ")
    )
}

/// Read the dev container in `dir`, with its parents, which must all be
/// inside one of `roots`.  `search` holds the directories a `name` parent
/// is looked for in (`dev-containers-search`).
pub fn load(
    dir: &Path,
    roots: &[PathBuf],
    search: &[PathBuf],
) -> Result<DevContainer, Vec<ConfigError>> {
    if !dir.is_dir() {
        return Err(vec![ConfigError::new(
            Some(dir),
            "",
            "the dev container directory doesn't exist",
        )]);
    }
    let mut errors = Vec::new();
    if !dir.join(DOCKERFILE).is_file() {
        errors.push(ConfigError::new(
            Some(dir),
            "",
            format!("the dev container has no {DOCKERFILE}"),
        ));
    }
    // Canonical, so it reads well in messages and the universal configuration.
    let dir = dir.canonicalize().unwrap_or_else(|_| dir.to_path_buf());
    let env_file = dir.join(DEV_ENV_FILE);
    let env = resolve(&env_file, roots, search).and_then(|value| parse(value, &env_file));
    match env {
        Ok(env) if errors.is_empty() => Ok(DevContainer { dir, env }),
        Ok(_) => Err(errors),
        Err(e) => {
            errors.extend(e);
            Err(errors)
        }
    }
}

/// Read `file` and all its parents, giving the merged values with
/// `parent-builds` in place of `parents`.
fn resolve(file: &Path, roots: &[PathBuf], search: &[PathBuf]) -> Result<Value, Vec<ConfigError>> {
    let mut resolver = Resolver {
        roots: roots.to_vec(),
        search: search.to_vec(),
        ..Resolver::default()
    };
    let merged = resolver.values(file);
    let mut builds = Vec::new();
    if resolver.errors.is_empty() {
        resolver.builds(file, &mut Vec::new(), &mut builds);
    }
    match merged {
        Some(mut merged) if resolver.errors.is_empty() => {
            let builds = builds
                .into_iter()
                .map(|b| {
                    let mut entry = Mapping::new();
                    entry.insert("dir".into(), b.dir.to_string_lossy().into_owned().into());
                    entry.insert("tag".into(), b.tag.into());
                    Value::Mapping(entry)
                })
                .collect();
            merged.insert(PARENT_BUILDS.into(), Value::Sequence(builds));
            Ok(Value::Mapping(merged))
        }
        _ => Err(resolver.errors),
    }
}

/// One `dev-env.yaml` as written: its own values, and its parents.
struct Node {
    own: Mapping,
    parents: Vec<Parent>,
}

/// A parent, its paths made absolute.
enum Parent {
    /// A local container directory, built with `tag`.
    Local {
        dir: PathBuf,
        tag: String,
        key: String,
    },
    /// Only a `dev-env.yaml`; its image comes from a repository.
    Published { file: PathBuf },
}

impl Parent {
    /// Its `dev-env.yaml`, if it has one.  A local parent needn't: it then
    /// adds no values, and has no parents to build.
    fn file(&self) -> Option<PathBuf> {
        match self {
            Parent::Local { dir, .. } => Some(dir.join(DEV_ENV_FILE)).filter(|file| file.is_file()),
            Parent::Published { file } => Some(file.clone()),
        }
    }
}

/// Reads the files in a parent graph, each once.
#[derive(Default)]
struct Resolver {
    /// By canonical file; `None` if it couldn't be read.
    nodes: BTreeMap<PathBuf, Option<Node>>,
    /// Merged values, by canonical file.
    merged: BTreeMap<PathBuf, Option<Mapping>>,
    /// The files being merged, to find cycles.
    visiting: Vec<PathBuf>,
    errors: Vec<ConfigError>,
    /// Where parents may be.
    roots: Vec<PathBuf>,
    /// Where `name` parents are looked for.
    search: Vec<PathBuf>,
}

impl Resolver {
    /// The merged values of `file`: its parents', then its own.
    fn values(&mut self, file: &Path) -> Option<Mapping> {
        let file = file.canonicalize().unwrap_or_else(|_| file.to_path_buf());
        if let Some(done) = self.merged.get(&file) {
            return done.clone();
        }
        if let Some(at) = self.visiting.iter().position(|f| *f == file) {
            let cycle: Vec<String> = self.visiting[at..]
                .iter()
                .chain([&file])
                .map(|f| f.display().to_string())
                .collect();
            self.errors.push(ConfigError::new(
                Some(&file),
                PARENTS,
                format!("is its own parent: {}", cycle.join(" -> ")),
            ));
            return None;
        }
        self.read(&file);
        let parents: Vec<PathBuf> = match &self.nodes[&file] {
            Some(node) => node.parents.iter().filter_map(Parent::file).collect(),
            None => {
                self.merged.insert(file, None);
                return None;
            }
        };
        self.visiting.push(file.clone());
        let mut merged = Some(Mapping::new());
        for parent in parents {
            match (self.values(&parent), &mut merged) {
                (Some(values), Some(merged)) => merge(merged, values),
                _ => merged = None,
            }
        }
        self.visiting.pop();
        if let (Some(merged), Some(Some(node))) = (&mut merged, self.nodes.get(&file)) {
            merge(merged, node.own.clone());
        }
        self.merged.insert(file, merged.clone());
        merged
    }

    /// Add the local parents of `file` to build, depth first, so each comes
    /// after its own.  `seen` holds the directories already added.  Only
    /// called once `values` found the graph whole and without cycles.
    fn builds(&mut self, file: &Path, seen: &mut Vec<PathBuf>, out: &mut Vec<ParentBuild>) {
        let file = file.canonicalize().unwrap_or_else(|_| file.to_path_buf());
        let Some(Some(node)) = self.nodes.get(&file) else {
            return;
        };
        let locals: Vec<(PathBuf, String, String)> = node
            .parents
            .iter()
            .filter_map(|p| match p {
                Parent::Local { dir, tag, key } => Some((dir.clone(), tag.clone(), key.clone())),
                // Its image is already built, parents and all.
                Parent::Published { .. } => None,
            })
            .collect();
        for (dir, tag, key) in locals {
            let dir = dir.canonicalize().unwrap_or(dir);
            if let Some(i) = seen.iter().position(|d| *d == dir) {
                if out[i].tag != tag {
                    self.errors.push(ConfigError::new(
                        Some(&file),
                        &child_key(&key, "tag"),
                        format!(
                            "{} is also built as '{}'; a parent has one tag",
                            dir.display(),
                            out[i].tag
                        ),
                    ));
                }
                continue;
            }
            self.builds(&dir.join(DEV_ENV_FILE), seen, out);
            seen.push(dir.clone());
            out.push(ParentBuild { dir, tag });
        }
    }

    /// Read `file` (canonical) into `nodes`, unless it's there already.
    fn read(&mut self, file: &Path) {
        if self.nodes.contains_key(file) {
            return;
        }
        let node = read_limited(file)
            .and_then(|text| parse_yaml(&text))
            .map_err(|m| vec![ConfigError::new(Some(file), "", m)])
            .and_then(|value| parse_node(value, file, &self.roots, &self.search));
        let node = node.map_err(|e| self.errors.extend(e)).ok();
        self.nodes.insert(file.to_path_buf(), node);
    }
}

/// Check one `dev-env.yaml` as written, separating its parents from its
/// values.  Parents must be inside one of `roots`; `name` parents are looked
/// for in `search`.
fn parse_node(
    value: Value,
    file: &Path,
    roots: &[PathBuf],
    search: &[PathBuf],
) -> Result<Node, Vec<ConfigError>> {
    let error = |key: &str, message: &str| ConfigError::new(Some(file), key, message);
    let tags = tag_errors(&value);
    if !tags.is_empty() {
        return Err(tags
            .into_iter()
            .map(|(key, message)| error(&key, &message))
            .collect());
    }
    let mut own = match value {
        Value::Mapping(map) => map,
        Value::Null => Mapping::new(),
        _ => return Err(vec![error("", "must be a mapping")]),
    };
    let mut errors = Vec::new();
    if own.contains_key(PARENT_BUILDS) {
        errors.push(error(
            PARENT_BUILDS,
            "is worked out from `parents`; don't set it",
        ));
    }
    let base = file.parent().unwrap_or(Path::new(""));
    let mut parents = Vec::new();
    match own.remove(PARENTS) {
        None | Some(Value::Null) => {}
        Some(Value::Sequence(items)) => {
            for (i, item) in items.iter().enumerate() {
                let key = index_key(PARENTS, i);
                let Value::Mapping(entry) = item else {
                    errors.push(error(
                        &key,
                        "must be a mapping with `dir` and `tag`, `name` and `tag`, or `file`",
                    ));
                    continue;
                };
                let mut problems = Vec::new();
                let parent = match (entry.get("dir"), entry.get("name"), entry.get("file")) {
                    (Some(_), None, None) => {
                        let dir = text_field(entry, &key, "dir", false, &mut problems);
                        let tag = text_field(entry, &key, "tag", false, &mut problems);
                        dir.zip(tag).map(|(dir, tag)| Parent::Local {
                            dir: base.join(dir),
                            tag,
                            key: key.clone(),
                        })
                    }
                    (None, Some(_), None) => {
                        let name = text_field(entry, &key, "name", false, &mut problems);
                        let tag = text_field(entry, &key, "tag", false, &mut problems);
                        let dir = name.and_then(|name| {
                            find_by_name(&name, search)
                                .map_err(|m| problems.push((child_key(&key, "name"), m)))
                                .ok()
                        });
                        dir.zip(tag).map(|(dir, tag)| Parent::Local {
                            dir,
                            tag,
                            key: key.clone(),
                        })
                    }
                    (None, None, Some(_)) => {
                        let path = text_field(entry, &key, "file", false, &mut problems);
                        if entry.contains_key("tag") {
                            problems.push((
                                child_key(&key, "tag"),
                                "goes with `dir`; a `file` parent isn't built".to_string(),
                            ));
                        }
                        path.map(|path| Parent::Published {
                            file: base.join(path),
                        })
                    }
                    _ => {
                        problems.push((
                            key.clone(),
                            "needs one of `dir`, `name` or `file`".to_string(),
                        ));
                        None
                    }
                };
                for k in entry.keys() {
                    if !matches!(k.as_str(), Some("dir" | "name" | "tag" | "file")) {
                        problems.push((key.clone(), format!("has an unknown key {k:?}")));
                    }
                }
                errors.extend(problems.iter().map(|(k, m)| error(k, m)));
                match parent {
                    Some(Parent::Local { ref dir, .. }) if !dir.join(DOCKERFILE).is_file() => {
                        errors.push(error(
                            &child_key(&key, "dir"),
                            &format!("{} has no {DOCKERFILE}", dir.display()),
                        ));
                    }
                    Some(Parent::Published { ref file }) if !file.is_file() => {
                        errors.push(error(&key, &format!("{} doesn't exist", file.display())));
                    }
                    Some(Parent::Local { ref dir, .. }) if !within(dir, roots) => {
                        let field = if entry.contains_key("name") {
                            "name"
                        } else {
                            "dir"
                        };
                        errors.push(error(&child_key(&key, field), &outside_message(dir, roots)));
                    }
                    Some(Parent::Published { ref file }) if !within(file, roots) => {
                        errors.push(error(
                            &child_key(&key, "file"),
                            &outside_message(file, roots),
                        ));
                    }
                    Some(p) if problems.is_empty() => parents.push(p),
                    _ => {}
                }
            }
        }
        Some(_) => errors.push(error(PARENTS, "must be a list")),
    }
    if errors.is_empty() {
        Ok(Node { own, parents })
    } else {
        Err(errors)
    }
}

/// Lay `over` on `base`: mappings merge key by key, anything else replaces.
fn merge(base: &mut Mapping, over: Mapping) {
    for (key, value) in over {
        match (base.get_mut(&key), value) {
            (Some(Value::Mapping(inner)), Value::Mapping(value)) => merge(inner, value),
            (_, value) => {
                base.insert(key, value);
            }
        }
    }
}

/// Check a resolved `dev-env.yaml` (as `load` makes, and the universal
/// configuration keeps); `file` is where it came from.  `parents` must
/// already be resolved into `parent-builds`.
pub fn parse(value: Value, file: &Path) -> Result<DevEnv, Vec<ConfigError>> {
    let error = |key: &str, message: String| ConfigError::new(Some(file), key, message);
    let tags = tag_errors(&value);
    if !tags.is_empty() {
        return Err(tags
            .into_iter()
            .map(|(key, message)| error(&key, message))
            .collect());
    }
    let mut map = match value {
        Value::Mapping(map) => map,
        Value::Null => Mapping::new(),
        _ => return Err(vec![error("", "must be a mapping".to_string())]),
    };

    let mut problems: Vec<(String, String)> = Vec::new();
    if map.contains_key(PARENTS) {
        problems.push((
            PARENTS.to_string(),
            format!("must already be resolved into `{PARENT_BUILDS}`"),
        ));
    }
    let user =
        text_field(&map, "", "user", false, &mut problems).and_then(
            |user| match user_name_problem(&user) {
                Some(problem) => {
                    problems.push(("user".to_string(), problem));
                    None
                }
                None => Some(user),
            },
        );
    let mounts = match map.get("mounts") {
        Some(Value::Mapping(mounts)) => {
            let mut path = |key| container_path_field(mounts, "mounts", key, &mut problems);
            match (path(PROJECT_CLONE), path(PERSISTENT_STORE), path(SETUP)) {
                (Some(project_clone), Some(persistent_store), Some(setup)) => {
                    let mounts = Mounts {
                        project_clone,
                        persistent_store,
                        setup,
                    };
                    match mounts_overlap(&mounts.named()) {
                        Some(problem) => {
                            problems.push(("mounts".to_string(), problem));
                            None
                        }
                        None => Some(mounts),
                    }
                }
                _ => None,
            }
        }
        Some(_) => {
            problems.push((
                "mounts".to_string(),
                format!(
                    "must be a mapping with `{PROJECT_CLONE}`, `{PERSISTENT_STORE}` and `{SETUP}`"
                ),
            ));
            None
        }
        None => {
            problems.push(("mounts".to_string(), "is required".to_string()));
            None
        }
    };
    let broker_bin = container_path_field(&map, "", "broker-bin", &mut problems);
    let sshd_port_env = match map.get(SSHD_PORT_ENV) {
        None | Some(Value::Null) => None,
        Some(Value::String(name)) if is_env_name(name) => Some(name.clone()),
        Some(_) => {
            problems.push((
                SSHD_PORT_ENV.to_string(),
                "must be an environment variable name (letters, digits and `_`, not starting with a digit)"
                    .to_string(),
            ));
            None
        }
    };
    let port_problem = match map.get(SSHD_PORT) {
        Some(Value::Number(n))
            if n.as_u64()
                .and_then(|n| u16::try_from(n).ok())
                .is_some_and(|n| n > 0) =>
        {
            None
        }
        // The container picks it, and says so through `sshd-port-env`.
        None | Some(Value::Null) if sshd_port_env.is_some() => None,
        None => Some(format!("is required, unless `{SSHD_PORT_ENV}` is set")),
        Some(Value::Null) => Some(format!("may be null only when `{SSHD_PORT_ENV}` is set")),
        Some(_) => Some("must be a port number, 1 to 65535, or null".to_string()),
    };
    let sshd_port = match port_problem {
        Some(problem) => {
            problems.push((SSHD_PORT.to_string(), problem));
            None
        }
        None => map
            .get(SSHD_PORT)
            .and_then(Value::as_u64)
            .and_then(|n| u16::try_from(n).ok()),
    };
    let parent_builds = match map.get(PARENT_BUILDS) {
        None | Some(Value::Null) => Vec::new(),
        Some(Value::Sequence(items)) => {
            let mut builds = Vec::new();
            for (i, item) in items.iter().enumerate() {
                let key = index_key(PARENT_BUILDS, i);
                match item {
                    Value::Mapping(entry) => {
                        let dir = text_field(entry, &key, "dir", true, &mut problems);
                        let tag = text_field(entry, &key, "tag", false, &mut problems);
                        if let (Some(dir), Some(tag)) = (dir, tag) {
                            builds.push(ParentBuild {
                                dir: PathBuf::from(dir),
                                tag,
                            });
                        }
                    }
                    _ => problems.push((key, "must be a mapping with `dir` and `tag`".to_string())),
                }
            }
            builds
        }
        Some(_) => {
            problems.push((PARENT_BUILDS.to_string(), "must be a list".to_string()));
            Vec::new()
        }
    };

    match (user, mounts, broker_bin) {
        (Some(user), Some(mounts), Some(broker_bin)) if problems.is_empty() => {
            // Every key the tools use is there, for those reading `raw`.
            for key in [SSHD_PORT, SSHD_PORT_ENV] {
                map.entry(key.into()).or_insert(Value::Null);
            }
            map.entry(PARENT_BUILDS.into())
                .or_insert(Value::Sequence(Vec::new()));
            Ok(DevEnv {
                user,
                mounts,
                broker_bin,
                sshd_port,
                sshd_port_env,
                parent_builds,
                raw: Value::Mapping(map),
            })
        }
        _ => Err(problems
            .into_iter()
            .map(|(key, message)| error(&key, message))
            .collect()),
    }
}

/// Why `path` can't be a path in the container, written into the compose
/// `.env` file or a command line, if it can't: absolute, not `/` itself,
/// `/`-separated plain names (not empty, `.` or `..`), no trailing `/`, at
/// most 255 characters, and only ASCII letters, digits, `.`, `_`, `-` and
/// `/`.  Those mean nothing to compose, a shell or a mount option list.
pub fn container_path_problem(path: &str) -> Option<String> {
    const MAX_LEN: usize = 255;
    let problem = if !path.starts_with('/') {
        "must be an absolute path"
    } else if path == "/" {
        "can't be `/`"
    } else if path.len() > MAX_LEN {
        "must be at most 255 characters"
    } else if !path
        .chars()
        .all(|c| c.is_ascii_alphanumeric() || matches!(c, '.' | '_' | '-' | '/'))
    {
        "may only have ASCII letters, digits, `.`, `_`, `-` and `/`"
    } else if path.ends_with('/') {
        "must not end with `/`"
    } else {
        return path[1..].split('/').find_map(|part| match part {
            "" => Some("has an empty part; separate names with one `/`".to_string()),
            "." | ".." => Some(format!("can't have a `{part}` part")),
            _ => None,
        });
    };
    Some(problem.to_string())
}

/// Why `mounts` (each a key and a valid container path) can't be used
/// together, if they can't: two at the same place, or one inside another.
/// A mount inside another hides part of it, and the bridge would map paths
/// there to the wrong host directory.
pub fn mounts_overlap(mounts: &[(&str, &str)]) -> Option<String> {
    mounts.iter().enumerate().find_map(|(i, (key, path))| {
        mounts[i + 1..].iter().find_map(|(other, other_path)| {
            if path == other_path {
                Some(format!("`{key}` and `{other}` are both at {path}"))
            } else if other_path.starts_with(&format!("{path}/")) {
                Some(format!(
                    "`{other}` ({other_path}) is inside `{key}` ({path})"
                ))
            } else if path.starts_with(&format!("{other_path}/")) {
                Some(format!(
                    "`{key}` ({path}) is inside `{other}` ({other_path})"
                ))
            } else {
                None
            }
        })
    })
}

/// Why `user` can't be the container's user, if it can't: a plain user
/// name, `[A-Za-z_][A-Za-z0-9._-]{0,31}`, as `ssh_hosts.py` writes into
/// `~/.ssh/config` (DEV-1).
pub fn user_name_problem(user: &str) -> Option<String> {
    let mut chars = user.chars();
    let valid = user.len() <= 32
        && chars
            .next()
            .is_some_and(|c| c.is_ascii_alphabetic() || c == '_')
        && chars.all(|c| c.is_ascii_alphanumeric() || matches!(c, '.' | '_' | '-'));
    (!valid).then(|| {
        "must be a user name: a letter or `_`, then letters, digits, `.`, `_` or `-`, at most 32 characters"
            .to_string()
    })
}

/// A required container path, checked by `container_path_problem`.
fn container_path_field(
    map: &Mapping,
    parent: &str,
    key: &str,
    problems: &mut Vec<(String, String)>,
) -> Option<String> {
    let path = text_field(map, parent, key, false, problems)?;
    match container_path_problem(&path) {
        Some(problem) => {
            problems.push((child_key(parent, key), problem));
            None
        }
        None => Some(path),
    }
}

/// An environment variable name: `[A-Za-z_][A-Za-z0-9_]*`.
fn is_env_name(name: &str) -> bool {
    let mut chars = name.chars();
    chars
        .next()
        .is_some_and(|c| c.is_ascii_alphabetic() || c == '_')
        && chars.all(|c| c.is_ascii_alphanumeric() || c == '_')
}

/// A required, non-empty string; `absolute` requires an absolute container path.
/// The `name` parent `name`'s directory: the first of `search` holding
/// `NAME/Dockerfile`.
fn find_by_name(name: &str, search: &[PathBuf]) -> Result<PathBuf, String> {
    if !valid_name(name) {
        return Err(format!(
            "'{name}' must be a directory name, without '/' or '\\'"
        ));
    }
    if let Some(dir) = search
        .iter()
        .map(|dir| dir.join(name))
        .find(|dir| dir.join(DOCKERFILE).is_file())
    {
        return Ok(dir);
    }
    if search.is_empty() {
        return Err(format!(
            "can't find parent '{name}': the system configuration sets no `scoter` `path-lists` `dev-containers-search`"
        ));
    }
    let searched: Vec<String> = search.iter().map(|d| d.display().to_string()).collect();
    Err(format!(
        "can't find parent '{name}' (a directory with {DOCKERFILE}) in: {}",
        searched.join(", ")
    ))
}

fn text_field(
    map: &Mapping,
    parent: &str,
    key: &str,
    absolute: bool,
    problems: &mut Vec<(String, String)>,
) -> Option<String> {
    let at = child_key(parent, key);
    let problem = match map.get(key) {
        None => "is required",
        Some(Value::String(s)) if s.is_empty() => "must not be empty",
        Some(Value::String(s)) if absolute && !s.starts_with('/') => "must be an absolute path",
        Some(Value::String(s)) => return Some(s.clone()),
        Some(_) => "must be a string",
    };
    problems.push((at, problem.to_string()));
    None
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::config::testing::TempDir;
    use std::fs;

    const DEV_ENV: &str = "user: ubuntu\nmounts:\n  project-clone: /project\n  persistent-store: /persistent-store\n  setup: /setup\nbroker-bin: /usr/local/bin/scoter-container\nsshd-port: 22022\n";

    fn parse_text(text: &str) -> Result<DevEnv, Vec<String>> {
        parse(parse_yaml(text).unwrap(), Path::new("/c/dev-env.yaml")).map_err(|errors| {
            errors
                .into_iter()
                .map(|e| format!("{}: {}", e.key.unwrap_or_default(), e.message))
                .collect()
        })
    }

    /// The whole temporary directory as the only allowed root.
    fn roots(dir: &TempDir) -> Vec<PathBuf> {
        vec![dir.path().to_path_buf()]
    }

    fn container(dir: &TempDir, at: &str) -> PathBuf {
        dir.write(&format!("{at}/{DOCKERFILE}"), "FROM scratch\n");
        dir.write(&format!("{at}/{DEV_ENV_FILE}"), DEV_ENV);
        dir.path().join(at)
    }

    #[test]
    fn parse_full() {
        let env = parse_text(&format!("{DEV_ENV}extra:\n  anything: [1, 2]\n")).unwrap();
        assert_eq!(env.user, "ubuntu");
        assert_eq!(
            env.mounts,
            Mounts {
                project_clone: "/project".to_string(),
                persistent_store: "/persistent-store".to_string(),
                setup: "/setup".to_string()
            }
        );
        assert_eq!(env.broker_bin, "/usr/local/bin/scoter-container");
        assert_eq!(env.sshd_port, Some(22022));
        assert_eq!(env.sshd_port_env, None);
        assert!(env.parent_builds.is_empty());
        // Everything is kept, including keys the tools don't use, and the
        // keys the tools use are all there.
        assert!(env.raw.get("extra").is_some());
        assert_eq!(env.raw.get("sshd-port-env"), Some(&Value::Null));
        assert_eq!(env.raw.get("parent-builds"), Some(&Value::Sequence(vec![])));
    }

    #[test]
    fn parse_errors_are_gathered() {
        assert_eq!(
            parse_text("").unwrap_err(),
            [
                "user: is required",
                "mounts: is required",
                "broker-bin: is required",
                "sshd-port: is required, unless `sshd-port-env` is set",
            ]
        );
        assert_eq!(
            parse_text("user: 5\nmounts:\n  project-clone: rel\n  persistent-store: /p\n  setup: ''\nbroker-bin: bin/broker\nsshd-port: 70000\n").unwrap_err(),
            [
                "user: must be a string",
                "mounts.project-clone: must be an absolute path",
                "mounts.setup: must not be empty",
                "broker-bin: must be an absolute path",
                "sshd-port: must be a port number, 1 to 65535, or null",
            ],
        );
        assert_eq!(parse_text("[a]\n").unwrap_err(), [": must be a mapping"]);
        assert_eq!(
            parse_text(&DEV_ENV.replace("22022", "'22'")).unwrap_err(),
            ["sshd-port: must be a port number, 1 to 65535, or null"]
        );
        assert_eq!(
            parse_text(&DEV_ENV.replace("22022", "0")).unwrap_err(),
            ["sshd-port: must be a port number, 1 to 65535, or null"]
        );
    }

    #[test]
    fn parse_checks_container_paths() {
        let with_clone = |path: &str| {
            DEV_ENV.replace("project-clone: /project", &format!("project-clone: {path}"))
        };
        for path in ["/w", "/home/ubuntu", "/a.b/c_d-e", "/0"] {
            assert_eq!(
                parse_text(&with_clone(path)).unwrap().mounts.project_clone,
                path
            );
        }
        for (path, problem) in [
            ("w", "must be an absolute path"),
            ("/", "can't be `/`"),
            ("/w/", "must not end with `/`"),
            ("/a//b", "has an empty part"),
            ("/a/../b", "can't have a `..` part"),
            ("/a/./b", "can't have a `.` part"),
            (
                "\"/w\\nWORKSPACE_HOST=/home/you\"",
                "may only have ASCII letters",
            ),
            ("'/w:/x'", "may only have ASCII letters"),
            ("'/w,ro'", "may only have ASCII letters"),
            ("'/w x'", "may only have ASCII letters"),
            ("'/${HOME}'", "may only have ASCII letters"),
            ("'/w#x'", "may only have ASCII letters"),
            ("\"/w'x\"", "may only have ASCII letters"),
            ("/caf\u{e9}", "may only have ASCII letters"),
        ] {
            let errors = parse_text(&with_clone(path)).unwrap_err();
            assert_eq!(errors.len(), 1, "{path}: {errors:?}");
            assert!(
                errors[0].starts_with("mounts.project-clone: ") && errors[0].contains(problem),
                "{path}: {errors:?}"
            );
        }
        let long = format!("/{}", "a".repeat(255));
        assert!(parse_text(&with_clone(&long)).unwrap_err()[0].contains("at most 255"));
        // `broker-bin` is a container path too.
        assert_eq!(
            parse_text(&DEV_ENV.replace("/usr/local/bin/scoter-container", "'/bin/x;y'"))
                .unwrap_err(),
            ["broker-bin: may only have ASCII letters, digits, `.`, `_`, `-` and `/`"]
        );
    }

    #[test]
    fn parse_refuses_overlapping_mounts() {
        for (clone, problem) in [
            ("/setup", "`project-clone` and `setup` are both at /setup"),
            (
                "/persistent-store/p",
                "`project-clone` (/persistent-store/p) is inside `persistent-store` (/persistent-store)",
            ),
            (
                "/setup/x",
                "`project-clone` (/setup/x) is inside `setup` (/setup)",
            ),
        ] {
            assert_eq!(
                parse_text(&DEV_ENV.replace(
                    "project-clone: /project",
                    &format!("project-clone: {clone}")
                ))
                .unwrap_err(),
                [format!("mounts: {problem}")]
            );
        }
        assert_eq!(
            parse_text(&DEV_ENV.replace("setup: /setup", "setup: /project/s")).unwrap_err(),
            ["mounts: `setup` (/project/s) is inside `project-clone` (/project)"]
        );
        // A shared prefix isn't nesting.
        assert!(parse_text(&DEV_ENV.replace("setup: /setup", "setup: /project-setup")).is_ok());
    }

    #[test]
    fn parse_checks_the_user() {
        for user in ["ubuntu", "_u", "a.b-c_d", &"u".repeat(32)] {
            assert_eq!(
                parse_text(&DEV_ENV.replace("user: ubuntu", &format!("user: {user}")))
                    .unwrap()
                    .user,
                user
            );
        }
        for user in [
            "'1u'",
            "'-u'",
            "'u u'",
            "\"u\\nMatch all\"",
            "'u:x'",
            &"u".repeat(33),
        ] {
            let errors =
                parse_text(&DEV_ENV.replace("user: ubuntu", &format!("user: {user}"))).unwrap_err();
            assert_eq!(errors.len(), 1, "{user}: {errors:?}");
            assert!(
                errors[0].starts_with("user: must be a user name"),
                "{user}: {errors:?}"
            );
        }
    }

    #[test]
    fn parse_rejects_tags() {
        let errors = parse_text(&format!("{DEV_ENV}x: !include y\n")).unwrap_err();
        assert_eq!(errors.len(), 1);
        assert!(errors[0].starts_with("x: YAML tags"), "{errors:?}");
    }

    #[test]
    fn find_by_path() {
        let dir = TempDir::new();
        let expected = container(&dir, "proj/.surf-scoter.d/env");
        let project = dir.path().join("proj/.surf-scoter.d/project.yaml");
        let found = find(&ContainerRef::Path("./env".to_string()), &project, &[]).unwrap();
        assert_eq!(found.dir.canonicalize().unwrap(), expected);
        assert_eq!(found.env.sshd_port, Some(22022));
        // An absolute path is used as is.
        let abs = expected.display().to_string();
        assert_eq!(
            find(&ContainerRef::Path(abs), &project, &[]).unwrap().dir,
            expected
        );
    }

    #[test]
    fn find_by_name() {
        let dir = TempDir::new();
        // Incomplete: no dev-env.yaml.
        dir.write("a/env/Dockerfile", "");
        let expected = container(&dir, "b/env");
        container(&dir, "c/env");
        let lookup = [
            dir.path().join("missing"),
            dir.path().join("a"),
            dir.path().join("b"),
            dir.path().join("c"),
        ];
        let found = find(
            &ContainerRef::Name("env".to_string()),
            Path::new("/p/scoter.yaml"),
            &lookup,
        )
        .unwrap();
        assert_eq!(found.dir, expected);
        assert_eq!(found.dockerfile(), expected.join(DOCKERFILE));
        assert_eq!(found.env_file(), expected.join(DEV_ENV_FILE));
    }

    #[test]
    fn find_outside_errors() {
        let dir = TempDir::new();
        let project = dir.write("proj/.surf-scoter.d/project.yaml", "");
        let outside = container(&dir, "proj/env");
        let errors = find(&ContainerRef::Path("../env".to_string()), &project, &[]).unwrap_err();
        assert_eq!(errors.len(), 1, "{errors:?}");
        assert_eq!(
            errors[0].key.as_deref(),
            Some("container.dev-container-path")
        );
        assert!(
            errors[0].message.ends_with(&format!(
                "is outside the project files directory and the `dev-containers-search` directories: {}",
                dir.path().join("proj/.surf-scoter.d").display()
            )),
            "{}",
            errors[0]
        );
        // An absolute path is no different.
        let abs = ContainerRef::Path(outside.display().to_string());
        assert!(find(&abs, &project, &[]).is_err());
        // A search directory makes it allowed.
        assert_eq!(
            find(&abs, &project, &[dir.path().join("proj")])
                .unwrap()
                .dir,
            outside
        );
    }

    #[cfg(unix)]
    #[test]
    fn find_symlink_outside_errors() {
        let dir = TempDir::new();
        let outside = container(&dir, "elsewhere/env");
        fs::create_dir_all(dir.path().join("proj/.surf-scoter.d")).unwrap();
        fs::create_dir_all(dir.path().join("search")).unwrap();
        let project = dir.path().join("proj/.surf-scoter.d/project.yaml");
        std::os::unix::fs::symlink(&outside, dir.path().join("proj/.surf-scoter.d/env")).unwrap();
        std::os::unix::fs::symlink(&outside, dir.path().join("search/env")).unwrap();

        let errors = find(&ContainerRef::Path("env".to_string()), &project, &[]).unwrap_err();
        assert_eq!(
            errors[0].key.as_deref(),
            Some("container.dev-container-path")
        );
        let errors = find(
            &ContainerRef::Name("env".to_string()),
            &project,
            &[dir.path().join("search")],
        )
        .unwrap_err();
        assert_eq!(errors[0].key.as_deref(), Some("container.dev-container"));
    }

    #[test]
    fn load_parents_outside_errors() {
        let dir = TempDir::new();
        container(&dir, "outside/base");
        container_with(
            &dir,
            "proj/.surf-scoter.d/env",
            concat!(
                "parents:\n",
                "  - {dir: ../../../outside/base, tag: base}\n",
                "  - {file: ../../../outside/base/dev-env.yaml}\n",
            ),
        );
        let roots = [dir.path().join("proj/.surf-scoter.d")];
        let errors: Vec<String> = load(&dir.path().join("proj/.surf-scoter.d/env"), &roots, &[])
            .unwrap_err()
            .into_iter()
            .map(|e| e.key.unwrap_or_default())
            .collect();
        assert_eq!(errors, ["parents[0].dir", "parents[1].file"]);
        // With the other directory allowed, it loads.
        let roots = [
            dir.path().join("proj/.surf-scoter.d"),
            dir.path().join("outside"),
        ];
        assert!(load(&dir.path().join("proj/.surf-scoter.d/env"), &roots, &[]).is_ok());
    }

    #[test]
    fn find_by_name_errors() {
        let dir = TempDir::new();
        let project = Path::new("/p/scoter.yaml");
        let name = ContainerRef::Name("env".to_string());
        let errors = find(&name, project, &[]).unwrap_err();
        assert_eq!(
            errors[0].to_string(),
            "/p/scoter.yaml: container.dev-container: can't find dev container 'env': the system configuration sets no `scoter` `path-lists` `dev-containers-search`",
        );
        let errors = find(&name, project, &[dir.path().to_path_buf()]).unwrap_err();
        assert!(
            errors[0]
                .message
                .ends_with(&format!("in: {}", dir.path().display())),
            "{}",
            errors[0]
        );
    }

    #[test]
    fn load_errors() {
        let dir = TempDir::new();
        let missing = load(&dir.path().join("nothing"), &roots(&dir), &[]).unwrap_err();
        assert_eq!(missing.len(), 1);
        assert!(missing[0].message.contains("doesn't exist"));

        dir.write("bad/dev-env.yaml", "user: ubuntu\n");
        let errors = load(&dir.path().join("bad"), &roots(&dir), &[]).unwrap_err();
        let messages: Vec<String> = errors.iter().map(|e| e.message.clone()).collect();
        // No Dockerfile, plus the three missing fields.
        assert_eq!(messages.len(), 4, "{messages:?}");
        assert_eq!(messages[0], "the dev container has no Dockerfile");

        dir.write("big/Dockerfile", "");
        dir.write("big/dev-env.yaml", &"#".repeat(3 * 1024 * 1024));
        let errors = load(&dir.path().join("big"), &roots(&dir), &[]).unwrap_err();
        assert_eq!(errors[0].message, "the file is larger than the 2 MB limit");
    }
    #[test]
    fn parse_sshd_port_env() {
        let dynamic = DEV_ENV.replace("sshd-port: 22022\n", "sshd-port-env: SSHD_PORT\n");
        let env = parse_text(&dynamic).unwrap();
        assert_eq!(env.sshd_port, None);
        assert_eq!(env.sshd_port_env.as_deref(), Some("SSHD_PORT"));
        assert_eq!(env.raw.get("sshd-port"), Some(&Value::Null));
        let null_port = DEV_ENV.replace("22022", "null\nsshd-port-env: _P1");
        assert_eq!(parse_text(&null_port).unwrap().sshd_port, None);
        // Both: the container is told the fixed port.
        let both = parse_text(&format!("{DEV_ENV}sshd-port-env: P\n")).unwrap();
        assert_eq!(both.sshd_port, Some(22022));
        assert_eq!(both.sshd_port_env.as_deref(), Some("P"));

        assert_eq!(
            parse_text(&DEV_ENV.replace("22022", "null")).unwrap_err(),
            ["sshd-port: may be null only when `sshd-port-env` is set"]
        );
        assert_eq!(
            parse_text(&DEV_ENV.replace("22022", "null\nsshd-port-env: null")).unwrap_err(),
            ["sshd-port: may be null only when `sshd-port-env` is set"]
        );
        for bad in ["1PORT", "''", "A-B", "5"] {
            let errors = parse_text(&format!("{DEV_ENV}sshd-port-env: {bad}\n")).unwrap_err();
            assert_eq!(errors.len(), 1, "{bad}: {errors:?}");
            assert!(errors[0].starts_with("sshd-port-env: must be an environment variable name"));
        }
    }

    #[test]
    fn parse_resolved_parents() {
        let env = parse_text(&format!(
            "{DEV_ENV}parent-builds:\n  - {{dir: /c/base, tag: 'base:1'}}\n"
        ))
        .unwrap();
        assert_eq!(
            env.parent_builds,
            [ParentBuild {
                dir: PathBuf::from("/c/base"),
                tag: "base:1".to_string()
            }]
        );
        assert_eq!(
            parse_text(&format!(
                "{DEV_ENV}parents: []\nparent-builds:\n  - {{dir: rel, tag: t}}\n  - x\n"
            ))
            .unwrap_err(),
            [
                "parents: must already be resolved into `parent-builds`",
                "parent-builds[0].dir: must be an absolute path",
                "parent-builds[1]: must be a mapping with `dir` and `tag`",
            ]
        );
    }

    /// A container directory with a `dev-env.yaml` of `text`.
    fn container_with(dir: &TempDir, at: &str, text: &str) -> PathBuf {
        dir.write(&format!("{at}/{DOCKERFILE}"), "FROM scratch\n");
        dir.write(&format!("{at}/{DEV_ENV_FILE}"), text);
        dir.path().join(at).canonicalize().unwrap()
    }

    fn build(dir: &Path, tag: &str) -> ParentBuild {
        ParentBuild {
            dir: dir.to_path_buf(),
            tag: tag.to_string(),
        }
    }

    #[test]
    fn load_local_parent_only() {
        let dir = TempDir::new();
        let base = container(&dir, "base").canonicalize().unwrap();
        container_with(
            &dir,
            "env",
            "parents:\n  - {dir: ../base, tag: 'base:latest'}\n",
        );
        let env = load(&dir.path().join("env"), &roots(&dir), &[])
            .unwrap()
            .env;
        assert_eq!(env.user, "ubuntu");
        assert_eq!(env.sshd_port, Some(22022));
        assert_eq!(env.parent_builds, [build(&base, "base:latest")]);
        // `parents` is replaced by what it resolves to.
        assert!(env.raw.get("parents").is_none());
        assert_eq!(
            env.raw["parent-builds"][0]["dir"],
            Value::from(base.display().to_string())
        );
        // What `load` makes, `parse` accepts as is.
        let again = parse(env.raw.clone(), Path::new("/x/dev-env.yaml")).unwrap();
        assert_eq!(again, env);
    }

    #[test]
    fn published_parent_overrides() {
        let dir = TempDir::new();
        // A published image's parents are already in it: not built.
        container(&dir, "published-base");
        dir.write(
            "published/dev-env.yaml",
            "parents: [{dir: ../published-base, tag: pb}]\nuser: root\nextra: {a: 1, b: 2}\n",
        );
        container_with(
            &dir,
            "env",
            concat!(
                "parents:\n  - file: ../published/dev-env.yaml\n",
                "mounts: {setup: /other}\nsshd-port: null\nsshd-port-env: PORT\n",
                "extra: {b: 3}\n",
            ),
        );
        let env = load(&dir.path().join("env"), &roots(&dir), &[])
            .unwrap()
            .env;
        assert!(env.parent_builds.is_empty());
        // Nearest wins; mappings merge key by key.
        assert_eq!(env.user, "root");
        assert_eq!(
            env.mounts,
            Mounts {
                project_clone: "/project".to_string(),
                persistent_store: "/persistent-store".to_string(),
                setup: "/other".to_string()
            }
        );
        assert_eq!(env.sshd_port, None);
        assert_eq!(env.sshd_port_env.as_deref(), Some("PORT"));
        assert_eq!(env.raw["extra"]["a"], Value::from(1));
        assert_eq!(env.raw["extra"]["b"], Value::from(3));
    }

    #[test]
    fn local_parent_without_dev_env() {
        let dir = TempDir::new();
        // Say, the project's CI build image: only a Dockerfile.
        dir.write("ci/Dockerfile", "FROM scratch\n");
        let ci = dir.path().join("ci").canonicalize().unwrap();
        container_with(
            &dir,
            "env",
            &format!("parents: [{{dir: ../ci, tag: 'ci:latest'}}]\n{DEV_ENV}"),
        );
        let env = load(&dir.path().join("env"), &roots(&dir), &[])
            .unwrap()
            .env;
        assert_eq!(env.parent_builds, [build(&ci, "ci:latest")]);
        assert_eq!(env.user, "ubuntu");

        // It adds no values, so this file must give them all.
        container_with(&dir, "bare", "parents: [{dir: ../ci, tag: ci}]\n");
        let errors = load(&dir.path().join("bare"), &roots(&dir), &[]).unwrap_err();
        assert_eq!(errors.len(), 4, "{errors:?}");
    }

    #[test]
    fn parent_graph_in_build_order() {
        let dir = TempDir::new();
        // root <- a, root <- b, and env builds on both, then `c` on a file.
        let root = container(&dir, "root").canonicalize().unwrap();
        let a = container_with(
            &dir,
            "a",
            "parents: [{dir: ../root, tag: root}]\nuser: from-a\n",
        );
        let b = container_with(
            &dir,
            "b",
            "parents: [{dir: ../root, tag: root}]\nuser: from-b\n",
        );
        container_with(
            &dir,
            "env",
            "parents:\n  - {dir: ../a, tag: a}\n  - {dir: ../b, tag: b}\n",
        );
        let env = load(&dir.path().join("env"), &roots(&dir), &[])
            .unwrap()
            .env;
        assert_eq!(
            env.parent_builds,
            [build(&root, "root"), build(&a, "a"), build(&b, "b")]
        );
        // The later parent wins.
        assert_eq!(env.user, "from-b");
    }

    #[test]
    fn load_parent_errors() {
        let dir = TempDir::new();
        container_with(&dir, "x", "parents: [{dir: ../y, tag: y}]\n");
        container_with(&dir, "y", "parents: [{dir: ../x, tag: x}]\n");
        let errors = load(&dir.path().join("x"), &roots(&dir), &[]).unwrap_err();
        assert_eq!(errors.len(), 1, "{errors:?}");
        assert!(
            errors[0].message.starts_with("is its own parent: "),
            "{}",
            errors[0]
        );

        container(&dir, "root");
        container_with(&dir, "a", "parents: [{dir: ../root, tag: one}]\n");
        container_with(
            &dir,
            "two-tags",
            "parents: [{dir: ../a, tag: a}, {dir: ../root, tag: two}]\n",
        );
        let errors = load(&dir.path().join("two-tags"), &roots(&dir), &[]).unwrap_err();
        assert_eq!(errors.len(), 1, "{errors:?}");
        assert_eq!(errors[0].key.as_deref(), Some("parents[1].tag"));
        assert!(
            errors[0]
                .message
                .ends_with("is also built as 'one'; a parent has one tag")
        );

        dir.write("no-docker/dev-env.yaml", DEV_ENV);
        container_with(
            &dir,
            "bad",
            concat!(
                "parent-builds: []\n",
                "parents:\n",
                "  - {dir: ../no-docker, tag: t}\n",
                "  - {file: ../missing.yaml}\n",
                "  - {dir: ../root}\n",
                "  - {dir: ../root, file: ../root/dev-env.yaml}\n",
                "  - {file: ../root/dev-env.yaml, tag: t, other: 1}\n",
                "  - text\n",
            ),
        );
        let errors: Vec<String> = load(&dir.path().join("bad"), &roots(&dir), &[])
            .unwrap_err()
            .into_iter()
            .map(|e| format!("{}: {}", e.key.unwrap_or_default(), e.message))
            .collect();
        let at = |p: &str| dir.path().canonicalize().unwrap().join("bad/..").join(p);
        assert_eq!(
            errors,
            [
                "parent-builds: is worked out from `parents`; don't set it".to_string(),
                format!(
                    "parents[0].dir: {} has no Dockerfile",
                    at("no-docker").display()
                ),
                format!("parents[1]: {} doesn't exist", at("missing.yaml").display()),
                "parents[2].tag: is required".to_string(),
                "parents[3]: needs one of `dir`, `name` or `file`".to_string(),
                "parents[4].tag: goes with `dir`; a `file` parent isn't built".to_string(),
                "parents[4]: has an unknown key String(\"other\")".to_string(),
                "parents[5]: must be a mapping with `dir` and `tag`, `name` and `tag`, or `file`"
                    .to_string(),
            ]
        );

        // The merged result must still be whole.
        container_with(&dir, "partial", "user: u\n");
        container_with(&dir, "child", "parents: [{dir: ../partial, tag: p}]\n");
        let errors = load(&dir.path().join("child"), &roots(&dir), &[]).unwrap_err();
        assert_eq!(errors.len(), 3, "{errors:?}");
    }

    // ---- `name` parents ----

    #[test]
    fn name_parent_from_second_dir() {
        let dir = TempDir::new();
        // Not a container: no Dockerfile.
        dir.write("first/base/dev-env.yaml", DEV_ENV);
        let base = container_with(&dir, "second/base", "user: from-base\n");
        container_with(&dir, "third/base", "user: from-third\n");
        let child = container_with(
            &dir,
            "proj/env",
            "parents: [{name: base, tag: 'base:1'}]\nmounts: {project-clone: /w, persistent-store: /p, setup: /s}\nbroker-bin: /b\nsshd-port: 1\n",
        );
        let search = [
            dir.path().join("missing"),
            dir.path().join("first"),
            dir.path().join("second"),
            dir.path().join("third"),
        ];
        let env = load(&child, &roots(&dir), &search).unwrap().env;
        assert_eq!(env.user, "from-base");
        assert_eq!(env.parent_builds, [build(&base, "base:1")]);
    }

    #[test]
    fn load_name_parent_errors() {
        let dir = TempDir::new();
        let child = container_with(
            &dir,
            "proj/env",
            "parents:\n  - {name: base, tag: b}\n  - {name: ../up, tag: u}\n  - {name: base}\n  - {name: base, dir: ., tag: t}\n",
        );
        let search = [dir.path().join("a"), dir.path().join("b")];
        let errors: Vec<String> = load(&child, &roots(&dir), &search)
            .unwrap_err()
            .iter()
            .map(|e| format!("{}: {}", e.key.as_deref().unwrap_or(""), e.message))
            .collect();
        assert_eq!(
            errors,
            [
                format!(
                    "parents[0].name: can't find parent 'base' (a directory with Dockerfile) in: {}, {}",
                    search[0].display(),
                    search[1].display()
                ),
                "parents[1].name: '../up' must be a directory name, without '/' or '\\'"
                    .to_string(),
                "parents[2].tag: is required".to_string(),
                format!(
                    "parents[2].name: can't find parent 'base' (a directory with Dockerfile) in: {}, {}",
                    search[0].display(),
                    search[1].display()
                ),
                "parents[3]: needs one of `dir`, `name` or `file`".to_string(),
            ]
        );
        let errors = load(&child, &roots(&dir), &[]).unwrap_err();
        assert_eq!(
            errors[0].message,
            "can't find parent 'base': the system configuration sets no `scoter` `path-lists` `dev-containers-search`"
        );
    }

    #[cfg(unix)]
    #[test]
    fn name_parent_symlink_outside() {
        let dir = TempDir::new();
        let outside = container(&dir, "elsewhere/base");
        fs::create_dir_all(dir.path().join("search")).unwrap();
        std::os::unix::fs::symlink(&outside, dir.path().join("search/base")).unwrap();
        let child = container_with(&dir, "proj/env", "parents: [{name: base, tag: b}]\n");
        let roots = [dir.path().join("proj"), dir.path().join("search")];
        let errors = load(&child, &roots, &[dir.path().join("search")]).unwrap_err();
        assert_eq!(errors.len(), 1, "{errors:?}");
        assert_eq!(errors[0].key.as_deref(), Some("parents[0].name"));
        assert!(
            errors[0]
                .message
                .contains("is outside the project files directory"),
            "{}",
            errors[0]
        );
    }

    #[test]
    fn mixed_parents_in_build_order() {
        let dir = TempDir::new();
        let low = container_with(&dir, "search/low", "sshd-port: 1\n");
        let named = container_with(
            &dir,
            "search/named",
            "parents: [{dir: ../low, tag: low}]\nuser: named\n",
        );
        let local = container_with(&dir, "proj/local", "user: local\n");
        dir.write("proj/published.yaml", "sshd-port: 3\n");
        let child = container_with(
            &dir,
            "proj/env",
            concat!(
                "parents:\n",
                "  - {name: named, tag: named}\n",
                "  - {dir: ../local, tag: local}\n",
                "  - {file: ../published.yaml}\n",
                "mounts: {project-clone: /w, persistent-store: /p, setup: /s}\nbroker-bin: /b\n",
            ),
        );
        let env = load(&child, &roots(&dir), &[dir.path().join("search")])
            .unwrap()
            .env;
        assert_eq!(
            env.parent_builds,
            [
                build(&low, "low"),
                build(&named, "named"),
                build(&local, "local")
            ]
        );
        // Later parents win: `local`'s user, the published file's port.
        assert_eq!(env.user, "local");
        assert_eq!(env.sshd_port, Some(3));
    }
}
