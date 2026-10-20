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
    /// The commands `scoter-host` runs.  Until the accepted settings feed it
    /// (step 8 of `docs/contributors/projects-v1.md`), it's empty unless
    /// `BuildOptions::unreviewed_project_commands` builds it from the
    /// project's `commands`: each with `program` set to the executable found
    /// on the host, or `null`, and `env` set to the command's whole
    /// environment plan: the project's `env` with the command's added, its
    /// `${section:key}` references expanded.  `@NAME@` is left as written,
    /// as it's the environment of the program running the commands.
    pub host_brokered: Vec<Json>,
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
    /// The project's dev container directory.
    #[serde(default)]
    pub dev_container_path: Option<String>,
    /// The dev container's mounts: the project workspace, and the settings
    /// directory.
    #[serde(default)]
    pub dev_container_workspace_dir: Option<String>,
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
}

/// How to build the universal configuration.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub struct BuildOptions {
    /// Build `host-brokered` from the project's `commands`, unreviewed.  A
    /// stand-in until the accepted settings feed it (step 8).
    pub unreviewed_project_commands: bool,
}

impl BuildOptions {
    /// `host-brokered` from the project's commands, unreviewed.
    pub const UNREVIEWED: BuildOptions = BuildOptions {
        unreviewed_project_commands: true,
    };
}

/// Gather the universal configuration from a loaded configuration.
pub fn build(config: &FullConfig, options: &BuildOptions) -> Result<Universal, ConfigErrors> {
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

    let mut host_brokered = Vec::new();
    if let Some(project) = config
        .project
        .as_ref()
        .filter(|_| options.unreviewed_project_commands)
    {
        for (i, (rule, raw)) in project
            .commands
            .iter()
            .zip(&project.raw_commands)
            .enumerate()
        {
            let key = index_key("commands", i);
            let mut entry = match yaml_to_json(raw, &project.path, &key, &mut errors) {
                Json::Object(entry) => entry,
                // Entries were checked to be mappings when the file was read.
                _ => Map::new(),
            };
            let program = rule.program.as_deref().unwrap_or(&rule.command);
            let found = system
                .find_program(program)
                .map(|p| Json::String(path_text(&p)));
            entry.insert("program".to_string(), found.unwrap_or(Json::Null));
            let env = serde_json::to_value(&rule.env_plan).expect("only text is stored");
            entry.insert("env".to_string(), env);
            host_brokered.push(Json::Object(entry));
        }
    }

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
            dev_container_path: location(system::DEV_CONTAINER_PATH),
            dev_container_workspace_dir: location(system::DEV_CONTAINER_WORKSPACE_DIR),
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
        host_brokered,
        dev_container,
        setup: system.setup_record().clone(),
        loaded_from: system.loaded_from().clone(),
    })
}

