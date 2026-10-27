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
| `dev-env.yaml` values | The project | `parse` and `text_field` in [`dev_env.rs`](../../host/src/config/dev_env.rs); `_write_compose_env_file` in [`main_run.py`](../../scripts/main_run.py); `write_host` in [`ssh_hosts.py`](../../scripts/surf_scoter_lib/ssh_hosts.py); [`ssh_config.py`](../../scripts/surf_scoter_lib/ssh_config.py) |
| `Dockerfile`, build context, parent directories and their tags | The project, as accepted | `_build_containers` in [`main_run.py`](../../scripts/main_run.py); [`staging.py`](../../scripts/surf_scoter_lib/verified/staging.py); `docker: runs: build` in [`setup.yaml`](../../defaults/setup.yaml) |
| Image contents (`USER`, `/etc/group`, binaries) | The project | [`docker-compose.yaml`](../../defaults/docker-compose.yaml), which doesn't override them |
| Workspace files, including permission bits | Code in the container | The host file system, and whatever later reads it |
| The home directory, kept in the workspace | Code in the container, and the project's repository | [`entrypoint.sh`](../../containers/dev-env/entrypoint.sh) |
| Processes and files inside the container | Code in the container | The [runtime constraints](dev-container.md#runtime-constraints) |
| The IDE's remote session | Code in the container (server side) | The IDE |


## Summary

| ID | Vector | Severity | Status |
| --- | --- | --- | --- |
| [DEV-1](#dev-1-user-is-written-into-the-ssh-configuration) | `user` is written into the SSH configuration | Critical | Mitigated |
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
| [DEV-13](#dev-13-bridge-links-in-a-root-owned-directory) | Bridge links in a root-owned directory | Low | Closed |
| [DEV-14](#dev-14-unbounded-writable-storage) | Unbounded writable storage | Low | Open |
| [DEV-15](#dev-15-the-project-chooses-shared-image-tags) | The project chooses shared image tags | Medium | Open |
| [DEV-16](#dev-16-the-home-directory-persists-in-the-workspace) | The home directory persists in the workspace | Medium | Open |


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

**Severity:** Critical.  **Status:** Mitigated where it's written: `write_host` refuses a `user` that isn't a plain user name (letters, digits, `.`, `_` and `-`, at most 32), and the `Host` is no longer made from it (it's `${ssh:host-prefix}${scoter:project-id}`, checked against a strict pattern).  The generator still takes any non-empty string, so any new place that writes `user` into a host file needs its own check.  The review doesn't show `user` ([VER-3](verification-attack-vectors.md#ver-3-the-review-doesnt-show-everything-it-accepts)).
**Where:** `text_field` in [`dev_env.rs`](../../host/src/config/dev_env.rs); `write_host` and `host_name` in [`ssh_hosts.py`](../../scripts/surf_scoter_lib/ssh_hosts.py); `add_host` and `add_key_value` in [`ssh_config.py`](../../scripts/surf_scoter_lib/ssh_config.py).

`user` went into the developer's `~/.ssh/config`, in the container's `Host` line and its `User` line, without escaping.  The only check is that it's a non-empty string.  A value containing a newline adds arbitrary directives, such as a `Match all` block with a `ProxyCommand`, which runs on the host the next time the developer uses `ssh` for anything.  A value containing spaces adds extra `Host` patterns.  This happens before any container starts.

**Red team:** a `dev-env.yaml` with `user: "ubuntu\nMatch all\n  ProxyCommand ..."`, delivered in a cloned project or a parent `dev-env.yaml`.

**When changing this code:** validate every value written to the SSH configuration against a strict pattern, and escape it as well.  The [open questions](#open-design-questions) propose splitting `user`.

### DEV-2: Mount points are written into the compose `.env` file

**Severity:** Critical.  **Status:** Open.
**Where:** `text_field` in [`dev_env.rs`](../../host/src/config/dev_env.rs); `_write_compose_env_file` in [`main_run.py`](../../scripts/main_run.py).

`mounts: workspace` and `mounts: setup` go into the `.env` file unescaped.  The only check is a leading `/`.  A newline adds a line, and compose takes the last value for a repeated name (confirmed with podman-compose 1.6.0), so `"/workspace\nWORKSPACE_HOST=/home/you"` mounts the developer's home directory into the container, read-write.

Project verification doesn't close this.  The run uses the accepted `dev-env.yaml`, but:

* `dev-env.yaml` gets no control-character check: the generator's text checks cover only the project file and the review;
* the review doesn't show the mount points, or the contents of a new `dev-env.yaml` ([VER-3](verification-attack-vectors.md#ver-3-the-review-doesnt-show-everything-it-accepts));
* `scoter verify --with-exemption` accepts it without showing it at all ([VER-4](verification-attack-vectors.md#ver-4-exemptions-accept-what-nobody-reviewed)).

**Red team:** override `WORKSPACE_HOST` or `SETTINGS_HOST` this way.  Repeat with Docker Compose, whose `.env` parsing may differ.  Try `:` and `,` in mount points to change mount options.

**When changing this code:** refuse control characters, `:` and `,` in mount points, and escape every value written to `.env`.  Any new value that flows from `dev-env.yaml` into a host-side file has the same exposure.

### DEV-3: Image builds run outside the isolation

**Severity:** High.  **Status:** Open for the network; the build's inputs are now mitigated.
**Where:** `_build_containers` in [`main_run.py`](../../scripts/main_run.py); [`staging.py`](../../scripts/surf_scoter_lib/verified/staging.py); `docker: runs: build` in [`setup.yaml`](../../defaults/setup.yaml).

None of the [required network properties](network.md#required-properties) hold during a build.  `RUN` steps have the builder's ordinary network access, including the LAN and cloud metadata endpoints, and can bake anything into the image.

What gets built is narrower than it was.  The project's containers and their parents must be inside `.surf-scoter.d` or a `dev-containers-search` directory, and the ones inside `.surf-scoter.d` are built from the accepted copy, restored into a private staging directory, so the build context holds only the files that were reviewed, and a change to the live files after the check never reaches the build.  But a reviewed `Dockerfile` can still fetch anything at build time, and the review doesn't show `RUN` steps' effects; a change that matches the accepted one on meaning isn't reviewed at all ([VER-1](verification-attack-vectors.md#ver-1-the-dockerfile-meaning-match-can-be-fooled)).  During an [exemption](verification.md#exemptions), the live container files are built.

**Red team:** a `Dockerfile` whose `RUN` steps reach the LAN, the metadata endpoint and the internet.  A `dir` parent pointing outside `.surf-scoter.d`.

**When changing this code:** consider `--network=none` or building through the gateway.  Keep building from the staged copy.

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

The IDE server (such as `~/.vscode-server`) now lives in the home directory, which is kept in the workspace ([DEV-16](#dev-16-the-home-directory-persists-in-the-workspace)), so a server or extension changed in one session is the one the IDE talks to in every later session, even after the image is rebuilt.

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

**Severity:** Low.  **Status:** Closed.
**Where:** [`entrypoint.sh`](../../containers/dev-env/entrypoint.sh); [`containers/dev-env/Dockerfile`](../../containers/dev-env/Dockerfile).

The start-up script used to link the bridge commands into `/usr/local/bin`, which the base image leaves owned by root, so the link would fail.  It now links them into `/usr/local/scoter-bridge`, which the image creates owned by `ubuntu` and puts first on the `PATH`.  Code in the container can replace those links, or put its own programs ahead of others on the `PATH`; that's expected ([DEV-8](#dev-8-no-separation-inside-the-container)), and doesn't cross the host's checks.

**Red team:** confirm that a replaced link or client binary can't send anything the host's checks would refuse.

### DEV-14: Unbounded writable storage

**Severity:** Low.  **Status:** Open.
**Where:** [`docker-compose.yaml`](../../defaults/docker-compose.yaml) (no `read_only`, no size-limited `tmpfs`).

Code in the container can fill the disk that backs the container's writable layer, and the workspace's disk.

**Red team:** fill the disk and observe the effect on the host.

### DEV-15: The project chooses shared image tags

**Severity:** Medium.  **Status:** Open.
**Where:** the `parents` `tag` in [`dev_env.rs`](../../host/src/config/dev_env.rs); `_build_containers` in [`main_run.py`](../../scripts/main_run.py).

Each parent a `dev-env.yaml` builds is tagged with the `tag` the project gives, and nothing checks it.  Tags are shared by every project on the computer.  A project can tag its own parent with a name another project builds `FROM`, such as the distribution's `local-surf-scoter/dev-env:distro` (the `scoter init` template's base), and that project's next build uses the first project's image without a review of its own.  The review lists parent tags, but doesn't flag one that isn't the project's.

**Red team:** with two projects, have one build a `dir` parent tagged as the other's base image, then build the other.

**When changing this code:** put each project's parent tags under a prefix made from its environment id, or refuse tags outside it, and build the distribution's containers under tags no project may use.

### DEV-16: The home directory persists in the workspace

**Severity:** Medium.  **Status:** Open.
**Where:** [`entrypoint.sh`](../../containers/dev-env/entrypoint.sh); [`containers/dev-env/Dockerfile`](../../containers/dev-env/Dockerfile) (`/home` writable by `ubuntu`); `container: workspace-subdir` in the project file.

At start-up, the entrypoint copies the image's home directory into `/workspace`, keeping any file already there, and replaces `$HOME` with a link to `/workspace`.  So the home directory is kept in the workspace on the host, between sessions:

* **What a session plants stays.**  Shell start-up files, `~/.cargo/bin` and other programs on the `PATH`, the IDE server ([DEV-5](#dev-5-the-ide-session-as-a-channel-to-the-host)), and `~/.config/sshd/sshd_config.template`, from which each start makes the SSH server's configuration, all win over the image's copies.  They survive rebuilding the image, `--clean-workspace` (which removes only the project's clone), and revoking and accepting the project again.  A fix to the image doesn't reach an existing workspace.
* **Secrets reach the host disk.**  The SSH host private key is copied into `~/.config/sshd`, so it's now on the host, in the workspace, as is anything else the container keeps in its home, such as shell history and tokens.
* **Without `workspace-subdir`, home is the project's clone.**  The project's repository then supplies `.bashrc`, `.ssh/`, `.config/sshd/` and the rest, ahead of the image's, and every dotfile the container writes shows up in the clone as an untracked file.  The default container's `dev-env.yaml` says the project must be in a sub-directory, but nothing enforces it.
* **`/workspace` is fixed.**  The entrypoint ignores `mounts: workspace`, so a `dev-env.yaml` that mounts the workspace elsewhere gets a home directory that isn't kept.

None of this crosses to the host by itself; it's the container's own state, made permanent.  It matters with [FILE-3](files-attack-vectors.md#file-3-the-file-vcs-merge) and [FILE-11](files-attack-vectors.md#file-11-kill---fetch-fetches-from-the-workspace-root), which bring the home directory's contents back to the host.

**Red team:** change `.bashrc`, the sshd template and a program on the `PATH` in one session, then rebuild the image and run `scoter run --clean-workspace`, and check what the next session runs.  With a project that sets no `workspace-subdir`, commit a `.ssh/authorized_keys` and a `.config/sshd/sshd_config.template` to the repository.

**When changing this code:** keep the files that make up the container's own configuration (the sshd template, the host key) out of the persisted home, and refresh them from the image on every start.  Require `workspace-subdir`, or keep the home directory somewhere other than the project's clone.  Give `--clean-workspace` a way to clear the home directory too.


## Sensitive code

* [`host/src/config/dev_env.rs`](../../host/src/config/dev_env.rs): validation of every `dev-env.yaml` value.
* [`scripts/main_run.py`](../../scripts/main_run.py): everything written to `~/.ssh/config`, `known_hosts`, the `.env` file and the setup directory, and the image builds.
* [`scripts/surf_scoter_lib/ssh_config.py`](../../scripts/surf_scoter_lib/ssh_config.py): the SSH configuration writer.
* [`defaults/docker-compose.yaml`](../../defaults/docker-compose.yaml): the `dev` service's user, capabilities, mounts and limits.
* [`containers/dev-env/`](../../containers/dev-env/): the base image, start-up script (including the home directory link) and `sshd_config`.
* [`scripts/surf_scoter_lib/verified/staging.py`](../../scripts/surf_scoter_lib/verified/staging.py): what the build is made from.


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
