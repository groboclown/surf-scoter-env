#SPDX:MIT
"""Surf Scoter Sub-command: run"""
import argparse
import datetime
import json
import os
import re
import shlex
import subprocess  # nosec: B404  #  We know the issues around running subprocesses.
import sys
import tempfile
import time
from collections.abc import Sequence
from pathlib import Path

from surf_scoter_lib import (
    BAD_EXECUTION_ARGS_CODE,
    UserConfig,
    defaults,
    fileutil,
    gen_parser,
    generator,
    jdata,
)
from surf_scoter_lib.active_env import ActiveEnvironmentSet, EnvironmentDesc
from surf_scoter_lib.err import ConfigErr, VerifiedStoreErr
from surf_scoter_lib.ssh_config import SshConfig
from surf_scoter_lib.verified import staging

# The network gateway's configuration, in the resources directory.
GATEWAY_CONFIG_FILE = "gateway-config"

# Where scoter-host's copy of the configuration goes, under `scoter: paths: state-dir`.
SUC_COPY_DIR = "environments"
SUC_COPY_FILE = "suc.json"
# The accepted copy of the project's files, staged to build from, beside it.
BUILD_DIR = "build"

NOT_ACCEPTED = (
    "You haven't accepted this project's settings.  "
    "Run `scoter verify` to review them, then run again."
)

# ---------------------------------------------------------------------
# Start and manage a dev environment.


