//! The universal configuration file: the whole configuration, gathered into
//! one JSON document that tools without a YAML parser can read.  See
//! `docs/contributors/universal-configuration-file.md`.
//!
//! `resolved` holds every section of the system configuration, `scoter`
//! included, as its `paths` (file locations), its `programs` (found on the
//! `PATH`), its `runs` commands, its `path-lists`, `value-lists` and `values`.  A
//! program that isn't there is `null`, as is every value referring to it.
//!
//! Each `runs` command is a list of arguments, the first being the program,
//! with quotes already removed and `{x:name}` placeholders left in place.
//! Fill in the placeholders argument by argument; a value with spaces stays
//! one argument.
//!
//! `host-command-checks` holds every section's `host-commands` checks, put
//! together: each check expanded, the program each command name they map
//! runs, the action the name-only checks give each of those commands, and
//! notices about checks that are removed, replaced or overridden.
//!
//! `setup` holds the system setup files' settings as written, so a later run
//! can start from this file (with a project file found since, say) without
//! reading the setup files again.

use std::collections::BTreeMap;
use std::path::Path;

use serde::{Deserialize, Serialize};
use serde_json::{Map, Number, Value as Json};
use yaml_serde::Value as Yaml;

use crate::config::host_commands::{self, Combined};
use crate::config::simple_checks::{EGRESS_HOSTS, PROJECT_FILES, SimpleCheck};
use crate::config::system::{self, LoadedFrom, ScalarValue, SectionDefaults, SetupRecord};
use crate::config::{ConfigError, ConfigErrors, FullConfig, child_key, index_key};

/// The universal configuration file's contents.
#[derive(Debug, Clone, PartialEq, Serialize)]
#[serde(rename_all = "kebab-case")]
pub struct Universal {
    /// Where the configuration in effect came from.  (The setup files read
    /// are in `setup`.)
    pub active_config_locations: ActiveConfigLocations,
    /// Every section, expanded.
    pub resolved: BTreeMap<String, UniversalSection>,
    /// Every section's `host-commands` checks, put together.
    pub host_command_checks: Combined,
    /// The project configuration file, as written: only a record of the
    /// project's suggestion, which the review is made from.
    pub current_project_setup: Option<Json>,
    /// The project file's `env` blocks worked out against the setup, each on
    /// its own (not merged): `{"env": PLAN, "commands": [{"env": PLAN}]}`,
    /// `${section:key}` expanded and `@NAME@` left.  The review shows them.
    pub current_project_resolved: Option<Json>,
    /// The commands `scoter-host` runs: the accepted settings' `commands`
    /// (empty without them), each with `program` the absolute executable, or
    /// `null`, and `env` the command's whole
    /// environment plan: the project's `env` with the command's added, its
    /// `${section:key}` references expanded.  `@NAME@` is left as written,
    /// as it's the environment of the program running the commands.
    pub host_brokered: Vec<Json>,
    /// The accepted settings `host-brokered` was built from: the verified
    /// store's file, and its contents; `null` without them.
    pub accepted_settings: Option<Json>,
    /// The accepted egress rules, and the gateway they were accepted for;
    /// `null` without accepted settings.
    pub egress: Option<Json>,
    /// The user's review file, when given: its `file`, its `result`
    /// (`accept` or `reject`), and the accepted settings it gives (`null`
    /// when rejected), which the checks judge before anything is stored.
    pub review: Option<Json>,
    /// An earlier acceptance's review file, when given: its `file`, and its
    /// `data` as the user left it, which a new review starts from.
    pub previous_review: Option<Json>,
    /// The dev container's `dev-env.yaml`.
    pub dev_container: Option<Json>,
    /// The system setup as read.
    pub setup: SetupRecord,
    /// A history of the runs' inputs.
    pub loaded_from: LoadedFrom,
}

/// Where the configuration in effect came from: the `scoter` section's
/// `paths` of the same names.
#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "kebab-case")]
pub struct ActiveConfigLocations {
    /// The project configuration file, if any.
    #[serde(default)]
    pub project_config: Option<String>,
    /// The project directory: `--project-dir`, or the project file's.
    #[serde(default)]
    pub project_dir: Option<String>,
    /// The root of the repository holding the project (`--vcs-root`).
    #[serde(default)]
    pub vcs_root: Option<String>,
    /// The project's id (`scoter:project-id`), worked out from its directory.
    #[serde(default)]
    pub project_id: Option<String>,
    /// The project's branch, made safe as a path part (`scoter:project-branch`).
    #[serde(default)]
    pub project_branch: Option<String>,
    /// The project's dev container directory.
    #[serde(default)]
    pub dev_container_path: Option<String>,
    /// The dev container's mounts: the project workspace, and the settings
    /// directory.
    #[serde(default)]
    pub dev_container_workspace_dir: Option<String>,
    /// Where the project is cloned: the workspace, with the project file's
    /// `container: workspace-subdir`.  Worked out each run.
    #[serde(default)]
    pub dev_container_project_dir: Option<String>,
    #[serde(default)]
    pub dev_container_settings_dir: Option<String>,
    /// The user's setup file found, and the user directory it's in.
    #[serde(default)]
    pub user_setup_file: Option<String>,
    #[serde(default)]
    pub user_dir: Option<String>,
    /// The distribution directory.
    #[serde(default)]
    pub distribution_dir: Option<String>,
    /// The distribution's programs: `${distribution-dir}/bin`, unless
    /// another program changed it.
    #[serde(default)]
    pub distribution_bin_dir: Option<String>,
    /// The directory the tool ran in.
    #[serde(default)]
    pub cwd: Option<String>,
}

/// One section of the system configuration.  A value is `null` when it is,
/// or refers to, a program that isn't there.
#[derive(Debug, Clone, PartialEq, Serialize)]
#[serde(rename_all = "kebab-case")]
pub struct UniversalSection {
    /// File locations, by key.
    pub paths: BTreeMap<String, Option<String>>,
    /// Programs found, by key.
    pub programs: BTreeMap<String, Option<String>>,
    /// Commands, by key, as their arguments.
    pub runs: BTreeMap<String, Option<Vec<String>>>,
    /// Lists of file locations, by key.
    pub path_lists: BTreeMap<String, Option<Vec<String>>>,
    /// Lists of plain values, by key.
    pub value_lists: BTreeMap<String, Option<Vec<String>>>,
    /// Plain values, by key.
    pub values: BTreeMap<String, ScalarValue>,
    /// The `project-files` and `egress-hosts` checks, as written, by key.
    #[serde(skip_serializing_if = "BTreeMap::is_empty")]
    pub project_files: BTreeMap<String, SimpleCheck>,
    #[serde(skip_serializing_if = "BTreeMap::is_empty")]
    pub egress_hosts: BTreeMap<String, SimpleCheck>,
}

/// The problems with the `project-files` and `egress-hosts` checks' overrides,
/// which no one file can tell: each must name a check of the same group that
/// is set, and that isn't an override itself.
fn simple_check_problems(
    system: &crate::config::system::SystemConfig,
    resolved: &BTreeMap<String, UniversalSection>,
) -> Vec<ConfigError> {
    let mut errors = Vec::new();
    for group in [PROJECT_FILES, EGRESS_HOSTS] {
        let checks: BTreeMap<String, (&str, &str, &SimpleCheck)> = resolved
            .iter()
            .flat_map(|(name, section)| {
                let checks = if group == EGRESS_HOSTS {
                    &section.egress_hosts
                } else {
                    &section.project_files
                };
                checks.iter().map(move |(key, check)| {
                    (
                        format!("{name}:{key}"),
                        (name.as_str(), key.as_str(), check),
                    )
                })
            })
            .collect();
        for (name, key, check) in checks.values() {
            for (i, target) in check.overrides.iter().enumerate() {
                let problem = match checks.get(target) {
                    None => Some(format!("`{target}` isn't one of the `{group}` checks set")),
                    Some((_, _, overridden)) if !overridden.overrides.is_empty() => Some(format!(
                        "`{target}` is an override itself; override the check it overrides"
                    )),
                    Some(_) => None,
                };
                if let Some(message) = problem {
                    errors.push(ConfigError::new(
                        system.check_file(name, group, key),
                        &format!("{name}.{group}.{key}.overrides[{i}]"),
                        message,
                    ));
                }
            }
        }
    }
    errors
}

