# File Access

The development container has to read and change your project's files; that's the point of it.  But your computer holds much more than the project, and Surf Scoter keeps the container's view of the file system small.  It treats everything the container writes as untrusted until you've reviewed it.

This document describes which files the container can see, how they get there, and how changes come back.  [File access attack vectors](files-attack-vectors.md) analyzes the risks and weak points.


## The directories

`scoter run` keeps a project's run files in a directory of their own, outside the project: by default `${scoter:state-dir}/projects/${scoter:project-id}` (such as `~/.config/surf-scoter.d/state/projects/client-3f9a0b1c2d4e`), readable only by you.  The `project-state` section of [`defaults/setup.yaml`](../../defaults/setup.yaml) sets the locations; [the system configuration file](../system-config-file.md) describes the project id, and the branch you can add for a clone per branch.  It has three subdirectories:

| Host directory | Contents | In the dev container | Who writes it |
| --- | --- | --- | --- |
| `project-clone` | A clone of your project, the directory itself | Read-write, at `mounts: project-clone` (default `/project`), or the project file's `container: mounts: project-clone` | The container |
| `setup` | SSH host key, `authorized_keys`, bridge command list | Read-only, at `mounts: setup` (default `/opt/surf-scoter-setup`) | `scoter` on the host |
| `resources` | Compose file, compose `.env`, the universal configuration file | Not mounted | `scoter` on the host |