def run_env_runner(config: UserConfig, args: argparse.Namespace) -> int:
    """Start the dev environment."""
    # Nothing runs from a project the user hasn't accepted: not its host
    # commands, its egress rules, or its container.
    if config.accepted_settings_file() is None:
        print(NOT_ACCEPTED, file=sys.stderr)
        return 1
    vcs_section = config.get_vcs_section()

    # Prepare the active environment set for writing.
    # Note that this requires explicit "close" mechanics when successfully used.
    active_envs = ActiveEnvironmentSet.open(
        config.require_path("dev-env", "running-env-config"),
        config.require_path("dev-env", "running-env-config-lock"),
    )
    # Set once reserved: the environment's directory is named after it.
    host = ""
    try:
        # -- Discovery
        # Configure the sshd port.  It might be dynamic.
        _configure_sshd_port(config, active_envs)

        # Setup the SSH config file for running.
        host_prefix = _setup_ssh_config(config)
        if not host_prefix:
            active_envs.close()
            return 1

        # Discover the host suffix to use.
        host_suffix = active_envs.reserve_environ(host_prefix)
        host = host_prefix + host_suffix

        # -- Stage the accepted copy of the project's files to build from.
        plan = _stage_accepted(config, host)
        if plan is None:
            active_envs.close()
            return 1

        # -- Build the containers
        image_tags = _build_containers(config, host, plan) # (dev-env, firewall, gateway)
        if not image_tags:
            _remove_environment_dir(config, host)
            active_envs.close()
            return 1

        # Remember to mark the work directories as ignored.

        # -- workspace directory
        # Setup the workspace directory via the vcs command.
        workspace_dir = _setup_workspace(
            config, vcs_section, args.clean_workspace, args.workspace_fetch
        )
        _ignore_directory(config, vcs_section, workspace_dir)

        # -- settings directory
        setup_dir = config.active_location("dev-container-settings-dir")
        if setup_dir is None:
            setup_dir = config.require_path("workspace", "setup-dir")
            config.set_active_location("dev-container-settings-dir", setup_dir)
        setup_dir.mkdir(exist_ok=True)
        _ignore_directory(config, vcs_section, setup_dir)

        # Write the authorized keys file.
        key_file = config.require_path("ssh", "key-file")
        pub_key_file = Path(str(key_file) + defaults.PUB_KEY_EXT)
        # Ensure they exists.
        if not key_file.is_file() or not pub_key_file.is_file():
            raise ConfigErr(
                f"Your ssh key pair ({key_file} and {pub_key_file}) don't exist.  "
                f"Make sure you ran user setup first."
            )
        pub_key_file.copy(setup_dir / defaults.AUTHORIZED_KEYS_FILE_NAME)
        fileutil.touch(setup_dir / defaults.AUTHORIZED_KEYS_FILE_NAME, 0o666)

        # Create the host key.
        host_key_file = setup_dir / defaults.CONTAINER_HOST_KEY_FILE_NAME
        if not host_key_file.exists():
            print("Creating host key...")
            config.run_command(
                "ssh", "mk-host-key", capture_stdout=True,
                template={"host-key-file": str(host_key_file)},
            )
        fileutil.touch(host_key_file, 0o666)
        # Add that as a known host
        # The file should already exist from the user setup.
        _add_known_host(config, host, host_key_file)

        # Write the host-bridge command list into the setup directory.
        with open(setup_dir / defaults.BRIDGE_COMMANDS_FILE_NAME, "w", encoding="utf-8") as inp:
            inp.write("\n".join(config.project_bridge_commands()))

        # -- resources directory
        resources_dir = config.require_path("workspace", "resources-dir")
        resources_dir.mkdir(exist_ok=True)
        _ignore_directory(config, vcs_section, resources_dir)

        # The network gateway's configuration, from the egress rules.
        gateway_config = _write_gateway_config(config, resources_dir / GATEWAY_CONFIG_FILE)

        # Setup the .env file in the resources directory.
        _write_docker_compose_env_file(
            config=config, file=resources_dir / defaults.COMPOSE_ENV_FILENAME,
            host=host, workspace_dir=workspace_dir,
            settings_dir=setup_dir, gateway_config=gateway_config,
        )

        # Setup the docker-compose file in the resources directory.
        config.require_path("dev-env", "docker-compose").copy(
            resources_dir / defaults.DOCKER_COMPOSE_FILENAME
        )

        # scoter-host's copy of the configuration: it decides what runs on
        # this computer, so it's private, and outside the project.
        universal_config_file = fileutil.write_private(
            config.require_path("scoter", "state-dir"),
            (SUC_COPY_DIR, host, SUC_COPY_FILE),
            config.dumps().encode("utf-8"),
        )
    except:
        print("Aborting execution.", file=sys.stderr)
        _remove_environment_dir(config, host)
        # Close without saving.
        active_envs.close()
        raise

    # -- Record this environment
    # If the save fails, then something grabbed the lock
    # from under us.
    if not active_envs.save(close=False):
        raise ConfigErr(
            "Lost our lock on the active environments.  "
            "Please wait longer before launching a second one."
        )
    active_envs.update_environ(EnvironmentDesc(
        host_prefix=host_prefix,
        host_suffix=host_suffix,

        # See _write_docker_compose_env_file
        dev_env_name=host,
        firewall_router_name=f"{host}-fr",
        network_gateway_name=f"{host}-ng",
        
        project_dir=str(config.require_active_location("project-dir")),
        resources_dir=str(resources_dir),
        workspace_dir=str(workspace_dir),
        setup_dir=str(setup_dir),
        compose_file=str(resources_dir / defaults.DOCKER_COMPOSE_FILENAME),
        compose_env_file=str(resources_dir / defaults.COMPOSE_ENV_FILENAME),
        dev_env_ssh_port=str(config.dev_container_sshd_port()),
        dev_env_ssh_user=config.dev_container_str("user"),
        created_at=datetime.datetime.now().isoformat(),
        suc=json.loads(config.dumps()),  # expensive deep copy
    ))
    active_envs.save(close=True)

    # -- start the containers
    # Start the docker-compose.yaml (daemon mode).
    print("Starting the environment cluster...")
    config.run_command(
        "docker", "compose-up",
        template={
            "env-file": str(resources_dir / defaults.COMPOSE_ENV_FILENAME),
            "compose-file": str(resources_dir / defaults.DOCKER_COMPOSE_FILENAME),
        },
        capture_stdout=False,
        capture_stderr=False,
    )

    try:
        # Tell the user the setup for the container (host name).
        print("==========================================")
        print(f"dev-container running as {host}")
        print(f"Connect to the container using `ssh ${host}`")
        print("Waiting for it to start...")
        if not _wait_for_container(config, host, timeout_sec=defaults.CONTAINER_START_TIME_SEC):
            print("Container didn't start in time.")
        else:
            print("Container started.")
            print("Running the host-bridge process.")
            print("Press 'ctrl-c' to stop and tear down the environment.")
            # Start scoter-host
            if args.bin_dir:
                scoter_host = fileutil.require_exe(Path(args.bin_dir) / defaults.SCOTER_HOST_EXEC)
            else:
                scoter_host = fileutil.require_exe(
                    Path(args.distribution_dir) / defaults.DISTRIBUTION_BIN_DIR / defaults.SCOTER_HOST_EXEC
                )
            print([scoter_host, "--universal", str(universal_config_file), host])
            res = subprocess.run(  # nosec: B603  # We checked the input above.
                [scoter_host, "--universal", str(universal_config_file), host],
                check=False,
                shell=False,
            )
            if res.returncode != 0:
                print("Failed running the host process.")

    except KeyboardInterrupt:
        print("User requested termination.")

    # -- Tear it all down.

    # Shut down the containers
    print("Stopping the environment cluster...")
    config.run_command(
        "docker", "compose-down",
        template={
            "env-file": str(resources_dir / defaults.COMPOSE_ENV_FILENAME),
            "compose-file": str(resources_dir / defaults.DOCKER_COMPOSE_FILENAME),
        },
    )

    # Remove from active mode.
    active_envs = ActiveEnvironmentSet.open(
        config.require_path("dev-env", "running-env-config"),
        config.require_path("dev-env", "running-env-config-lock"),
    )
    active_envs.rm_environ_by_host(host)
    active_envs.save(close=True)

    # scoter-host's copy of the configuration.
    fileutil.rm(universal_config_file.parent)

    # Clean up the setup and resource directories.
    #_rm(setup_dir)
    #_rm(resources_dir)
    # Keep the 'workspace' directory, though.

    return 0


