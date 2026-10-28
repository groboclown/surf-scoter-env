# File Access Attack Vectors

The weak points of [file access](files.md): how code in the development container could use the files it sees, or the files it leaves behind, against the host.  Contributors should treat changes to the code listed here as security-sensitive, and the red team can use each vector as a starting point.  See the [attack vector overview](attack-vectors.md) for how to read the severity and status labels.


## Attacker goals

* **Read host secrets:** keys and credentials in the home directory, environment files, the project's untracked files.
* **Run code on the host** through files the host acts on without asking: Git hooks and configuration, IDE settings, build scripts, shell start-up files.
* **Change the project** by slipping changes into the developer's real repository, and from there into the team's.


## Attack surface

| Input | Controlled by | Handled in |
| --- | --- | --- |
| Everything in the project clone, including `.git` | Code in the container | `_setup_project_clone` in [`main_run.py`](../../scripts/main_run.py); `rm` in [`fileutil.py`](../../scripts/surf_scoter_lib/fileutil.py); the VCS `runs` in [`setup.yaml`](../../defaults/setup.yaml); `scoter kill --fetch` in [`main_kill.py`](../../scripts/main_kill.py); the developer's tools |
| The persistent store | Code in the container | The container tool; nothing on the host reads it |
| `container: mounts: project-clone` | The project, as accepted | `parse_container` in [`project.rs`](../../host/src/config/project.rs); `project_mount_problems` in [`config.rs`](../../host/src/config.rs) |
| Object files hard-linked between the project and the clone | Code in the container | `git-vcs: runs: local-clone` in [`setup.yaml`](../../defaults/setup.yaml) |
| Setup directory files | `scoter`, and any local account that can write them | `run_env_runner` in [`main_run.py`](../../scripts/main_run.py); `touch` in [`fileutil.py`](../../scripts/surf_scoter_lib/fileutil.py) |
| Changes brought back to the project | Code in the container, through the developer | The developer's review |


## Summary