/// The universal configuration as compact JSON, ending with a newline.
/// (Compact, to stay well within `MAX_UNIVERSAL_FILE_SIZE`; a tool such as
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
fn yaml_to_json(value: &Yaml, file: &Path, key: &str, errors: &mut Vec<ConfigError>) -> Json {
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
        let universal = build(&config, &BuildOptions::UNREVIEWED).unwrap();
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
        // The locations are also the `scoter` section's paths.
        let active = json["active-config-locations"].as_object().unwrap();
        assert_eq!(active.len(), 11);
        for (key, value) in active {
            let path = &json["resolved"]["scoter"]["paths"][key];
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
        let json: Json = serde_json::from_str(&to_json(
            &build(&config, &BuildOptions::UNREVIEWED).unwrap(),
        ))
        .unwrap();
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
        build(&config, &BuildOptions::UNREVIEWED)
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
                "host-bridge:\n  host-commands:\n",
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
                ("bash".to_string(), mapped(None, &["host-bridge:shells"])),
                ("docker".to_string(), mapped(p(&podman), &["docker:docker"])),
                ("mytool".to_string(), mapped(p(&mytool), &["mine:tools"])),
                ("py".to_string(), mapped(p(&python), &["mine:tools"])),
                ("sh".to_string(), mapped(p(&sh), &["host-bridge:shells"])),
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
                    verdict(Action::Refuse, &["host-bridge:shells"])
                ),
                (
                    "docker".to_string(),
                    verdict(Action::Warn, &["docker:docker"])
                ),
                // The same file as `sh`.
                (
                    "mytool".to_string(),
                    verdict(Action::Refuse, &["host-bridge:shells", "mine:tools"])
                ),
                // By the base name of its executable.
                (
                    "py".to_string(),
                    verdict(Action::Refuse, &["host-bridge:interpreters", "mine:tools"])
                ),
                // And the other way around.
                (
                    "sh".to_string(),
                    verdict(Action::Refuse, &["host-bridge:shells", "mine:tools"])
                ),
            ])
        );
        let shims = &checks.checks["host-bridge:shims"];
        assert_eq!(
            shims.paths,
            [dir.path().join("dist/shims").display().to_string()]
        );
        assert_eq!(
            shims.files,
            [dir.path().join("dist/setup.yaml").display().to_string()]
        );
        assert_eq!(checks.checks["host-bridge:code-options"].options, ["-c"]);
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
                "host-bridge:\n  host-commands:\n    ksh-ok:\n      action: allow\n      reason: mine\n",
                "      commands: [ksh, zsh]\n      overrides: ['host-bridge:shells', 'host-bridge:gone']\n",
            ),
            concat!(
                "host-bridge:\n  host-commands:\n",
                "    shells: {action: refuse, reason: shell, commands: [sh, ksh]}\n",
                "    korn: {action: warn, reason: korn, commands: [ksh]}\n",
            ),
        )
        .unwrap();
        // First the most restrictive of the matching checks (refuse); then
        // the override takes `shells`' place, and `korn` still applies.
        let ksh = &checks.commands["ksh"];
        assert_eq!(ksh.action, Action::Warn);
        assert_eq!(ksh.checks, ids(&["host-bridge:korn", "host-bridge:ksh-ok"]));
        assert_eq!(
            ksh.overridden,
            [Overridden {
                check: "host-bridge:shells".to_string(),
                by: "host-bridge:ksh-ok".to_string()
            }]
        );
        assert_eq!(checks.commands["sh"].action, Action::Refuse);
        assert!(checks.commands["sh"].overridden.is_empty());
        // Nothing to override: an override alone doesn't decide a command.
        assert!(!checks.commands.contains_key("zsh"));
        assert_eq!(
            messages(&checks.notices),
            [
                "`host-bridge:ksh-ok` overrides `host-bridge:shells` for the command `zsh`, which `host-bridge:shells` doesn't match; the override does nothing for it",
                "`host-bridge:ksh-ok` overrides `host-bridge:gone`, which isn't set; the override does nothing",
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
                "host-bridge:\n  host-commands:\n    shells: {action: refuse, reason: r, commands: [sh]}\n",
                "a:\n  host-commands:\n    one: {action: warn, reason: r, command-map: {docker: podman}}\n",
                "c:\n  host-commands:\n    x: {action: warn, reason: r, commands: [sh], overrides: ['c:x']}\n",
            ),
            concat!(
                "b:\n  host-commands:\n    two: {action: warn, reason: r, command-map: {docker: sh}}\n",
                "    relax: {action: allow, reason: r, commands: [sh], overrides: ['host-bridge:shells']}\n",
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
                    "b.host-commands.relax: overrides `host-bridge:shells`, from a higher priority setup file ({}); a check can only override checks from its own file or a lower priority one",
                    dir.path().join("home/setup.yaml").display()
                ),
                "c.host-commands.x: can't override itself".to_string(),
            ]
        );
    }

    #[cfg(unix)]
    #[test]
    fn host_command_program_in_the_project() {
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
            error_texts(build(&config, &BuildOptions::UNREVIEWED).unwrap_err()),
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
                "host-bridge:\n  host-commands:\n",
                "    shells: {action: warn, reason: mine, commands: [sh]}\n",
                "    runners: null\n",
                "    same: {action: refuse, reason: mine, commands: [x, y]}\n",
            ),
            concat!(
                "host-bridge:\n  host-commands:\n",
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
                    "{user} removes `host-bridge:runners`, so {dist}'s check no longer applies, nor will what it adds later"
                ),
                format!(
                    "{user} replaces `host-bridge:shells` from {dist} without its commands `ksh`, `nu`; what {dist} adds to it later won't apply (`overrides` relaxes a check without replacing it)"
                ),
                format!(
                    "{user} replaces `host-bridge:shells` from {dist} with `warn` in place of `refuse`"
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
            checks.checks["host-bridge:shells"].files,
            [user.clone(), dist.clone()]
        );
        assert!(!checks.checks.contains_key("host-bridge:runners"));
        assert_eq!(checks.checks["host-bridge:same"].reason, "mine");
    }

    #[cfg(unix)]
    #[test]
    fn host_command_checks_survive_the_universal_file() {
        let dir = TempDir::new();
        tool(&dir, "bin/sh");
        let config = load_setups(
            &dir,
            "a:\n  host-commands:\n    c: {action: warn, reason: r, commands: [sh], overrides: ['b:d']}\n",
            "b:\n  host-commands:\n    d: {action: refuse, reason: r, commands: [sh]}\n    e: null\n",
        )
        .unwrap();
        let first = build(&config, &BuildOptions::UNREVIEWED).unwrap();
        let file = dir.write("universal.json", &to_json(&first));
        let env = Env {
            path: Some(dir.path().join("bin").into_os_string()),
            ..Env::default()
        };
        let again = crate::config::load_universal_with_env(&file, env).unwrap();
        assert_eq!(
            build(&again, &BuildOptions::UNREVIEWED)
                .unwrap()
                .host_command_checks,
            first.host_command_checks
        );
        assert_eq!(first.setup, again.system.setup_record().clone());
    }

    #[cfg(unix)]
    #[test]
    fn host_brokered_needs_the_unreviewed_flag() {
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
        let reviewed = build(&config, &BuildOptions::default()).unwrap();
        assert!(reviewed.host_brokered.is_empty());
        // The project is still there, as a record.
        assert_eq!(
            reviewed.current_project_setup.as_ref().unwrap()["commands"][0]["command"],
            "aws"
        );
        let unreviewed = build(&config, &BuildOptions::UNREVIEWED).unwrap();
        assert_eq!(unreviewed.host_brokered.len(), 1);
    }

    #[test]
    fn egress_wildcards_refused_by_the_setup() {
        let dir = TempDir::new();
        dir.write("proj/.surf-scoter.d/env/Dockerfile", "FROM scratch\n");
        dir.write("proj/.surf-scoter.d/env/dev-env.yaml", DEV_ENV);
        dir.write(
            "proj/.surf-scoter.d/project.yaml",
            "container:\n  dev-container-path: ./env\negress:\n  - {host: '*.github.io', protocol: tls}\n  - {host: '*.example.com', protocol: tls}\n",
        );
        dir.write(
            "home/setup.yaml",
            "scoter:\n  values:\n    project-files-dir: .surf-scoter.d\n    project-file-name: project.yaml\n  value-lists:\n    egress-refused-wildcards: [github.io, co.uk]\n",
        );
        let options = LoadOptions {
            project_dir: Some(dir.path().join("proj")),
            ..LoadOptions::default()
        };
        let env = Env {
            scoter_home: Some(dir.path().join("home")),
            ..Env::default()
        };
        let errors = error_texts(load_all_with_env(&options, env).unwrap_err());
        assert_eq!(
            errors,
            [
                "egress[0].host: '*.github.io' would allow every name under `github.io`, where anyone can have one (your setup's `scoter: value-lists: egress-refused-wildcards`)"
            ]
        );
    }
}