def _environment_dir(config: UserConfig, host: str) -> Path:
    return config.require_path("scoter", "state-dir") / SUC_COPY_DIR / host


def _remove_environment_dir(config: UserConfig, host: str | None) -> None:
    if host:
        fileutil.rm(_environment_dir(config, host))


def _stage_accepted(config: UserConfig, host: str) -> staging.BuildPlan | None:
    """Stage the accepted copy of the project's files; None, having said why, if it can't be."""
    directory = _environment_dir(config, host)
    # A run that crashed may have left one behind.
    fileutil.rm(directory)
    build_dir = fileutil.private_dir(
        config.require_path("scoter", "state-dir"), (SUC_COPY_DIR, host, BUILD_DIR),
    )
    try:
        plan = staging.prepare(
            config, build_dir, generator.run, datetime.datetime.now(datetime.UTC),
        )
    except staging.StagingErr as err:
        print(err, file=sys.stderr)
        if err.reverify:
            print("Run `scoter verify` to review the project's settings, then run again.", file=sys.stderr)
        plan = None
    except (OSError, VerifiedStoreErr) as err:
        print(f"Couldn't stage the accepted copy of the project's files: {err}", file=sys.stderr)
        plan = None
    if plan is None:
        # `prepare` gives None only without accepted settings, checked at the start.
        fileutil.rm(directory)
    return plan


