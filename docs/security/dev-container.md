# The Development Container

The development container is where your tools run, and so it's where code you didn't write runs: dependencies, build plugins, test fixtures, and agents.  Every other part of Surf Scoter is arranged around it.  The network decides what it can reach, the mounts decide what it can see, and the host bridge decides what it can ask for.

This document describes what the container is made of and how it's run.  [Dev container attack vectors](dev-container-attack-vectors.md) analyzes what a compromised container could do.


## Why it's special

* **It runs untrusted code.**  The firewall and gateway containers run fixed programs.  The dev container runs whatever the project and its dependencies bring.
* **The project supplies it.**  The firewall and gateway come from Surf Scoter or from your own settings.  The dev container comes from the project's `Dockerfile` and `dev-env.yaml`, which arrive with the code you clone.
* **Every channel ends there.**  The workspace mount, the setup mount, the SSH connection, the host bridge's broker and the proxy all meet in this one container.


## Who controls what

| Piece | Comes from | What it decides |
| --- | --- | --- |
| [Base image](../../containers/dev-env/Dockerfile) | Surf Scoter | The broker and client binaries, the SSH server and its configuration, the start-up script, the default user |
| Project `Dockerfile` | The project | Everything installed on top, or a different base entirely |
| `dev-env.yaml` (and its parents) | The project | The user name, mount points, broker location and SSH port |
| Capabilities, limits, networks, mounts | Your [settings](settings.md) (the compose file) | What the running container is allowed to do |
| The setup directory | `scoter`, on your host | SSH keys, allowed domains, the bridge command list |
| The workspace | The container itself | Everything in it, including `.git` |

The split matters.  The project can change anything *inside* the image, but it can't add capabilities, networks, ports or mounts.  Those come from the compose file, which the project doesn't supply.  The protections that hold up against a hostile project are the ones outside the image: the [network](network.md), the [mounts](files.md), the [host bridge's checks](host-bridge.md), and your settings.


## The base image

[`containers/dev-env`](../../containers/dev-env/) builds in two stages:

1. A Rust stage compiles and tests the bridge's container side: `scoter-client` and the broker, `scoter-container`.
2. The runtime stage, on `ubuntu:rolling`, adds those binaries, `openssh-server`, `socat` (for the SSH proxy), CA certificates, and a few basic tools (`git`, `mercurial`, `curl`, `bash`).  It sets up the SSH configuration template for the unprivileged `ubuntu` user (uid 1000), and ends with `USER ubuntu`.

The [start-up script](../../containers/dev-env/entrypoint.sh) then:

1. writes the SSH server configuration with the port from `SCOTER_SSHD_PORT`;
2. copies `authorized_keys` and the SSH host key from the read-only setup directory;
3. hard-links `scoter-client` into `/usr/local/bin` under each name in `bridge-cmds.txt`;
4. replaces itself with `sshd`, running as the container user.


## Building the image

`scoter run` builds the image on your host with the `docker build` command from your [settings](settings.md).  It builds local parent images first, then the project's own `Dockerfile`, using the dev container directory as the build context.

A build is not an environment run.  `RUN` steps execute in the builder with its ordinary network access, outside the firewall and gateway, and none of the runtime limits apply.


## Runtime constraints

The default [compose file](../../defaults/docker-compose.yaml) runs the dev container with:

| Setting | Effect |
| --- | --- |
| `networks: [isolated]` | No route out except the gateway ([N1](network.md#required-properties)) |
| `cap_drop: [ALL]` | No Linux capabilities, even for root in the container |
| `no-new-privileges` | `setuid` programs such as `sudo` can't raise privileges |
| `pids_limit`, `mem_limit`, `memswap_limit`, `cpus` | Fork bombs and runaway builds stay contained |
| Logging `max-size` | Container logs can't fill the disk |
| `userns_mode: keep-id` (Podman) | The container user maps to your host user, so workspace files stay yours |
| Workspace mount, read-write | The project clone; see [file access](files.md) |
| Setup mount, read-only | Keys and lists the container mustn't change |

The compose file does **not** set a read-only root file system, size limits on writable storage, a `user:`, or custom seccomp or AppArmor profiles.  The runtime's default profiles apply.  The container never gets the container runtime's socket, your home directory, or any published port.


## Ways in

### SSH

[`sshd_config`](../../containers/dev-env/sshd_config) is set up for one user connecting from your host through the runtime's exec channel (see [the network](network.md#inbound-ssh-through-the-runtime)):

* It listens only on `127.0.0.1` inside the container.
* It accepts only public key authentication, only for the `ubuntu` user, never root, without PAM.
* It turns off agent forwarding, X11 forwarding, tunnels and Unix socket forwarding, and refuses remote listeners (`PermitListen none`).
* It allows TCP forwarding, for IDE port forwarding.
* It sets the proxy variables for SSH sessions, which start with a fresh environment.
* It logs at `VERBOSE`.

On the host side, `scoter` gives each launch a host key in the setup directory and writes it into your `known_hosts` under the container's name, with `StrictHostKeyChecking yes`.  Your client key (`~/.ssh/scoter-dev-container` by default) has no passphrase.  It only opens dev containers, and never enters one.

### The container runtime

Anyone who can use the container runtime can `docker exec` into the container.  That's you, and it's how both the SSH proxy and the host bridge reach it.

### The broker socket

The host bridge's broker listens on `/tmp/container-uds.sock` with mode `0600`.  Every process in the container runs as the same user, so every process can use the bridge.  The host's checks are what limit it; see [the host bridge](host-bridge.md).


## The `dev-env.yaml` values

The project's [`dev-env.yaml`](../project-dockerfile.md#the-dev-envyaml-file) tells `scoter` how to run the container.  Some of its values stay inside the container, and some are written into files on your host:

| Value | Checked for | Used in |
| --- | --- | --- |
| `user` | A non-empty string | Your `~/.ssh/config` (`Host` and `User` lines) |
| `mounts: workspace`, `mounts: setup` | A string starting with `/` | The compose `.env` file |
| `broker-bin` | A string starting with `/` | The command that starts the broker inside the container |
| `sshd-port` | A port number | The compose `.env` file and `~/.ssh/config` |
| `sshd-port-env` | A valid environment variable name | The compose file, as a variable in the container |

`dev-container: NAME` in the project configuration searches the project's directories before the installation's, so a project can supply its own `dev-env`.
