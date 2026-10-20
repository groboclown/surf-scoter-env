# File Access Attack Vectors

The weak points of [file access](files.md): how code in the development container could use the files it sees, or the files it leaves behind, against the host.  Contributors should treat changes to the code listed here as security-sensitive, and the red team can use each vector as a starting point.  See the [attack vector overview](attack-vectors.md) for how to read the severity and status labels.


## Attacker goals

* **Read host secrets:** keys and credentials in the home directory, environment files, the project's untracked files.
* **Run code on the host** through files the host acts on without asking: Git hooks and configuration, IDE settings, build scripts, shell start-up files.
* **Change the project** by slipping changes into the developer's real repository, and from there into the team's.


## Attack surface

| Input | Controlled by | Handled in |
| --- | --- | --- |
| Everything in the workspace, including `.git` | Code in the container | `_setup_workspace` in [`main_run.py`](../../scripts/main_run.py); the VCS `runs` in [`setup.yaml`](../../defaults/setup.yaml); the developer's tools |
| Object files hard-linked between the project and the workspace | Code in the container | `git-vcs: runs: local-clone` in [`setup.yaml`](../../defaults/setup.yaml) |
| Setup directory files | `scoter`, and any local account that can write them | `run_env_runner` in [`main_run.py`](../../scripts/main_run.py); `touch` in [`fileutil.py`](../../scripts/surf_scoter_lib/fileutil.py) |
| Changes brought back to the project | Code in the container, through the developer | The developer's review |


## Summary

