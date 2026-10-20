# Projects in v1

v1 changes how a project describes its development environment, and how much of that description the tools trust.  Today the tools act on the project's [configuration file](../project-configuration-file.md) directly.  In v1, the project file only *suggests* settings.  The developer reviews them and accepts, edits or denies them.  The tools then run only from the developer's accepted copy.

This page collects the design, the reasons behind it, and the weak points to keep in mind while building it.  It builds on the [configuration weaknesses](../security/configuration-weaknesses.md) analysis, and addresses several vectors in the [file access](../security/files-attack-vectors.md) and [host bridge](../security/host-bridge-attack-vectors.md) attack vector lists.


## Summary of the changes

1. All Surf Scoter project files live in one dedicated directory, `.surf-scoter.d`.
2. That directory may sit anywhere in the repository, which allows more than one environment in a mono repo.  The VCS still clones the whole repository.
3. The project file lists the egress the environment needs, as host, port and protocol rules that don't depend on the gateway.
4. The project file is a suggestion.  The tools turn it into a resolved, annotated file for the developer to review.  The accepted result, stored in the developer's configuration directory, is the only thing the tools run from.


## The `.surf-scoter.d` directory

Everything Surf Scoter reads from the project lives in `.surf-scoter.d`: the project file, and the development container directories (`Dockerfile`, `dev-env.yaml` and any files they copy in).

**Why:**