/// Gather the universal configuration from a loaded configuration.
pub fn build(config: &FullConfig) -> Result<Universal, ConfigErrors> {
    let mut errors = Vec::new();
    let system = &config.system;

    let mut resolved = BTreeMap::new();
    let mut sections = Vec::new();
    for name in system.sections() {
        match system.section(name, &SectionDefaults::default()) {
            Ok(section) => {
                let found = |map: &BTreeMap<String, Option<std::path::PathBuf>>| {
                    map.iter()
                        .map(|(key, path)| (key.clone(), path.as_deref().map(path_text)))
                        .collect()
                };
                let runs = section
                    .runs
                    .iter()
                    .map(|(key, template)| (key.clone(), template.as_ref().map(|t| t.to_args())))
                    .collect();
                let path_lists = section
                    .path_lists
                    .iter()
                    .map(|(key, list)| {
                        let list = list
                            .as_ref()
                            .map(|l| l.iter().map(|p| path_text(p)).collect());
                        (key.clone(), list)
                    })
                    .collect();
                resolved.insert(
                    name.to_string(),
                    UniversalSection {
                        paths: found(&section.paths),
                        programs: found(&section.programs),
                        runs,
                        path_lists,
                        value_lists: section.value_lists.clone(),
                        values: section.values.clone(),
                        project_files: section.project_files.clone(),
                        egress_hosts: section.egress_hosts.clone(),
                    },
                );
                sections.push(section);
            }
            Err(e) => errors.push(e),
        }
    }
    let host_command_checks = host_commands::combine(system, &sections)
        .map_err(|e| errors.extend(e))
        .unwrap_or_default();
    errors.extend(simple_check_problems(system, &resolved));

    // Only the accepted commands run, as accepted: never the project file's.
    let host_brokered = config
        .accepted
        .as_ref()
        .map(|accepted| accepted.raw_commands.clone())
        .unwrap_or_default();

    let current_project_setup = config
        .project
        .as_ref()
        .map(|p| yaml_to_json(&p.raw, &p.path, "", &mut errors));
    let dev_container = config
        .dev_container
        .as_ref()
        .map(|dev| yaml_to_json(&dev.env.raw, &dev.env_file(), "", &mut errors));

    if !errors.is_empty() {
        return Err(ConfigErrors(errors));
    }
    let location = |key| system.get(system::SCOTER_SECTION, key);
    Ok(Universal {
        active_config_locations: ActiveConfigLocations {
            project_config: location(system::PROJECT_CONFIG),
            project_dir: location(system::PROJECT_DIR),
            vcs_root: location(system::VCS_ROOT),
            project_id: location(system::PROJECT_ID),
            project_branch: location(system::PROJECT_BRANCH),
            dev_container_path: location(system::DEV_CONTAINER_PATH),
            dev_container_workspace_dir: location(system::DEV_CONTAINER_WORKSPACE_DIR),
            dev_container_project_dir: location(system::DEV_CONTAINER_PROJECT_DIR),
            dev_container_settings_dir: location(system::DEV_CONTAINER_SETTINGS_DIR),
            user_setup_file: location(system::USER_SETUP_FILE),
            user_dir: location(system::USER_DIR),
            distribution_dir: location(system::DISTRIBUTION_DIR),
            distribution_bin_dir: location(system::DISTRIBUTION_BIN_DIR),
            cwd: location(system::CWD),
        },
        resolved,
        host_command_checks,
        current_project_setup,
        current_project_resolved: config.project.as_ref().map(|project| {
            let plan = |plan: &crate::config::environment::EnvPlan| {
                serde_json::to_value(plan).expect("only text is stored")
            };
            serde_json::json!({
                "env": plan(&project.env_resolved),
                "commands": project
                    .commands
                    .iter()
                    .map(|rule| serde_json::json!({"env": plan(&rule.env_resolved)}))
                    .collect::<Vec<_>>(),
            })
        }),
        host_brokered,
        accepted_settings: config.accepted.as_ref().map(|a| a.to_universal()),
        egress: config.accepted.as_ref().map(|a| a.egress_universal()),
        review: config.review.as_ref().map(|review| {
            let vcs = config.project.as_ref().and_then(|p| p.vcs.as_deref());
            let gateway = system.get(
                crate::config::accepted::GATEWAY_SECTION,
                crate::config::accepted::GATEWAY_NAME,
            );
            serde_json::json!({
                "file": review.file.to_string_lossy(),
                "result": if review.rejected { "reject" } else { "accept" },
                "accepted": (!review.rejected).then(|| review.accepted(vcs, gateway.as_deref())),
            })
        }),
        previous_review: config.previous_review.as_ref().map(|review| {
            serde_json::json!({
                "file": review.file.to_string_lossy(),
                "data": review.as_written,
            })
        }),
        dev_container,
        setup: system.setup_record().clone(),
        loaded_from: system.loaded_from().clone(),
    })
}

/// The universal configuration as compact JSON, ending with a newline.
/// (Compact, to stay well within `MAX_UNIVERSAL_FILE_BYTES`; a tool such as
/// `jq` lays it out for reading.)
pub fn to_json(universal: &Universal) -> String {
    let mut text = serde_json::to_string(universal).expect("only JSON-ready values are stored");
    text.push('\n');
    text
}

fn path_text(path: &Path) -> String {
    path.to_string_lossy().into_owned()
}

/// Convert a YAML value read from `file` (at `key`), recording what JSON
/// can't hold: numbers that aren't finite, and keys that aren't single values.
pub(crate) fn yaml_to_json(
    value: &Yaml,
    file: &Path,
    key: &str,
    errors: &mut Vec<ConfigError>,
) -> Json {
    let mut error = |message: &str| errors.push(ConfigError::new(Some(file), key, message));
    match value {
        Yaml::Null => Json::Null,
        Yaml::Bool(b) => Json::Bool(*b),
        Yaml::Number(n) => {
            if let Some(i) = n.as_i64() {
                Json::from(i)
            } else if let Some(u) = n.as_u64() {
                Json::from(u)
            } else {
                match n.as_f64().and_then(Number::from_f64) {
                    Some(number) => Json::Number(number),
                    None => {
                        error("JSON can't hold this number");
                        Json::Null
                    }
                }
            }
        }
        Yaml::String(s) => Json::String(s.clone()),
        Yaml::Sequence(items) => Json::Array(
            items
                .iter()
                .enumerate()
                .map(|(i, item)| yaml_to_json(item, file, &index_key(key, i), errors))
                .collect(),
        ),
        Yaml::Mapping(map) => {
            let mut object = Map::new();
            for (k, v) in map {
                let name = match k {
                    Yaml::String(s) => s.clone(),
                    Yaml::Number(n) => n.to_string(),
                    Yaml::Bool(b) => b.to_string(),
                    _ => {
                        errors.push(ConfigError::new(
                            Some(file),
                            key,
                            "keys must be single values",
                        ));
                        continue;
                    }
                };
                let converted = yaml_to_json(v, file, &child_key(key, &name), errors);
                object.insert(name, converted);
            }
            Json::Object(object)
        }
        // Tags are refused when the files are read.
        Yaml::Tagged(_) => {
            error("YAML tags aren't supported");
            Json::Null
        }
    }
}