| ID | Vector | Severity | Status |
| --- | --- | --- | --- |
| [FILE-1](#file-1-scoter-run-runs-git-inside-the-workspace) | `scoter run` runs Git inside the workspace | Critical | Open |
| [FILE-2](#file-2-object-files-shared-with-the-project-repository) | Object files shared with the project repository | High | Mitigated |
| [FILE-3](#file-3-the-file-vcs-merge) | The `file` VCS merge | High | Open |
| [FILE-4](#file-4-host-tools-acting-on-the-workspace) | Host tools acting on the workspace | High | By design |
| [FILE-5](#file-5-changes-brought-back-to-the-project) | Changes brought back to the project | High | By design |
| [FILE-6](#file-6-the-workspace-and-setup-separation) | The workspace and setup separation | High | Mitigated |
| [FILE-7](#file-7-world-writable-setup-files) | World-writable setup files | Medium | Open |
| [FILE-8](#file-8-everything-readable-in-the-container-is-exposed) | Everything readable in the container is exposed | Medium | By design |
| [FILE-9](#file-9-the-workspace-inside-the-project-directory) | The workspace inside the project directory | Low | By design |

Related vectors elsewhere: crafted mount points ([DEV-2](dev-container-attack-vectors.md#dev-2-mount-points-are-written-into-the-compose-env-file)), and permission bits left on workspace files ([DEV-4](dev-container-attack-vectors.md#dev-4-host-ids-under-rootful-docker), [DEV-7](dev-container-attack-vectors.md#dev-7-setuid-files-that-run-as-the-developer), [DEV-10](dev-container-attack-vectors.md#dev-10-loosened-permissions-on-workspace-files)).


## Vectors

### FILE-1: `scoter run` runs Git inside the workspace

**Severity:** Critical.  **Status:** Open.
**Where:** `_setup_workspace` in [`main_run.py`](../../scripts/main_run.py); `git-vcs: runs: fetch-changes` in [`setup.yaml`](../../defaults/setup.yaml).

When the workspace already exists, `scoter run` (unless given `--no-workspace-fetch`) runs `git fetch --all` on the host, with the workspace as its working directory.  Git reads the workspace's `.git/config` and hooks, which an earlier container session could have rewritten.  A remote's `uploadpack` or `sshCommand` setting, or a `reference-transaction` hook, runs a program on the host.

**Red team:** from the container, set `remote.origin.uploadpack` in the workspace's `.git/config`, or plant `.git/hooks/reference-transaction`, then wait for the next `scoter run`.

**When changing this code:** never run a VCS command with the workspace as its working directory or its repository.  Refresh the workspace from the project side instead, for example by pushing from the project into it.

### FILE-2: Object files shared with the project repository

**Severity:** High.  **Status:** Mitigated, for workspaces created after the fix.
**Where:** `git-vcs: runs: local-clone` in [`setup.yaml`](../../defaults/setup.yaml); the clone in [`run-container.sh`](../../run-container.sh).

By default, `git clone` of a local path hard-links the object files when it can, and the workspace sits on the same file system as the project.  The project and the workspace then share each object file's inode, owned by the developer.  With `keep-id`, the container runs as that owner, so it can make an object file writable and replace its contents.  The developer's real repository then returns the attacker's content under the original, already-reviewed hash.

This was confirmed with loose objects: after the workspace copy of a blob was rewritten, `git show` in the source repository printed the new content.  A fresh checkout, a branch switch, `git archive` or a new local clone in the project would use it, without any review.  Only `git fsck` reported the mismatch.  Pushing would likely fail, because the receiving side recomputes object hashes.  Packed objects haven't been tested.

Both clones now use `--no-local`, which makes Git copy the objects through its normal transport, so the workspace shares no files with the project.  Workspaces created before the fix still share them, and need recreating with `scoter run --clean-workspace`.  A user setup file that overrides `local-clone` without the flag brings the vector back.

**Red team:** in a new workspace, look for any file whose link count is above one (`find .git -links +1`), or any other way the workspace shares storage with the project.  Check user setup files and other VCS implementations for clones without the flag.

**When changing this code:** keep `--no-local`, and see the [VCS checklist](files.md#adding-a-vcs-implementation) before adding a new way to create the workspace.

### FILE-3: The `file` VCS merge

**Severity:** High, for projects that use it.  **Status:** Open.
**Where:** `file-vcs: runs` in [`setup.yaml`](../../defaults/setup.yaml).

`merge-changes` copies the whole workspace over the project with `cp -R`, with no review step and no protection against symlinks.  A symlink planted in one session, followed by a directory of the same name in the next, can make the copy write outside the project.  `show-fetch-changes` uses `diff -R`, which follows symlinks to host files.  `local-clone` copies untracked and ignored files, including secrets, into the workspace.

**Red team:** the two-step symlink-then-directory sequence against `merge-changes`; a symlink to a host secret, shown through `show-fetch-changes`.

**When changing this code:** the `TODO` in `setup.yaml` proposes a dedicated script; it should refuse symlinks and special files, and copy only what the developer approves.

### FILE-4: Host tools acting on the workspace

**Severity:** High.  **Status:** By design; the developer must not use host tools there.
**Where:** the developer's tools; the guidance in the [main README](../../README.md#dont-run-git-or-your-ide-on-the-workspace-from-your-host).

Many tools read configuration from the directory they run in: Git (hooks, `core.fsmonitor`), IDEs (`.vscode/tasks.json`, `.idea` run configurations, recommended extensions), environment managers (`.envrc`, `.mise.toml`, `.tool-versions`), and build tools (`Makefile`, `package.json` scripts).  Opening or running any of them in `.dev-env/workspace` on the host runs the container's code there.

**Red team:** plant each kind of file in the workspace and see which tools act on it when a developer browses the project directory on the host.

**When changing this code:** keep the workspace somewhere host tools are unlikely to visit (see FILE-9).

### FILE-5: Changes brought back to the project

**Severity:** High.  **Status:** By design; the developer's review is the control.
**Where:** the [recommended return path](files.md#bringing-changes-back).

`git fetch` from the workspace runs nothing, but what it brings is code the container wrote.  Changes that are easy to miss in review:

* build and test scripts, CI workflows, package manifests with install hooks;
* anything in `.surf-scoter.d/` (the project file, and the dev containers' `Dockerfile`, `dev-env.yaml` and build files, and its `egress` rules), which widen what the *next* environment may do;
* `.vscode/`, `.idea/` and other settings tools act on when they open a folder;
* symlinks, which point at host files once checked out on the host;
* `.gitmodules` entries pointing at attacker-controlled repositories.
* when the project is Surf Scoter itself, its own scripts, `setup.yaml`, containers and host programs, which run on the host if the tools run from that checkout (see [Working on Surf Scoter With Surf Scoter](../../CONTRIBUTORS.md#working-on-surf-scoter-with-surf-scoter)).

**Red team:** get a change from each category through a realistic review.

**When changing this code:** tooling that highlights these categories in a fetched change would directly reduce this risk.

### FILE-6: The workspace and setup separation

**Severity:** High.  **Status:** Mitigated.
**Where:** `_setup_workspace` and `run_env_runner` in [`main_run.py`](../../scripts/main_run.py); the mounts in [`docker-compose.yaml`](../../defaults/docker-compose.yaml).

The workspace is a `git clone`, so it gets no untracked files, ignored files, hooks or local configuration from the project.  The setup directory is mounted read-only.  The gateway's allowed hosts come from the project, not the workspace.  Nothing else from the host is mounted.

**Red team:** look for anything from the host that reaches the container: project files, environment, mounts added through settings, and anything that shares storage with the project, as in FILE-2.

**When changing this code:** any new mount, any new file in the setup directory, and any change to how the workspace is created needs this vector rechecked.

### FILE-7: World-writable setup files

**Severity:** Medium (shared computers).  **Status:** Open.
**Where:** `run_env_runner` in [`main_run.py`](../../scripts/main_run.py), which calls `touch(..., 0o666)`.

`scoter run` sets `authorized_keys` and the SSH host private key to mode `0666`.  Another local account could read the host key, or add a key to `authorized_keys` before the container starts.  (The allowed domain list it used to create with mode `0666` is gone: the gateway's configuration is made from the egress rules on each run, mode `0644`, in the resources directory.)

**Red team:** as another local account, change each file between `scoter run` writing it and the container reading it, and for the allowlist, between runs.

**When changing this code:** write setup files with the narrowest mode that the container's id mapping allows, and recreate them on every run.

### FILE-8: Everything readable in the container is exposed

**Severity:** Medium.  **Status:** By design.
**Where:** the workspace.

Isolation limits where data can go, not what the container can read.  Committed secrets, the full Git history, and anything the `file` VCS copied are available to the code running there.

**Red team:** search the workspace and history for credentials.

### FILE-9: The workspace inside the project directory

**Severity:** Low.  **Status:** By design.
**Where:** `workspace: paths` in [`setup.yaml`](../../defaults/setup.yaml).

`.dev-env/` is under the project directory, so host tools that walk the project tree (search indexers, test runners, language servers, backup tools) may find the workspace too.  `scoter` adds the workspace to `.dev-env/.gitignore`, but other tools have their own rules.

**Red team:** list the host tools that descend into `.dev-env/workspace` from the project directory, and what each reads there.

**When changing this code:** moving the workspace out of the project directory would also put the workspace on a different file system from the project, ruling out FILE-2 entirely.


## Sensitive code

* [`scripts/main_run.py`](../../scripts/main_run.py): workspace creation and refresh, and the setup directory.
* [`defaults/setup.yaml`](../../defaults/setup.yaml): the `git-vcs` and `file-vcs` commands, and the workspace locations.
* [`scripts/surf_scoter_lib/fileutil.py`](../../scripts/surf_scoter_lib/fileutil.py): file modes.
* The `volumes` of [`defaults/docker-compose.yaml`](../../defaults/docker-compose.yaml).