* Every security-relevant project file sits under one path.  A change brought back from the workspace that touches `.surf-scoter.d` is easy to spot, which helps with [FILE-5](../security/files-attack-vectors.md#file-5-changes-brought-back-to-the-project).  Projects can protect it with CODEOWNERS or a review rule, and `scoter` can warn when a fetch touches it.
* The tools can fingerprint the whole directory (contents, file modes and file types) instead of only the `Dockerfile`.

**Rules for the whole directory:**

* Open the project file without following symlinks (`O_NOFOLLOW`), check with `fstat` that it's a regular file, and read it with a size limit.  A FIFO in its place would make the tools hang, and opening some device files has side effects; the size limit doesn't help with either, because the problem happens on open.
* The fingerprint records a symlink as a link (its target text), and never hashes what it points to.  That keeps the fingerprint about the directory's own contents.
* Warn about hard links (a link count above one) outside the container directories, rather than refusing them.  The project file's content is reviewed whatever it's linked to, and pnpm, deduplicating file systems and some backup tools create hard links legitimately.
* Keep the workspace and the setup directory out of `.surf-scoter.d`.  The directory the tools fingerprint must not contain anything the container writes to.

Outside the container directories, these rules are mostly defense in depth.  The real protection for the project file is that the tools parse it as data, the developer reviews it, and the runtime reads only the accepted copy (see below).  A symlinked project file is still parsed and reviewed; at worst, a link into the workspace causes nuisance re-reviews when the container writes to it.

### Container directories

Each development container directory inside `.surf-scoter.d` holds a `Dockerfile`, its `dev-env.yaml`, and any files they copy in.

**Why:**

* The tools set the Docker build context to the container directory, never to the project root.  The `.env` files, keys and other secrets elsewhere in the project can't end up in the image.  Don't rely on a `.dockerignore` file for this: it's a convention the project controls, not a control.

**Rules:**

* Refuse symlinks, hard links and special files anywhere in a container directory.  Docker itself is safe here: the Docker CLI packs the build context without following symlinks, and `COPY` of a link that points outside the context fails.  The rule protects against Surf Scoter's own code that reads or copies the directory (fingerprinting, setup directory staging, the `file` VCS), and against builders that may behave differently, such as some Podman or Buildah versions.  Without it, `.surf-scoter.d/dev-env/creds -> ../../.env` brings the secrets back through one of those steps.  Hard links on the same file system as the project could also share storage with files outside the directory, as in [FILE-2](../security/files-attack-vectors.md#file-2-object-files-shared-with-the-project-repository).
* Warn about files that look like secrets (`.env*`, `*.pem`, `id_*` and so on).  Everything in a container directory is input to the image.
* Recommend, and warn when missing, a digest on base images (`FROM image@sha256:...`).  A tag can point at new content at any time, so a fingerprint of the directory doesn't pin the image.

### Naming

This also settles the naming.  The docs currently mention `.scoter-config.yaml`, `.surf-scoter.yaml`, `.surf-scoter/allowed-domains.txt` and `.dev-env/`; v1 replaces the project-side ones with `.surf-scoter.d`.


## Repository layout and the VCS

We *suggest* putting `.surf-scoter.d` in the project's root directory, but it may live anywhere in the repository.  A mono repo could have one under `client/` and another under `server/`.

### Cloning the whole repository

The VCS clones the whole repository, not just the directory holding `.surf-scoter.d`.  Git has no way to clone only a sub-directory that also keeps the rest of the repository away from the container:

* A sparse checkout without a filter still copies every object into the workspace's `.git`, so the container can read the other directories and their history.
* A partial clone with a sparse filter needs upload-pack filter support, which is poorly supported and slow, and it interacts with `--no-local` ([FILE-2](../security/files-attack-vectors.md#file-2-object-files-shared-with-the-project-repository)).
* Rewriting the history (`git subtree split`, or exporting with `git archive`) works, but bringing changes back then needs a subtree merge or patches applied under the prefix.

So the whole repository is visible to the container.  This is a documented exposure, not a boundary: in a mono repo, the client's environment can read the server's code and any secrets committed to it.  [FILE-8](../security/files-attack-vectors.md#file-8-everything-readable-in-the-container-is-exposed) needs updating to say so.

### Finding the root

The repository root is either detected or given as `root`:

* Detection walks up from `.surf-scoter.d` looking for the VCS directory (`.git`, `.hg`).
    * In Git, `.git` can be a file (`gitdir: ...`) in worktrees and submodules.  Decide whether a submodule counts as a root.
    * Stop at the developer's home directory or a file system boundary.
    * Ignore `GIT_DIR` and `GIT_WORK_TREE` from the environment.
    * Walk and parse the files directly.  Don't run `git rev-parse --show-toplevel`, which reads the repository's configuration.
* `root` may only name an ancestor of `.surf-scoter.d`.  It decides what the container can read, so it goes in the accepted file (see below) like every other security-relevant setting.

Finding `.surf-scoter.d` from the current directory has the same concern in reverse.  Walking up could find one planted in a parent directory, such as an extracted archive or a shared folder.  Stop at the repository root, and identify the environment by the canonical absolute path of its `.surf-scoter.d`.

With more than one environment in a repository, the container names, the `~/.ssh/config` host aliases, the workspace paths and the accepted files all need an id per environment, not per repository.

### Shallow clones

The default Git setup clones with `--depth 4`.

* It limits how much old history, including old secrets, reaches the container, and it makes cloning faster.  Secrets in the current tree are still there, so treat it as reducing exposure, not as a control.
* Git ignores `--depth` for local clones that take the local shortcut.  With `--no-local` it should take effect.  Add a test that checks for `.git/shallow` in a new workspace.
* `--depth` implies `--single-branch`.  Developers who need other branches will be surprised, so make the depth and the branch choice user settings, not project settings.
* Bringing changes back still works: fetching from the shallow workspace into the full project succeeds, because the project already has the base commits.  A rebase or merge in the workspace that reaches past the depth will fail.
* [FILE-1](../security/files-attack-vectors.md#file-1-scoter-run-runs-git-inside-the-workspace) still applies.  Refresh a shallow workspace by pushing from the project into it, never by running `git fetch` inside it.

Mercurial and other VCS implementations follow the [VCS checklist](../security/files.md#adding-a-vcs-implementation), and need their own answer for root detection and history depth.

### The `file` VCS

The future `file` VCS script will take a `.gitignore`-style file that says what to copy.

* The file is part of the accepted configuration.
* Ship default exclusions for secret-looking files (`.env*`, `*.pem`, `id_*`, `.aws/`).
* Refuse symlinks and special files whatever the patterns say ([FILE-3](../security/files-attack-vectors.md#file-3-the-file-vcs-merge)).
* Use one well-tested matcher, such as `pathspec`.  The `.gitignore` negation and anchoring rules are easy to get subtly wrong.
* Consider an allowlist instead of, or as well as, the ignore list.  Forgetting to list a file is safer than forgetting to exclude one.


## Egress rules

The project file lists the network destinations the environment needs.  The rules describe *intent*, independent of the gateway, so v1 can grow beyond Squid.  `scoter run` translates the accepted rules into each gateway's configuration.

Use structured entries rather than a string mini-language.  They're easier to validate, diff and show during review:

```yaml
egress:
  - host: registry.npmjs.org
    port: 443
    protocol: tls
  - host: mirror.example.com
    port: 80
    protocol: http
    methods: [GET, HEAD]
```

### What each gateway can enforce

Each gateway implementation declares which parts of a rule it enforces (host, port, protocol, methods).  When an accepted rule asks for something the active gateway can't enforce, `scoter run` refuses to start, or warns loudly with the developer's explicit consent.  It never drops the condition quietly and allows the broader traffic.

* **Methods on TLS:** the Squid gateway sees only `CONNECT host:443`, and the design deliberately avoids intercepting TLS (see [network](../security/network.md)).  It can't enforce `methods` on `tls`.  Accepting `methods` on a TLS rule would only make the developer think it restricts something.
* **Ports other than 443:** the current gateway's guarantees (N2 and N4) assume TCP 443 through `CONNECT`.  Other ports need changes to Squid's port rules and to the firewall.  Plain TCP protocols (SSH on 22, Postgres on 5432) can't go through an HTTP proxy at all.  The firewall works on IP addresses, so hostname rules on raw TCP need the hostname pinned to its DNS answer, which holds up poorly for hosts behind a CDN.
* **Tunnels:** a tunnel such as WireGuard filters nothing by hostname on its own.  A gateway built on one still needs a separate policy component (nftables, a proxy, or the far end).

Record the gateway in the accepted file.  The same rule enforces different things on different gateways, so changing the gateway triggers a new review.

### Rule checks

* `methods` is only allowed with `protocol: http`.  Even then, a `GET` carries data in its path, query and headers.  Present a method limit as reducing damage (for example, no uploads to an artifact API), not as preventing data leaks.
* Flag plain `http` during review.  It allows tampering with downloads in transit, as well as reading them.
* Define exactly what `*.example.com` matches.  Refuse a bare `*` and wildcards on public suffixes.
* Flag hosts that can carry data out whatever the other rules say: code hosts, paste sites, `*.s3.amazonaws.com`, user content CDNs, webhook services.
* Keep refusing private, loopback, link-local and metadata addresses (`169.254.169.254`) after DNS resolution, even for a listed hostname (N3).
* Display hostnames in their `xn--` (punycode) form, or refuse non-ASCII hostnames, so a look-alike name (a Cyrillic `а` in `github.com`) is visible.


## Host commands

### `program` is a name

In the project file, a command's `program` is a name only, never a path.  It's the name the client is linked as inside the container, and the name the host looks up when it runs the command.  Refuse:

* `/` and `\`;
* `.`, `..` and empty names;
* NUL and other control characters, and non-ASCII characters;
* a leading `-`;
* drive and stream syntax (`C:aws`, `aws:stream`);
* Windows device names (`NUL`, `CON` and so on).

Decide how names match `PATHEXT` on Windows (`aws` against `aws.exe`), and whether matching ignores case on Windows and macOS.

The accepted file holds both the name the client sends in the handshake and the absolute path of the host executable.  The handshake name comes from the container, so the host only looks it up in the accepted file, as an exact match.  It never looks up the handshake name on the `PATH`.  This also closes [BRIDGE-11](../security/host-bridge-attack-vectors.md#bridge-11-the-hosts-path-lookup): what the host runs no longer depends on the `PATH` that `scoter-host` started with.

[The project configuration file](../project-configuration-file.md) needs updating to match: its example shows `program: /usr/local/bin/aws`, and its text says commands are found on the `PATH`.


## Reviewing the project file

The tools never act on the project file directly.  Instead:

1. **Parse** the project file as data, which drops its comments.  Nothing the project wrote as a comment reaches the reviewer, so a project can't add `# reviewed by security: safe`.
2. **Validate** it against the schema.
3. **Check names and arguments.**
4. **Resolve** host paths: each `program` to an absolute path, `root`, and the `${section:key}` and `@NAME@` references.
5. **Check paths.**
6. **Render** a new file in a private temporary location.  It holds the resolved values, plus comments generated by the tools that explain each risk.
7. **Ask** the developer to *use as-is*, *deny* or *edit*.

### Parsing defensively

* Use `safe_load`, with a size limit.
* Refuse anchors and aliases (they allow alias-expansion bombs), merge keys (`<<`), explicit tags and duplicate keys.  PyYAML keeps the last duplicate without warning.
* Refuse unknown keys, as the project file already does.

### Checks

Name-based checks have to look at what actually runs, not just the name:

* After resolving a `program`, also check the base name of its real target.  `mytool -> /usr/bin/env` passes a check on the name alone.  Treat multi-call binaries (`busybox`, `toybox`) as their own category.
* **Refuse:** shells, `env`, interpreters (`python`, `node`, `perl`, `ruby`, `pwsh`), privilege tools (`sudo`, `doas`), `xargs`, `ssh`.
* **Warn, with an explanation:** commands that run configuration from their working directory or their file arguments, such as `npm`, `make`, `docker`, `git`, `terraform`, `kubectl`, and `find` (because of `-exec`).  This is the [BRIDGE-2](../security/host-bridge-attack-vectors.md) class.  An allowed `docker` command can also mount the developer's home directory and rewrite the accepted file itself.
* Flag an `any` placeholder right after an option that runs code (`-c`, `-e`, `--eval`, `--exec`), whatever the command.
* Flag broad `env` `allow` patterns that pass variables which steer loading (`LD_PRELOAD`, `PATH`, `PYTHONPATH`, `NODE_OPTIONS`, `GIT_SSH_COMMAND`, `DOCKER_HOST`), per [BRIDGE-5](../security/host-bridge-attack-vectors.md#bridge-5-allowed-environment-variables-steer-the-program).

Path checks run on the resolved paths.  Refuse or flag:

* relative `PATH` entries;
* programs inside the project or the workspace;
* programs whose file, or any of its parent directories, other users can write to;
* version manager shims (asdf, mise, pyenv, nodenv, rbenv and similar).  A shim is an absolute path that picks the real program from `.tool-versions` or a similar file in its working directory, which may be the workspace.  Require the real executable.

Decide whether editing can override a refusal.  If an edit can bring back a refused command, the refusal is only advice.  Either apply the same refusals to edited files, or require an explicit override that the tools record.

### Rendering

Values from the project still reach the rendered file, so they need care too:

* Write every string in double-quoted style with escapes.  A block scalar (`|`) could put lines that look like comments on the page.  Refuse newlines in fields that don't need them, which is nearly all of them.
* Strip control characters, including terminal escape sequences, before anything is written to the file or the terminal.
* Restrict command names to ASCII, and display hostnames as described above.  This guards against bidirectional overrides, zero-width characters and look-alike characters.
* Mark generated comments with a fixed prefix (for example `#scoter:`) and regenerate them on every review.  That keeps warnings current, and keeps them apart from notes the developer adds.
* Start the file with a short summary of the highest-risk items.  On a re-review, show only what changed since the last accepted version.  Reviewing everything every time leads to rubber-stamping.

### The temporary file and the editor

* Create the file with `mkstemp` in a directory only the developer can read.  Open it without following symlinks, and read it once.
* Use `VISUAL`, then `EDITOR`.
* Start the editor in a neutral directory, such as the temporary directory, not the project.  Editors read project-local configuration (vim modelines and `.exrc`, VS Code workspace settings), which is [FILE-4](../security/files-attack-vectors.md#file-4-host-tools-acting-on-the-workspace) on the host.
* After the editor exits, run steps 1 to 6 again on the edited file, then ask again, the way `visudo` does.  Paths in the edited file now come from the developer, and get the same path checks as resolved ones.  Never store a file that fails validation.

### Defaults

* Pressing Enter without a choice denies.
* Without a terminal, the tools refuse to prompt and fail.  Automation passes an already accepted file explicitly (for example `--accept-file`); it never accepts automatically.


## The accepted file

The accepted file lives in the developer's configuration directory (`$XDG_CONFIG_HOME`, usually `~/.config`).  It is the only source the runtime reads for anything that affects security:

* the egress rules, and the gateway they were accepted for;
* the host commands: the handshake name, the absolute executable path, `args`, `env`, `cwd` and the path rules;
* the top-level `env`;
* `root` and the VCS settings, including the `file` VCS patterns;
* the fingerprint of the `.surf-scoter.d` directory, including the container directory, which covers `dev-env.yaml`'s mounts ([DEV-2](../security/dev-container-attack-vectors.md#dev-2-mount-points-are-written-into-the-compose-env-file)) and `broker-bin`.

If any of these is still read live from the project, it's a way around the review.

### What to store

* Store the canonical data the developer accepted, not just the rendered text.  The runtime loads that data and checks it.
* Store the fingerprint of the project file the data came from, along with the parsed suggestion itself.  With the accepted data, the last seen suggestion and the current suggestion, a re-review can tell "the project changed X" apart from "you changed Y".  Without that, each upstream change either overwrites the developer's edits or forces a full re-review.
* Freeze `${section:key}` and `@NAME@` references at their resolved values.  Frozen values are easier to audit, and a change on the host shows up as a difference at the next review.
* Optionally record each executable's hash, and warn, rather than refuse, when it changes.  When an executable's path disappears (after an upgrade, for example), ask for a new review rather than looking it up on the `PATH` again.

### Protecting it

* Create it with mode `0600`, and check its owner and mode on every read.
* Never put it in the setup directory or mount it into a container.  Unlike the setup files in [FILE-7](../security/files-attack-vectors.md#file-7-world-writable-setup-files), no other account should be able to touch it.

### Identity, expiry and revoking

The accepted file is keyed by the canonical path of `.surf-scoter.d`.  A second clone of the same repository, or a moved directory, gets a new review.  The expiry, temporary exemption and revoking ideas from [configuration weaknesses](../security/configuration-weaknesses.md#authorization-for-a-project) still apply, and expiry matters more here: the permissions of the cloud services a project uses can change while an old acceptance stays the same.


## Documents to update

* [The project configuration file](../project-configuration-file.md): the new location, `egress`, `program` as a name, `root`, and the review flow.
* [Network](../security/network.md): the allowed domain list becomes the accepted egress rules, translated per gateway.
* [File access attack vectors](../security/files-attack-vectors.md): FILE-5 (one directory to review), FILE-8 (the whole repository is exposed), FILE-9 (the workspace location).
* [Host bridge attack vectors](../security/host-bridge-attack-vectors.md): BRIDGE-11 (closed by absolute paths), and shims as a new vector.
* [Configuration weaknesses](../security/configuration-weaknesses.md): the accepted file replaces fingerprint-only authorization.
* [The developer environment Dockerfile](../project-dockerfile.md): `parents: dir` limited to `.surf-scoter.d` and the installation directory.
* [The system configuration file](../system-config-file.md): the new `project-file-search` and `dev-containers-search` paths.


# Implementation Plan

For each step, the documentation and tests should correspondingly have updates.  Steps that touch security-sensitive code list the red-team cases their tests must cover.

## 1. Change the default search paths for the project file and container directories.

Update the [defaults/setup.yaml](../../defaults/setup.yaml) file to now have the one place for the project file.

* `project-file-search` only looks for `.surf-scoter.d/project.yaml`.  Remove the `.surf-scoter.yaml`, `.surf-scoter.yml` and `.surf-scoter/container.y*ml` entries.  The project file must be in a directory named `.surf-scoter.d` (even when given with `--project-config`), and the project directory is the one holding it.
* `dev-containers-search` only looks in `.surf-scoter.d` and the installation's `containers` directory.  Remove `${scoter:project-dir}`, `containers/` and `.surf-scoter/`.
* The dev container, and every `parents` `dir` and `file` entry in a `dev-env.yaml`, must resolve (following symlinks) inside the project's `.surf-scoter.d` or one of the `dev-containers-search` directories.  The search list comes only from the user's and the installation's setup files, so it's user-trusted; the setup file warns against listing directories a project controls.
* Decide whether the installation's own container directories (such as `dev-env`) are fingerprinted, or trusted as part of the installation.
* Open the project file without following symlinks (`O_NOFOLLOW`), check with `fstat` that it's a regular file, and read it with a size limit.
* The `allowed-domains` setup entry stays until the gateway step (step 8) replaces it.  It still reads `.surf-scoter/allowed-domains.txt`, so a project that keeps its list in `.surf-scoter.d` (as this repository does) gets an empty allowlist until then; see step 8.
* **Remaining:** a dev container's `parents` can only name a `dir` or `file` by path.  A project's container that builds on one from the installation (such as `dev-env`) can't reach it: a path from `.surf-scoter.d` into the installation isn't portable, and a path to the checkout's own `containers/` is refused unless the checkout is the distribution.  Add a parent found by name through `dev-containers-search`, for example `parents: [{name: dev-env, tag: local-surf-scoter-env/dev-env:build}]`: the first search directory holding `NAME/Dockerfile` (and, when it has one, `NAME/dev-env.yaml`), with the same confinement as the other parents.  This repository's own `.surf-scoter.d/dev-env-container/dev-env.yaml` needs it to run with the self-host copy (see [CONTRIBUTORS.md](../../CONTRIBUTORS.md#working-on-surf-scoter-with-surf-scoter)).

Discovery walks up from the current directory to find `.surf-scoter.d`.  Until the VCS root detection exists (step 3), it stops at the current directory or `--project-dir`; after that, it stops at the VCS root.  A given `--project-dir` replaces the current directory (it's the `${scoter:cwd}` value), so the search never falls back to the directory the tool ran in.

Double check that the [SUC generator](../../host/src/bin/gen-scoter-uc.rs) follows those paths.

**Tests:** a symlink or FIFO in place of the project file; a `parents: dir` that points outside `.surf-scoter.d`; a `.surf-scoter.d` planted in a directory above the repository; a `name` parent found in the second search directory, missing from all of them, or found through a symlink that leads outside them.

## 2. Register Python as a program

The setup file needs to register the Python executable as a runnable program.

This will allow adding the distribution script directory files as part of the execution path.  It means rethinking how the [scoter script](../../scoter.py) find the Python executable - what's there works and is fine, but maybe it should instead pull from the registered python executable; this will allow for a `.venv` directory with installed libraries for the `main_*.py` scripts, if we think they need them.

## 3. Change VCS to discover the root directory.

Add a new VCS program to find the project root directory.  If the VCS doesn't have it out-of-the-box, then this means adding a new [script file](../../scripts/) to handle it (the name would be something like `(name)-vcs-find-root.py`).

Root detection rules:

* Walk up from `.surf-scoter.d`, and parse the files directly.  Don't run commands such as `git rev-parse --show-toplevel`, which read the repository's configuration.
* In Git, `.git` can be a file (`gitdir: ...`) in worktrees and submodules.  Decide whether a submodule counts as a root.
* Stop at the user's home directory or a file system boundary.
* Ignore `GIT_DIR` and `GIT_WORK_TREE` from the environment.
* A `root` value from the project may only name an ancestor of `.surf-scoter.d`.

The setup file checks already need the repository root: `SURF_SCOTER_HOME`, the user's setup file and a `distribution-dir` from the setup files must be outside it.  Until this step, `repository_root` in [`system.rs`](../../host/src/config/system.rs) stands in, walking up from the project directory to the nearest `.git` or `.hg` (stopping below the home directory).  Replace it with this step's detection, so both agree on where the repository is.

`${scoter:project-dir}` currently means both the directory to clone and the place for `.dev-env`.  Split it into two values:

* the VCS root, which `local-clone`, `show-fetch-changes` and `merge-changes` use;
* the project directory, the parent of `.surf-scoter.d`, which the environment is keyed by.

The container's starting directory in the workspace is the project directory's path relative to the VCS root.

With more than one `.surf-scoter.d` in a repository, anything named after the project directory needs to be named per environment, not per repository.  Give each environment an id: the project directory's name plus a short hash of its canonical absolute path.  The id needs no stored state, so the same value works at run time and in the verified storage (step 5).  Use it for:

* the container names;
* the workspace path.

Keep `--no-local --depth 4` on the Git clone, and add a test that the new workspace has a `.git/shallow` file and shares no files with the project (`find .git -links +1`).  Make the depth and the branch choice user settings.

**Tests:** `.git` as a file; `GIT_DIR` set in the environment; a `root` that isn't an ancestor; a repository nested inside another one; two environments in one repository, running at the same time.

## 4. Add programs and files to prohibit or warn to the default settings

Rather than hard-code program names and file glob patterns, these belong in the default settings file.  Then we can grow the list organically.

The lists to start with:

* **Refused programs:** shells, `env`, interpreters (`python`, `node`, `perl`, `ruby`, `pwsh`), privilege tools (`sudo`, `doas`), `xargs`, `ssh`, and multi-call binaries (`busybox`, `toybox`).
* **Programs to warn about:** commands that run configuration from their working directory or file arguments (`npm`, `make`, `docker`, `git`, `terraform`, `kubectl`, `find`).
* **Options that run code:** `-c`, `-e`, `--eval`, `--exec`, flagged when an `any` placeholder follows them.
* **Environment variables that steer loading:** `LD_PRELOAD`, `PATH`, `PYTHONPATH`, `NODE_OPTIONS`, `GIT_SSH_COMMAND`, `DOCKER_HOST`.
* **Version manager shims:** the install directories of asdf, mise, pyenv, nodenv, rbenv and similar.
* **Secret-looking files:** `.env*`, `*.pem`, `id_*`, `.aws/`.
* **Hosts that can carry data out:** code hosts, paste sites, `*.s3.amazonaws.com`, user content CDNs, webhook services.

The user's setup file can add to these lists.  Decide whether it can also remove entries.

## 5. Design the user-directory verified storage

The verified storage lives in the directory the setup files name as `scoter: paths: verified-dir` (default `~/.config/surf-scoter.d/verified`), not in `${scoter:user-dir}`, which isn't set when the user's setup file is `~/.config/surf-scoter.setup.yaml` or there's none.  Loading the setup files already refuses a `verified-dir` that's relative, or inside the project, its workspace or its repository.  It needs a solid structure around how to store each project's verified storage.

`running-env-config` and `running-env-config-lock` have the same gap: [defaults/setup.yaml](../../defaults/setup.yaml) puts them under `${scoter:user-dir}`, so they have no value without a user directory.  Give them a dedicated `scoter: paths` setting of their own (say, `state-dir`, default `~/.config/surf-scoter.d/state`), checked the same way as `verified-dir`, as they hold the ports and names of the running environments.

These should be stored in JSON formats so that Python can easily read it.  In order to allow for multiple project directories, I recommend an index file that points the local directory to the stored directory.  The stored directory is named by the environment id from step 3, so two directories with the same name don't collide and need no conflict resolution.  The directory here is *always* the parent of the `.surf-scoter.d` directory, not the VCS root; this allows for multi-project repositories.  The index maps the full canonical path to the id, which also makes it easy to list and revoke acceptances.

For the fingerprinting and diff'ing of files, this implies keeping the full text of the project setup file and all used container directories (following the dev-env.yaml file parent DAG).  I recommend a tar.gz store.

* Refuse symlinks, hard links and special files when building the archive.  Never extract it with the default `tarfile` behavior; use `filter="data"`, or read members without extracting.
* Store three versions: the accepted data, the last project suggestion the user saw, and the current suggestion.  The diff step (step 16) needs all three.

I started a little of the implementation in the [auth_project_file.py](../../scripts/surf_scoter_lib/project_auth/auth_project_file.py) file, but it needs improvements.  I also wonder about the need to store fingerprints if the whole files are stored, though fingerprinting the Dockerfiles and the setup file by looking at the content rather than the whole file may be helpful.  Fingerprints still earn their place as a lookup key: storing several accepted snapshots keyed by the fingerprint of the suggestion lets a branch switch find an earlier acceptance without reading every archive.

Fix the related code while here:

* [`docker.py`](../../scripts/surf_scoter_lib/project_auth/docker.py) - With whole directories stored, hash the file bytes directly.

Record the setup the review was made under.  The accepted data is only right for the boundaries it was reviewed with, and those come from the setup files, not the project.  A user or distribution change to any of them changes what gets built and run, so store their resolved values with each acceptance, and ask for a new review when one changes:

* `scoter: values: project-files-dir`;
* `scoter: path-lists: dev-containers-search`;
* the VCS section's `runs` for the project's `vcs` (`local-clone`, `fetch-changes-into`, `show-fetch-changes`, `merge-changes`);
* the gateway the egress rules were accepted for (step 8);
* `scoter: paths: verified-dir` itself, so a moved store can't quietly start empty, or be swapped for another.

Protect the storage:

* Directories have mode `0700`, and files `0600`.  Check the owner and mode on every read.
* Update the index by writing a new file and renaming it over the old one, with a lock so two `scoter` runs can't interleave.

To keep in mind:

1. The user may want to blanket authorize the directory, regardless of changes (they'd still need to perform the "git fetch" process manually).  This would cover use cases where the user creates a private project that never gets contributions from others.  A blanket authorization skips the review only: path resolution, the refused lists (step 4) and a new review on a gateway change (step 8) still apply.
2. The user may switch between branches that have different project setups.  This may mean each branch has its own separate user configuration, or the user has just one.  In either case, this shouldn't mean the user needs to re-authorize changes when they change branches, if the branch hasn't made changes.  The fingerprint-keyed snapshots above handle this, limited by each snapshot's expiry.

I would prefer that the management of the validated projects within the user directory remain in the Python code.  I may revisit this decision later.

**Tests:** an archive member that's a symlink or has `..` in its name; a storage file with the wrong owner or mode; two runs updating the index at once.

## 6. Update the project file format

Change around the project file format schema to include the new features - egress registration and VCS details.

This will affect the schema, the [universal file document](universal-configuration-file.md), and the SUC generator.  The generator will include this file's contents in a new wrapped area, "current-project-setup", and it will no longer directly be used to construct the "host-brokered" section.

Land this step together with step 7, or keep the old way of building "host-brokered" behind a flag until step 7 is done, so the main branch always works.

The schema enforces:

* `program` is a name only: no `/` or `\`, no `.`, `..` or empty names, no control or non-ASCII characters, no leading `-`, no drive or stream syntax (`C:aws`, `aws:stream`), and no Windows device names.
* `egress` entries are structured (`host`, `port`, `protocol`, optional `methods`).  `methods` is only allowed with `protocol: http`.  A bare `*` and wildcards on public suffixes are refused.  Hostnames are stored and shown in their `xn--` (punycode) form.
* `root`, when given, is relative and names an ancestor (checked in step 3).
* No newlines in fields that don't need them.

The YAML parsing refuses anchors and aliases, merge keys (`<<`), explicit tags and duplicate keys, and applies a size limit.  Check how the Rust YAML library handles duplicate keys; libraries differ.

Decide whether `${section:key}` and `@NAME@` references are frozen at acceptance.  Today `scoter-host` resolves `@NAME@` when it starts, which conflicts with the design's frozen values.

**Tests:** duplicate keys; an alias bomb; a `program` with each refused form; `methods` on a `tls` rule; a non-ASCII hostname.

## 7. Update the SUC generator to also pull in the user's verified project settings

Add another CLI option to the SUC generator to read from the user's verified project settings, and integrate the results into the SUC file.  At this point, the "host-brokered" section will become valid again.

This will introduce a new section for the egress settings, and expand the VCS settings.  A consumer of the SUC should be able to recreate the original user's verified project settings.

"host-brokered" is built only from the verified settings, never from "current-project-setup".

From this step on, the SUC is security-critical: whoever can write it decides what runs on the host, and `scoter-host` loads it without running the setup checks again.

* Write it only with `mkstemp` (mode `0600`) in a directory only the user can read, as `scoter.py` does now, and never into the setup directory the container mounts.
* Pass sub-commands its path, never its contents through the environment, and remove it when the run ends.
* Decide what `scoter-host` checks again when it loads the SUC.  At least: the file's owner and mode, that its project and dev container locations are still where the accepted settings say, and that the accepted settings it carries match the ones in `verified-dir` (by fingerprint), so an old or edited SUC can't stand in for a review.

## 8. Add a default section for the network gateway settings

With the addition of the egress setup, the network gateway translation of those settings to the one used by the container become important.  Most likely, this will fork off to a script in the scripts directory that will manage the implementation-specific translation.  The settings will also need to include a list of mount mappings for the generated file (would need to be ingested by the docker-compose section).  First cut will limit the output to a single file.

This will also need a method to read those settings and report on what it can and can't manage.  The caller will use this reported information for end-user notices.  The information will need sufficient context to cover the above scenarios.

* The first version refuses any accepted rule the gateway can't enforce.  It never drops a condition and allows the broader traffic.  For Squid, that means `tls` on port 443 without `methods`, and plain `http` with `methods`.
* Ports other than 443 also need firewall rule changes (N4), not just Squid changes.  The first cut can refuse them.
* Keep refusing private, loopback, link-local and metadata addresses after DNS resolution.
* The verified settings record the gateway they were accepted for.  A different gateway at run time triggers a new review.
* This replaces the `allowed-domains` setup entry and `.surf-scoter/allowed-domains.txt`.  Until then, the entry in [defaults/setup.yaml](../../defaults/setup.yaml) points at the old location, and a missing list makes `main_run.py` create an empty one with mode `0666` (FILE-7).  If this step is a while off, change the default to `${scoter:project-dir}/${scoter:project-files-dir}/allowed-domains.txt` in the meantime, and refuse to start without a list instead of creating one.

**Tests:** `methods` on a `tls` rule; a rule for port 22; a listed hostname that resolves to `169.254.169.254` or a private address; a gateway change after acceptance.

## 9. Update `scoter-host` to run accepted executables

The host bridge (Rust) needs to change to match:

* Run the absolute executable path from the SUC.  Remove the `PATH` lookup in `find_on_host` ([`exec.rs`](../../host/src/host/exec.rs)), which closes BRIDGE-11.
* Match the name the client sends in the handshake exactly against the accepted names, and never look it up anywhere else.
* Decide how names compare on Windows (case, and `PATHEXT` such as `aws` against `aws.exe`).

**Tests:** a handshake name that isn't accepted; a relative `PATH` entry on the host; an executable removed after acceptance.

## 10. Add the verification checks library

A Python library, used by the verify and run scripts, that runs the checks from the design using the lists from step 4:

* **`.surf-scoter.d` contents:** refuse symlinks, hard links and special files in container directories; warn about hard links elsewhere; warn about secret-looking files; warn when a `FROM` line has no digest.
* **Program resolution:** resolve each `program` to an absolute path, then check the base name of its real target against the refused list.
* **Path checks:** refuse or flag relative `PATH` entries, programs inside the project or workspace, programs whose file or parent directories other users can write to, and version manager shims.
* **Arguments and environment:** flag `any` placeholders after options that run code, and broad `env` `allow` patterns that pass variables which steer loading.
* **Egress:** flag plain `http` and hosts that can carry data out.

Each finding carries a severity (refuse or warn) and an explanation for the generated comments.

**Tests:** `mytool -> /usr/bin/env`; a shim; a program in a directory others can write to; a symlink in a container directory; a `.env` file in a container directory.

## 11. Establish a user verification file format

When verifying a project directory, the user will need an easy to use file format.  As the rest of the scoter files use YAML, I think this should also be in YAML format.

The file needs to include all the options the user needs to sign-off on.  This will need some iterating to make it easy to use.

Ideas:

- Start with a short summary of the highest-risk findings.
- Commands and egress can show as the resolved version of what's in the project file.
- Docker container directories may list the parent image, warnings about not pinning the parent image to a SHA, may list the file contents.
  - Show the digest each parent image resolves to at verification time.  This may require calling out to `docker`, which may need to fetch remote images which it may not have access to (not currently logged in), and may take a while (network connection times).
  - Maybe have a "allow: true/false" for these, but that isn't very reasonable, as disallowing a single file or pointing at a different parent image will generally cause problems.
- Include the "expires-at" field, so the user can declare how long it's good until, with a default value.  The "exempt-until" should be handled outside this user file.
- Include the current branch, if the VCS can identify it.  Read it from the project directory, never the workspace.
- Include the gateway the egress rules are accepted for.
- Generated comments start with a fixed prefix (for example `#scoter:`), so they can be regenerated and kept apart from the user's own notes.  This means storing the user's version of this file, and not just keep the parsed data as JSON.

## 12. Add the user verification file format parser

Parsing the user verification file requires YAML parsing, which we currently only parse through the SUC generator.  It makes sense to allow this to be an additional input file passed to the SUC generator as another input file format.  It would output into the SUC in the same way as the verified project settings.

The same parsing rules as step 6 apply.  After parsing, the checks library (step 10) runs on the result before anything is stored: paths the user typed get the same path checks as resolved ones, and the refused lists still apply.

A missing piece here is the fingerprint parts of the project directory.  However, that may not be included in the file, based on its design in part 5 above.

## 13. Add the main_verify.py script

Add in a script that can process the user-directory verified storage and construct the items for the user to verify.

This will need to construct the user verification file, which includes comments about the discoveries.

The script can construct a YAML file without adding a library - strings output using JSON encoded strings, everything else is simple indented dict and list.  Keep `ensure_ascii=True`, so bidirectional, zero-width and other non-ASCII characters show up as `\u` escapes, and strip control characters from anything printed to the terminal.

The code that constructs the pieces for verification should go into a library for use by other scripts.

**Tests:** a value containing a newline and `# reviewed: safe`; bidirectional override and zero-width characters; terminal escape sequences.

## 14. Add the use / deny / edit prompt

`main_verify.py` presents the verification file and asks the user to use it as-is, deny it, or edit it.

* Create the file with `mkstemp` in a directory only the user can read.  After editing, open it without following symlinks and read it once.
* Use `VISUAL`, then `EDITOR`, started with the temporary directory as its working directory, not the project.
* After the editor exits, parse and check the file again (step 12) and ask again.  Never store a file that fails.
* Pressing Enter without a choice denies.
* Without a terminal, refuse to prompt and fail.

**Tests:** the temporary file replaced by a symlink while the editor is open; an edit that adds a refused program; no terminal.

## 15. Add verification handling in main_run.py

The main_run.py script should then consume the verification section from the SUC, and use the above library to determine if the verification applies to the current settings.

Build the container from the stored, accepted copy (step 5), or from a copy staged from it, never from the live `.surf-scoter.d`.  Otherwise the directory can change between the check and the build.

In the first cut, the run script will not allow re-validation, but instead will instruct the user how to re-verify.

**Tests:** a change to `.surf-scoter.d` after verification and before the build; an expired acceptance.

## 16. Add diff verify to main_verify.py script

Add in the capability for the main_verify.py script to perform a diff if the directory changed.

Use the three stored versions (step 5) to show what the project changed separately from what the user changed, and show only those differences in the verification file.

## 17. Final bow

- Allow a timed exemption for working on a project.
- Allow a user to declare a project "accept" through CLI args (also used by CI/CD).  This passes an already accepted file explicitly; it never accepts a suggestion automatically.
- Allow a user to revoke a project's acceptance.
- Allow a user to list accepted projects, with their expiry and exemptions.


# Future Tasks

## Choosing the setup for a project run

`--user-setup` was removed: with the egress rules in the project file, and the accepted settings in the user's own storage, a project no longer needs its own setup file.  The remaining need is to run a project with a different docker-compose file and related images (say, an alternate gateway, or extra service containers).

Instead of reading another setup file, let the user choose, for a project run, which setup section to use for those parts, from sections their own setup files (or the distribution's) define.  The choice is the user's, recorded with the project's accepted settings, never the project's.

This also bears on the accepted egress rules: they were reviewed for one gateway (step 8), so a section with a different gateway, or a different compose file, may enforce them differently.  Switching sections should check the accepted rules against what the chosen section's gateway can enforce, and ask for a new review when it can't enforce them, or enforces them differently.
