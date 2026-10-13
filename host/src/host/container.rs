//! Starts the broker inside the container, connected over its stdin / stdout.
//!
//! The command comes from the system configuration's `docker` section:
//!
//! ```yaml
//! docker:
//!     programs:
//!         exec: docker
//!     runs:
//!         broker: ${exec} exec -i {x:container} {x:broker}
//! ```
//!
//! Both keys are optional, defaulting to the values above.  `{x:container}` is
//! the container name given on the command line, and `{x:broker}` is the dev
//! container's `broker-bin`.  No TTY may be requested: it would mangle the
//! binary protocol.

use std::collections::HashMap;
use std::io;
use std::process::{Child, ChildStdin, ChildStdout, Command, Stdio};

use scoter_host::config::system::{EXEC, SectionDefaults, SystemConfig};

/// The system configuration section for the container tool.
pub(crate) const CONTAINER_TOOL: &str = "docker";
/// The `runs` key, in that section, of the command that runs the broker.
pub(crate) const RUN_BROKER: &str = "broker";
pub(crate) const DEFAULT_EXEC: &str = "docker";
pub(crate) const DEFAULT_RUN_BROKER: &str = "${exec} exec -i {x:container} {x:broker}";

/// The running broker process and the host's ends of its pipes.
pub(crate) struct Broker {
    pub child: Child,
    pub stdin: ChildStdin,
    pub stdout: ChildStdout,
}

/// Check that `name` looks like a container name or id, so it can't be
/// mistaken for an option by the container tool.
pub(crate) fn validate_name(name: &str) -> Result<(), String> {
    let mut chars = name.chars();
    let valid = match chars.next() {
        Some(first) => {
            first.is_ascii_alphanumeric()
                && chars.all(|c| c.is_ascii_alphanumeric() || matches!(c, '_' | '.' | '-'))
        }
        None => false,
    };
    if valid {
        Ok(())
    } else {
        Err(format!(
            "invalid container name '{name}': must start with a letter or digit, \
             followed by letters, digits, '_', '.' or '-'"
        ))
    }
}

/// The command line that runs `broker` in `container`.
pub(crate) fn broker_argv(
    system: &SystemConfig,
    container: &str,
    broker: &str,
) -> Result<Vec<String>, String> {
    validate_name(container)?;
    let defaults = SectionDefaults {
        programs: &[(EXEC, DEFAULT_EXEC)],
        runs: &[(RUN_BROKER, DEFAULT_RUN_BROKER)],
        ..SectionDefaults::default()
    };
    let section = system
        .section(CONTAINER_TOOL, &defaults)
        .map_err(|e| e.to_string())?;
    let template = match section.runs.get(RUN_BROKER) {
        Some(Some(template)) => template,
        // It needs an executable that isn't there.
        Some(None) => {
            let missing: Vec<String> = section
                .missing
                .iter()
                .map(|(key, program)| {
                    format!("'{program}' (set by `{CONTAINER_TOOL}.programs.{key}`)")
                })
                .collect();
            return Err(format!("cannot find the program {}", missing.join(", ")));
        }
        None => return Err("no command to run the broker".to_string()),
    };
    let values = HashMap::from([("container", container), ("broker", broker)]);
    template
        .render(&values)
        .map_err(|e| format!("`{CONTAINER_TOOL}.runs.{RUN_BROKER}`: {e}"))
}

/// Build the command for `argv` (the program, then its arguments).
/// Its stdin / stdout are piped; stderr goes to the host's stderr.
pub(crate) fn broker_command(argv: &[String]) -> io::Result<Command> {
    let Some((program, args)) = argv.split_first() else {
        return Err(io::Error::new(
            io::ErrorKind::InvalidInput,
            "the broker command is empty",
        ));
    };
    let mut cmd = Command::new(program);
    cmd.args(args)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::inherit());
    Ok(cmd)
}

