//! Writes the universal configuration file: every Surf Scoter setting,
//! gathered from all its sources, as JSON.  See
//! `docs/contributors/universal-configuration-file.md`.
//!
//! Usage: gen-scoter-uc [--config PROJECT.yaml] [--project-dir DIR]
//!                      [--dev-container-workspace-dir DIR] [--dev-container-settings-dir DIR]
//!                      [--user-setup SETUP.yaml]... [--distribution-dir DIR]
//!                      [--universal UNIVERSAL.json] [--no-setup-search]
//!                      [--output FILE]
//!
//! `--user-setup` may be given more than once, say to add a project's own
//! setup to the user's; a later one wins.  Setup files are only read from a
//! project when given this way: they can run any program, so the tools never
//! look for them there.
//!
//! Without `--output`, the JSON goes to stdout.  Problems in the
//! configuration go to stderr, all of them, and nothing is written.
//!
//! `--universal` starts from a universal configuration file made earlier:
//! its system setup is used without reading the setup files again, and its
//! project file unless `--config` gives another.  `--no-setup-search` skips
//! looking for the user's setup file and the install directory's.  So a
//! first run can find the setup, and a second can add the project file:
//!
//!     gen-scoter-uc --output first.json
//!     gen-scoter-uc --universal first.json --no-setup-search --config PROJECT.yaml

use std::ffi::OsString;
use std::fs;
use std::io::{self, Write};
use std::path::PathBuf;

use scoter_host::config::{self, LoadOptions, read};
use scoter_host::universal;

const EXIT_OK: i32 = 0;
const EXIT_FAILURE: i32 = 1;
const EXIT_USAGE: i32 = 2;

const USAGE: &str = "Usage: gen-scoter-uc [--config PROJECT.yaml] [--project-dir DIR] \
                     [--dev-container-workspace-dir DIR] [--dev-container-settings-dir DIR] [--user-setup SETUP.yaml]... \
                     [--distribution-dir DIR] [--universal UNIVERSAL.json] [--no-setup-search] \
                     [--output FILE]";

fn main() {
    std::process::exit(run(std::env::args_os().skip(1)));
}

/// Run with the command line arguments (without the program name).
/// Returns the process exit code.
fn run<I: IntoIterator<Item = OsString>>(args: I) -> i32 {
    let cli = match parse_args(args) {
        Ok(Some(cli)) => cli,
        Ok(None) => {
            println!("{USAGE}");
            println!("Writes the whole Surf Scoter configuration as JSON.");
            println!("--config gives the project configuration file, if any.");
            println!(
                "--project-dir gives the project directory, in place of the project file's directory."
            );
            println!(
                "--dev-container-workspace-dir and --dev-container-settings-dir give the dev container's workspace and settings directories."
            );
            println!(
                "--user-setup gives a system setup file to read first, and may be repeated (a later one wins); --distribution-dir, where Surf Scoter is installed."
            );
            println!(
                "--universal starts from a universal configuration file made earlier, without reading its setup files again."
            );
            println!(
                "--no-setup-search skips looking for the user's and the install directory's setup files."
            );
            println!("--output gives the file to write; otherwise, the JSON goes to stdout.");
            return EXIT_OK;
        }
        Err(e) => {
            eprintln!("gen-scoter-uc: {e}");
            eprintln!("{USAGE}");
            return EXIT_USAGE;
        }
    };
    let universal = config::load_all(&cli.options).and_then(|config| universal::build(&config));
    let universal = match universal {
        Ok(u) => u,
        Err(errors) => {
            for e in &errors.0 {
                eprintln!("gen-scoter-uc: {e}");
            }
            return EXIT_FAILURE;
        }
    };
    let json = universal::to_json(&universal);
    // A file the tools would refuse to read is no use.
    if json.len() as u64 > read::MAX_UNIVERSAL_FILE_SIZE {
        eprintln!(
            "gen-scoter-uc: cannot write the configuration: {}",
            read::size_error(read::MAX_UNIVERSAL_FILE_SIZE)
        );
        return EXIT_FAILURE;
    }
    let written = match &cli.output {
        Some(path) => fs::write(path, &json).map_err(|e| format!("{}: {e}", path.display())),
        None => {
            let mut stdout = io::stdout().lock();
            stdout
                .write_all(json.as_bytes())
                .and_then(|()| stdout.flush())
                .map_err(|e| e.to_string())
        }
    };
    match written {
        Ok(()) => EXIT_OK,
        Err(e) => {
            eprintln!("gen-scoter-uc: cannot write the configuration: {e}");
            EXIT_FAILURE
        }
    }
}