def _build_containers(
    config: UserConfig, host: str, plan: staging.BuildPlan,
) -> tuple[str, str, str] | None:
    # Build the dev-env container + firewall-router + network-gateway.
    ret = (
        config.require_value("dev-env", "dev-env-image-prefix") + host,
        config.require_value("dev-env", "firewall-router-image"),
        config.require_value("dev-env", "network-gateway-image"),
    )
    # The dev-env's local parents come first, lowest first.  The project's
    # build from the accepted copy (`plan`), never the live files.
    containers: list[tuple[str, str, Path | None]] = [
        ("dev-env parent", tag, path)
        for tag, path in plan.parents
    ]
    containers += [
        (
            "dev-env",
            ret[0],
            plan.dev_container,
        ),
        (
            "firewall-router",
            ret[1],
            config.require_path("dev-env", "firewall-router"),
        ),
        (
            "network-gateway",
            ret[2],
            config.require_path("dev-env", "network-gateway"),
        ),
    ]
    for kind,tag,path in containers:
        if not path:
            print(f"No build directory set for {kind} container", file=sys.stderr)
            return None
        print(f"Building {kind} image...")
        print(f"DEBUG {path}")
        res = config.run_command(
            "docker", "build",
            check=False,
            capture_stderr=True,
            template={
                "tag": tag,
                "dir": str(path),
            },
        )
        # TODO write the stderr + stdout to a file in the resources directory
        # so the user can inspect what happened.
        if res.returncode != 0:
            print(res.stdout.decode("utf-8"))
            print(res.stderr.decode("utf-8"), file=sys.stderr)
            print(f"Failed building {kind} image ({path})", file=sys.stderr)
            return None
    return ret


def _wait_for_container(config: UserConfig, host: str, timeout_sec: float) -> bool:
    # Wait for the host to start running.
    end_at = time.time() + timeout_sec
    while time.time() < end_at:
        # Sleep at the start, because this should be called while the
        # containers are starting.
        time.sleep(1.0)
        res = config.run_command(
            "docker", "inspect-container",
            capture_stdout=True,
            capture_stderr=True,
            template={"name": host},
        )
        if res.returncode == 125:
            # Not found.
            continue
        elif res.returncode != 0:
            raise subprocess.CalledProcessError(
                returncode=res.returncode,
                cmd=res.args,
                output=res.stdout,
                stderr=res.stderr,
            )

        try:
            data: jdata.JsonData = json.loads(res.stdout)
            if isinstance(data, list) and data:
                data = data[0]
            if not isinstance(data, dict):
                raise TypeError(f"unexpected JSON result: {res.stdout.decode('utf-8')}")
            state = data.get("State")
            print(f"host image state: {state}")
            if not isinstance(state, dict):
                # Assume not good state.
                continue
            if state.get("Running", False):
                return True
            if state.get("Status") == "exited":
                print(f"Host process exited quickly (exit code {state.get('ExitCode')})")
                print("=========== Log Start")
                config.run_command(
                    "docker", "image-logs",
                    capture_stdout=False,
                    capture_stderr=False,
                    check=False,
                    template={"name": host},
                )
                print("=========== Log End")
                return False
        except (json.JSONDecodeError, ValueError, TypeError):
            pass
    return False


def _setup_workspace(
    config: UserConfig,
    vcs_section: str | None,
    clean_workspace: bool,
    workspace_fetch: bool
) -> Path:
    # Setup the workspace directory.
    workspace_dir = config.active_location("dev-container-workspace-dir")
    if workspace_dir is None:
        workspace_dir = config.require_path("workspace", "workspace-dir")
        config.set_active_location("dev-container-workspace-dir", workspace_dir)
    if workspace_dir.exists():
        if clean_workspace:
            print(f"Cleaning out the workspace directory ({workspace_dir})")
            fileutil.rm(workspace_dir)
        elif workspace_fetch and vcs_section:
            print(f"Fetching your project into {workspace_dir} ...")
            config.run_command(
                vcs_section,
                "fetch-changes",
                cwd=workspace_dir,
            )
        else:
            print("Not copying project into the workspace.")
    if not workspace_dir.exists():
        if vcs_section:
            print("Cloning into the workspace")
            print(f"Cloning your project into {workspace_dir} ...")
            config.run_command(
                vcs_section,
                "local-clone",
                cwd=config.active_location("project-dir"),
                template={
                    "workspace-dir": str(workspace_dir),
                }
            )
        else:
            print("Not copying project into the workspace (no version control system defined).")
            workspace_dir.mkdir(parents=True)
    return workspace_dir


