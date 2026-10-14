//! The universal configuration file: the whole configuration, gathered into
//! one JSON document that tools without a YAML parser can read.  See
//! `docs/contributors/universal-configuration-file.md`.
//!
//! `resolved` holds every section of the system configuration, `scoter`
//! included, as its `paths` (file locations), its `programs` (found on the
//! `PATH`), its `runs` commands, its `path-lists` and its `values`.  A
//! program that isn't there is `null`, as is every value referring to it.
//!
//! Each `runs` command is a list of arguments, the first being the program,
//! with quotes already removed and `{x:name}` placeholders left in place.
//! Fill in the placeholders argument by argument; a value with spaces stays
//! one argument.
//!
//! `setup` holds the system setup files' settings as written, so a later run
//! can start from this file (with a project file found since, say) without
//! reading the setup files again.

use std::collections::BTreeMap;
use std::path::Path;

use serde::{Deserialize, Serialize};
use serde_json::{Map, Number, Value as Json};
use yaml_serde::Value as Yaml;

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
    /// The project configuration file, as written.
    pub project: Option<Json>,
    /// The project's `commands`, with `program` set to the executable found
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
    /// Plain values, by key.
    pub values: BTreeMap<String, ScalarValue>,
}

/// Gather the universal configuration from a loaded configuration.
pub fn build(config: &FullConfig) -> Result<Universal, ConfigErrors> {
    let mut errors = Vec::new();
    let system = &config.system;

    let mut resolved = BTreeMap::new();
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
                        values: section.values,
                    },
                );
            }
            Err(e) => errors.push(e),
        }
    }

    let mut host_brokered = Vec::new();
    if let Some(project) = &config.project {
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

    let project = config
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
            dev_container_path: location(system::DEV_CONTAINER_PATH),
            dev_container_workspace_dir: location(system::DEV_CONTAINER_WORKSPACE_DIR),
            dev_container_settings_dir: location(system::DEV_CONTAINER_SETTINGS_DIR),
            user_setup_file: location(system::USER_SETUP_FILE),
            user_dir: location(system::USER_DIR),
            distribution_dir: location(system::DISTRIBUTION_DIR),
            cwd: location(system::CWD),
        },
        resolved,
        project,
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
    use crate::config::{LoadOptions, load_all_with_env};
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
            project: project.map(|p| dir.write("proj/scoter.yaml", p)),
            user_setup: Some(dir.write("setup.yaml", setup)),
            ..LoadOptions::default()
        };
        let env = Env {
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
        dir.write("proj/env/Dockerfile", "FROM scratch\n");
        dir.write("proj/env/dev-env.yaml", DEV_ENV);
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
                "  - {command: deploy, program: /nonexistent/deploy, args: []}\n",
            )),
            concat!(
                "docker:\n  programs:\n    exec: podman\n  runs:\n    build: ${exec} build -t {x:tag} '{x:dir}/a b'\n",
                "hoodoo:\n  programs:\n    exec: podman\n    helper: my-hoodoo\n  runs:\n    go: ${exec}\n    help: ${helper} --help\n",
                "ssh:\n  paths:\n    config-file: ./ssh/config\n",
                "  values:\n    port: 22022\n    ratio: 0.5\n    debug: false\n    unset: null\n    note: '${config-file} {x:keep}'\n",
                "scoter:\n  paths:\n    distribution-dir: /opt/scoter\n    note: ${ssh:config-file}\n",
                "  path-lists:\n    compose: [./compose, /etc/compose]\n",
            ),
        );
        let universal = build(&config).unwrap();
        let p = |path: &PathBuf| path.display().to_string();
        assert_eq!(
            universal.active_config_locations.project_config,
            Some(p(&dir.path().join("proj/scoter.yaml")))
        );
        assert_eq!(universal.setup.files.len(), 1);
        assert_eq!(
            universal.setup.files[0].file,
            p(&dir.path().join("setup.yaml"))
        );
        // No `./`: the path is canonical.
        assert_eq!(
            universal.active_config_locations.dev_container_path,
            Some(p(&dir.path().join("proj/env")))
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
                        ..section(
                            &[
                                ("dev-container-path", Some(p(&dir.path().join("proj/env")))),
                                ("distribution-dir", Some("/opt/scoter".to_string())),
                                ("note", Some(ssh_config.clone())),
                                (
                                    "project-config",
                                    Some(p(&dir.path().join("proj/scoter.yaml")))
                                ),
                                ("project-dir", Some(p(&dir.path().join("proj")))),
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
        assert_eq!(active.len(), 9);
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
            serde_json::json!([p(&dir.path().join("proj/scoter.yaml"))])
        );
        assert_eq!(
            json["setup"].as_object().unwrap().len(),
            1,
            "{}",
            json["setup"]
        );
        // The project file, as written, `vcs` included.
        assert_eq!(json["project"]["vcs"], "git");
        assert_eq!(json["project"]["container"]["dev-container-path"], "./env");
        assert_eq!(
            json["project"]["commands"][1]["program"],
            "/nonexistent/deploy"
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
        assert_eq!(json["project"]["env"]["set"]["CONF"], "${ssh:config-file}");
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
        let json: Json = serde_json::from_str(&to_json(&build(&config).unwrap())).unwrap();
        let active = &json["active-config-locations"];
        assert_eq!(active["project-config"], Json::Null);
        assert!(json.get("project-config").is_none());
        // Kept, though it isn't there.
        assert_eq!(json["resolved"]["ssh"]["paths"]["home"], "/h/.ssh");
        for group in ["programs", "runs", "path-lists", "values"] {
            assert_eq!(json["resolved"]["ssh"][group], Json::Object(Map::new()));
        }
        assert_eq!(
            json["resolved"]["scoter"]["paths"],
            Json::Object(Map::new())
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
}