#[derive(Debug, PartialEq, Eq)]
struct Cli {
    options: LoadOptions,
    output: Option<PathBuf>,
}

/// Parse the arguments.  `Ok(None)` means help was requested.
/// Options take a value as `--name VALUE` or `--name=VALUE`.
fn parse_args<I: IntoIterator<Item = OsString>>(args: I) -> Result<Option<Cli>, String> {
    let mut config: Option<OsString> = None;
    let mut project_dir: Option<OsString> = None;
    let mut dev_container_workspace_dir: Option<OsString> = None;
    let mut dev_container_settings_dir: Option<OsString> = None;
    let mut user_setup: Vec<PathBuf> = Vec::new();
    let mut distribution_dir: Option<OsString> = None;
    let mut universal: Option<OsString> = None;
    let mut no_setup_search = false;
    let mut output: Option<OsString> = None;
    let mut args = args.into_iter();
    while let Some(arg) = args.next() {
        let Some(s) = arg.to_str().filter(|s| s.starts_with('-')) else {
            return Err(format!("unexpected argument '{}'", arg.to_string_lossy()));
        };
        if s == "-h" || s == "--help" {
            return Ok(None);
        }
        if s == "--no-setup-search" {
            if no_setup_search {
                return Err(format!("{s} given more than once"));
            }
            no_setup_search = true;
            continue;
        }
        let (name, inline) = match s.split_once('=') {
            Some((name, value)) => (name, Some(OsString::from(value))),
            None => (s, None),
        };
        if name == "--user-setup" {
            let value = match inline {
                Some(value) => value,
                None => args.next().ok_or_else(|| format!("{name} needs a value"))?,
            };
            if value.is_empty() {
                return Err(format!("{name} needs a value"));
            }
            user_setup.push(PathBuf::from(value));
            continue;
        }
        let slot = match name {
            "--config" => &mut config,
            "--project-dir" => &mut project_dir,
            "--dev-container-workspace-dir" => &mut dev_container_workspace_dir,
            "--dev-container-settings-dir" => &mut dev_container_settings_dir,
            "--distribution-dir" => &mut distribution_dir,
            "--universal" => &mut universal,
            "--output" => &mut output,
            _ => return Err(format!("unknown option '{s}'")),
        };
        let value = match inline {
            Some(value) => value,
            None => args.next().ok_or_else(|| format!("{name} needs a value"))?,
        };
        if value.is_empty() {
            return Err(format!("{name} needs a value"));
        }
        if slot.is_some() {
            return Err(format!("{name} given more than once"));
        }
        *slot = Some(value);
    }
    Ok(Some(Cli {
        options: LoadOptions {
            project: config.map(PathBuf::from),
            project_dir: project_dir.map(PathBuf::from),
            dev_container_workspace_dir: dev_container_workspace_dir.map(PathBuf::from),
            dev_container_settings_dir: dev_container_settings_dir.map(PathBuf::from),
            user_setup,
            distribution_dir: distribution_dir.map(PathBuf::from),
            universal: universal.map(PathBuf::from),
            no_setup_search,
        },
        output: output.map(PathBuf::from),
    }))
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::path::Path;

    fn os_args(args: &[&str]) -> Vec<OsString> {
        args.iter().map(OsString::from).collect()
    }

    /// A fresh directory for one test.
    fn temp_dir(name: &str) -> PathBuf {
        let dir =
            std::env::temp_dir().join(format!("gen-scoter-uc-test-{}-{name}", std::process::id()));
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(&dir).unwrap();
        dir
    }

    #[test]
    fn args_valid() {
        assert_eq!(
            parse_args(os_args(&[])),
            Ok(Some(Cli {
                options: LoadOptions::default(),
                output: None
            })),
        );
        let parsed = parse_args(os_args(&[
            "--config",
            "p.yaml",
            "--project-dir=proj",
            "--dev-container-workspace-dir",
            "/ws",
            "--dev-container-settings-dir=settings",
            "--user-setup=s.yaml",
            "--user-setup",
            "project-setup.yaml",
            "--distribution-dir",
            "/opt/scoter",
            "--universal=first.json",
            "--no-setup-search",
            "--output=out.json",
        ]));
        assert_eq!(
            parsed,
            Ok(Some(Cli {
                options: LoadOptions {
                    project: Some(PathBuf::from("p.yaml")),
                    project_dir: Some(PathBuf::from("proj")),
                    dev_container_workspace_dir: Some(PathBuf::from("/ws")),
                    dev_container_settings_dir: Some(PathBuf::from("settings")),
                    user_setup: vec![PathBuf::from("s.yaml"), PathBuf::from("project-setup.yaml")],
                    distribution_dir: Some(PathBuf::from("/opt/scoter")),
                    universal: Some(PathBuf::from("first.json")),
                    no_setup_search: true,
                },
                output: Some(PathBuf::from("out.json")),
            })),
        );
    }

    #[test]
    fn args_help() {
        assert_eq!(parse_args(os_args(&["-h"])), Ok(None));
        assert_eq!(parse_args(os_args(&["--config", "x", "--help"])), Ok(None));
    }

    #[test]
    fn args_errors() {
        let err = |args: &[&str]| parse_args(os_args(args)).unwrap_err();
        assert_eq!(err(&["project.yaml"]), "unexpected argument 'project.yaml'");
        assert_eq!(err(&["--config"]), "--config needs a value");
        assert_eq!(err(&["--output="]), "--output needs a value");
        assert_eq!(
            err(&["--output", "a", "--output", "b"]),
            "--output given more than once"
        );
        assert_eq!(err(&["--workspace", "x"]), "unknown option '--workspace'");
        assert_eq!(
            err(&["--no-setup-search", "--no-setup-search"]),
            "--no-setup-search given more than once"
        );
        assert_eq!(err(&["--universal"]), "--universal needs a value");
        assert_eq!(err(&["--user-setup"]), "--user-setup needs a value");
        assert_eq!(err(&["--user-setup="]), "--user-setup needs a value");
    }

    #[test]
    fn run_usage_and_errors() {
        assert_eq!(run(os_args(&["--help"])), EXIT_OK);
        assert_eq!(run(os_args(&["extra"])), EXIT_USAGE);
        assert_eq!(
            run(os_args(&["--config", "/nonexistent/scoter.yaml"])),
            EXIT_FAILURE
        );
    }

    #[test]
    fn run_writes_output_file() {
        let dir = temp_dir("output");
        fs::create_dir_all(dir.join("env")).unwrap();
        fs::write(dir.join("env/Dockerfile"), "FROM scratch\n").unwrap();
        fs::write(
            dir.join("env/dev-env.yaml"),
            "user: u\nmounts: {workspace: /w, setup: /s}\nbroker-bin: /b\nsshd-port: 22\n",
        )
        .unwrap();
        fs::write(
            dir.join("scoter.yaml"),
            "container:\n  dev-container-path: env\n",
        )
        .unwrap();
        fs::write(dir.join("setup.yaml"), "ssh:\n  paths:\n    home: ~/.ssh\n").unwrap();
        let out = dir.join("universal.json");
        let path = |p: &Path| p.display().to_string();
        let code = run(os_args(&[
            "--config",
            &path(&dir.join("scoter.yaml")),
            "--user-setup",
            &path(&dir.join("setup.yaml")),
            "--output",
            &path(&out),
        ]));
        let written = fs::read_to_string(&out);
        let _ = fs::remove_dir_all(&dir);
        assert_eq!(code, EXIT_OK);
        let json: serde_json::Value = serde_json::from_str(&written.unwrap()).unwrap();
        assert_eq!(json["dev-container"]["broker-bin"], "/b");
        // The resolved form has every key, `parent-builds` and all.
        assert_eq!(
            json["dev-container"]["sshd-port-env"],
            serde_json::Value::Null
        );
        assert_eq!(
            json["dev-container"]["parent-builds"],
            serde_json::json!([])
        );
        assert_eq!(
            json["setup"]["files"][0]["file"],
            path(&dir.join("setup.yaml"))
        );
        assert!(json.get("system-configs").is_none());
    }

    #[test]
    fn run_cannot_write() {
        let dir = temp_dir("unwritable");
        fs::write(dir.join("setup.yaml"), "").unwrap();
        let setup = dir.join("setup.yaml").display().to_string();
        let out = dir.join("missing/dir/out.json").display().to_string();
        let code = run(os_args(&["--user-setup", &setup, "--output", &out]));
        let _ = fs::remove_dir_all(&dir);
        assert_eq!(code, EXIT_FAILURE);
    }
}