def _ignore_directory(config: UserConfig, vcs_section: str | None, path: Path) -> None:
    """Mark the directory as ignored by adding the name to the parent's ignore file."""
    if not vcs_section:
        # No ignore possible.
        return
    ignore_file_name = config.require_value(vcs_section, "ignore-file")
    if not ignore_file_name:
        # No ignore possible.
        return
    
    path.parent.mkdir(parents=True, exist_ok=True)
    ignore_file = path.parent / ignore_file_name
    ignore_lines: list[str]
    if ignore_file.exists():
        # load up the lines.
        with open(ignore_file, "r", encoding="utf-8") as out:
            # This includes newlines at the end.
            ignore_lines = out.readlines()
    else:
        ignore_lines = []

    path_pattern = f"/{path.name}/\n"
    if path_pattern not in ignore_lines:
        ignore_lines.append("\n")
        ignore_lines.append("# Surf Scoter work directory\n")
        ignore_lines.append(path_pattern)

        # Need to modify the file
        ignore_file.write_text("".join(ignore_lines), encoding="utf-8")


def _configure_sshd_port(config: UserConfig, envs: ActiveEnvironmentSet) -> None:
    port = config.dev_container_sshd_port()
    if port is not None:
        # Already set.
        return

    # Check the ssh config for all ports already declared,
    # and pick one that isn't active but has the same username.
    user = config.dev_container_str("user")
    all_config_ports: set[str] = set()
    available_config_ports: set[str] = set()
    ssh_config = SshConfig.read_ssh_config(config)
    if ssh_config:
        # It exists.  Find a port with the same username.
        # The port is baked into the host pattern expression.
        user_port_pattern = re.compile(
            r"^" +
            re.escape(config.require_value("ssh", "host-prefix")) +
            r"-(.*?)-" +
            r"-(\d+)-\*$"
        )
        for host in ssh_config.by_host:
            if not host:
                continue
            match = user_port_pattern.match(host)
            if match:
                all_config_ports.add(match.group(2))
                if ssh_config.get_host_key(host, "user") == user:
                    # We have a winner.
                    available_config_ports.add(match.group(1))
    # Eliminate active environments' port.
    for env in envs.environs.values():
        if (
            env.dev_env_ssh_port in available_config_ports
            and env.dev_env_ssh_user == user
        ):
            available_config_ports.remove(env.dev_env_ssh_port)
    ssh_port = ""
    if available_config_ports:
        # Reuse one.
        ssh_port = next(iter(available_config_ports))
    else:
        # get an available one
        start_sshd_port = config.require_value("dev-env", "start-sshd-port")
        try:
            ssh_port_i = int(start_sshd_port)
        except (ValueError, TypeError) as err:
            raise ConfigErr(
                f"invalid dev-env values start-sshd-port ({start_sshd_port}): "
                f"it must be an integer"
            ) from err
        while ssh_port_i < defaults.TOP_PORT_NUMBER:
            ssh_port = str(ssh_port_i)
            if ssh_port not in all_config_ports:
                break
            ssh_port_i += 1
    if not ssh_port:
        raise ConfigErr("no sshd listening port available")
    config.set_dev_container_sshd_port(ssh_port)


def _add_known_host(config: UserConfig, host: str, host_key_file: Path) -> None:
    # Add the host to the known-hosts file.
    # It should already exist from the user setup command.
    known_hosts_file = config.require_path("ssh", "known-hosts")
    print(f"Removing the {host} entry from {known_hosts_file}")
    config.run_command(
        "ssh", "remove-known-host",
        template={"host": host},
        # If the host doesn't exist, that's fine.
        capture_stdout=True,
        capture_stderr=True,
    )
    pub_key_file = Path(str(host_key_file) + defaults.PUB_KEY_EXT)
    pub_key = pub_key_file.read_text(encoding="utf-8")
    # Format is: type + ' ' + key + ' ' + optional comment
    parts = pub_key.split(" ")
    if len(parts) < 2:
        raise ConfigErr(f"Bad pub key file format ({pub_key_file})")
    # Added into the known-hosts file in the format:
    #   host + ' ' + type + ' ' + key
    known_hosts = known_hosts_file.read_text(encoding="utf-8")
    if known_hosts and known_hosts[-1] != "\n":
        known_hosts += "\n"
    known_hosts += f"{host} {parts[0]} {parts[1]}\n"
    known_hosts_file.write_text(known_hosts, encoding="utf-8")


