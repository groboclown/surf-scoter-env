//! The universal configuration file: the whole configuration, gathered into
//! one JSON document that tools without a YAML parser can read.  See
//! `docs/contributors/universal-configuration-file.md`.
//!
//! `sections` holds every section of the system configuration, `scoter`
//! included, as its `paths` (file locations, with executables found on the
//! `PATH`) and its `run` command expressions.  A section with an executable
//! that isn't there is `null`.
//!
//! Each `run` command is a list of arguments, the first being the program,
//! with quotes already removed and `{x:name}` placeholders left in place.
//! Fill in the placeholders argument by argument; a value with spaces stays
//! one argument.

use std::collections::BTreeMap;
use std::path::Path;

use serde::Serialize;
use serde_json::{Map, Number, Value as Json};
use yaml_serde::Value as Yaml;

use crate::config::system::{SectionDefaults, SectionLookup};
use crate::config::{ConfigError, ConfigErrors, FullConfig, child_key, index_key};

/// The universal configuration file's contents.
#[derive(Debug, Clone, PartialEq, Serialize)]
#[serde(rename_all = "kebab-case")]
pub struct Universal {
    /// The project configuration file, if one was given.
    pub project_config: Option<String>,
    /// The system setup files read, highest priority first.
    pub system_configs: Vec<String>,
    /// The project's dev container directory.
    pub dev_container_path: Option<String>,
    /// The `NAME-paths` lists, by `NAME`.
    pub lookup_paths: BTreeMap<String, Vec<String>>,
    /// Every section, or `null` when one of its executables isn't there.
    pub sections: BTreeMap<String, Option<UniversalSection>>,
    /// The project's `commands`, with `program` set to the executable found
    /// on the host, or `null`.
    pub host_brokered: Vec<Json>,
    /// The dev container's `dev-env.yaml`.
    pub dev_container: Option<Json>,
}

/// One section of the system configuration.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct UniversalSection {
    /// File locations, by key.
    pub paths: BTreeMap<String, String>,
    /// Commands, by key, as their arguments.
    pub run: BTreeMap<String, Vec<String>>,
}

