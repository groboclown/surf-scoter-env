//! Starts the broker inside the container, connected over its stdin / stdout.
//!
//! The command is `<container.command...> exec -i <container name> <container.broker>`.
//! No TTY is requested: it would mangle the binary protocol.

use std::io;
use std::process::{Child, ChildStdin, ChildStdout, Command, Stdio};

use super::config::ContainerConfig;


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

/// Build the command that runs the broker in `container`.
/// Its stdin / stdout are piped; stderr goes to the host's stderr.
pub(crate) fn broker_command(config: &ContainerConfig, container: &str) -> Command {
    let mut cmd = Command::new(&config.command[0]);
    cmd.args(&config.command[1..])
        .args(["exec", "-i", container, config.broker.as_str()])
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::inherit());
    cmd
}

/// Start the broker in `container`.
pub(crate) fn launch(config: &ContainerConfig, container: &str) -> io::Result<Broker> {
    validate_name(container).map_err(|msg| io::Error::new(io::ErrorKind::InvalidInput, msg))?;
    let mut child = broker_command(config, container).spawn()?;
    let stdin = child.stdin.take().expect("stdin is piped");
    let stdout = child.stdout.take().expect("stdout is piped");
    Ok(Broker { child, stdin, stdout })
}


#[cfg(test)]
mod tests {
    use super::*;
    use std::ffi::OsStr;

    fn config(command: &[&str]) -> ContainerConfig {
        ContainerConfig {
            command: command.iter().map(|s| s.to_string()).collect(),
            broker: "/usr/local/bin/scoter-container".to_string(),
        }
    }

    fn args_of(cmd: &Command) -> Vec<&OsStr> {
        cmd.get_args().collect()
    }

    #[test]
    fn command_for_docker() {
        let cmd = broker_command(&config(&["docker"]), "dev-container");
        assert_eq!(cmd.get_program(), "docker");
        assert_eq!(args_of(&cmd), ["exec", "-i", "dev-container", "/usr/local/bin/scoter-container"]);
    }

    #[test]
    fn command_keeps_leading_arguments() {
        let cmd = broker_command(&config(&["docker", "--context", "remote"]), "c1");
        assert_eq!(cmd.get_program(), "docker");
        assert_eq!(args_of(&cmd), ["--context", "remote", "exec", "-i", "c1", "/usr/local/bin/scoter-container"]);
    }

    #[test]
    fn command_uses_configured_broker() {
        let mut cfg = config(&["podman"]);
        cfg.broker = "/opt/broker".to_string();
        let cmd = broker_command(&cfg, "c1");
        assert_eq!(cmd.get_program(), "podman");
        assert_eq!(args_of(&cmd).last().unwrap(), &"/opt/broker");
    }

    #[test]
    fn valid_names() {
        for name in ["dev-container", "a", "A1", "my_env.2", "0123abcdef"] {
            assert_eq!(validate_name(name), Ok(()), "{name}");
        }
    }

    #[test]
    fn invalid_names() {
        for name in ["", "-it", "--privileged", "_x", ".x", "a b", "a/b", "a;b", "ünï"] {
            assert!(validate_name(name).is_err(), "{name}");
        }
    }

    #[test]
    fn launch_rejects_invalid_name() {
        let err = launch(&config(&["docker"]), "--privileged").err().unwrap();
        assert_eq!(err.kind(), io::ErrorKind::InvalidInput);
    }

    #[test]
    fn launch_missing_tool() {
        assert!(launch(&config(&["/nonexistent/docker"]), "c1").is_err());
    }

    #[cfg(unix)]
    #[test]
    fn launch_connects_pipes() {
        use std::io::{Read, Write};
        // `sh -c cat exec -i c1 BROKER` runs `cat`; the remaining arguments become $0.. and are ignored.
        let mut broker = launch(&config(&["sh", "-c", "cat"]), "c1").unwrap();
        broker.stdin.write_all(b"ping").unwrap();
        drop(broker.stdin);
        let mut out = Vec::new();
        broker.stdout.read_to_end(&mut out).unwrap();
        assert_eq!(out, b"ping");
        assert!(broker.child.wait().unwrap().success());
    }
}