def _setup_ssh_config(config: UserConfig) -> str | None:
    # Ensure the right SSH config host block exists.
    # It generates the host prefix.
    # This isn't 100% right, in that it doesn't check for all host matches.
    ssh_config = SshConfig.read_ssh_config(config)
    if not ssh_config:
        return None

    user = config.dev_container_str("user")
    port = config.dev_container_str("sshd-port")  # force a value here.
    key_file = config.require_path("ssh", "key-file")
    proxy = config.require_run("ssh", "proxy-command", template={"port": port})
    host_prefix = (
        config.require_value("ssh", "host-prefix") +
        user + "-" +
        port + "-"
    )
    host = host_prefix + "*"

    if ssh_config.has_host(host):
        errs: list[str] = []
        r_user = ssh_config.get_host_key(host, "user")
        if r_user != user:
            errs.append(f"  User {r_user}, requires {user}")
        r_identity = ssh_config.get_host_key(host, "IdentityFile")
        if not r_identity:
            errs.append("  No IdentityFile line")
        else:
            r_identity = os.path.expanduser(r_identity)
            if not os.path.samefile(r_identity, key_file):
                errs.append(f"  IdentityFile {r_identity}, requires {key_file}")
        r_proxy_raw = ssh_config.get_host_key(host, "ProxyCommand")
        if not r_proxy_raw:
            errs.append(f"  ProxyCommand requires {proxy}")
        else:
            r_proxy = shlex.split(r_proxy_raw)
            if r_proxy != proxy:
                errs.append(f"  ProxyCommand '{r_proxy_raw}', requires {proxy}")
        if errs:
            print(
                f"Your {config.require_path('ssh', 'key-file')} file "
                f"has issues that prevents execution:",
                file=sys.stderr,
            )
            for err in errs:
                print(err, file=sys.stderr)
            return None
        # It's all good.
    else:
        ssh_config.add_host(host)
        ssh_config.add_comment(host, f"For Surf Scoter dev container user {user} on port {port}")
        ssh_config.add_key_value(host, "User", user)
        ssh_config.add_key_value(host, "IdentityFile", str(key_file))
        ssh_config.add_key_value(host, "IdentitiesOnly", "yes")
        ssh_config.add_key_value(host, "StrictHostKeyChecking", "yes")
        ssh_config.add_key_value(host, "ProxyCommand", shlex.join(proxy))
        ssh_config_file = config.require_path("ssh", "config-file")
        fileutil.backup(ssh_config_file, config.require_value("scoter", "backup-file-extension"))
        ssh_config_file.write_text(ssh_config.output(), encoding="utf-8")
        # Ensure the file mode wasn't changed.
        fileutil.touch(ssh_config_file, 0o600)

    return host_prefix