/// For tests that need a `host-brokered` list without accepted settings:
/// the universal configuration, with `host-brokered` made from the
/// project's commands as they'd be accepted (each program found on the
/// `PATH`, each environment its whole plan).
#[cfg(test)]
pub(crate) fn build_brokering_project(config: &FullConfig) -> Result<Universal, ConfigErrors> {
    let mut universal = build(config)?;
    let Some(project) = &config.project else {
        return Ok(universal);
    };
    let mut errors = Vec::new();
    universal.host_brokered = project
        .commands
        .iter()
        .zip(&project.raw_commands)
        .enumerate()
        .map(|(i, (rule, raw))| {
            let key = index_key("commands", i);
            let mut entry = match yaml_to_json(raw, &project.path, &key, &mut errors) {
                Json::Object(entry) => entry,
                _ => Map::new(),
            };
            let program = rule.program.as_deref().unwrap_or(&rule.command);
            let found = config
                .system
                .find_program(program)
                .map(|p| Json::String(path_text(&p)));
            entry.insert("program".to_string(), found.unwrap_or(Json::Null));
            let env = serde_json::to_value(&rule.env_plan).expect("only text is stored");
            entry.insert("env".to_string(), env);
            Json::Object(entry)
        })
        .collect();
    if errors.is_empty() {
        Ok(universal)
    } else {
        Err(ConfigErrors(errors))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::config::system::Env;
    use crate::config::testing::TempDir;
    use crate::config::{ConfigErrors, LoadOptions, load_all_with_env};
    use std::path::PathBuf;

    const DEV_ENV: &str = "user: ubuntu\nmounts:\n  workspace: /workspace\n  setup: /setup\nbroker-bin: /usr/local/bin/scoter-container\nsshd-port: 22022\nextra:\n  ratio: 0.5\n  1: one\n";

    #[cfg(unix)]
    fn tool(dir: &TempDir, name: &str) -> PathBuf {
        use std::os::unix::fs::PermissionsExt;
        let path = dir.write(name, "#!/bin/sh\n");
        std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o755)).unwrap();
        path
    }

    impl UniversalSection {
        fn with_programs(self, programs: &[(&str, Option<String>)]) -> Self {
            UniversalSection {
                programs: programs
                    .iter()
                    .map(|(k, v)| (k.to_string(), v.clone()))
                    .collect(),
                ..self
            }
        }
    }

    fn load(dir: &TempDir, project: Option<&str>, setup: &str) -> FullConfig {
        let options = LoadOptions {
            project_dir: project.map(|p| {
                dir.write("proj/.surf-scoter.d/project.yaml", p);
                dir.path().join("proj")
            }),
            ..LoadOptions::default()
        };
        dir.write("home/setup.yaml", setup);
        let env = Env {
            scoter_home: Some(dir.path().join("home")),
            path: Some(dir.path().join("bin").into_os_string()),
            ..Env::default()
        };
        load_all_with_env(&options, env).unwrap()
    }

    #[cfg(unix)]
    #[test]
    fn full_configuration() {
        let dir = TempDir::new();
        let podman = tool(&dir, "bin/podman");
        let aws = tool(&dir, "bin/aws");
        dir.write("proj/.surf-scoter.d/env/Dockerfile", "FROM scratch\n");
        dir.write("proj/.surf-scoter.d/env/dev-env.yaml", DEV_ENV);
        let config = load(
            &dir,
            Some(concat!(
                "container:\n  dev-container-path: ./env\n",
                "vcs: git\n",
                "env:\n  allow: [LANG]\n  error: ['SECRET_*']\n  set: {CONF: '${ssh:config-file}', HOME: '@HOME@'}\n",
                "commands:\n",
                "  - command: aws\n    args: [s3, ls, {any: {prefix: 's3://b/'}}]\n",
                "    env:\n      allow: ['AWS_*']\n      drop: [AWS_SESSION_TOKEN]\n",
                "      default: {AWS_REGION: us-east-1}\n",
                "  - {command: deploy, program: not-installed-deploy, args: []}\n",
            )),
            concat!(
                "docker:\n  programs:\n    exec: podman\n  runs:\n    build: ${exec} build -t {x:tag} '{x:dir}/a b'\n",
                "hoodoo:\n  programs:\n    exec: podman\n    helper: my-hoodoo\n  runs:\n    go: ${exec}\n    help: ${helper} --help\n",
                "ssh:\n  paths:\n    config-file: ./ssh/config\n",
                "  values:\n    port: 22022\n    ratio: 0.5\n    debug: false\n    unset: null\n    note: '${config-file} {x:keep}'\n",
                "scoter:\n  paths:\n    distribution-dir: /opt/scoter\n    note: ${ssh:config-file}\n",
                "  path-lists:\n    compose: [./compose, /etc/compose]\n",
                "  values:\n    project-files-dir: .surf-scoter.d\n    project-file-name: project.yaml\n",
            ),
        );
        let universal = build_brokering_project(&config).unwrap();
        let p = |path: &PathBuf| path.display().to_string();
        assert_eq!(
            universal.active_config_locations.project_config,
            Some(p(&dir.path().join("proj/.surf-scoter.d/project.yaml")))
        );
        assert_eq!(universal.setup.files.len(), 1);
        assert_eq!(
            universal.setup.files[0].file,
            p(&dir.path().join("home/setup.yaml"))
        );
        // No `./`: the path is canonical.
        assert_eq!(
            universal.active_config_locations.dev_container_path,
            Some(p(&dir.path().join("proj/.surf-scoter.d/env")))
        );
        let strings =
            |items: &[&str]| -> Vec<String> { items.iter().map(|s| s.to_string()).collect() };
        let section = |paths: &[(&str, Option<String>)], runs: &[(&str, Option<Vec<String>>)]| {
            UniversalSection {
                paths: paths
                    .iter()
                    .map(|(k, v)| (k.to_string(), v.clone()))
                    .collect(),
                programs: BTreeMap::new(),
                runs: runs
                    .iter()
                    .map(|(k, v)| (k.to_string(), v.clone()))
                    .collect(),
                path_lists: BTreeMap::new(),
                value_lists: BTreeMap::new(),
                values: BTreeMap::new(),
                project_files: BTreeMap::new(),
                egress_hosts: BTreeMap::new(),
            }
        };
        let ssh_config = p(&dir.path().join("proj/ssh/config"));
        let podman_text = p(&podman);
        assert_eq!(
            universal.resolved,
            BTreeMap::from([
                (
                    "docker".to_string(),
                    section(
                        &[],
                        // Quotes are removed, and the path with a space is one argument.
                        &[(
                            "build",
                            Some(strings(&[
                                &podman_text,
                                "build",
                                "-t",
                                "{x:tag}",
                                "{x:dir}/a b"
                            ]))
                        )],
                    )
                    .with_programs(&[("exec", Some(podman_text.clone()))]),
                ),
                // `helper` isn't there: it, and the command using it, are null.
                (
                    "hoodoo".to_string(),
                    section(
                        &[],
                        &[("go", Some(strings(&[&podman_text]))), ("help", None),],
                    )
                    .with_programs(&[("exec", Some(podman_text.clone())), ("helper", None)]),
                ),
                (
                    "scoter".to_string(),
                    UniversalSection {
                        path_lists: BTreeMap::from([(
                            "compose".to_string(),
                            // Kept whether or not they're there.
                            Some(vec![
                                p(&dir.path().join("proj/compose")),
                                "/etc/compose".to_string()
                            ]),
                        )]),
                        values: BTreeMap::from([
                            (
                                "project-file-name".to_string(),
                                ScalarValue::String("project.yaml".to_string()),
                            ),
                            (
                                "project-files-dir".to_string(),
                                ScalarValue::String(".surf-scoter.d".to_string()),
                            ),
                            (
                                "project-id".to_string(),
                                ScalarValue::String(crate::config::system::environment_id(
                                    &dir.path().join("proj"),
                                )),
                            ),
                        ]),
                        ..section(
                            &[
                                (
                                    "dev-container-path",
                                    Some(p(&dir.path().join("proj/.surf-scoter.d/env")))
                                ),
                                ("distribution-bin-dir", Some("/opt/scoter/bin".to_string())),
                                ("distribution-dir", Some("/opt/scoter".to_string())),
                                ("note", Some(ssh_config.clone())),
                                (
                                    "project-config",
                                    Some(p(&dir.path().join("proj/.surf-scoter.d/project.yaml")))
                                ),
                                ("project-dir", Some(p(&dir.path().join("proj")))),
                                ("user-dir", Some(p(&dir.path().join("home")))),
                                (
                                    "user-setup-file",
                                    Some(p(&dir.path().join("home/setup.yaml")))
                                ),
                            ],
                            &[],
                        )
                    },
                ),
                (
                    "ssh".to_string(),
                    UniversalSection {
                        values: BTreeMap::from([
                            ("debug".to_string(), ScalarValue::Bool(false)),
                            // Only text is expanded; `{x:...}` is kept as text.
                            (
                                "note".to_string(),
                                ScalarValue::String(format!("{ssh_config} {{x:keep}}"))
                            ),
                            ("port".to_string(), ScalarValue::Int(22022)),
                            ("ratio".to_string(), ScalarValue::Float(0.5)),
                            ("unset".to_string(), ScalarValue::Null),
                        ]),
                        ..section(&[("config-file", Some(ssh_config.clone()))], &[])
                    },
                ),
            ]),
        );

        let json: Json = serde_json::from_str(&to_json(&universal)).unwrap();
        // The locations are also the `scoter` section's paths; the project's
        // id and branch, its values (text, to build paths from).
        let active = json["active-config-locations"].as_object().unwrap();
        assert_eq!(active.len(), 14);
        for (key, value) in active {
            let group = if key == "project-id" || key == "project-branch" {
                "values"
            } else {
                "paths"
            };
            let path = &json["resolved"]["scoter"][group][key];
            if value.is_null() {
                assert!(path.is_null(), "{key}");
            } else {
                assert_eq!(path, value, "{key}");
            }
        }
        // Programs are their own group, found on the `PATH`, and recorded as written.
        assert_eq!(json["resolved"]["docker"]["programs"]["exec"], podman_text);
        assert_eq!(
            json["resolved"]["docker"]["paths"],
            Json::Object(Map::new())
        );
        assert_eq!(
            json["setup"]["files"][0]["sections"]["docker"]["programs"]["exec"],
            "podman"
        );
        // Values keep their JSON types.
        let values = &json["resolved"]["ssh"]["values"];
        assert_eq!(values["port"], 22022);
        assert_eq!(values["ratio"], 0.5);
        assert_eq!(values["debug"], false);
        assert_eq!(values["unset"], Json::Null);
        assert!(values.as_object().unwrap().contains_key("unset"));
        assert_eq!(
            json["resolved"]["scoter"]["path-lists"]["compose"][1],
            "/etc/compose"
        );
        // The history of the inputs is its own block.
        assert_eq!(
            json["loaded-from"]["project-config"],
            serde_json::json!([p(&dir.path().join("proj/.surf-scoter.d/project.yaml"))])
        );
        assert_eq!(
            json["setup"].as_object().unwrap().len(),
            1,
            "{}",
            json["setup"]
        );
        // The project file, as written, `vcs` included.
        assert_eq!(json["current-project-setup"]["vcs"], "git");
        // Each `env` block resolved on its own: `${...}` expanded, `@NAME@` left.
        let resolved = &json["current-project-resolved"];
        assert_eq!(resolved["env"]["set"]["CONF"], ssh_config);
        assert_eq!(resolved["env"]["set"]["HOME"], "@HOME@");
        assert_eq!(resolved["commands"][0]["env"]["allow"][0], "AWS_*");
        assert_eq!(
            resolved["commands"][0]["env"]["default"]["AWS_REGION"],
            "us-east-1"
        );
        assert_eq!(resolved["commands"][1]["env"]["allow"], Json::Array(vec![]));
        assert_eq!(
            json["current-project-setup"]["container"]["dev-container-path"],
            "./env"
        );
        assert_eq!(
            json["current-project-setup"]["commands"][1]["program"],
            "not-installed-deploy"
        );
        assert_eq!(json["host-brokered"][0]["command"], "aws");
        assert_eq!(json["host-brokered"][0]["program"], p(&aws));
        assert_eq!(
            json["host-brokered"][0]["args"][2]["any"]["prefix"],
            "s3://b/"
        );
        assert_eq!(json["host-brokered"][1]["program"], Json::Null);
        // Each command's whole environment, expanded: the project's `env`
        // with the command's added.
        assert_eq!(
            json["host-brokered"][0]["env"],
            serde_json::json!({
                "allow": ["LANG", "AWS_*"],
                "drop": ["AWS_SESSION_TOKEN"],
                "error": ["SECRET_*"],
                // `@NAME@` is for the program running the commands.
                "set": {"CONF": ssh_config, "HOME": "@HOME@"},
                "default": {"AWS_REGION": "us-east-1"},
            })
        );
        assert_eq!(
            json["host-brokered"][1]["env"],
            serde_json::json!({
                "allow": ["LANG"],
                "drop": [],
                "error": ["SECRET_*"],
                "set": {"CONF": ssh_config, "HOME": "@HOME@"},
                "default": {},
            })
        );
        // The project file keeps them as written.
        assert_eq!(
            json["current-project-setup"]["env"]["set"]["CONF"],
            "${ssh:config-file}"
        );
        assert_eq!(
            json["dev-container"]["broker-bin"],
            "/usr/local/bin/scoter-container"
        );
        assert_eq!(json["dev-container"]["sshd-port"], 22022);
        assert_eq!(json["dev-container"]["extra"]["ratio"], 0.5);
        assert_eq!(json["dev-container"]["extra"]["1"], "one");
        assert!(to_json(&universal).ends_with("}\n"));
    }

    #[test]
    fn without_project() {
        let dir = TempDir::new();
        let config = load(&dir, None, "ssh:\n  paths:\n    home: /h/.ssh\n");
        let json: Json =
            serde_json::from_str(&to_json(&build_brokering_project(&config).unwrap())).unwrap();
        let active = &json["active-config-locations"];
        assert_eq!(active["project-config"], Json::Null);
        assert!(json.get("project-config").is_none());
        // Kept, though it isn't there.
        assert_eq!(json["resolved"]["ssh"]["paths"]["home"], "/h/.ssh");
        for group in ["programs", "runs", "path-lists", "value-lists", "values"] {
            assert_eq!(json["resolved"]["ssh"][group], Json::Object(Map::new()));
        }
        // Only where the user's setup file was found.
        let home = |p: &str| dir.path().join(p).display().to_string();
        assert_eq!(
            json["resolved"]["scoter"]["paths"],
            serde_json::json!({
                "user-dir": home("home"),
                "user-setup-file": home("home/setup.yaml"),
            })
        );
        assert_eq!(active["dev-container-path"], Json::Null);
        assert_eq!(json["dev-container"], Json::Null);
        assert_eq!(json["host-brokered"], Json::Array(vec![]));
        assert!(json.get("lookup-paths").is_none());
    }

    #[test]
    fn yaml_conversion_errors() {
        let mut errors = Vec::new();
        let value: Yaml =
            yaml_serde::from_str("a: .nan\nb: [1, .inf]\n? [x]\n: y\nok: 1.5\n").unwrap();
        let json = yaml_to_json(&value, Path::new("/f.yaml"), "", &mut errors);
        let messages: Vec<String> = errors.iter().map(ToString::to_string).collect();
        assert_eq!(
            messages,
            [
                "/f.yaml: a: JSON can't hold this number",
                "/f.yaml: b[1]: JSON can't hold this number",
                "/f.yaml: keys must be single values",
            ]
        );
        assert_eq!(json["ok"], 1.5);
        assert_eq!(json["b"][0], 1);
    }

    // ---- host-commands checks ----

    /// Load the user's and the distribution's setup files, with `bin` as
    /// the `PATH`.
    fn load_setups(dir: &TempDir, user: &str, dist: &str) -> Result<FullConfig, Vec<String>> {
        dir.write("home/setup.yaml", user);
        dir.write("dist/setup.yaml", dist);
        let options = LoadOptions {
            distribution_dir: Some(dir.path().join("dist")),
            ..LoadOptions::default()
        };
        let env = Env {
            scoter_home: Some(dir.path().join("home")),
            path: Some(dir.path().join("bin").into_os_string()),
            ..Env::default()
        };
        load_all_with_env(&options, env).map_err(error_texts)
    }

    fn error_texts(errors: ConfigErrors) -> Vec<String> {
        errors
            .0
            .into_iter()
            .map(|e| format!("{}: {}", e.key.unwrap_or_default(), e.message))
            .collect()
    }

    fn combined(dir: &TempDir, user: &str, dist: &str) -> Result<Combined, Vec<String>> {
        let config = load_setups(dir, user, dist)?;
        build_brokering_project(&config)
            .map(|u| u.host_command_checks)
            .map_err(error_texts)
    }

    fn ids(items: &[&str]) -> Vec<String> {
        items.iter().map(|s| s.to_string()).collect()
    }

    #[cfg(unix)]
    #[test]
    fn host_command_checks_combined() {
        use crate::config::host_commands::{Action, MappedCommand, Verdict};
        let dir = TempDir::new();
        let sh = tool(&dir, "bin/sh");
        let podman = tool(&dir, "bin/podman");
        let python = tool(&dir, "bin/python3.12");
        let mytool = dir.path().join("bin/mytool");
        std::os::unix::fs::symlink(&sh, &mytool).unwrap();
        let checks = combined(
            &dir,
            concat!(
                "docker:\n  programs:\n    exec: podman\n",
                "  host-commands:\n    docker:\n      action: warn\n      reason: mounts\n",
                "      command-map:\n        docker: ${exec}\n",
                "mine:\n  host-commands:\n    tools:\n      action: allow\n      reason: mine\n",
                "      command-map: {mytool: mytool, py: python3.12}\n",
            ),
            concat!(
                "verification:\n  host-commands:\n",
                "    shells: {action: refuse, reason: shell, commands: [sh, bash]}\n",
                "    interpreters: {action: refuse, reason: interp, name-patterns: ['python*']}\n",
                "    code-options: {action: warn, reason: opts, options: [-c]}\n",
                "    shims: {action: refuse, reason: shim, paths: ['${scoter:distribution-dir}/shims']}\n",
            ),
        )
        .unwrap();
        let p = |path: &PathBuf| Some(path.display().to_string());
        let mapped = |program: Option<String>, checks: &[&str]| MappedCommand {
            program,
            checks: ids(checks),
        };
        assert_eq!(
            checks.command_map,
            BTreeMap::from([
                ("bash".to_string(), mapped(None, &["verification:shells"])),
                ("docker".to_string(), mapped(p(&podman), &["docker:docker"])),
                ("mytool".to_string(), mapped(p(&mytool), &["mine:tools"])),
                ("py".to_string(), mapped(p(&python), &["mine:tools"])),
                ("sh".to_string(), mapped(p(&sh), &["verification:shells"])),
            ])
        );
        let verdict = |action, checks: &[&str]| Verdict {
            action,
            checks: ids(checks),
            overridden: Vec::new(),
        };
        assert_eq!(
            checks.commands,
            BTreeMap::from([
                (
                    "bash".to_string(),
                    verdict(Action::Refuse, &["verification:shells"])
                ),
                (
                    "docker".to_string(),
                    verdict(Action::Warn, &["docker:docker"])
                ),
                // The same file as `sh`.
                (
                    "mytool".to_string(),
                    verdict(Action::Refuse, &["mine:tools", "verification:shells"])
                ),
                // By the base name of its executable.
                (
                    "py".to_string(),
                    verdict(Action::Refuse, &["mine:tools", "verification:interpreters"])
                ),
                // And the other way around.
                (
                    "sh".to_string(),
                    verdict(Action::Refuse, &["mine:tools", "verification:shells"])
                ),
            ])
        );
        let shims = &checks.checks["verification:shims"];
        assert_eq!(
            shims.paths,
            [dir.path().join("dist/shims").display().to_string()]
        );
        assert_eq!(
            shims.files,
            [dir.path().join("dist/setup.yaml").display().to_string()]
        );
        assert_eq!(checks.checks["verification:code-options"].options, ["-c"]);
        assert!(checks.notices.is_empty(), "{:?}", checks.notices);
    }

    fn messages(notices: &[host_commands::Notice]) -> Vec<String> {
        notices.iter().map(|n| n.message.clone()).collect()
    }

    #[cfg(unix)]
    #[test]
    fn host_command_overrides() {
        use crate::config::host_commands::{Action, NoticeValue, Overridden};
        let dir = TempDir::new();
        tool(&dir, "bin/sh");
        tool(&dir, "bin/ksh");
        let checks = combined(
            &dir,
            concat!(
                "verification:\n  host-commands:\n    ksh-ok:\n      action: allow\n      reason: mine\n",
                "      commands: [ksh, zsh]\n      overrides: ['verification:shells', 'verification:gone']\n",
            ),
            concat!(
                "verification:\n  host-commands:\n",
                "    shells: {action: refuse, reason: shell, commands: [sh, ksh]}\n",
                "    korn: {action: warn, reason: korn, commands: [ksh]}\n",
            ),
        )
        .unwrap();
        // First the most restrictive of the matching checks (refuse); then
        // the override takes `shells`' place, and `korn` still applies.
        let ksh = &checks.commands["ksh"];
        assert_eq!(ksh.action, Action::Warn);
        assert_eq!(
            ksh.checks,
            ids(&["verification:korn", "verification:ksh-ok"])
        );
        assert_eq!(
            ksh.overridden,
            [Overridden {
                check: "verification:shells".to_string(),
                by: "verification:ksh-ok".to_string()
            }]
        );
        assert_eq!(checks.commands["sh"].action, Action::Refuse);
        assert!(checks.commands["sh"].overridden.is_empty());
        // Nothing to override: an override alone doesn't decide a command.
        assert!(!checks.commands.contains_key("zsh"));
        assert_eq!(
            messages(&checks.notices),
            [
                "`verification:ksh-ok` overrides `verification:shells` for the command `zsh`, which `verification:shells` doesn't match; the override does nothing for it",
                "`verification:ksh-ok` overrides `verification:gone`, which isn't set; the override does nothing",
            ]
        );
        let user = dir.path().join("home/setup.yaml").display().to_string();
        let dist = dir.path().join("dist/setup.yaml").display().to_string();
        let notice = &checks.notices[0];
        assert_eq!(notice.id, "override-not-matched");
        assert_eq!(notice.sources, [user.clone(), dist]);
        assert_eq!(
            notice.parameters["name"],
            NoticeValue::Text("zsh".to_string())
        );
        assert_eq!(checks.notices[1].id, "override-target-unset");
        assert_eq!(checks.notices[1].sources, [user]);
    }

    #[cfg(unix)]
    #[test]
    fn host_command_override_errors() {
        let dir = TempDir::new();
        tool(&dir, "bin/ksh");
        tool(&dir, "bin/git");
        let errors = combined(
            &dir,
            concat!(
                "a:\n  host-commands:\n",
                "    one: {action: warn, reason: r, commands: [ksh], overrides: ['h:shells']}\n",
                "    two: {action: allow, reason: r, commands: [ksh], overrides: ['h:shells']}\n",
                "    three: {action: allow, reason: r, commands: [ksh], overrides: ['a:one']}\n",
                "    c: {action: allow, reason: r, commands: [git], options: [-c], overrides: ['h:git']}\n",
                "    e: {action: allow, reason: r, commands: [git], options: [-e], overrides: ['h:git']}\n",
                "    all: {action: allow, reason: r, commands: [git], overrides: ['h:git']}\n",
            ),
            concat!(
                "h:\n  host-commands:\n",
                "    shells: {action: refuse, reason: r, commands: [ksh]}\n",
                "    git: {action: warn, reason: r, commands: [git], options: [-c, -e]}\n",
            ),
        )
        .unwrap_err();
        assert_eq!(
            errors,
            [
                // `a:all` covers every option; `a:c` and `a:e` cover different ones.
                "a.host-commands.c: overrides `h:git` for `git`, as `a:all` does; only one override can say what a check becomes for a command",
                "a.host-commands.e: overrides `h:git` for `git`, as `a:all` does; only one override can say what a check becomes for a command",
                "a.host-commands.three: overrides `a:one`, which is an override itself; an override can only replace checks without `overrides`",
                "a.host-commands.two: overrides `h:shells` for `ksh`, as `a:one` does; only one override can say what a check becomes for a command",
            ]
        );
    }

    #[cfg(unix)]
    #[test]
    fn host_command_errors() {
        let dir = TempDir::new();
        let sh = tool(&dir, "bin/sh");
        let podman = tool(&dir, "bin/podman");
        let errors = combined(
            &dir,
            concat!(
                "verification:\n  host-commands:\n    shells: {action: refuse, reason: r, commands: [sh]}\n",
                "a:\n  host-commands:\n    one: {action: warn, reason: r, command-map: {docker: podman}}\n",
                "c:\n  host-commands:\n    x: {action: warn, reason: r, commands: [sh], overrides: ['c:x']}\n",
            ),
            concat!(
                "b:\n  host-commands:\n    two: {action: warn, reason: r, command-map: {docker: sh}}\n",
                "    relax: {action: allow, reason: r, commands: [sh], overrides: ['verification:shells']}\n",
            ),
        )
        .unwrap_err();
        assert_eq!(
            errors,
            [
                format!(
                    "b.host-commands.two: maps `docker` to {}, but `a:one` maps it to {}; a command runs one program, so change or override one of them",
                    sh.display(),
                    podman.display()
                ),
                format!(
                    "b.host-commands.relax: overrides `verification:shells`, from a higher priority setup file ({}); a check can only override checks from its own file or a lower priority one",
                    dir.path().join("home/setup.yaml").display()
                ),
                "c.host-commands.x: can't override itself".to_string(),
            ]
        );
    }

    #[cfg(unix)]
    #[test]
    fn host_program_in_project() {
        let dir = TempDir::new();
        let tool_path = tool(&dir, "proj/tool");
        dir.write("proj/.surf-scoter.d/env/Dockerfile", "FROM scratch\n");
        dir.write("proj/.surf-scoter.d/env/dev-env.yaml", DEV_ENV);
        let config = load(
            &dir,
            Some("container:\n  dev-container-path: ./env\nvcs: git\n"),
            concat!(
                "p:\n  host-commands:\n    c: {action: warn, reason: r, command-map: {tool: ./tool}}\n",
                "scoter:\n  values:\n    project-files-dir: .surf-scoter.d\n    project-file-name: project.yaml\n",
            ),
        );
        assert_eq!(
            error_texts(build_brokering_project(&config).unwrap_err()),
            [format!(
                "p.host-commands.c: `tool` runs {}, which is in the project (`scoter:project-dir`); a host command never runs a program from the project",
                tool_path.display()
            )]
        );
    }

    #[test]
    fn host_command_replacements() {
        let dir = TempDir::new();
        let checks = combined(
            &dir,
            concat!(
                "verification:\n  host-commands:\n",
                "    shells: {action: warn, reason: mine, commands: [sh]}\n",
                "    runners: null\n",
                "    same: {action: refuse, reason: mine, commands: [x, y]}\n",
            ),
            concat!(
                "verification:\n  host-commands:\n",
                "    shells: {action: refuse, reason: shell, commands: [sh, ksh, nu]}\n",
                "    runners: {action: refuse, reason: run, commands: [env]}\n",
                "    same: {action: warn, reason: theirs, commands: [x]}\n",
            ),
        )
        .unwrap();
        let user = dir.path().join("home/setup.yaml").display().to_string();
        let dist = dir.path().join("dist/setup.yaml").display().to_string();
        assert_eq!(
            messages(&checks.notices),
            [
                format!(
                    "{user} removes `verification:runners`, so {dist}'s check no longer applies, nor will what it adds later"
                ),
                format!(
                    "{user} replaces `verification:shells` from {dist} without its commands `ksh`, `nu`; what {dist} adds to it later won't apply (`overrides` relaxes a check without replacing it)"
                ),
                format!(
                    "{user} replaces `verification:shells` from {dist} with `warn` in place of `refuse`"
                ),
            ]
        );
        let ids_of: Vec<&str> = checks.notices.iter().map(|n| n.id.as_str()).collect();
        assert_eq!(
            ids_of,
            [
                "check-removed",
                "check-replaced-without",
                "check-replaced-less-restrictive"
            ]
        );
        assert_eq!(
            checks.notices[1].parameters["entries"],
            host_commands::NoticeValue::List(ids(&["ksh", "nu"]))
        );
        assert!(
            checks
                .notices
                .iter()
                .all(|n| n.sources == [user.clone(), dist.clone()])
        );
        // The check in effect is the user's; the distribution's is hidden.
        assert_eq!(
            checks.checks["verification:shells"].files,
            [user.clone(), dist.clone()]
        );
        assert!(!checks.checks.contains_key("verification:runners"));
        assert_eq!(checks.checks["verification:same"].reason, "mine");
    }

    #[cfg(unix)]
    #[test]
    fn host_checks_survive_universal() {
        let dir = TempDir::new();
        tool(&dir, "bin/sh");
        let config = load_setups(
            &dir,
            "a:\n  host-commands:\n    c: {action: warn, reason: r, commands: [sh], overrides: ['b:d']}\n",
            "b:\n  host-commands:\n    d: {action: refuse, reason: r, commands: [sh]}\n    e: null\n",
        )
        .unwrap();
        let first = build_brokering_project(&config).unwrap();
        let file = dir.write("universal.json", &to_json(&first));
        let env = Env {
            path: Some(dir.path().join("bin").into_os_string()),
            ..Env::default()
        };
        let again = crate::config::load_universal_with_env(&file, env).unwrap();
        assert_eq!(
            build_brokering_project(&again).unwrap().host_command_checks,
            first.host_command_checks
        );
        assert_eq!(first.setup, again.system.setup_record().clone());
    }

    #[test]
    fn dev_container_project_dir() {
        let dir = TempDir::new();
        dir.write("proj/.surf-scoter.d/env/Dockerfile", "FROM scratch\n");
        dir.write("proj/.surf-scoter.d/env/dev-env.yaml", DEV_ENV);
        dir.write(
            "home/setup.yaml",
            "scoter:\n  values:\n    project-files-dir: .surf-scoter.d\n    project-file-name: project.yaml\n",
        );
        let workspace = dir.path().join("ws");
        let p = |path: &PathBuf| path.display().to_string();
        let generate = |project: &str, workspace: Option<&Path>| {
            dir.write("proj/.surf-scoter.d/project.yaml", project);
            let options = LoadOptions {
                project_dir: Some(dir.path().join("proj")),
                dev_container_workspace_dir: workspace.map(Path::to_path_buf),
                ..LoadOptions::default()
            };
            let env = Env {
                scoter_home: Some(dir.path().join("home")),
                ..Env::default()
            };
            build(&load_all_with_env(&options, env).unwrap()).unwrap()
        };
        let with_subdir = "container:\n  dev-container-path: ./env\n  workspace-subdir: src/p\n";

        let universal = generate(with_subdir, Some(&workspace));
        let expected = p(&workspace.join("src/p"));
        assert_eq!(
            universal.active_config_locations.dev_container_project_dir,
            Some(expected.clone())
        );
        let json: Json = serde_json::from_str(&to_json(&universal)).unwrap();
        assert_eq!(
            json["resolved"]["scoter"]["paths"]["dev-container-project-dir"],
            expected
        );
        assert_eq!(
            json["current-project-setup"]["container"]["workspace-subdir"],
            "src/p"
        );
        // `scoter-host` works it out again from the file.
        let file = dir.write("universal.json", &to_json(&universal));
        let again = crate::config::load_universal_with_env(&file, Env::default()).unwrap();
        assert_eq!(
            again
                .system
                .get(system::SCOTER_SECTION, system::DEV_CONTAINER_PROJECT_DIR),
            Some(expected)
        );

        // Without a subdir, the workspace itself; without a workspace, none.
        let without = "container:\n  dev-container-path: ./env\n";
        assert_eq!(
            generate(without, Some(&workspace))
                .active_config_locations
                .dev_container_project_dir,
            Some(p(&workspace))
        );
        assert_eq!(
            generate(with_subdir, None)
                .active_config_locations
                .dev_container_project_dir,
            None
        );
    }

    #[cfg(unix)]
    #[test]
    fn no_accepted_no_host_brokered() {
        let dir = TempDir::new();
        tool(&dir, "bin/aws");
        dir.write("proj/.surf-scoter.d/env/Dockerfile", "FROM scratch\n");
        dir.write("proj/.surf-scoter.d/env/dev-env.yaml", DEV_ENV);
        let config = load(
            &dir,
            Some(
                "container:\n  dev-container-path: ./env\ncommands:\n  - {command: aws, args: [s3, ls]}\n",
            ),
            "scoter:\n  values:\n    project-files-dir: .surf-scoter.d\n    project-file-name: project.yaml\n",
        );
        let universal = build(&config).unwrap();
        assert!(universal.host_brokered.is_empty());
        // The project is still there, as a record.
        assert_eq!(
            universal.current_project_setup.as_ref().unwrap()["commands"][0]["command"],
            "aws"
        );
    }

    #[test]
    fn setup_refuses_egress_wildcard() {
        let dir = TempDir::new();
        dir.write("proj/.surf-scoter.d/env/Dockerfile", "FROM scratch\n");
        dir.write("proj/.surf-scoter.d/env/dev-env.yaml", DEV_ENV);
        dir.write(
            "proj/.surf-scoter.d/project.yaml",
            "container:\n  dev-container-path: ./env\negress:\n  - {host: '*.github.io', protocol: tls}\n  - {host: '*.example.com', protocol: tls}\n",
        );
        dir.write(
            "home/setup.yaml",
            "scoter:\n  values:\n    project-files-dir: .surf-scoter.d\n    project-file-name: project.yaml\nverification:\n  value-lists:\n    egress-refused-wildcards: [github.io, co.uk]\n",
        );
        let options = LoadOptions {
            project_dir: Some(dir.path().join("proj")),
            ..LoadOptions::default()
        };
        let env = Env {
            scoter_home: Some(dir.path().join("home")),
            ..Env::default()
        };
        let errors = error_texts(load_all_with_env(&options, env.clone()).unwrap_err());
        assert_eq!(
            errors,
            [
                "egress[0].host: '*.github.io' would allow every name under `github.io`, where anyone can have one (your setup's `verification: value-lists: egress-refused-wildcards`)"
            ]
        );

        // The Public Suffix List files the setup names: a wildcard on a public
        // suffix is refused, but not on an exception to a wildcard rule.
        dir.write("psl.dat", "// A sample.\nexample.com\n*.ck\n!www.ck\n");
        dir.write(
            "proj/.surf-scoter.d/project.yaml",
            "container:\n  dev-container-path: ./env\negress:\n  - {host: '*.example.com', protocol: tls}\n  - {host: '*.a.ck', protocol: tls}\n  - {host: '*.www.ck', protocol: tls}\n",
        );
        let with_list = |list: &str| {
            dir.write(
                "home/setup.yaml",
                &format!(
                    "scoter:\n  values:\n    project-files-dir: .surf-scoter.d\n    project-file-name: project.yaml\nverification:\n  path-lists:\n    public-suffix-lists: ['{list}']\n"
                ),
            );
            load_all_with_env(&options, env.clone())
        };
        let psl = dir.path().join("psl.dat").display().to_string();
        assert_eq!(
            error_texts(with_list(&psl).unwrap_err()),
            [
                "egress[0].host: '*.example.com' would allow every name under `example.com`, where anyone can have one (a public suffix, in your setup's `verification: path-lists: public-suffix-lists`)",
                "egress[1].host: '*.a.ck' would allow every name under `a.ck`, where anyone can have one (a public suffix, in your setup's `verification: path-lists: public-suffix-lists`)",
            ]
        );
        // A file that isn't there is an error, not a list with nothing in it.
        let errors = error_texts(with_list("/no/such/psl.dat").unwrap_err());
        assert!(
            errors[0].contains("https://publicsuffix.org/list/public_suffix_list.dat"),
            "{errors:?}"
        );

        // A wildcard typed in the review is refused the same way.
        dir.write(
            "proj/.surf-scoter.d/project.yaml",
            "container:\n  dev-container-path: ./env\n",
        );
        let review = dir.write(
            "review.yaml",
            &format!(
                "review: \"surf-scoter-review-1\"\nproject-dir: \"{}\"\nexpires-at: \"2999-01-01T00:00:00Z\"\negress:\n  - {{host: \"*.example.com\", protocol: \"tls\"}}\n",
                dir.path().join("proj").display()
            ),
        );
        let reviewed = LoadOptions {
            review: Some(review),
            ..options.clone()
        };
        dir.write(
            "home/setup.yaml",
            &format!("scoter:\n  values:\n    project-files-dir: .surf-scoter.d\n    project-file-name: project.yaml\nverification:\n  path-lists:\n    public-suffix-lists: ['{psl}']\n"),
        );
        let errors = error_texts(load_all_with_env(&reviewed, env).unwrap_err());
        assert_eq!(errors.len(), 1, "{errors:?}");
        assert!(errors[0].starts_with(
            "egress[0].host: '*.example.com' would allow every name under `example.com`"
        ));
    }

    // ---- accepted settings ----

    /// A project with commands, a verified store, and an accepted settings
    /// file in it for `program` (written private).
    #[cfg(unix)]
    fn accepted_project(
        dir: &TempDir,
        program: &str,
        expires: &str,
    ) -> (LoadOptions, Env, PathBuf) {
        use std::os::unix::fs::PermissionsExt;
        dir.write("proj/.surf-scoter.d/env/Dockerfile", "FROM scratch\n");
        dir.write("proj/.surf-scoter.d/env/dev-env.yaml", DEV_ENV);
        dir.write(
            "proj/.surf-scoter.d/project.yaml",
            "container:\n  dev-container-path: ./env\ncommands:\n  - {command: unreviewed, args: []}\n",
        );
        let verified = dir.path().join("verified");
        dir.write(
            "home/setup.yaml",
            &format!(
                "verification:\n  paths:\n    store: {}\nscoter:\n  values:\n    project-files-dir: .surf-scoter.d\n    project-file-name: project.yaml\n",
                verified.display()
            ),
        );
        let accepted = dir.write(
            "verified/env-1/acceptances/a1/accepted.json",
            &format!(
                r#"{{"$schema": "surf-scoter-accepted-settings-1", "project-dir": "{}", "expires-at": "{expires}",
                    "vcs": null, "gateway": null,
                    "egress": [{{"host": "registry.npmjs.org", "protocol": "tls", "port": 443, "methods": []}}],
                    "commands": [{{"command": "aws", "program": "{program}", "args": ["s3", "ls"],
                        "env": {{"allow": [], "drop": [], "error": [], "set": {{}}, "default": {{}}}}}}]}}"#,
                dir.path().join("proj").display()
            ),
        );
        for path in [&verified, &accepted] {
            let mode = if path.is_dir() { 0o700 } else { 0o600 };
            std::fs::set_permissions(path, std::fs::Permissions::from_mode(mode)).unwrap();
        }
        let options = LoadOptions {
            project_dir: Some(dir.path().join("proj")),
            accepted_settings: Some(accepted.clone()),
            ..LoadOptions::default()
        };
        let env = Env {
            scoter_home: Some(dir.path().join("home")),
            ..Env::default()
        };
        (options, env, accepted)
    }

    #[cfg(unix)]
    #[test]
    fn host_brokered_from_accepted() {
        let dir = TempDir::new();
        let aws = tool(&dir, "bin/aws");
        let (options, env, accepted) =
            accepted_project(&dir, &aws.display().to_string(), "2999-01-01T00:00:00Z");
        let config = load_all_with_env(&options, env).unwrap();
        let universal = build(&config).unwrap();
        assert_eq!(universal.host_brokered.len(), 1);
        assert_eq!(universal.host_brokered[0]["command"], "aws");
        assert_eq!(
            universal.host_brokered[0]["program"],
            aws.display().to_string()
        );
        let settings = universal.accepted_settings.as_ref().unwrap();
        assert_eq!(settings["file"], accepted.display().to_string());
        assert_eq!(settings["contents"]["commands"][0]["command"], "aws");
        let egress = universal.egress.as_ref().unwrap();
        assert_eq!(egress["rules"][0]["host"], "registry.npmjs.org");
        // The project's own commands are only a record.
        assert_eq!(
            universal.current_project_setup.as_ref().unwrap()["commands"][0]["command"],
            "unreviewed"
        );
        // Without accepted settings, there's neither.
        let plain = load(&dir, None, "a:\n  paths:\n    x: /x\n");
        let universal = build(&plain).unwrap();
        assert!(universal.accepted_settings.is_none() && universal.egress.is_none());
    }

    #[cfg(unix)]
    #[test]
    fn accepted_checked_against_run() {
        let dir = TempDir::new();
        let in_project = tool(&dir, "proj/tool");
        let (options, env, _) = accepted_project(
            &dir,
            &in_project.display().to_string(),
            "2999-01-01T00:00:00Z",
        );
        let errors = error_texts(load_all_with_env(&options, env.clone()).unwrap_err());
        assert_eq!(errors.len(), 1, "{errors:?}");
        assert!(
            errors[0].starts_with("commands[0].program: ")
                && errors[0].contains("is in the project"),
            "{errors:?}"
        );
        let (options, env, _) = accepted_project(&dir, "/nonexistent/aws", "2999-01-01T00:00:00Z");
        let errors = error_texts(load_all_with_env(&options, env.clone()).unwrap_err());
        assert!(
            errors[0].contains("isn't an executable file any more"),
            "{errors:?}"
        );
        // Accepted for another project.
        let aws = tool(&dir, "bin/aws");
        let (mut options, env, _) =
            accepted_project(&dir, &aws.display().to_string(), "2999-01-01T00:00:00Z");
        std::fs::create_dir_all(dir.path().join("other/.surf-scoter.d/env")).unwrap();
        dir.write("other/.surf-scoter.d/env/Dockerfile", "FROM scratch\n");
        dir.write("other/.surf-scoter.d/env/dev-env.yaml", DEV_ENV);
        dir.write(
            "other/.surf-scoter.d/project.yaml",
            "container:\n  dev-container-path: ./env\n",
        );
        options.project_dir = Some(dir.path().join("other"));
        let errors = error_texts(load_all_with_env(&options, env).unwrap_err());
        assert!(
            errors[0].starts_with("project-dir: ")
                && errors[0].contains("accepted for another project"),
            "{errors:?}"
        );
    }

    #[cfg(unix)]
    #[test]
    fn accepted_for_another_gateway() {
        let dir = TempDir::new();
        let aws = tool(&dir, "bin/aws");
        let (options, env, accepted) =
            accepted_project(&dir, &aws.display().to_string(), "2999-01-01T00:00:00Z");
        let text = std::fs::read_to_string(&accepted).unwrap();
        std::fs::write(
            &accepted,
            text.replace(r#""gateway": null"#, r#""gateway": "squid""#),
        )
        .unwrap();
        // The setup names no gateway: not the one accepted.
        let errors = error_texts(load_all_with_env(&options, env.clone()).unwrap_err());
        assert_eq!(
            errors,
            [
                "gateway: the egress rules were accepted for the `squid` gateway, but this setup's is `none`; review the project again"
            ]
        );
        // The same gateway is fine.
        let setup = std::fs::read_to_string(dir.path().join("home/setup.yaml")).unwrap();
        dir.write(
            "home/setup.yaml",
            &format!("{setup}dev-env:\n  values:\n    network-gateway-name: squid\n"),
        );
        assert!(load_all_with_env(&options, env).is_ok());
    }

    #[cfg(unix)]
    #[test]
    fn scoter_host_start_checks() {
        use crate::config::accepted::{check_for_run, parse_time};
        use std::os::unix::fs::PermissionsExt;
        let dir = TempDir::new();
        let aws = tool(&dir, "bin/aws");
        let (options, env, accepted) =
            accepted_project(&dir, &aws.display().to_string(), "2030-01-01T00:00:00Z");
        let first = build(&load_all_with_env(&options, env).unwrap()).unwrap();
        let file = dir.write("suc.json", &to_json(&first));
        let private = |path: &Path, mode: u32| {
            std::fs::set_permissions(path, std::fs::Permissions::from_mode(mode)).unwrap()
        };
        private(&file, 0o600);
        let loaded = crate::config::load_universal_with_env(&file, Env::default()).unwrap();
        let check = |now: i64| -> Vec<String> {
            check_for_run(&file, loaded.accepted.as_ref(), &loaded.system, now)
                .into_iter()
                .map(|e| format!("{}: {}", e.key.unwrap_or_default(), e.message))
                .collect()
        };
        let before = parse_time("2029-01-01T00:00:00Z").unwrap();
        assert_eq!(check(before), Vec::<String>::new());
        // Expired by the time it starts.
        let after = parse_time("2030-01-01T00:00:00Z").unwrap();
        assert_eq!(
            check(after),
            ["accepted-settings: the acceptance has expired; review the project again"]
        );
        // The universal file must be private.
        private(&file, 0o644);
        assert!(
            check(before)[0].contains("others can use it"),
            "{:?}",
            check(before)
        );
        private(&file, 0o600);
        // The store's file changed (or was revoked).
        let original = std::fs::read_to_string(&accepted).unwrap();
        std::fs::write(&accepted, original.replace("s3", "s4")).unwrap();
        assert!(
            check(before)[0].contains("no longer says what this file does"),
            "{:?}",
            check(before)
        );
        std::fs::remove_file(&accepted).unwrap();
        assert!(
            check(before)[0].starts_with("accepted-settings: "),
            "{:?}",
            check(before)
        );
        std::fs::write(&accepted, &original).unwrap();
        private(&accepted, 0o644);
        assert!(
            check(before)[0].contains("others can use it"),
            "{:?}",
            check(before)
        );
        private(&accepted, 0o600);
        assert_eq!(check(before), Vec::<String>::new());
        // The dev container went away.
        std::fs::remove_dir_all(dir.path().join("proj/.surf-scoter.d/env")).unwrap();
        assert_eq!(
            check(before),
            ["active-config-locations.dev-container-path: the dev container directory is gone"]
        );
    }

    // ---- project-files and egress-hosts checks ----

    #[test]
    fn simple_checks() {
        let dir = TempDir::new();
        dir.write(
            "dist/setup.yaml",
            concat!(
                "verification:\n",
                "  project-files:\n",
                "    secrets: {action: warn, reason: secrets, patterns: [.env, .aws/]}\n",
                "    keys: {action: refuse, reason: keys, patterns: ['*.pem']}\n",
                "  egress-hosts:\n",
                "    code-hosts: {action: warn, reason: uploads, hosts: [github.com, '*.githubusercontent.com']}\n",
            ),
        );
        let load = |user: &str| {
            dir.write("home/setup.yaml", user);
            let options = LoadOptions {
                distribution_dir: Some(dir.path().join("dist")),
                ..LoadOptions::default()
            };
            let env = Env {
                scoter_home: Some(dir.path().join("home")),
                ..Env::default()
            };
            load_all_with_env(&options, env).and_then(|config| build(&config))
        };
        // The user's file replaces one, removes another, and relaxes a third for one host.
        let universal = load(concat!(
            "verification:\n",
            "  project-files:\n",
            "    secrets: {action: refuse, reason: mine, patterns: [.env]}\n",
            "    keys: null\n",
            "  egress-hosts:\n",
            "    our-git: {action: allow, reason: ours, hosts: [github.com], overrides: ['verification:code-hosts']}\n",
        ))
        .unwrap();
        let section = &universal.resolved["verification"];
        assert_eq!(
            section.project_files.keys().collect::<Vec<_>>(),
            ["secrets"]
        );
        assert_eq!(section.project_files["secrets"].patterns, [".env"]);
        assert_eq!(
            section.egress_hosts.keys().collect::<Vec<_>>(),
            ["code-hosts", "our-git"]
        );
        let json: Json = serde_json::from_str(&to_json(&universal)).unwrap();
        assert_eq!(
            json["resolved"]["verification"]["egress-hosts"]["our-git"]["overrides"][0],
            "verification:code-hosts"
        );
        // Other sections have none, and the file says nothing of them.
        assert!(json["resolved"]["scoter"].get("project-files").is_none());
        // Overrides must name a check of the same group that's set, and not an override.
        let errors = error_texts(
            load(concat!(
                "verification:\n",
                "  egress-hosts:\n",
                "    a: {action: allow, reason: r, hosts: [x.example.com], overrides: ['verification:secrets']}\n",
                "    b: {action: allow, reason: r, hosts: [github.com], overrides: ['verification:code-hosts']}\n",
                "    c: {action: allow, reason: r, hosts: [github.com], overrides: ['verification:b']}\n",
            ))
            .unwrap_err(),
        );
        assert_eq!(
            errors,
            [
                "verification.egress-hosts.a.overrides[0]: `verification:secrets` isn't one of the `egress-hosts` checks set",
                "verification.egress-hosts.c.overrides[0]: `verification:b` is an override itself; override the check it overrides",
            ]
        );
        // A reference to one is an error.
        assert!(load("other:\n  values:\n    x: ${verification:code-hosts}\n").is_err());
    }

    // ---- the project's id and branch ----

    #[test]
    fn project_id_and_branch() {
        let dir = TempDir::new();
        dir.write("proj/.surf-scoter.d/env/Dockerfile", "FROM scratch\n");
        dir.write("proj/.surf-scoter.d/env/dev-env.yaml", DEV_ENV);
        dir.write(
            "proj/.surf-scoter.d/project.yaml",
            "container:\n  dev-container-path: ./env\n",
        );
        dir.write(
            "home/setup.yaml",
            "scoter:\n  values:\n    project-files-dir: .surf-scoter.d\n    project-file-name: project.yaml\nworkspace:\n  paths:\n    per-branch: /w/${scoter:project-id}/${scoter:project-branch}\n",
        );
        let env = || Env {
            scoter_home: Some(dir.path().join("home")),
            ..Env::default()
        };
        let options = LoadOptions {
            project_dir: Some(dir.path().join("proj")),
            project_branch: Some("feature/../x".to_string()),
            ..LoadOptions::default()
        };
        let universal = build(&load_all_with_env(&options, env()).unwrap()).unwrap();
        let id = crate::config::system::environment_id(&dir.path().join("proj"));
        let active = &universal.active_config_locations;
        assert_eq!(active.project_id.as_deref(), Some(id.as_str()));
        assert_eq!(active.project_branch.as_deref(), Some("feature-..-x"));
        assert_eq!(
            universal.resolved["workspace"].paths["per-branch"],
            Some(format!("/w/{id}/feature-..-x"))
        );
        // Carried over by a later run from the universal file.
        let first = dir.write("first.json", &to_json(&universal));
        let again = LoadOptions {
            universal: Some(first),
            no_setup_search: true,
            ..LoadOptions::default()
        };
        let carried = build(&load_all_with_env(&again, env()).unwrap()).unwrap();
        assert_eq!(
            carried.active_config_locations.project_branch.as_deref(),
            Some("feature-..-x")
        );
        assert_eq!(
            carried.active_config_locations.project_id.as_deref(),
            Some(id.as_str())
        );
        // With the same project given again too (a review's runs do); not
        // with another.
        let same = LoadOptions {
            project_dir: Some(dir.path().join("proj")),
            ..again.clone()
        };
        let carried = build(&load_all_with_env(&same, env()).unwrap()).unwrap();
        assert_eq!(
            carried.active_config_locations.project_branch.as_deref(),
            Some("feature-..-x")
        );
        dir.write("other/.surf-scoter.d/env/Dockerfile", "FROM scratch\n");
        dir.write("other/.surf-scoter.d/env/dev-env.yaml", DEV_ENV);
        dir.write(
            "other/.surf-scoter.d/project.yaml",
            "container:\n  dev-container-path: ./env\n",
        );
        let other = LoadOptions {
            project_dir: Some(dir.path().join("other")),
            ..again.clone()
        };
        let carried = build(&load_all_with_env(&other, env()).unwrap()).unwrap();
        assert_eq!(carried.active_config_locations.project_branch, None);
        // The setup can't set them.
        dir.write(
            "home/setup.yaml",
            "scoter:\n  values:\n    project-files-dir: .surf-scoter.d\n    project-file-name: project.yaml\n    project-id: mine\n",
        );
        assert!(load_all_with_env(&options, env()).is_err());
    }

    // ---- review files ----

    #[test]
    fn review_into_universal_file() {
        let dir = TempDir::new();
        dir.write("proj/.surf-scoter.d/env/Dockerfile", "FROM scratch\n");
        dir.write("proj/.surf-scoter.d/env/dev-env.yaml", DEV_ENV);
        dir.write(
            "proj/.surf-scoter.d/project.yaml",
            "container:\n  dev-container-path: ./env\nvcs: git\n",
        );
        dir.write("mine/my-env/Dockerfile", "FROM scratch\n");
        dir.write("mine/my-env/dev-env.yaml", DEV_ENV);
        dir.write(
            "home/setup.yaml",
            &format!(
                "scoter:\n  values:\n    project-files-dir: .surf-scoter.d\n    project-file-name: project.yaml\n  path-lists:\n    dev-containers-search: ['{}']\ndev-env:\n  values:\n    network-gateway-name: squid\n",
                dir.path().join("mine").display()
            ),
        );
        let project = dir.path().join("proj").display().to_string();
        let write_review = |body: &str| {
            dir.write(
                "review.yaml",
                &format!("review: \"surf-scoter-review-1\"\nproject-dir: \"{project}\"\n{body}"),
            )
        };
        let load_with = |review: &Path| {
            let options = LoadOptions {
                project_dir: Some(dir.path().join("proj")),
                review: Some(review.to_path_buf()),
                ..LoadOptions::default()
            };
            let env = Env {
                scoter_home: Some(dir.path().join("home")),
                ..Env::default()
            };
            load_all_with_env(&options, env)
        };
        let review = write_review(concat!(
            "expires-at: \"2999-01-01T00:00:00Z\"\n",
            "commands:\n  - {command: \"aws\", program: \"/usr/local/bin/aws\", args: [\"s3\", \"ls\"]}\n",
            "dev-container:\n  use: {name: \"my-env\"}\n",
        ));
        let universal = build(&load_with(&review).unwrap()).unwrap();
        let section = universal.review.unwrap();
        assert_eq!(section["result"], "accept");
        assert_eq!(section["accepted"]["vcs"], "git");
        assert_eq!(section["accepted"]["gateway"], "squid");
        assert_eq!(
            section["accepted"]["commands"][0]["program"],
            "/usr/local/bin/aws"
        );
        assert_eq!(
            section["accepted"]["dev-container"]["use"]["name"],
            "my-env"
        );
        // A review isn't accepted settings: nothing runs from it.
        assert!(universal.host_brokered.is_empty());

        let rejected = write_review("result: reject\n");
        let section = build(&load_with(&rejected).unwrap())
            .unwrap()
            .review
            .unwrap();
        assert_eq!(section["result"], "reject");
        assert_eq!(section["accepted"], Json::Null);

        let wrong = dir.write(
            "wrong.yaml",
            "review: \"surf-scoter-review-1\"\nproject-dir: \"/elsewhere\"\nexpires-at: \"2000-01-01T00:00:00Z\"\ndev-container:\n  use: {name: \"nope\"}\n",
        );
        assert_eq!(
            error_texts(load_with(&wrong).unwrap_err()),
            [
                "project-dir: isn't this run's project; leave it as the review gave it",
                "expires-at: must be in the future",
                "dev-container.use: there's no container 'nope' in your `dev-containers-search` directories outside the project",
            ]
        );
    }

    #[test]
    fn previous_review_as_written() {
        let dir = TempDir::new();
        dir.write("proj/.surf-scoter.d/env/Dockerfile", "FROM scratch\n");
        dir.write("proj/.surf-scoter.d/env/dev-env.yaml", DEV_ENV);
        dir.write(
            "proj/.surf-scoter.d/project.yaml",
            "container:\n  dev-container-path: ./env\n",
        );
        dir.write(
            "home/setup.yaml",
            "scoter:\n  values:\n    project-files-dir: .surf-scoter.d\n    project-file-name: project.yaml\n",
        );
        // Expired, and for a directory that isn't this run's: neither matters here.
        let previous = dir.write(
            "previous.yaml",
            concat!(
                "review: \"surf-scoter-review-1\"\n",
                "project-dir: \"/elsewhere\"\n",
                "expires-at: \"2000-01-01T00:00:00Z\"\n",
                "$comment: \"for the release\"\n",
                "egress:\n  - {host: \"github.com\", protocol: \"tls\", $comment: \"read only\"}\n",
                "commands:\n  - {command: \"sh\", program: \"/bin/sh\", args: [\"-c\", {any: {}}], $override: [\"verification:shells\"]}\n",
            ),
        );
        let load_with = |file: &Path| {
            let options = LoadOptions {
                project_dir: Some(dir.path().join("proj")),
                previous_review: Some(file.to_path_buf()),
                ..LoadOptions::default()
            };
            let env = Env {
                scoter_home: Some(dir.path().join("home")),
                ..Env::default()
            };
            load_all_with_env(&options, env)
        };
        let universal = build(&load_with(&previous).unwrap()).unwrap();
        let section = universal.previous_review.unwrap();
        assert_eq!(section["file"], previous.display().to_string());
        let data = &section["data"];
        assert_eq!(data["$comment"], "for the release");
        assert_eq!(data["egress"][0]["$comment"], "read only");
        assert_eq!(data["commands"][0]["$override"][0], "verification:shells");
        // Nothing is accepted from it.
        assert!(universal.review.is_none());
        assert!(universal.host_brokered.is_empty());
        // It's still parsed as a review is.
        let broken = dir.write("broken.yaml", "review: \"surf-scoter-review-1\"\nproject-dir: \"/p\"\nexpires-at: \"2000-01-01T00:00:00Z\"\nmystery: 1\n");
        assert_eq!(
            error_texts(load_with(&broken).unwrap_err()),
            ["mystery: unknown key"]
        );
    }

    // ---- the accepted settings' container ----

    /// A project, the user's containers in `mine` (searched after the
    /// project's own files directory, as the default setup does), and
    /// accepted settings with `dev_container`.
    #[cfg(unix)]
    fn accepted_container(dir: &TempDir, dev_container: &str) -> Result<FullConfig, ConfigErrors> {
        use std::os::unix::fs::PermissionsExt;
        dir.write("proj/.surf-scoter.d/env/Dockerfile", "FROM scratch\n");
        dir.write("proj/.surf-scoter.d/env/dev-env.yaml", DEV_ENV);
        // The same names as the user's, in the project: never used for them.
        dir.write("proj/.surf-scoter.d/my-env/Dockerfile", "FROM scratch\n");
        dir.write("proj/.surf-scoter.d/my-env/dev-env.yaml", "user: project\n");
        dir.write("proj/.surf-scoter.d/base/Dockerfile", "FROM scratch\n");
        dir.write("proj/.surf-scoter.d/base/dev-env.yaml", "user: project\n");
        dir.write(
            "proj/.surf-scoter.d/project.yaml",
            "container:\n  dev-container-path: ./env\n",
        );
        dir.write("mine/base/Dockerfile", "FROM scratch\n");
        dir.write("mine/base/dev-env.yaml", DEV_ENV);
        dir.write("mine/my-env/Dockerfile", "FROM base\n");
        dir.write(
            "mine/my-env/dev-env.yaml",
            "user: me\nparents:\n  - {name: base, tag: my-base}\n",
        );
        let project = dir.path().join("proj");
        dir.write(
            "home/setup.yaml",
            &format!(
                "scoter:\n  values:\n    project-files-dir: .surf-scoter.d\n    project-file-name: project.yaml\n  path-lists:\n    dev-containers-search: ['{}/.surf-scoter.d', '{}']\n",
                project.display(),
                dir.path().join("mine").display()
            ),
        );
        let accepted = dir.write(
            "verified/env-1/acceptances/a1/accepted.json",
            &format!(
                r#"{{"$schema": "surf-scoter-accepted-settings-1", "project-dir": "{}", "expires-at": "2999-01-01T00:00:00Z",
                    "vcs": null, "gateway": null, "egress": [], "commands": [], "dev-container": {dev_container}}}"#,
                project.display()
            ),
        );
        for path in [&dir.path().join("verified"), &accepted] {
            let mode = if path.is_dir() { 0o700 } else { 0o600 };
            std::fs::set_permissions(path, std::fs::Permissions::from_mode(mode)).unwrap();
        }
        let options = LoadOptions {
            project_dir: Some(project),
            accepted_settings: Some(accepted),
            ..LoadOptions::default()
        };
        let env = Env {
            scoter_home: Some(dir.path().join("home")),
            ..Env::default()
        };
        load_all_with_env(&options, env)
    }

    #[cfg(unix)]
    #[test]
    fn accepted_users_container() {
        let dir = TempDir::new();
        let config =
            accepted_container(&dir, r#"{"use": {"name": "my-env"}, "images": {}}"#).unwrap();
        let universal = build(&config).unwrap();
        let mine = dir.path().join("mine");
        assert_eq!(
            universal.active_config_locations.dev_container_path,
            Some(mine.join("my-env").display().to_string())
        );
        let dev = universal.dev_container.unwrap();
        assert_eq!(dev["user"], "me");
        // Its parent is the user's too, not the project's `base`.
        assert_eq!(
            dev["parent-builds"][0]["dir"],
            mine.join("base").display().to_string()
        );
        // The project file is still the project's.
        assert_eq!(
            universal.current_project_setup.unwrap()["container"]["dev-container-path"],
            "./env"
        );
    }

    #[cfg(unix)]
    #[test]
    fn accepted_projects_container() {
        let dir = TempDir::new();
        let config = accepted_container(&dir, r#"{"use": "project", "images": {}}"#).unwrap();
        let universal = build(&config).unwrap();
        assert_eq!(
            universal.active_config_locations.dev_container_path,
            Some(
                dir.path()
                    .join("proj/.surf-scoter.d/env")
                    .display()
                    .to_string()
            )
        );
    }

    #[cfg(unix)]
    #[test]
    fn accepted_users_not_in_project() {
        let dir = TempDir::new();
        // `env` is only in the project: not one of the user's.
        let errors = error_texts(
            accepted_container(&dir, r#"{"use": {"name": "env"}, "images": {}}"#).unwrap_err(),
        );
        assert_eq!(
            errors,
            [
                "dev-container.use: there's no container 'env' in your `dev-containers-search` directories outside the project"
            ]
        );
    }
}
