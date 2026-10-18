# Dev Container Attack Vectors

The weak points of [the development container](dev-container.md).  Contributors should treat changes to the code listed here as security-sensitive, and the red team can use each vector as a starting point.  See the [attack vector overview](attack-vectors.md) for how to read the severity and status labels.

This analysis starts from one assumption: **anything inside the dev container may be hostile.**  The question isn't whether code in the container can be trusted, but what a compromised container can reach, and which of its walls the project gets to build.


## Attacker goals

* **Escape to the host** through files `scoter` writes, files the container leaves behind, or the build.
* **Widen the container's own access** through values the project supplies.
* **Hijack the developer's session** inside the container: the IDE, terminals and bridged commands.


## Attack surface

| Input | Controlled by | Handled in |
| --- | --- | --- |
| `dev-env.yaml` values | The project | `parse` and `text_field` in [`dev_env.rs`](../../host/src/config/dev_env.rs); `_setup_ssh_config` and `_write_docker_compose_env_file` in [`main_run.py`](../../scripts/main_run.py); [`ssh_config.py`](../../scripts/surf_scoter_lib/ssh_config.py) |
| `Dockerfile`, build context, parent directories | The project | `_build_containers` in [`main_run.py`](../../scripts/main_run.py); `docker: runs: build` in [`setup.yaml`](../../defaults/setup.yaml) |
| Image contents (`USER`, `/etc/group`, binaries) | The project | [`docker-compose.yaml`](../../defaults/docker-compose.yaml), which doesn't override them |
| Workspace files, including permission bits | Code in the container | The host file system, and whatever later reads it |
| Processes and files inside the container | Code in the container | The [runtime constraints](dev-container.md#runtime-constraints) |
| The IDE's remote session | Code in the container (server side) | The IDE |


## Summary

| ID | Vector | Severity | Status |
| --- | --- | --- | --- |
| [DEV-1](#dev-1-user-is-written-into-the-ssh-configuration) | `user` is written into the SSH configuration | Critical | Open |
| [DEV-2](#dev-2-mount-points-are-written-into-the-compose-env-file) | Mount points are written into the compose `.env` file | Critical | Open |
| [DEV-3](#dev-3-image-builds-run-outside-the-isolation) | Image builds run outside the isolation | High | Open |
| [DEV-4](#dev-4-host-ids-under-rootful-docker) | Host ids under rootful Docker | High | Open |
| [DEV-5](#dev-5-the-ide-session-as-a-channel-to-the-host) | The IDE session as a channel to the host | High | Unverified |
| [DEV-6](#dev-6-the-image-chooses-the-container-user) | The image chooses the container user | Medium | Open |
| [DEV-7](#dev-7-setuid-files-that-run-as-the-developer) | Setuid files that run as the developer | Medium | Open |
| [DEV-8](#dev-8-no-separation-inside-the-container) | No separation inside the container | Medium | By design |
| [DEV-9](#dev-9-unpinned-base-images) | Unpinned base images | Medium | Open |
| [DEV-10](#dev-10-loosened-permissions-on-workspace-files) | Loosened permissions on workspace files | Low | Open |
| [DEV-11](#dev-11-subordinate-ids-under-rootless-runtimes) | Subordinate ids under rootless runtimes | Low | Open |
| [DEV-12](#dev-12-group-access-inside-the-image) | Group access inside the image | Low | Mitigated |
| [DEV-13](#dev-13-bridge-links-in-a-root-owned-directory) | Bridge links in a root-owned directory | Low | Unverified |
| [DEV-14](#dev-14-unbounded-writable-storage) | Unbounded writable storage | Low | Open |


## Background: how container ids reach the host

Dropping every capability and setting `no-new-privileges` take away root's special powers, but not ordinary file permissions.  Without any capabilities, a process in the container can still set the setuid bit on files it owns, change a file's group to any group it belongs to and set the setgid bit, and make files group- or world-writable.

Inside the container these bits do nothing, because `no-new-privileges` ignores setuid and setgid.  But files in the workspace are real host files, and on the host the bits work normally.  What they grant depends on how the runtime maps the container's ids:

| Runtime | Container user on the host | Container groups on the host |
| --- | --- | --- |
| Rootless Podman with `keep-id` (the default compose file) | The developer | The developer's primary group; any other group maps to one of the developer's subordinate ids |
| Rootless Docker | Container root is the developer; other users map to subordinate ids | Subordinate ids |
| Rootful Docker, without `userns-remap` | The same number on the host, which may be a different account | The same numbers on the host |

DEV-4, DEV-7, DEV-10 and DEV-11 follow from this.  The recommended way of bringing changes back, `git fetch`, carries only file contents and the executable bit, so these vectors need the files to be used where they lie.


## Vectors

### DEV-1: `user` is written into the SSH configuration

**Severity:** Critical.  **Status:** Open.
**Where:** `text_field` in [`dev_env.rs`](../../host/src/config/dev_env.rs); `_setup_ssh_config` in [`main_run.py`](../../scripts/main_run.py); `add_host` and `add_key_value` in [`ssh_config.py`](../../scripts/surf_scoter_lib/ssh_config.py).

`user` goes into the developer's `~/.ssh/config`, in the container's `Host` line and its `User` line, without escaping.  The only check is that it's a non-empty string.  A value containing a newline adds arbitrary directives, such as a `Match all` block with a `ProxyCommand`, which runs on the host the next time the developer uses `ssh` for anything.  A value containing spaces adds extra `Host` patterns.  This happens before any container starts.

**Red team:** a `dev-env.yaml` with `user: "ubuntu\nMatch all\n  ProxyCommand ..."`, delivered in a cloned project or a parent `dev-env.yaml`.

**When changing this code:** validate every value written to the SSH configuration against a strict pattern, and escape it as well.  The [open questions](#open-design-questions) propose splitting `user`.

### DEV-2: Mount points are written into the compose `.env` file

**Severity:** Critical.  **Status:** Open.
**Where:** `text_field` in [`dev_env.rs`](../../host/src/config/dev_env.rs); `_write_docker_compose_env_file` in [`main_run.py`](../../scripts/main_run.py).

`mounts: workspace` and `mounts: setup` go into the `.env` file unescaped.  The only check is a leading `/`.  A newline adds a line, and compose takes the last value for a repeated name (confirmed with podman-compose 1.6.0), so `"/workspace\nWORKSPACE_HOST=/home/you"` mounts the developer's home directory into the container, read-write.

**Red team:** override `WORKSPACE_HOST` or `SETTINGS_HOST` this way.  Repeat with Docker Compose, whose `.env` parsing may differ.  Try `:` and `,` in mount points to change mount options.

**When changing this code:** refuse control characters, `:` and `,` in mount points, and escape every value written to `.env`.  Any new value that flows from `dev-env.yaml` into a host-side file has the same exposure.

### DEV-3: Image builds run outside the isolation

**Severity:** High.  **Status:** Open.
**Where:** `_build_containers` in [`main_run.py`](../../scripts/main_run.py); `docker: runs: build` in [`setup.yaml`](../../defaults/setup.yaml).

None of the [required network properties](network.md#required-properties) hold during a build.  `RUN` steps have the builder's ordinary network access, including the LAN and cloud metadata endpoints, and can bake anything into the image.  The build context is sent to the builder in full: a dev container directory at the project root sends untracked files like `.env`, and `COPY . .` puts them into image layers.  `parents` entries with `dir` can name directories outside the project, and those are built too.

**Red team:** a `Dockerfile` whose `RUN` steps reach the LAN, the metadata endpoint and the internet.  A project layout that pulls secrets into the build context.  A `dir` parent pointing outside the project.

**When changing this code:** consider `--network=none` or building through the gateway, and limiting `dir` parents to the project.

### DEV-4: Host ids under rootful Docker

**Severity:** High.  **Status:** Open.
**Where:** [`docker-compose.yaml`](../../defaults/docker-compose.yaml) (no `user:`, no `group_add`); the image's `USER` and `/etc/group`; see [background](#background-how-container-ids-reach-the-host).

Without a user namespace, container ids are host ids.  A container running as root (see DEV-6) can leave a setuid-root program in the workspace.  A container whose user belongs to a group whose number matches a privileged host group (`docker`, `disk`, `adm`, `shadow`) can leave a setgid program for that group.  Anyone who runs either on the host gains those privileges: `docker` or root means the whole host.  The project can't see the host's group numbers, but common ones are easy to guess.

**Red team:** under rootful Docker, from an image with `USER root` or with guessed group numbers in `/etc/group`, create setuid and setgid files in the workspace and check them from the host.

**When changing this code:** see the [open questions](#open-design-questions) on who chooses the ids.  A workspace check for setuid and setgid files would catch this before anyone runs them.

### DEV-5: The IDE session as a channel to the host

**Severity:** High.  **Status:** Unverified.
**Where:** IDE remote protocols (VS Code Remote - SSH, JetBrains Gateway); the IDE settings in the [main README](../../README.md#connecting-through-vs-code).

The IDE's server half runs in the container, and talks to its client half on the host.  Some extensions run on the host, the workspace can recommend extensions, and IDEs can open URLs, forward ports and read local files on the server's request.  What a hostile server can make the client do hasn't been analyzed.

**Red team:** with a modified IDE server or extension in the container, try to make the host client open URLs, install or enable extensions, forward ports, or run tasks locally.

**When changing this code:** any change to the recommended IDE settings needs this vector rechecked.

### DEV-6: The image chooses the container user

**Severity:** Medium.  **Status:** Open.
**Where:** [`docker-compose.yaml`](../../defaults/docker-compose.yaml), which sets no `user:`; the project's `Dockerfile`.

A project `Dockerfile` that ends with `USER root` runs the container as root.  Dropped capabilities and `no-new-privileges` still apply, but root can change files the image meant to protect, such as the SSH server configuration, and makes DEV-4 worse.  The [project Dockerfile guide](../project-dockerfile.md) says the run forces an unprivileged user; it doesn't.

**Red team:** run an image with `USER root` and list what it can change that `ubuntu` couldn't.

**When changing this code:** set `user:` in the compose file from host-checked values.

### DEV-7: Setuid files that run as the developer

**Severity:** Medium (shared computers).  **Status:** Open.
**Where:** the workspace mount; see [background](#background-how-container-ids-reach-the-host).

With `keep-id`, files the container creates are owned by the developer's host account.  A setuid bit on one hands that identity to any local account that can run it.  A home directory with mode `0700` or `0750` blocks other accounts from reaching it.

**Red team:** on a shared host, leave a setuid program in the workspace and run it as another account.

**When changing this code:** a workspace check, as above, would catch this.

### DEV-8: No separation inside the container

**Severity:** Medium.  **Status:** By design.
**Where:** the container as a whole.

The IDE server, terminal sessions, builds, agents and bridged commands all run as the same user.  Any of them can read the others' files, environment and sockets, and may be able to trace them, depending on the host's `ptrace` policy.

**Red team:** read IDE session tokens and other processes' environments; try to attach to the developer's shell or to a running bridged command to watch or inject input, such as an MFA prompt.

**When changing this code:** nothing secret should be placed in the container on the assumption that only the developer reads it.

### DEV-9: Unpinned base images

**Severity:** Medium.  **Status:** Open.
**Where:** [`containers/dev-env/Dockerfile`](../../containers/dev-env/Dockerfile) (`ubuntu:rolling`, `rust:slim-trixie`).

Tags, not digests, so the same `Dockerfile` can produce a different image from one build to the next.

**Red team:** supply-chain scenarios against the base images and the packages installed on them.

**When changing this code:** pin by digest, and update the pins deliberately.

### DEV-10: Loosened permissions on workspace files

**Severity:** Low.  **Status:** Open.
**Where:** the workspace mount; see [background](#background-how-container-ids-reach-the-host).

The container can make workspace files, including `.git`, group- or world-writable, or create setgid directories so files the developer adds later get a group it chose.  Other local accounts could then change the workspace between sessions.

**Red team:** on a shared host, loosen permissions from the container, then change the workspace as another account before the next session.

### DEV-11: Subordinate ids under rootless runtimes

**Severity:** Low.  **Status:** Open.
**Where:** the workspace mount; see [background](#background-how-container-ids-reach-the-host).

A setuid or setgid file owned by one of the developer's subordinate ids gives that id to whoever runs it.  The developer's other rootless container storage (under `~/.local/share/containers`) is owned by those same ids, so another local account could use this to tamper with other images.

**Red team:** create such a file, then use it as another account against the developer's container storage.

### DEV-12: Group access inside the image

**Severity:** Low.  **Status:** Mitigated.
**Where:** the image's `/etc/group`; the [runtime constraints](dev-container.md#runtime-constraints).

Groups such as `root`, `staff`, `shadow` and `adm` can open image files.  Every process already runs as the same user, the writable layer is discarded when the environment stops, `sudo` and `su` can't raise privileges under `no-new-privileges`, and device groups have no devices to open without `CAP_MKNOD`.  The setup directory is mounted read-only.

**Red team:** confirm that no group membership reaches anything outside the container.

**When changing this code:** keep `no-new-privileges` and `cap_drop: [ALL]`; this vector depends on both.

### DEV-13: Bridge links in a root-owned directory

**Severity:** Low.  **Status:** Unverified.
**Where:** [`entrypoint.sh`](../../containers/dev-env/entrypoint.sh); [`containers/dev-env/Dockerfile`](../../containers/dev-env/Dockerfile).

The start-up script runs as `ubuntu` and links into `/usr/local/bin`, which the base image leaves owned by root.  As built, the link should fail and stop the container whenever bridge commands are listed.  A project that makes `/usr/local/bin` writable to work around it lets code in the container replace the client binary.  That doesn't cross the host's checks.

**Red team:** start an environment with bridge commands on the base image, and with a writable `/usr/local/bin`.

### DEV-14: Unbounded writable storage

**Severity:** Low.  **Status:** Open.
**Where:** [`docker-compose.yaml`](../../defaults/docker-compose.yaml) (no `read_only`, no size-limited `tmpfs`).

Code in the container can fill the disk that backs the container's writable layer, and the workspace's disk.

**Red team:** fill the disk and observe the effect on the host.


## Sensitive code

* [`host/src/config/dev_env.rs`](../../host/src/config/dev_env.rs): validation of every `dev-env.yaml` value.
* [`scripts/main_run.py`](../../scripts/main_run.py): everything written to `~/.ssh/config`, `known_hosts`, the `.env` file and the setup directory, and the image builds.
* [`scripts/surf_scoter_lib/ssh_config.py`](../../scripts/surf_scoter_lib/ssh_config.py): the SSH configuration writer.
* [`defaults/docker-compose.yaml`](../../defaults/docker-compose.yaml): the `dev` service's user, capabilities, mounts and limits.
* [`containers/dev-env/`](../../containers/dev-env/): the base image, start-up script and `sshd_config`.


## Open design questions

**The container's identity**

* Should `dev-env.yaml` split `user` into two fields?
    * `uid` and `gid`, as integers, passed to the compose file as `user: "UID:GID"` so the image's `USER` stops mattering.  An image can map any name to uid 0, but an integer can be checked from the host: refuse 0, and perhaps anything below 1000, the usual range for system accounts.
    * `user`, kept only as the SSH login name, limited to a POSIX user name such as `[a-z_][a-z0-9_-]*`.  The `sshd_config` template would then take its `AllowUsers` from it, instead of hard-coding `ubuntu`.
* Should the compose file set `group_add` explicitly, so the image's `/etc/group` can't add supplementary groups?  How Docker and Podman handle supplementary groups for a numeric `user:` needs verification.
* Should the host choose the ids rather than the project?  Under rootful Docker, a project-chosen `gid` is a host group number (DEV-4).  One option: the host supplies the developer's own uid and gid, as `keep-id` does, and `scoter` refuses an image whose declared ids don't match.  Another: require rootless runtimes or `userns-remap`.

**Values written to host files**

* Should `scoter` refuse `:`, `,` and control characters in mount points, and escape everything it writes into `~/.ssh/config` and `.env` regardless of validation?

**The workspace**

* Should `scoter` check the workspace when an environment stops and before it fetches, flagging setuid and setgid files, files whose group isn't the developer's, and anything group- or world-writable?  For example: `find WORKSPACE \( -perm -4000 -o -perm -2000 -o -perm -o+w -o ! -user "$USER" -o ! -group "$(id -gn)" \) -print`.

**The image and runtime**

* Should the bridge links be made in a user-owned directory on the `PATH`, or at image build time?
* Should builds run with `--network=none`, or through the gateway?
* Should the base images be pinned by digest?
* Should `sshd_config` use `AllowTcpForwarding local`, so that remote forwarding is refused by the setting itself, rather than by `PermitListen` alone?
* Should the compose file add `read_only: true` with size-limited `tmpfs` mounts for the directories that need to be writable?