/// Start the broker with `argv`, from `broker_argv`.
pub(crate) fn launch(argv: &[String]) -> io::Result<Broker> {
    let mut child = broker_command(argv)?.spawn()?;
    let stdin = child.stdin.take().expect("stdin is piped");
    let stdout = child.stdout.take().expect("stdout is piped");
    Ok(Broker {
        child,
        stdin,
        stdout,
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use scoter_host::config::system::{self, Env};
    use std::ffi::OsStr;
    use std::path::PathBuf;

    use crate::host::exec::TempDir;

    /// A system configuration from `setup` (a setup file's text), with `PATH` set to `path`.
    fn system_config(setup: &str, path: &str) -> SystemConfig {
        let dir = TempDir::new().unwrap();
        let file = dir.path().join("setup.yaml");
        std::fs::write(&file, setup).unwrap();
        let env = Env {
            path: Some(path.into()),
            ..Env::default()
        };
        system::load(Some(&file), None, None, env).unwrap()
    }

    /// The directory holding `sh`, for a PATH.
    fn sh_dir() -> String {
        let sh = ["/bin/sh", "/usr/bin/sh"]
            .into_iter()
            .map(PathBuf::from)
            .find(|p| p.is_file())
            .unwrap();
        sh.parent().unwrap().display().to_string()
    }

    fn args_of(cmd: &Command) -> Vec<&OsStr> {
        cmd.get_args().collect()
    }

    #[cfg(unix)]
    #[test]
    fn argv_from_defaults() {
        // `docker` isn't required to be installed: point it at `sh`.
        let config = system_config("docker:\n  programs:\n    exec: sh\n", &sh_dir());
        let argv =
            broker_argv(&config, "dev-container", "/usr/local/bin/scoter-container").unwrap();
        assert!(argv[0].ends_with("/sh"), "{argv:?}");
        assert_eq!(
            argv[1..],
            [
                "exec",
                "-i",
                "dev-container",
                "/usr/local/bin/scoter-container"
            ]
        );
    }

    #[cfg(unix)]
    #[test]
    fn argv_from_configured_command() {
        let config = system_config(
            "docker:\n  programs:\n    exec: sh\n  runs:\n    broker: ${exec} --context remote exec -i {x:container} {x:broker}\n",
            &sh_dir(),
        );
        let argv = broker_argv(&config, "c1", "/opt/broker").unwrap();
        assert_eq!(
            argv[1..],
            ["--context", "remote", "exec", "-i", "c1", "/opt/broker"]
        );
    }

    #[test]
    fn argv_errors() {
        let config = system_config(
            "docker:\n  programs:\n    exec: no-such-container-tool\n",
            "/nonexistent",
        );
        assert_eq!(
            broker_argv(&config, "c1", "/b").unwrap_err(),
            "cannot find the program 'no-such-container-tool' (set by `docker.programs.exec`)",
        );
        assert!(
            broker_argv(&config, "--privileged", "/b")
                .unwrap_err()
                .starts_with("invalid container name")
        );
    }

    #[cfg(unix)]
    #[test]
    fn argv_unknown_placeholder() {
        let config = system_config(
            "docker:\n  programs:\n    exec: sh\n  runs:\n    broker: ${exec} {x:other}\n",
            &sh_dir(),
        );
        assert_eq!(
            broker_argv(&config, "c1", "/b").unwrap_err(),
            "`docker.runs.broker`: no value for `{x:other}`"
        );
    }

    #[test]
    fn command_from_argv() {
        let argv: Vec<String> = ["docker", "--context", "remote", "exec"]
            .map(String::from)
            .to_vec();
        let cmd = broker_command(&argv).unwrap();
        assert_eq!(cmd.get_program(), "docker");
        assert_eq!(args_of(&cmd), ["--context", "remote", "exec"]);
        assert_eq!(
            broker_command(&[]).err().unwrap().kind(),
            io::ErrorKind::InvalidInput
        );
    }

    #[test]
    fn valid_names() {
        for name in ["dev-container", "a", "A1", "my_env.2", "0123abcdef"] {
            assert_eq!(validate_name(name), Ok(()), "{name}");
        }
    }

    #[test]
    fn invalid_names() {
        for name in [
            "",
            "-it",
            "--privileged",
            "_x",
            ".x",
            "a b",
            "a/b",
            "a;b",
            "ünï",
        ] {
            assert!(validate_name(name).is_err(), "{name}");
        }
    }

    #[test]
    fn launch_missing_tool() {
        assert!(launch(&["/nonexistent/docker".to_string()]).is_err());
    }

    #[cfg(unix)]
    #[test]
    fn launch_connects_pipes() {
        use std::io::{Read, Write};
        // `sh -c cat ...` runs `cat`; the remaining arguments become $0.. and are ignored.
        let config = system_config(
            "docker:\n  programs:\n    exec: sh\n  runs:\n    broker: ${exec} -c cat {x:container} {x:broker}\n",
            &sh_dir(),
        );
        let mut broker = launch(&broker_argv(&config, "c1", "/b").unwrap()).unwrap();
        broker.stdin.write_all(b"ping").unwrap();
        drop(broker.stdin);
        let mut out = Vec::new();
        broker.stdout.read_to_end(&mut out).unwrap();
        assert_eq!(out, b"ping");
        assert!(broker.child.wait().unwrap().success());
    }
}