def _write_gateway_config(config: UserConfig, output: Path) -> Path:
    """Write the network gateway's configuration from the egress rules, with the setup's
    `network-gateway: runs: write-config`; the file written.

    The gateway enforces what it can of each rule.  When it can't enforce one,
    it writes nothing, and the environment doesn't start: a rule is never
    dropped, or loosened into something broader.
    """
    with tempfile.TemporaryDirectory(prefix="surf-scoter-") as directory:
        universal = Path(directory) / "universal.json"
        fd = os.open(universal, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            out.write(config.dumps())
        res = config.run_command(
            "network-gateway", "write-config",
            check=False,
            template={"universal": str(universal), "output": str(output)},
        )
    try:
        report = json.loads(res.stdout or b"{}")
    except ValueError as err:
        raise ConfigErr(f"The network gateway's `write-config` printed something that isn't JSON: {err}") from err
    refused = report.get("refused") if isinstance(report, dict) else None
    if refused:
        print(
            f"The network gateway ({report.get('gateway')}) can't enforce these egress rules, "
            "so the environment won't start:",
            file=sys.stderr,
        )
        for entry in refused:
            rule = entry.get("rule", {}) if isinstance(entry, dict) else {}
            print(
                f"  * {rule.get('host')} ({rule.get('protocol')}, port {rule.get('port')}): "
                f"{entry.get('reason') if isinstance(entry, dict) else entry}",
                file=sys.stderr,
            )
    if res.returncode != 0:
        raise ConfigErr("Couldn't make the network gateway's configuration.")
    return output


def _write_docker_compose_env_file(
    *,
    config: UserConfig, file: Path,
    host: str, workspace_dir: Path, settings_dir: Path, gateway_config: Path,
) -> None:
    # Write all the settings to the docker-compose file.
    # 90% of this have well-defined meanings that all docker-compose must use.
    mounts = config.dev_container_mounts()
    if "workspace" not in mounts or "setup" not in mounts:
        raise ConfigErr(f"dev-env does not have expected mounts; has {mounts}")

    lines: dict[str, str] = {
        # Image tags
        #   from _build_containers above
        "FIREWALL_ROUTER": config.require_value("dev-env", "firewall-router-image"),
        "NETWORK_GATEWAY": config.require_value("dev-env", "network-gateway-image"),
        "DEV_ENV": config.require_value("dev-env", "dev-env-image-prefix") + host,

        # Container name
        "FIREWALL_ROUTER_NAME": f"{host}-fr",
        "NETWORK_GATEWAY_NAME": f"{host}-ng",
        "CONTAINER_NAME": host,

        # File paths
        "WORKSPACE_HOST": str(workspace_dir),
        "SETTINGS_HOST": str(settings_dir),
        "GATEWAY_CONFIG_HOST": str(gateway_config),

        # Mount points
        "WORKSPACE_CONTAINER": mounts["workspace"],
        "SETTINGS_CONTAINER": mounts["setup"],
        "GATEWAY_CONFIG_CONTAINER": config.require_value("network-gateway", "config-mount"),

        # Limits
        "PIDS_LIMIT": config.require_value("dev-env", "pids-limit"),
        "MEM_LIMIT": config.require_value("dev-env", "mem-limit"),
        "CPUS": config.require_value("dev-env", "cpus"),
        "LOGGING_MAX_SIZE": config.require_value("dev-env", "logging-max-size"),

        # Environment variables
        "SSHD_PORT_ENV": (config.dev_container_sshd_port_env() or "SSHD_PORT_ENV_UNSET"),
        "SSHD_PORT": (config.dev_container_sshd_port() or "not set"),
    }
    file.write_text("\n".join([f"{k}={v}" for k,v in lines.items()]), encoding="utf-8")


def main(argv: Sequence[str]) -> int:
    """Configure the CLI arguments."""
    config, argv, parser = gen_parser(
        argv,
        (
            "Start and monitor the project dev container.  Press 'ctrl-c' while it's "
            "running to terminate and clean up the environment."
        ),
    )
    if not config:
        return BAD_EXECUTION_ARGS_CODE
    parser.add_argument(
        "--clean-workspace",
        action="store_true",
        dest="clean_workspace",
        default=False,
        help=(
            "If the dev-env workspace directory already exists from a previous run, "
            "remove it and recreate it."
        ),
    )
    parser.add_argument(
        "--no-workspace-fetch",
        action="store_false",
        dest="workspace_fetch",
        default=True,
        help=(
            "If the workspace directory exists, then don't fetch updates into it "
            "from the project directory.  This can lead to the workspace directory "
            "becoming out-of-date.  If the workspace directory doesn't exist, it "
            "will still be populated from the project (essentially ignoring this "
            "flag)."
        ),
    )
    parser.add_argument(
        "--vs-settings",
        action="store",
        dest="vs_settings",
        help="Update your VS user settings file to run in this container.",
    )
    args = parser.parse_args(argv)
    return run_env_runner(config, args)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
