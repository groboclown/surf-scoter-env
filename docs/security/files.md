# File Access

The development container has to read and change your project's files; that's the point of it.  But your computer holds much more than the project, and Surf Scoter keeps the container's view of the file system small.  It treats everything the container writes as untrusted until you've reviewed it.

This document describes which files the container can see, how they get there, and how changes come back.  [File access attack vectors](files-attack-vectors.md) analyzes the risks and weak points.


## The directories

`scoter run` creates a `.dev-env` directory in your project directory (the `workspace` section of [`defaults/setup.yaml`](../../defaults/setup.yaml) sets the locations) with three subdirectories:

| Host directory | Contents | In the dev container | Who writes it |
| --- | --- | --- | --- |
| `.dev-env/workspace` | A clone of your project | Read-write, at `mounts: workspace` (default `/workspace`) | The container |
| `.dev-env/setup` | SSH host key, `authorized_keys`, allowed domain list, bridge command list | Read-only, at `mounts: setup` (default `/opt/surf-scoter-setup`) | `scoter` on the host |
| `.dev-env/resources` | Compose file, compose `.env`, the universal configuration file | Not mounted | `scoter` on the host |

The gateway container also mounts `setup/allowed-domains.txt`, read-only.  Nothing else from your host is mounted.  In particular, the dev container never gets your home directory, your SSH keys, your cloud credential files, or the container runtime's socket.

### The workspace is a clone, not your project

The container works on a separate copy.  With `vcs: git`, `scoter run` makes it with `git clone` of your project directory, so the clone holds your committed history but none of your untracked files, ignored files, or your `.git` directory's hooks and local configuration.  The clone uses `--no-local`, so Git copies the objects rather than hard-linking them, and the workspace shares no files with your project.  The container's changes land in the clone, and your own project directory stays untouched until you choose to bring changes back.

When the workspace already exists, `scoter run` refreshes it with the VCS `fetch-changes` command, unless given `--no-workspace-fetch`.  `--clean-workspace` removes it and starts from a fresh clone.

With no `vcs`, `scoter` creates an empty workspace.  The `file` VCS setting copies the project with `cp -R`, which *does* carry untracked and ignored files into the container.

### Ownership

The dev container runs as the image's user (the unprivileged `ubuntu` in the base image), with every capability dropped and `no-new-privileges` set, so it can't gain root's powers, or change the ownership of the files it writes.  With Podman, the compose file's `userns_mode: keep-id` maps the container user to your host user, so the workspace files stay owned by you on the host.

### The setup directory

The setup directory carries what the container needs to start and nothing more.  Because it's read-only in the container, code there can't change its own SSH authorization, the gateway's allowed domain list, or the list of bridge commands.  The allowed domain list in it is copied from your *project directory* at launch, not from the workspace, so a change the container makes to its own copy has no effect until you bring that change back into your project.


## Bringing changes back

Everything in the workspace was writable by the container, including the clone's `.git` directory, so Surf Scoter's recommended pattern never runs anything inside the workspace on the host.  The pattern, also described in the [main README](../../README.md#dont-run-git-or-your-ide-on-the-workspace-from-your-host), runs Git *in your own repository* and only reads objects from the workspace:

```sh
cd ~/src/my-project
git fetch /path/to/my-project/.dev-env/workspace my-branch
git log -p HEAD..FETCH_HEAD
```

Git runs in your repository, with your configuration and your hooks.  The workspace side is served by `git upload-pack`, which Git designs to be safe in an untrusted repository: it doesn't run the workspace's hooks or the programs its configuration names.  Fetching carries only file contents and the executable bit, not ownership, group or other permission bits.

Review what arrives as untrusted code before merging, paying particular attention to:

* build and test scripts, CI workflows, and package manifests with install hooks;
* `.surf-scoter.yaml` and `.surf-scoter/` (they change what the *next* environment may do; see [the host bridge](host-bridge.md) and [network](network.md));
* `.vscode/`, `.idea/`, `.editorconfig`, and other files your tools act on when they open a folder;
* symlinks, which can point at files outside the project once they're on your host.

Your IDE connects to the container over SSH, rather than opening the workspace folder on the host.


## Adding a VCS implementation

A project's `vcs` value picks a `NAME-vcs` section of the [system configuration](../system-config-file.md), whose `runs` create the workspace, refresh it, show its changes and bring them back.  Those commands run on your host with your privileges, and most of them handle the container's files, so a new implementation is security-sensitive.  The [`git-vcs` and `file-vcs` sections](../../defaults/setup.yaml) are the examples; `git-vcs` is the reference, and `file-vcs` currently fails several of the points below (see [FILE-3](files-attack-vectors.md#file-3-the-file-vcs-merge)).

### Creating the workspace (`local-clone`)

* **Copy only committed content.**  Untracked and ignored files often hold secrets, such as `.env` files and local credentials.
* **Copy, never share.**  No hard links, symlinks or bind mounts between the project and the workspace.  The container owns the workspace's files, so anything shared can be changed in your project too.  Copy-on-write clones (reflinks) are fine, since a write breaks the sharing.  This is what `--no-local` does for Git ([FILE-2](files-attack-vectors.md#file-2-object-files-shared-with-the-project-repository)).
* **Leave the project's local settings behind.**  Hooks, local configuration and enabled extensions belong to your copy of the repository, not to the container's.

### Refreshing the workspace (`fetch-changes`)

* **Never run the VCS tool inside the workspace.**  A tool run with the workspace as its working directory or repository reads the workspace's configuration, hooks and extensions, which the container controls.  Push into the workspace from the project side instead.  The current `git-vcs` implementation doesn't do this yet ([FILE-1](files-attack-vectors.md#file-1-scoter-run-runs-git-inside-the-workspace)).

### Showing and bringing back changes (`fetch-changes-into`, `show-fetch-changes`, `merge-changes`)

* **Run from the project, and read the workspace as data.**  The tool must not load the workspace's configuration, hooks or extensions while reading from it.  Check this for each tool: Git's `upload-pack` is designed for it, but other systems may load a repository's settings whenever they open it.  Mercurial, for example, reads a repository's `.hg/hgrc`, which can enable hooks and extensions.
* **Don't follow symlinks** out of the workspace while showing or copying changes, and don't create symlinks in the project that point outside it.
* **Refuse special files:** devices, sockets and named pipes.
* **Carry only contents and the executable bit.**  Ownership, group, setuid and setgid bits, and other permissions from the workspace mustn't reach the project.
* **Never overwrite the project without review.**  Bring changes into a branch, or another place you can inspect, before they become active.

### General

* **Set `ignore-file`,** so the project's own VCS ignores the `.dev-env` directories rather than committing or scanning them.
* **Treat `{x:...}` values as untrusted text.**  Pass them as single arguments in `runs` templates; never build shell command lines from them.
* **Recheck the [file access attack vectors](files-attack-vectors.md)** against the new implementation, and add any new risk it brings as a vector there.