| ID | Vector | Severity | Status |
| --- | --- | --- | --- |
| [FILE-1](#file-1-scoter-run-runs-git-inside-the-project-clone) | `scoter run` runs Git inside the project clone | Critical | Closed |
| [FILE-2](#file-2-object-files-shared-with-the-project-repository) | Object files shared with the project repository | High | Mitigated |
| [FILE-3](#file-3-the-file-vcs-merge) | The `file` VCS merge | High | Open |
| [FILE-4](#file-4-host-tools-acting-on-the-project-clone) | Host tools acting on the project clone | High | By design |
| [FILE-5](#file-5-changes-brought-back-to-the-project) | Changes brought back to the project | High | By design |
| [FILE-6](#file-6-the-project-clone-and-setup-separation) | The project clone and setup separation | High | Mitigated |
| [FILE-7](#file-7-world-writable-setup-files) | World-writable setup files | Medium | Open |
| [FILE-8](#file-8-everything-readable-in-the-container-is-exposed) | Everything readable in the container is exposed | Medium | By design |
| [FILE-9](#file-9-the-project-state-inside-the-project-directory) | The project state inside the project directory | Low | Mitigated by default |
| [FILE-10](#file-10-the-clones-path-through-container-symlinks) | The clone's path through container symlinks | High | Closed |
| [FILE-11](#file-11-kill---fetch-fetches-from-the-wrong-directory) | `kill --fetch` fetches from the wrong directory | Low | Closed |

Related vectors elsewhere: crafted mount points ([DEV-2](dev-container-attack-vectors.md#dev-2-mount-points-are-written-into-the-compose-env-file)), what the persistent store keeps between sessions ([DEV-16](dev-container-attack-vectors.md#dev-16-the-persistent-store-outlives-the-session)), and permission bits left on the clone's files ([DEV-4](dev-container-attack-vectors.md#dev-4-host-ids-under-rootful-docker), [DEV-7](dev-container-attack-vectors.md#dev-7-setuid-files-that-run-as-the-developer), [DEV-10](dev-container-attack-vectors.md#dev-10-loosened-permissions-on-the-clones-files)).


## Vectors

### FILE-1: `scoter run` runs Git inside the project clone

**Severity:** Critical.  **Status:** Closed.
**Where:** `_setup_project_clone`, `_refresh_bundle` and `_point_clone_at_bundle` in [`main_run.py`](../../scripts/main_run.py); `git-vcs: runs: refresh-bundle` and `set-clone-origin` in [`setup.yaml`](../../defaults/setup.yaml); the fetch in the distribution's [`entrypoint.sh`](../../containers/dev-env/entrypoint.sh).

When the project clone already existed, `scoter run` refreshed it by running `git fetch --all` on the host, with the clone as its working directory.  Git read the clone's `.git/config` and hooks, which an earlier container session could have rewritten.  A remote's `uploadpack` or `sshCommand` setting, or a `reference-transaction` hook, ran a program on the host.  Project verification didn't help: the clone isn't part of what's reviewed.

**Pushing into the clone isn't a fix.**  `git push` to a local path starts `git receive-pack` *in the clone*, on the host, which reads the clone's configuration and runs its hooks (`pre-receive`, `update`, `post-receive`, `post-update`, `reference-transaction`, `proc-receive`, `push-to-checkout`), wherever the clone's `core.hooksPath` says.  The pushing side can't turn that off: for the local transport, Git removes `GIT_CONFIG_PARAMETERS` before starting receive-pack, so `git -c core.hooksPath=... push` doesn't reach it (confirmed with Git 2.56: a planted `reference-transaction` hook ran).  A `--receive-pack` wrapper that sets `core.hooksPath` stops the hooks, but not the rest of the configuration: `receive.denyCurrentBranch=updateInstead` checks out files through the clone's smudge filters and `core.fsmonitor`, and `receive.autogc` runs `git gc` there.

**What replaced it.**  The host never runs Git in the clone.  Each run, `refresh-bundle` runs `git bundle create --branches --tags` in your repository (`vcs-root`), and writes `project.bundle` into the read-only setup directory.  After a fresh clone, `set-clone-origin` points its `origin` at the bundle's path in the container, editing only the new clone's configuration file with `git config --file`, before the container has ever seen it.  Git *in the container* fetches `origin`; the distribution's dev container does so as it starts, which updates the remote-tracking branches, as `git fetch --all` did.  The clone's hooks and configuration now only ever run inside the container.

**Red team:** from the container, plant hooks, `core.hooksPath`, `core.fsmonitor` and smudge filters in the clone's `.git`, then run `scoter run`, with and without `--clean-project-clone`, and check that nothing runs on the host.  Look for any other command the tools run with the clone as its repository or working directory, in every VCS section.

**When changing this code:** never run a VCS command with the clone as its working directory or repository, including pushing into it.  Give the clone data (a bundle, a read-only mount) and let the VCS in the container fetch it.  Bring changes back only by reading the clone from your repository (`fetch-changes-into`).

### FILE-2: Object files shared with the project repository

**Severity:** High.  **Status:** Mitigated, for clones created after the fix.
**Where:** `git-vcs: runs: local-clone` in [`setup.yaml`](../../defaults/setup.yaml); the clone in `_setup_project_clone` in [`main_run.py`](../../scripts/main_run.py).

By default, `git clone` of a local path hard-links the object files when it can, and the clone sits on the same file system as the project.  The project and the clone then share each object file's inode, owned by the developer.  With `keep-id`, the container runs as that owner, so it can make an object file writable and replace its contents.  The developer's real repository then returns the attacker's content under the original, already-reviewed hash.

This was confirmed with loose objects: after the clone's copy of a blob was rewritten, `git show` in the source repository printed the new content.  A fresh checkout, a branch switch, `git archive` or a new local clone in the project would use it, without any review.  Only `git fsck` reported the mismatch.  Pushing would likely fail, because the receiving side recomputes object hashes.  Packed objects haven't been tested.

Both clones now use `--no-local`, which makes Git copy the objects through its normal transport, so the clone shares no files with the project.  A user setup file that overrides `local-clone` without the flag brings the vector back.

**Red team:** in a new clone, look for any file whose link count is above one (`find .git -links +1`), or any other way the clone shares storage with the project.  Check user setup files and other VCS implementations for clones without the flag.

**When changing this code:** keep `--no-local`, and see the [VCS checklist](files.md#adding-a-vcs-implementation) before adding a new way to create the clone.

### FILE-3: The `file` VCS merge

**Severity:** High, for projects that use it.  **Status:** Open.
**Where:** `file-vcs: runs` in [`setup.yaml`](../../defaults/setup.yaml).

`merge-changes` copies the whole project clone over the project with `cp -R`, with no review step and no protection against symlinks.  A symlink planted in one session, followed by a directory of the same name in the next, can make the copy write outside the project.  `show-fetch-changes` uses `diff -R`, which follows symlinks to host files.  `local-clone` copies untracked and ignored files, including secrets, into the clone.

The container's own files, in its persistent store, are a container volume, not part of the clone, so `merge-changes` doesn't copy them.  No `scoter` command calls `merge-changes` or `show-fetch-changes` yet (`scoter fetch` isn't implemented), so this needs the developer to run them.

**Red team:** the two-step symlink-then-directory sequence against `merge-changes`; a symlink to a host secret, shown through `show-fetch-changes`.

**When changing this code:** the `TODO` in `setup.yaml` proposes a dedicated script; it should refuse symlinks and special files, and copy only what the developer approves.

### FILE-4: Host tools acting on the project clone

**Severity:** High.  **Status:** By design; the developer must not use host tools there.
**Where:** the developer's tools; the guidance in the [main README](../../README.md#dont-run-git-or-your-ide-on-the-project-clone-from-your-host).

Many tools read configuration from the directory they run in: Git (hooks, `core.fsmonitor`), IDEs (`.vscode/tasks.json`, `.idea` run configurations, recommended extensions), environment managers (`.envrc`, `.mise.toml`, `.tool-versions`), and build tools (`Makefile`, `package.json` scripts).  Opening or running any of them in the project clone on the host runs the container's code there.

**Red team:** plant each kind of file in the clone and see which tools act on it when a developer browses the project's state directory on the host.

**When changing this code:** keep the clone somewhere host tools are unlikely to visit (see FILE-9).

### FILE-5: Changes brought back to the project

**Severity:** High.  **Status:** By design; the developer's review is the control.
**Where:** the [recommended return path](files.md#bringing-changes-back).

`git fetch` from the project clone runs nothing, but what it brings is code the container wrote.  Changes that are easy to miss in review:

* build and test scripts, CI workflows, package manifests with install hooks;
* anything in `.surf-scoter.d/` (the project file, and the dev containers' `Dockerfile`, `dev-env.yaml` and build files, and its `egress` rules), which widen what the *next* environment may do.  [Project verification](verification.md) asks for a new review before the next run uses them, which narrows this to the review's own gaps: changes that match on meaning ([VER-1](verification-attack-vectors.md#ver-1-the-dockerfile-meaning-match-can-be-fooled), [VER-2](verification-attack-vectors.md#ver-2-any-file-named-dev-envyaml-matches-on-meaning)), values the review doesn't show ([VER-3](verification-attack-vectors.md#ver-3-the-review-doesnt-show-everything-it-accepts)), and exemptions ([VER-4](verification-attack-vectors.md#ver-4-exemptions-accept-what-nobody-reviewed));
* `.vscode/`, `.idea/` and other settings tools act on when they open a folder;
* symlinks, which point at host files once checked out on the host;
* `.gitmodules` entries pointing at attacker-controlled repositories.
* when the project is Surf Scoter itself, its own scripts, `setup.yaml`, containers and host programs, which run on the host if the tools run from that checkout (see [Working on Surf Scoter With Surf Scoter](../../CONTRIBUTING.md#working-on-surf-scoter-with-surf-scoter)).

**Red team:** get a change from each category through a realistic review.

**When changing this code:** tooling that highlights these categories in a fetched change would directly reduce this risk.

### FILE-6: The project clone and setup separation

**Severity:** High.  **Status:** Mitigated.
**Where:** `_setup_project_clone` and `run_env_runner` in [`main_run.py`](../../scripts/main_run.py); the mounts in [`docker-compose.yaml`](../../defaults/docker-compose.yaml).

The project clone is a `git clone`, so it gets no untracked files, ignored files, hooks or local configuration from the project.  The setup directory is mounted read-only.  The gateway's allowed hosts and the bridge commands come from your accepted settings in the verified store, never from the project or the clone, and the dev container is built from the accepted copy of `.surf-scoter.d`.  The persistent store is a container volume, so it brings nothing from the host either.  Nothing else from the host is mounted.

**Red team:** look for anything from the host that reaches the container: project files, environment, mounts added through settings, and anything that shares storage with the project, as in FILE-2.

**When changing this code:** any new mount, any new file in the setup directory, and any change to how the clone or the persistent store is created needs this vector rechecked.

### FILE-7: World-writable setup files

**Severity:** Medium (shared computers).  **Status:** Open.
**Where:** `run_env_runner` in [`main_run.py`](../../scripts/main_run.py), which calls `touch(..., 0o666)`.

`scoter run` sets `authorized_keys` and the SSH host private key to mode `0666` (`MODE_SETTINGS_FILE` in `main_run.py`).  Another local account could read the host key, or add a key to `authorized_keys` before the container starts.  (The allowed domain list it used to create with mode `0666` is gone: the gateway's configuration is made from the egress rules on each run, mode `0644`, in the resources directory.)

**Red team:** as another local account, change each file between `scoter run` writing it and the container reading it, and for the allowlist, between runs.

**When changing this code:** write setup files with the narrowest mode that the container's id mapping allows, and recreate them on every run.

### FILE-8: Everything readable in the container is exposed

**Severity:** Medium.  **Status:** By design.
**Where:** the project clone, and the persistent store.

Isolation limits where data can go, not what the container can read.  Committed secrets, the full Git history, every local branch and tag (the clone's bundle holds them all, though a fresh clone only checks out one), anything the `file` VCS copied, and anything an earlier session left in the persistent store are available to the code running there.

**Red team:** search the clone, its history and the persistent store for credentials.

### FILE-9: The project state inside the project directory

**Severity:** Low.  **Status:** Mitigated by default.
**Where:** `project-state: paths` in [`setup.yaml`](../../defaults/setup.yaml).

The project's run files used to be under the project directory (`.dev-env/`), so host tools that walk the project tree (search indexers, test runners, language servers, backup tools) could find them too.  Their default is now outside the project: `${scoter:state-dir}/projects/${scoter:project-id}`.  A setup that moves them back inside the project has the risk again; `scoter run` then adds the directories to the project's ignore file (`ignore-file`), but other tools have their own rules.

**Red team:** with the project state configured inside the project, list the host tools that descend into it from the project directory, and what each reads there.

**When changing this code:** the default `state-dir` is in your home directory, often on the same file system as the project, so the clone still needs `--no-local` (FILE-2).

### FILE-10: The clone's path through container symlinks

**Severity:** High.  **Status:** Closed.
**Where:** `_setup_project_clone` and `_check_real_dir` in [`main_run.py`](../../scripts/main_run.py); `project-state: paths` in [`setup.yaml`](../../defaults/setup.yaml); the project clone mount in [`docker-compose.yaml`](../../defaults/docker-compose.yaml).

The project file's old `container: workspace-subdir` put the clone in a sub-directory of the directory mounted in the container, beside the container's home directory, and the host joined the sub-path to it.  The container could replace a directory on that path with a symlink between sessions, so the next `scoter run` cloned into, ran Git in, or (with `--clean-workspace`) deleted a directory of the container's choosing, such as `~/bin` or `~/.ssh`.

Now the clone is the mounted directory itself (`project-state: paths: project-clone-dir`), so nothing the container can write lies on the host's path to it.  The project can only choose where the clone appears *inside* the container (`container: mounts: project-clone`); that's a container path, and the host never builds a path from it.  `scoter run` also checks that the clone directory isn't a symlink, or anything but a directory, before it clones, fetches or removes it.  The container's own files moved to the persistent store, a container volume.

**Red team:** look for any other host path built below a directory the container can write, and for a way to make `project-clone-dir` a symlink between runs.

**When changing this code:** keep every host path the tools act on free of container-writable parts.  Never join a project-supplied value to a host directory the container can write.

### FILE-11: `kill --fetch` fetches from the wrong directory

**Severity:** Low.  **Status:** Closed.
**Where:** `project_clone_dir` in the environment record written by [`main_run.py`](../../scripts/main_run.py); the fetch in [`main_kill.py`](../../scripts/main_kill.py).

With the old `workspace-subdir`, the environment record kept the mounted directory, not the clone below it, so `scoter kill --fetch` fetched from the container's home directory, or whatever repository the container made there.  The record now keeps the clone's directory, which is the mounted directory itself, and that's what the fetch reads.

**Red team:** check that `scoter kill --fetch` reads from the clone `scoter run` made.


## Sensitive code

* [`scripts/main_run.py`](../../scripts/main_run.py): creating and refreshing the clone, the persistent store, and the setup directory.
* [`scripts/main_kill.py`](../../scripts/main_kill.py): fetching from the clone, and removing the clone and the persistent store.
* [`defaults/setup.yaml`](../../defaults/setup.yaml): the `git-vcs` and `file-vcs` commands, and the `project-state` locations.
* [`scripts/surf_scoter_lib/fileutil.py`](../../scripts/surf_scoter_lib/fileutil.py): file modes, and removal.
* The `volumes` of [`defaults/docker-compose.yaml`](../../defaults/docker-compose.yaml).