The dev container also gets its *persistent store*: a container volume, not a host directory, mounted read-write at `mounts: persistent-store` (default `/persistent-store`) and kept between runs (see [below](#the-persistent-store)).  The gateway container mounts its configuration (`resources/gateway-config`, made from the egress rules), read-only; the dev container doesn't get it.  Nothing else from your host is mounted.  In particular, the dev container never gets your home directory, your SSH keys, your cloud credential files, or the container runtime's socket.

Every mount is written in the compose file's long form, from values `scoter` checks: the mount points from `dev-env.yaml` and the project file must be plain container paths, so none can add a line to the `.env` file or an option to the mount ([DEV-2](dev-container-attack-vectors.md#dev-2-mount-points-are-written-into-the-compose-env-file)).

### The project clone is a clone, not your project

The container works on a separate copy.  With `vcs: git`, `scoter run` makes it with `git clone` of your project directory, so the clone holds your committed history but none of your untracked files, ignored files, or your `.git` directory's hooks and local configuration.  The clone uses `--no-local`, so Git copies the objects rather than hard-linking them, and the clone shares no files with your project.  The container's changes land in the clone, and your own project directory stays untouched until you choose to bring changes back.

The clone is the `project-clone` directory itself, mounted whole.  Nothing the container can write lies on the path from your home directory to it, so the container can't redirect where `scoter` clones, fetches or deletes ([FILE-10](files-attack-vectors.md#file-10-the-clones-path-through-container-symlinks)).  `scoter run` also refuses a `project-clone` that's a symlink, or isn't a directory.

When the clone already exists, `scoter run` refreshes it with the VCS `fetch-changes` command, unless given `--no-project-fetch`.  `--clean-project-clone` (or `--clean-project`) removes it and starts from a fresh clone.

With no `vcs`, `scoter` creates an empty clone directory.  The `file` VCS setting copies the project with `cp -R`, which *does* carry untracked and ignored files into the container.

### Ownership

The dev container runs as the image's user (the unprivileged `ubuntu` in the base image), with every capability dropped and `no-new-privileges` set, so it can't gain root's powers, or change the ownership of the files it writes.  With Podman, the compose file's `userns_mode: keep-id` maps the container user to your host user, so the clone's files stay owned by you on the host.

### The setup directory

The setup directory carries what the container needs to start and nothing more.  Because it's read-only in the container, code there can't change its own SSH authorization or the list of bridge commands.  The gateway's allowed hosts and the bridge commands come from the settings you accepted, kept in your verified store (see [project verification](verification.md)), so a change the container makes to its own copy of the project file has no effect until you bring that change back into your project, and review it again.

### The persistent store

The persistent store holds the container's own files between runs: its IDE server, caches, shell history.  It's a volume in the container tool, named `scoter-PROJECT-ID-store` by default (`project-state: values: persistent-store-volume`), not a directory on your host, so:

* **Host commands can't reach it.**  The host bridge maps only paths inside the project clone to the host ([BRIDGE-6](host-bridge-attack-vectors.md#bridge-6-path-mapping-and-resolution)); a path in the persistent store has no host directory to map to, and is refused.  A host command that needs a file must be given one in the project clone.
* **Host tools don't come across it.**  It isn't on a path you, your IDE or a search indexer would browse, and no VCS command copies it back into your project.
* **It's still on your disk**, inside the container tool's storage, so it doesn't change what permission bits on its files can do there ([DEV-4](dev-container-attack-vectors.md#dev-4-host-ids-under-rootful-docker), [DEV-11](dev-container-attack-vectors.md#dev-11-subordinate-ids-under-rootless-runtimes)).

What goes in it is up to the dev container.  The distribution's `dev-env` container keeps only the home directory entries listed in `/etc/scoter/persistent-store.txt` there, and takes everything else in its home directory, and its SSH server's configuration and keys, fresh from the image on each start.  A project's own container can keep anything there ([DEV-16](dev-container-attack-vectors.md#dev-16-the-persistent-store-outlives-the-session)).

`--clean-persistent-store`, on `scoter run` or `scoter kill`, removes it.


## Bringing changes back

Everything in the project clone was writable by the container, including its `.git` directory, so Surf Scoter's recommended pattern never runs anything inside the clone on the host.  The pattern, also described in the [main README](../../README.md#dont-run-git-or-your-ide-on-the-project-clone-from-your-host), runs Git *in your own repository* and only reads objects from the clone (`scoter kill --fetch BRANCH` does the same):

```sh
cd ~/src/my-project
git fetch ~/.config/surf-scoter.d/state/projects/my-project-3f9a0b1c2d4e/project-clone my-branch
git log -p HEAD..FETCH_HEAD
```

Git runs in your repository, with your configuration and your hooks.  The clone's side is served by `git upload-pack`, which Git designs to be safe in an untrusted repository: it doesn't run the clone's hooks or the programs its configuration names.  Fetching carries only file contents and the executable bit, not ownership, group or other permission bits.

Review what arrives as untrusted code before merging, paying particular attention to:

* build and test scripts, CI workflows, and package manifests with install hooks;
* `.surf-scoter.d/` and `.surf-scoter/` (they change what the *next* environment may do; see [the host bridge](host-bridge.md) and [network](network.md));
* `.vscode/`, `.idea/`, `.editorconfig`, and other files your tools act on when they open a folder;
* symlinks, which can point at files outside the project once they're on your host.

Your IDE connects to the container over SSH, rather than opening the clone's folder on the host.


## Adding a VCS implementation

A project's `vcs` value picks a `NAME-vcs` section of the [system configuration](../system-config-file.md), whose `runs` create the project clone, refresh it, show its changes and bring them back.  Those commands run on your host with your privileges, and most of them handle the container's files, so a new implementation is security-sensitive.  The [`git-vcs` and `file-vcs` sections](../../defaults/setup.yaml) are the examples; `git-vcs` is the reference, and `file-vcs` currently fails several of the points below (see [FILE-3](files-attack-vectors.md#file-3-the-file-vcs-merge)).

### Creating the clone (`local-clone`)

* **Copy only committed content.**  Untracked and ignored files often hold secrets, such as `.env` files and local credentials.
* **Copy, never share.**  No hard links, symlinks or bind mounts between the project and the clone.  The container owns the clone's files, so anything shared can be changed in your project too.  Copy-on-write clones (reflinks) are fine, since a write breaks the sharing.  This is what `--no-local` does for Git ([FILE-2](files-attack-vectors.md#file-2-object-files-shared-with-the-project-repository)).
* **Leave the project's local settings behind.**  Hooks, local configuration and enabled extensions belong to your copy of the repository, not to the container's.

### Refreshing the clone (`fetch-changes`)

* **Never run the VCS tool inside the clone.**  A tool run with the clone as its working directory or repository reads the clone's configuration, hooks and extensions, which the container controls.  Push into the clone from the project side instead.  The current `git-vcs` implementation doesn't do this yet ([FILE-1](files-attack-vectors.md#file-1-scoter-run-runs-git-inside-the-project-clone)).
* **Clone into `{x:project-clone-dir}` itself.**  It's the directory that's mounted; `scoter` has checked it isn't a symlink.  Don't build paths below it, which the container could redirect.

### Showing and bringing back changes (`fetch-changes-into`, `show-fetch-changes`, `merge-changes`)

* **Run from the project, and read the clone as data.**  The tool must not load the clone's configuration, hooks or extensions while reading from it.  Check this for each tool: Git's `upload-pack` is designed for it, but other systems may load a repository's settings whenever they open it.  Mercurial, for example, reads a repository's `.hg/hgrc`, which can enable hooks and extensions.
* **Don't follow symlinks** out of the clone while showing or copying changes, and don't create symlinks in the project that point outside it.
* **Refuse special files:** devices, sockets and named pipes.
* **Carry only contents and the executable bit.**  Ownership, group, setuid and setgid bits, and other permissions from the clone mustn't reach the project.
* **Never overwrite the project without review.**  Bring changes into a branch, or another place you can inspect, before they become active.

### General

* **Set `ignore-file`,** so that, if you keep the project state inside the project, the project's own VCS ignores those directories rather than committing or scanning them.  `scoter run` adds them to it only when they're inside the project.
* **Treat `{x:...}` values as untrusted text.**  Pass them as single arguments in `runs` templates; never build shell command lines from them.
* **Recheck the [file access attack vectors](files-attack-vectors.md)** against the new implementation, and add any new risk it brings as a vector there.