/// Gather the universal configuration from a loaded configuration.
pub fn build(config: &FullConfig) -> Result<Universal, ConfigErrors> {
    let mut errors = Vec::new();
    let system = &config.system;

    let lookup_paths = system
        .lookup_paths()
        .iter()
        .map(|(name, paths)| (name.clone(), paths.iter().map(|p| path_text(p)).collect()))
        .collect();

    let mut sections = BTreeMap::new();
    for name in system.sections() {
        match system.section(name, &SectionDefaults::default()) {
            Ok(SectionLookup::Found(section)) => {
                let paths = section
                    .paths
                    .iter()
                    .map(|(key, path)| (key.clone(), path_text(path)))
                    .collect();
                let run = section
                    .run
                    .iter()
                    .map(|(key, template)| (key.clone(), template.to_args()))
                    .collect();
                sections.insert(name.to_string(), Some(UniversalSection { paths, run }));
            }
            Ok(SectionLookup::MissingExecutable { .. }) => {
                sections.insert(name.to_string(), None);
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
            host_brokered.push(Json::Object(entry));
        }
    }

    let dev_container = config
        .dev_container
        .as_ref()
        .map(|dev| yaml_to_json(&dev.env.raw, &dev.env_file(), "", &mut errors));

    if !errors.is_empty() {
        return Err(ConfigErrors(errors));
    }
    Ok(Universal {
        project_config: config.project.as_ref().map(|p| path_text(&p.path)),
        system_configs: system.files().iter().map(|p| path_text(p)).collect(),
        dev_container_path: config.dev_container.as_ref().map(|d| path_text(&d.dir)),
        lookup_paths,
        sections,
        host_brokered,
        dev_container,
    })
}

/// The universal configuration as pretty-printed JSON, ending with a newline.
pub fn to_json(universal: &Universal) -> String {
    let mut text =
        serde_json::to_string_pretty(universal).expect("only JSON-ready values are stored");
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

    fn load(dir: &TempDir, project: Option<&str>, setup: &str) -> FullConfig {
        let options = LoadOptions {
            project: project.map(|p| dir.write("proj/scoter.yaml", p)),
            user_setup: Some(dir.write("setup.yaml", setup)),
            distribution_dir: None,
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
                "commands:\n",
                "  - command: aws\n    args: [s3, ls, {any: {prefix: 's3://b/'}}]\n",
                "  - {command: deploy, program: /nonexistent/deploy, args: []}\n",
            )),
            concat!(
                "docker:\n  paths:\n    exec: podman\n  run:\n    build: ${exec} build -t {x:tag} '{x:dir}/a b'\n",
                "hoodoo:\n  paths:\n    exec: podman\n    helper-cmd: my-hoodoo\n  run:\n    go: ${exec}\n",
                "ssh:\n  paths:\n    config-file: ./ssh/config\n",
                "scoter:\n  paths:\n    install-dir: /opt/scoter\n    note: ${ssh:config-file}\n",
                "docker-compose-paths: [./compose, /etc/compose]\n",
            ),
        );
        let universal = build(&config).unwrap();
        let p = |path: &PathBuf| path.display().to_string();
        assert_eq!(
            universal.project_config,
            Some(p(&dir.path().join("proj/scoter.yaml")))
        );
        assert_eq!(
            universal.system_configs,
            [p(&dir.path().join("setup.yaml"))]
        );
        // No `./`: the path is canonical.
        assert_eq!(
            universal.dev_container_path,
            Some(p(&dir.path().join("proj/env")))
        );
        assert_eq!(
            universal.lookup_paths,
            BTreeMap::from([(
                "docker-compose".to_string(),
                vec![
                    p(&dir.path().join("proj/compose")),
                    "/etc/compose".to_string()
                ],
            )]),
        );
        let section = |paths: &[(&str, String)], run: &[(&str, Vec<String>)]| {
            Some(UniversalSection {
                paths: paths
                    .iter()
                    .map(|(k, v)| (k.to_string(), v.clone()))
                    .collect(),
                run: run
                    .iter()
                    .map(|(k, v)| (k.to_string(), v.clone()))
                    .collect(),
            })
        };
        let ssh_config = p(&dir.path().join("proj/ssh/config"));
        assert_eq!(
            universal.sections,
            BTreeMap::from([
                (
                    "docker".to_string(),
                    section(
                        &[("exec", p(&podman))],
                        // Quotes are removed, and the path with a space is one argument.
                        &[(
                            "build",
                            [p(&podman).as_str(), "build", "-t", "{x:tag}", "{x:dir}/a b"]
                                .map(String::from)
                                .to_vec()
                        )],
                    ),
                ),
                // `helper-cmd` isn't there.
                ("hoodoo".to_string(), None),
                (
                    "scoter".to_string(),
                    section(
                        &[
                            ("install-dir", "/opt/scoter".to_string()),
                            ("note", ssh_config.clone()),
                        ],
                        &[],
                    ),
                ),
                (
                    "ssh".to_string(),
                    section(&[("config-file", ssh_config)], &[])
                ),
            ]),
        );

        let json: Json = serde_json::from_str(&to_json(&universal)).unwrap();
        assert_eq!(json["host-brokered"][0]["command"], "aws");
        assert_eq!(json["host-brokered"][0]["program"], p(&aws));
        assert_eq!(
            json["host-brokered"][0]["args"][2]["any"]["prefix"],
            "s3://b/"
        );
        assert_eq!(json["host-brokered"][1]["program"], Json::Null);
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
        assert_eq!(json["project-config"], Json::Null);
        assert_eq!(json["sections"]["ssh"]["paths"]["home"], "/h/.ssh");
        assert_eq!(json["sections"]["ssh"]["run"], Json::Object(Map::new()));
        assert_eq!(
            json["sections"]["scoter"]["paths"],
            Json::Object(Map::new())
        );
        assert_eq!(json["dev-container-path"], Json::Null);
        assert_eq!(json["dev-container"], Json::Null);
        assert_eq!(json["host-brokered"], Json::Array(vec![]));
        assert_eq!(json["lookup-paths"], Json::Object(Map::new()));
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
