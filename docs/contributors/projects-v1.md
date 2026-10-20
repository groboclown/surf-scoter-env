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

The repository root is detected, never given by the project:

* Detection walks up from `.surf-scoter.d` looking for the VCS directory (`.git`, `.hg`).
    * In Git, `.git` can be a file (`gitdir: ...`) in worktrees and submodules.  Decide whether a submodule counts as a root.
    * Stop at the developer's home directory or a file system boundary.
    * Ignore `GIT_DIR` and `GIT_WORK_TREE` from the environment.
    * Walk and parse the files directly.  Don't run `git rev-parse --show-toplevel`, which reads the repository's configuration.
* The project can't name a root of its own: what the container can read would then be the project's choice.  The `file` VCS copies the project directory, the parent of `.surf-scoter.d`.

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
4. **Resolve** host paths: each `program` to an absolute path, and the `${section:key}` and `@NAME@` references.
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
* the VCS settings, including the `file` VCS patterns;
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

* [The project configuration file](../project-configuration-file.md): the new location, `egress`, `program` as a name, and the review flow.
* [Network](../security/network.md): the allowed domain list becomes the accepted egress rules, translated per gateway.
* [File access attack vectors](../security/files-attack-vectors.md): FILE-5 (one directory to review), FILE-8 (the whole repository is exposed), FILE-9 (the workspace location).
* [Host bridge attack vectors](../security/host-bridge-attack-vectors.md): BRIDGE-11 (closed by absolute paths), and shims as a new vector.
* [Configuration weaknesses](../security/configuration-weaknesses.md): the accepted file replaces fingerprint-only authorization.
* [The developer environment Dockerfile](../project-dockerfile.md): `parents: dir` limited to `.surf-scoter.d` and the installation directory.
* [The system configuration file](../system-config-file.md): the new `project-files-dir`, `project-file-name` and `dev-containers-search` settings.


# Implementation Plan

For each step, the documentation and tests should correspondingly have updates.  Steps that touch security-sensitive code list the red-team cases their tests must cover.

## 1. Change the default search paths for the project file and container directories.

Update the [defaults/setup.yaml](../../defaults/setup.yaml) file to now have the one place for the project file.

* The tools take a project *directory*, never a project file: `scoter --project-dir` (where to start looking) and `gen-scoter-uc --project-dir` (the project).  The project file is `values: project-file-name` (default `project.yaml`) in the project directory's `values: project-files-dir` (default `.surf-scoter.d`), both single names from the setup.  `scoter --project-config`, `gen-scoter-uc --config` and the `project-file-search` path list are gone.
* `dev-containers-search` only looks in `.surf-scoter.d` and the installation's `containers` directory.  Remove `${scoter:project-dir}`, `containers/` and `.surf-scoter/`.
* The dev container, and every `parents` `dir` and `file` entry in a `dev-env.yaml`, must resolve (following symlinks) inside the project's `.surf-scoter.d` or one of the `dev-containers-search` directories.  The search list comes only from the user's and the installation's setup files, so it's user-trusted; the setup file warns against listing directories a project controls.
* Decide whether the installation's own container directories (such as `dev-env`) are fingerprinted, or trusted as part of the installation.
* Open the project file without following symlinks (`O_NOFOLLOW`), check with `fstat` that it's a regular file, and read it with a size limit.
* The `allowed-domains` setup entry stays until the gateway step (step 9) replaces it.  It still reads `.surf-scoter/allowed-domains.txt`, so a project that keeps its list in `.surf-scoter.d` (as this repository does) gets an empty allowlist until then; see step 9.

Discovery walks up from the current directory to find `.surf-scoter.d`, stopping at the VCS root (step 3); outside any repository, it only looks in the current directory or `--project-dir`.  A given `--project-dir` replaces the current directory (it's the `${scoter:cwd}` value), so the search never falls back to the directory the tool ran in.

Double check that the [SUC generator](../../host/src/bin/gen-scoter-uc.rs) follows those paths.

**Tests:** a symlink or FIFO in place of the project file; a `parents: dir` that points outside `.surf-scoter.d`; a `.surf-scoter.d` planted in a directory above the repository (tested with step 3's search).

Completed.

## 2. Register Python as a program

The setup file needs to register the Python executable as a runnable program.

This will allow adding the distribution script directory files as part of the execution path.  with this, the [scoter script](../../scoter.py) finds the Python executable through this path, rather than reusing the default Python interpreter.

Completed: [defaults/setup.yaml](../../defaults/setup.yaml) has a `python` section, with `programs: exec` (default `python3`) and `runs: sub-command` (default `${exec} -E -s {x:script}`).  [`scoter.py`](../../scoter.py) starts sub-commands with that command, not `sys.executable`, and stops with an error when the interpreter isn't found.  `-E` keeps a project's `PYTHONPATH` (say, from a `.envrc`) and other `PYTHON*` variables away from the scripts, and `-s` the user's site-packages.

The interpreter is a program the setup files name, so a `./` value (relative to the project directory) or one inside the project would run project-controlled code on the host.  The same holds for every `programs` entry.  `scoter user-setup` now checks for this ([`setup_check.py`](../../scripts/surf_scoter_lib/setup_check.py)): it reports, and fails on, any `programs` value written as `./...`, referring to a project location (`${scoter:project-dir}`, `${scoter:cwd}` and the like), or a relative path with a directory in it; any program found inside the project directory, the project files directory, the workspace or the repository; and any relative or empty `PATH` entry.

## 3. Change VCS to discover the root directory.

Find the root of the repository holding the project, so the whole repository is cloned, and the setup file checks know where the repository ends.

Completed:

* **Extensible, in separate scripts.**  Each `NAME-vcs` setup section that can find its root has a `runs: find-root` command, given `{x:start}`, that prints the root (or nothing).  The distribution has a script for each kind of search, [`vcs_root_nearest.py`](../../scripts/vcs_root_nearest.py) (Git `.git`, Mercurial `.hg`, Subversion `.svn`) and [`vcs_root_outermost.py`](../../scripts/vcs_root_outermost.py) (CVS's `CVS` directories, one in every directory of a checkout), both given the marker's name; they share [`vcs_root.py`](../../scripts/surf_scoter_lib/vcs_root.py).  Adding a VCS is a setup change.  The VCS is never run (no `git rev-parse --show-toplevel`, which reads the repository's configuration), no environment variable (such as `GIT_DIR`) is used, and the search stops below the home directory and at a file system boundary.  [defaults/setup.yaml](../../defaults/setup.yaml) gives Git's.
* **Found by `scoter`, given to the generator.**  [`scoter.py`](../../scoter.py) finds the root during its two-phase loading: phase 1 loads the setup alone; `scoter` then runs every VCS section's `find-root` from the search's start (the current directory, or `--project-dir`), and the nearest root wins.  Phase 2 passes the project directory and the root as `gen-scoter-uc --project-dir` and `--vcs-root`; the generator checks that the root holds the project directory and records it as `${scoter:vcs-root}`.
* **Submodules.**  A Git submodule's `.git` file is a marker, so running a command inside the submodule puts you in its context; above it, you're in the owning repository's.
* **Project search.**  The project directory is looked for in the search's start and each directory above it up to the root, never above it, so a `.surf-scoter.d` planted above the repository isn't found.  Outside any repository, only the start is searched.
* **The project's `vcs` must match.**  A project whose `vcs` has a `find-root` must be in a repository of that VCS; one without (e.g. `file`) can be anywhere.
* **One answer for the repository.**  The setup file checks (`SURF_SCOTER_HOME`, the user's setup file, `distribution-dir`, `verified-dir`) and [`setup_check.py`](../../scripts/surf_scoter_lib/setup_check.py) use `${scoter:vcs-root}`; the interim walk-up in `system.rs` is gone.
* **Cloning.**  `git-vcs`'s `local-clone` clones `${scoter:vcs-root}`; `file-vcs` still copies the project directory, as it has no repository.

**Tests:** in [`test_vcs_root.py`](../../scripts/tests/s_lib/test_vcs_root.py): `.git` as a directory and as a submodule's file; the `outermost` search, with directories not yet added and gaps; the home directory stop.  In [`test_scoter.py`](../../scripts/tests/test_scoter.py), running the scripts as `find-root`: the nearest root of several VCS; a submodule; two VCS with one root; failing or unusable `find-root` commands; the project directory found above the start but not above the root, with the setup's names; the project's `vcs` against the root's.  In [`config.rs`](../../host/src/config.rs): a `--vcs-root` that doesn't hold the project directory, or doesn't exist; the project file found with the setup's names.  Still to add: the starting directory in the workspace, and `GIT_DIR` set when the VCS commands run.

## 4. Add programs and files to prohibit or warn to the default settings

Rather than hard-code program names and file glob patterns, these belong in the default settings file.  Then we can grow the list organically.

The lists to start with:

* **Refused programs:** shells, `env`, interpreters (`python`, `node`, `perl`, `ruby`, `pwsh`), privilege tools (`sudo`, `doas`), `xargs`, `ssh`, and multi-call binaries (`busybox`, `toybox`).
* **Programs to warn about:** commands that run configuration from their working directory or file arguments (`npm`, `make`, `docker`, `git`, `terraform`, `kubectl`, `find`, `poetry`, `yarn`).
* **Options that run code:** `-c`, `-e`, `--eval`, `--exec`, flagged when an `any` placeholder follows them.
* **Environment variables that steer loading:** `LD_PRELOAD`, `PATH`, `PYTHONPATH`, `NODE_OPTIONS`, `GIT_SSH_COMMAND`, `DOCKER_HOST`.
* **Version manager shims:** the install directories of asdf, mise, pyenv, nodenv, rbenv and similar.
* **Secret-looking files:** `.env*`, `*.pem`, `id_*`, `.aws/`.
* **Hosts that can carry data out:** code hosts, paste sites, `*.s3.amazonaws.com`, user content CDNs, webhook services.

The user's setup file can add to these lists, and remove or replace entries (see [Combining the checks](#combining-the-checks)).

### Draft: a `host-commands` kind of value

None of the current kinds fit.  Each finding needs an action and an explanation (step 11), not just a name, and `path-lists` replace the whole list, so a user adding one program would have to copy the distribution's list and then keep it up to date.

Add a sixth kind, `host-commands`, next to `paths`, `programs`, `runs`, `path-lists` and `values`.  Each key is one check on the project's host commands: a mapping with a fixed set of fields, which the loader validates like the rest of the setup.  It's named for what it checks, so later checks (the project files, the egress hosts) get kinds of their own, with their own fields, rather than sharing one.

Any section can have `host-commands`, so a check can sit with the tool it's about: the `docker` section maps the `docker` command to its own `${exec}`, and the `git-vcs` section warns about `git -c`.  The checks that don't belong to any tool (shells, interpreters and the like) go in a `host-bridge` section.  The cost is that the checks are spread through the setup; the generator's combined section in the SUC (below), and a listing from `scoter user-setup`, put them back in one place, each with the `section:key` it came from, so a user knows what to override.

```yaml
docker:
    programs:
        exec: podman
    host-commands:
        docker:
            action: warn
            reason: >-
                An allowed `docker` can mount your home directory into a
                container, and rewrite the accepted settings themselves.
            command-map:
                docker: ${exec}

host-bridge:
    host-commands:
        shells:
            action: refuse
            reason: >-
                A shell runs whatever command it's given, so the container
                could run anything on your computer.
            commands: [sh, bash, dash, zsh, ksh, mksh, csh, tcsh, fish, nu, cmd, powershell, pwsh]
        version-manager-shims:
            action: refuse
            reason: >-
                A shim picks the real program from a file such as `.tool-versions`
                in its working directory, which may be the workspace.  Name the
                real program instead.
            paths: [~/.asdf/shims, ~/.local/share/mise/shims, ~/.pyenv/shims, ~/.nodenv/shims, ~/.rbenv/shims]

git-vcs:
    host-commands:
        git-options:
            action: warn
            reason: >-
                `-c` and `--config-env` set any Git configuration, including
                `core.sshCommand` and `core.fsmonitor`, which run commands.
            commands: [git]
            options: [-c, --config-env, --exec-path]
```

**Fields:**

* `action` (required): `refuse`, `warn` or `allow`, from most to least restrictive.
* `reason` (required): the explanation that goes into the generated `#scoter:` comment.
* What it matches, one or more of the fields below.  When a check has more than one, all of them must match (so `commands` with `options` means "these options, on these commands").  The loader refuses combinations that make no sense, such as `paths` with `options`.
    * `commands`: names, as the project file's `command` field gives them.  Each is found the way a `programs` value is (a bare name on the `PATH`), so `commands: [git]` is short for `command-map: {git: git}`.
    * `command-map`: names mapped to the host program each one runs, expanded as a `programs` value is, so it can refer to one (`docker: ${docker:exec}`).
    * `name-patterns`: name patterns, with `*`, `?` and `[...]` as in shell globs (`python*` covers `python3.12`).  Matched against the command's name, and the base name of the file its executable really is (with a Windows `PATHEXT` extension removed), case sensitive.  These aren't resolved: there's no one program for `python*`.
    * `options`: options that run code (each starting with `-`).  Matched when an `any` placeholder follows the option, or is joined to it (`--eval=...`).  Without `commands` or `name-patterns`, they apply to every command.
    * `env`: environment variable names, or patterns a project might write (`LD_*`), matched against a command's `env` `allow` entries, as exact text (see [Design decisions](#design-decisions)).
    * `paths`: directories, expanded like `path-lists` entries (`~`, `${...}`, and a whole entry that refers to a `path-lists` list puts its items in its place).  Matched when the resolved executable is inside one.
* `overrides` (optional): the checks (`section:key`) this one replaces, for the commands it matches only.  See [Relaxing a check without copying it](#relaxing-a-check-without-copying-it).

No other field is allowed, and the loader reports a misspelled one: a typo that quietly turned off a refusal would be the worst kind of setup mistake.  `command-map` values and `paths` entries are expanded; the other fields are used as written, and a `${` in them is an error, to catch a reference that would do nothing.

**Nothing can refer to a check.**  `${host-bridge:shells}` is unresolvable, and an error.  (`overrides` names checks, but as plain `section:key` text, never expanded.)

### Resolving the commands

The generator resolves every `commands` and `command-map` name to a host executable, so the resolved form only has a mapping, name to absolute path (or `null` when the program isn't there, as for `programs`).  The `commands` list doesn't appear in it.

* An executable inside the project directory, the project files directory, the workspace or the repository is an error, found after resolving symlinks, the same check the setup's `programs` get (step 2).
* Matching uses the file the executable really is, not just its name.  A project command `mytool` whose executable is a link to `/usr/bin/env` matches the check that names `env`, because both resolve to the same file.  `name-patterns` covers what identity can't: a `python3.11` next to the `python3.12` that `python3` resolves to.
* A command mapped to two different executables, by two checks, is an error that names both; the user overrides one of them.
* The mapping is what the review resolves a project's `command` to (step 11), in place of a `PATH` lookup, so `docker` runs the user's `podman`.  A name no check maps is still found on the `PATH`, as now.

### Combining the checks

Checks merge key by key across the setup files, like every other kind:

* **adding** is a new check under a new name, without copying anything;
* **changing** a check means writing the whole check again; its fields aren't merged with the lower priority file's;
* **removing** is setting the check to `null`.

The SUC gets a new top-level `host-command-checks` section with every section's checks combined: the merged `command-map` (each name with its executable and the check it came from), and the checks themselves, with their `section:key`, their expanded `paths`, and without the removed ones.  Each check lists the setup `files` that set its `section:key`, highest priority first: the check is the first one's alone, as fields aren't merged, and the rest are the versions it hides (which step 5 needs).  The `setup` record keeps them as written.

The section's `notices` are objects, ready for translation though the tools don't translate yet: the `message` to show, an `id` and `parameters` that say the same, and the `sources` (setup files) they're about.  The [universal configuration file](universal-configuration-file.md#notices) lists the ids.

A project command's action is worked out in two steps, and the review shows the reason of every check that counts:

1. The checks without `overrides` that match the command: the most restrictive of their actions.
2. Each override for the command takes the place of the check it overrides; the action is the most restrictive of what's left.  An override alters what one check says; it never decides a command on its own, so the other matching checks still count, and an override for a command no check matches does nothing.

For checks that only name commands, the generator works this out ahead of time, per command, and records which checks an override replaced.  Checks with `options`, `env` or `paths` depend on the project's command, so the checks library (step 11) applies the same rule when it runs.

### Relaxing a check without copying it

Replacing a check to relax one entry freezes the rest of it.  The user who copies `shells` without `ksh` keeps that copy when an upgrade adds a newly found shell to the distribution's `shells`, so the new shell is never refused.  The distribution needs to keep adding to its checks as new problems are found, so the normal way to relax a check has to leave the rest of it alone.

A check can name the checks it overrides, for the commands it lists only:

```yaml
host-bridge:
    host-commands:
        ksh-is-mine:
            action: warn
            reason: I review every ksh script this project's commands run.
            commands: [ksh]
            overrides: [host-bridge:shells]
```

For a command `ksh-is-mine` matches, `host-bridge:shells` doesn't apply, and `ksh-is-mine`'s action does.  Everything else about `shells` stays as the distribution defines it, including the shells it adds later.

The rules keep this narrow, as one careless override could undo a whole check:

* **Named checks only.**  `overrides` lists `section:key` names, never patterns.  Any other check that matches `ksh`, including one a later upgrade adds, still applies, and the most restrictive of those still wins.  An override switches off one check for some commands; it doesn't make a command trusted.
* **Exact names only.**  An overriding check matches with `commands` or `command-map`, plus `options` or `env` names when the check it overrides has them.  `name-patterns`, `env` patterns and `paths` aren't allowed, so `name-patterns: ['*']` can't switch off a whole check.
* **Only what the target covers.**  Each listed command must be one the overridden check matches.  One it doesn't match (the distribution dropped it, or the user misspelled it) is reported, so stale overrides get cleaned up.  An override of a check that doesn't exist is reported too, but isn't an error: the override then does nothing, which errs on the strict side.
* **Only downwards in priority.**  A check can only override checks from the same file or a lower priority one, so the distribution's setup can't override the user's.
* **Either direction.**  `action: refuse` in an override makes a command stricter than the check it replaces; that's allowed, though adding a check does the same without `overrides`.
* **One override per check and command.**  Two checks overriding the same check for the same command (or, with `options` or `env`, for an option or variable in common) is an error: there's no good answer to which one says what the check becomes.  An override also can't override another override; overrides only replace checks without `overrides`.

Replacing a whole check, or setting it to `null`, still works, for when the user really means to take over the check.  Those are the cases that miss upgrades, so the tools keep them visible:

* the generator compares a replaced check with the lower priority file's version, and reports the commands, options, `env` names and paths the lower one has that the replacement doesn't;
* `scoter user-setup` lists the distribution's checks the user's setup removes, replaces, or overrides, with what each one gives up;
* step 5 records, with each acceptance, the setup's resolved checks *and* the lower priority versions they hide, so an upgrade that changes a hidden check asks for a new review, even though the user's copy hasn't changed.

(`overload` was the other name considered; `overrides` says what it does to the named checks.)

`allow` adds nothing when another check matches, because the more restrictive one wins.  It's for commands no other check covers: the review can tell "your setup allows this" apart from "no check knows this command", and a later strict mode could refuse every command without an `allow`.

### Draft: the starting lists

`host-commands` checks (in `host-bridge`, unless the Section column says otherwise):

| Check | Section | Action | Matches |
|---|---|---|---|
| `shells` | | refuse | `sh`, `bash`, `dash`, `zsh`, `ksh`, `mksh`, `csh`, `tcsh`, `fish`, `nu`, `cmd`, `powershell`, `pwsh` |
| `command-runners` | | refuse | `env`, `xargs`, `nice`, `nohup`, `timeout`, `stdbuf`, `setsid`, `chroot`, `unshare`, `nsenter`, `script`, `watch`, `parallel`, `strace`, `ltrace`, `gdb` |
| `interpreters` | | refuse | `node`, `nodejs`, `deno`, `bun`, `awk`, `gawk`, `mawk`, `nawk`, `Rscript`, `julia`, `osascript`, `cscript`, `wscript`, `mshta`; name patterns `python*`, `pypy*`, `perl*`, `ruby*`, `php*`, `lua*`, `tclsh*`, `wish*` |
| `privilege` | | refuse | `sudo`, `doas`, `su`, `pkexec`, `runas`, `gsudo` |
| `remote-shells` | | refuse | `ssh`, `scp`, `sftp`, `telnet`, `rsh` |
| `multi-call` | | refuse | `busybox`, `toybox` |
| `project-config` | | warn | `npm`, `npx`, `yarn`, `pnpm`, `make`, `terraform`, `kubectl`, `helm`, `find`, `poetry`, `uv`, `cargo`, `go`, `mvn`, `gradle`, `rake`, `bundle`; name patterns `pip*` |
| `docker` | `docker` | warn | command map `docker: ${exec}` |
| `git` | `git-vcs` | warn | command map `git: ${exec}` |
| `git-options` | `git-vcs` | warn | `git` with `-c`, `--config-env`, `--exec-path` |
| `code-options` | | warn | options `-c`, `-e`, `--eval`, `--exec`, `--command`, on any command |
| `find-options` | | refuse | `find` with `-exec`, `-execdir`, `-ok`, `-okdir` |
| `tar-options` | | refuse | `tar` with `--to-command`, `--use-compress-program`, `-I`, `--checkpoint-action`, `--info-script`, `-F` |
| `rsync-options` | | refuse | `rsync` with `-e`, `--rsh`, `--rsync-path` |
| `loader-env` | | refuse | env `*`, `LD_*`, `LD_PRELOAD`, `LD_LIBRARY_PATH`, `LD_AUDIT`, `DYLD_*`, `DYLD_INSERT_LIBRARIES`, `DYLD_LIBRARY_PATH`, `PATH`, `PYTHON*`, `PYTHONPATH`, `PYTHONSTARTUP`, `PYTHONHOME`, `NODE_OPTIONS`, `NODE_PATH`, `PERL5LIB`, `PERL5OPT`, `PERLLIB`, `RUBYOPT`, `RUBYLIB`, `BASH_ENV`, `ENV`, `JAVA_TOOL_OPTIONS`, `_JAVA_OPTIONS` |
| `config-env` | | warn | env `HOME`, `XDG_*`, `XDG_CONFIG_HOME`, `GIT_*`, `GIT_SSH`, `GIT_SSH_COMMAND`, `GIT_EXEC_PATH`, `GIT_DIR`, `GIT_WORK_TREE`, `GIT_CONFIG_GLOBAL`, `GIT_CONFIG_SYSTEM`, `GIT_CONFIG_COUNT`, `GIT_ASKPASS`, `SSH_ASKPASS`, `EDITOR`, `VISUAL`, `PAGER`, `DOCKER_HOST`, `DOCKER_CONFIG`, `KUBECONFIG`, `AWS_CONFIG_FILE`, `NPM_CONFIG_*`, `NPM_CONFIG_USERCONFIG` |
| `version-manager-shims` | | refuse | paths `~/.asdf/shims`, `~/.local/share/mise/shims`, `~/.pyenv/shims`, `~/.pyenv/pyenv-win/shims`, `~/.nodenv/shims`, `~/.rbenv/shims`, `~/.goenv/shims`, `~/.jenv/shims`, `~/.plenv/shims`, `~/.volta/bin`, `~/.proto/shims` |

Some entries that look out of place:

* `awk` is an interpreter: `awk 'BEGIN{system("...")}'`.  GNU `sed`'s `e` command runs a shell command too, but refusing `sed` seems heavy; leave it out, or add a `sed` check that warns on an `any` script argument.  However, the user should have no reason to run `sed` or `awk` in a host session - these don't require elevated privileges.
* `scp` and `sftp` take `-o ProxyCommand=...`, which runs a local command, as well as reaching another host.
* The plain `-c`/`-e` option check warns on every command, so it will also flag `grep -e` and `tar -c`.  The per-command checks (`git-options` and the rest) are where the precise ones go; the generic one is a safety net.

### Design decisions

* **Names are case sensitive.**  The project file's `command` is case sensitive, and so are `commands`, `command-map` keys and `name-patterns`.  How a name resolves to a host executable (including `PATHEXT` and file systems that ignore case, on Windows and macOS) is the generator's business; a user whose host spells a program differently maps it with `command-map`.
* **`env` entries match exactly.**  An `env` entry matches a command's `env` `allow` entry only when the two are the same text.  Matching the check's patterns against the command's patterns (does `allow: [GIT_*]` pass `GIT_SSH_COMMAND`?) is pattern overlap, which is harder to get right, so it waits for later.  Until then, the lists name both the variables (`LD_PRELOAD`) and the broad patterns a project is likely to write (`LD_*`, `*`), each as plain text.  A pattern the lists don't name, such as `L*`, isn't caught: a known gap.
* **`command-map` stays in the checks, for now.**  Mapping a command is a statement about it (`docker` runs `podman`, and warns), and one use puts it squarely there: mapping a command to a wrapper script that checks its arguments at run time, for commands too complex for the project file's argument patterns.  The cost is that a user who only wants to map a command writes an `allow` check with nothing but a `command-map`, and has to know that the mapping lives in the checks.  Moving it to a kind of its own (say, a section's `host-command-map`), with the checks only judging, would lighten that.  Worth revisiting once the review flow (steps 12 to 15) shows how people use it.

### Completed

* **The kind.**  [`host_commands.rs`](../../host/src/config/host_commands.rs) has the check as written (`CheckRecord`, with its field checks), the expanded check, the name patterns, and `combine`.  [`system.rs`](../../host/src/config/system.rs) reads `host-commands` like the other kinds, keeps it in the `setup` record (so a `--universal` run checks it again), expands `command-map` values and `paths`, and refuses references to a check.
* **Combined in the SUC.**  [`universal.rs`](../../host/src/universal.rs) writes `host-command-checks`: the checks (each with its `files`), the `command-map`, the precomputed action for each mapped command from the checks that match on names alone (by name, by the file the program really is, and by `name-patterns` on that file's name; overrides by exact name only), with the checks overrides replaced, and the notices as objects.
* **Errors:** a program in the project, the project files directory, the workspace or the repository; a command two checks map to different programs (a program that isn't there doesn't count, so the one found wins); an override of a check from a higher priority file, of another override, or of itself; two overrides of one check for the same command.  These need every setup file, so the generator reports them with the other setup errors, rather than the loader with a single file's.
* **Notices:** an override of a check that isn't set, or for a command, option or `env` name the check doesn't match; a check a setup file removes, or replaces without some of the lower priority file's entries or with a less restrictive action.  [`scoter user-setup`](../../scripts/main_user_setup.py) prints them, with each override in effect ([`setup_check.py`](../../scripts/surf_scoter_lib/setup_check.py)); they don't fail it.
* **Defaults.**  [defaults/setup.yaml](../../defaults/setup.yaml) has the `host-bridge` checks from the table above, `docker: host-commands: docker` and `git-vcs`'s `git` and `git-options`.  The `git` checks use `command-map: {git: ${exec}}` rather than `commands: [git]`, so a user who points `git-vcs: programs: exec` elsewhere doesn't get a conflicting map.
* **Docs:** [the system configuration file](../system-config-file.md#host-command-checks) and [the universal configuration file](universal-configuration-file.md).

**Tests:** in `host_commands.rs`: parsing, each field's checks, the combinations, the name patterns.  In `system.rs`: references to a check, errors and their keys, a kind change across files, expansion (`${exec}`, a missing program, `~`, a spliced `path-lists`), a null check hiding the distribution's.  In `universal.rs`: the combined checks (identity through a symlink, `name-patterns` on the real file's name), overrides (another matching check still counting after one; an override with nothing to override) and their notices, the override errors (two overrides of one check and command, with and without options; an override of an override), the other errors, a program in the project, replacement notices with their ids, parameters and sources, the `files` of a replaced check, and the checks surviving a `--universal` round trip.  In `test_setup_check.py`: the `user-setup` report.

## 5. Design the user-directory verified storage

The verified storage lives in the directory the setup files name as `scoter: paths: verified-dir` (default `~/.config/surf-scoter.d/verified`), not in `${scoter:user-dir}`, which isn't set when the user's setup file is `~/.config/surf-scoter.setup.yaml` or there's none.  Loading the setup files already refuses a `verified-dir` that's relative, or inside the project, its workspace or its repository.  It needs a solid structure around how to store each project's verified storage.

`running-env-config` and `running-env-config-lock` have the same gap: [defaults/setup.yaml](../../defaults/setup.yaml) puts them under `${scoter:user-dir}`, so they have no value without a user directory.  Give them a dedicated `scoter: paths` setting of their own (say, `state-dir`, default `~/.config/surf-scoter.d/state`), checked the same way as `verified-dir`, as they hold the ports and names of the running environments.

With more than one `.surf-scoter.d` in a repository, anything named after the project directory needs to be named per environment, not per repository.  Give each environment an id: the project directory's name plus a short hash of its canonical absolute path.  The id needs no stored state, so the same value works at run time and in the verified storage.  Use it for:

* the container names;
* the workspace path;
* the verified storage directory (below).

These should be stored in JSON formats so that Python can easily read it.  In order to allow for multiple project directories, I recommend an index file that points the local directory to the stored directory.  The stored directory is named by the environment id above, so two directories with the same name don't collide and need no conflict resolution.  The directory here is *always* the parent of the `.surf-scoter.d` directory, not the VCS root; this allows for multi-project repositories.  The index maps the full canonical path to the id, which also makes it easy to list and revoke acceptances.

For the fingerprinting and diff'ing of files, this implies keeping the full text of the project setup file and all used container directories (following the dev-env.yaml file parent DAG).  I recommend a tar.gz store.

* Refuse symlinks, hard links and special files when building the archive.  Never extract it with the default `tarfile` behavior; use `filter="data"`, or read members without extracting.
* Store three versions: the accepted data, the last project suggestion the user saw, and the current suggestion.  The diff step (step 17) needs all three.

I started a little of the implementation in the [auth_project_file.py](../../scripts/surf_scoter_lib/project_auth/auth_project_file.py) file, but it needs improvements.  I also wonder about the need to store fingerprints if the whole files are stored, though fingerprinting the Dockerfiles and the setup file by looking at the content rather than the whole file may be helpful.  Fingerprints still earn their place as a lookup key: storing several accepted snapshots keyed by the fingerprint of the suggestion lets a branch switch find an earlier acceptance without reading every archive.

Fix the related code while here:

* [`docker.py`](../../scripts/surf_scoter_lib/project_auth/docker.py) - With whole directories stored, hash the file bytes directly.

Record the setup the review was made under.  The accepted data is only right for the boundaries it was reviewed with, and those come from the setup files, not the project.  A user or distribution change to any of them changes what gets built and run, so store their resolved values with each acceptance, and ask for a new review when one changes:

* `scoter: values: project-files-dir`;
* `scoter: path-lists: dev-containers-search`;
* the VCS section's `runs` for the project's `vcs` (`local-clone`, `fetch-changes-into`, `show-fetch-changes`, `merge-changes`);
* the gateway the egress rules were accepted for (step 9);
* `scoter: paths: verified-dir` itself, so a moved store can't quietly start empty, or be swapped for another.

Protect the storage:

* Directories have mode `0700`, and files `0600`.  Check the owner and mode on every read.
* Update the index by writing a new file and renaming it over the old one, with a lock so two `scoter` runs can't interleave.

To keep in mind:

1. The user may want to blanket authorize the directory, regardless of changes (they'd still need to perform the "git fetch" process manually).  This would cover use cases where the user creates a private project that never gets contributions from others.  A blanket authorization skips the review only: path resolution, the refused lists (step 4) and a new review on a gateway change (step 9) still apply.
2. The user may switch between branches that have different project setups.  This may mean each branch has its own separate user configuration, or the user has just one.  In either case, this shouldn't mean the user needs to re-authorize changes when they change branches, if the branch hasn't made changes.  The fingerprint-keyed snapshots above handle this, limited by each snapshot's expiry.

I would prefer that the management of the validated projects within the user directory remain in the Python code.  I may revisit this decision later.

**Tests:** an archive member that's a symlink or has `..` in its name; a storage file with the wrong owner or mode; two runs updating the index at once.

### Design: the environment id

`NAME-HASH`, where `NAME` is the project directory's name, lower-cased, with each run of characters outside `[a-z0-9-]` turned into one `-`, cut to 32 characters, without a leading or trailing `-` (and `project` if nothing is left), and `HASH` is the first 12 hex digits of the SHA-256 of the project directory's canonical absolute path (as bytes).  For example, `client-3f9a0c17b2e4`.  It's valid as a container name, a host name part and a file name everywhere.

For now only the verified storage uses it, so the Python code works it out.  Using it for the container names and the workspace path is a future task, and moves the calculation into the generator (see [Future Tasks](#naming-by-the-environment-id)).

Case-insensitive file systems (macOS, Windows) can give two spellings of one path, and the canonical path doesn't always fix the case.  The first cut doesn't fold the case; two spellings get two environments, and the user reviews twice.

### Design: the layout

```
verified-dir/                     0700
    index.json                    0600  canonical project directory -> environment id
    lock                          0600  held while anything below changes
    blobs/                        0700  file contents, by SHA-256, shared by every environment
        3f/3f9a...                0600
    ENV-ID/                       0700
        environment.json          0600  the environment's acceptances
        acceptances/
            ACCEPTANCE-ID/        0700
                accepted.json     0600  the canonical accepted settings
                suggestion.json   0600  the parsed project file and dev container it was reviewed from
                review.yaml       0600  the user's verification file, as they left it (step 12)
```

**Snapshots are a manifest plus blobs.**  A snapshot of the project's files is a manifest: each file's path (relative to `.surf-scoter.d`), its SHA-256, its size, whether it's executable, and for some files a canonical hash (below).  The contents go in `blobs/`, named by their hash.

* Nothing is ever extracted from an archive, so the archive member attacks (`..`, absolute names, links, devices, duplicate names) can't happen; what's left is checking each manifest path before it's used, which is one function.
* Branch switching keeps many snapshots that differ in a file or two.  Blobs are shared, so each costs one copy of what changed.
* The manifest's own hash (over its canonical JSON) is the snapshot's id: the lookup key for an earlier acceptance.
* Restoring a snapshot (step 16 builds from it) copies blobs into a private staging directory, checking each blob's hash as it's read.

**Disk space.**  The store grows with each acceptance: a copy of every project file that changed since the last one.  Container directories can hold large files copied into the image, so this matters.  The tools keep it bounded:

* removing an acceptance removes the blobs nothing else refers to;
* every write to an environment prunes it: expired acceptances beyond `scoter: values: verified-keep` (default 10) go, oldest first;
* `scoter` lists the store's size with the accepted projects (step 20), and can prune or revoke them.

[The system configuration file](../system-config-file.md) says where the space goes, and how to reclaim it.

### Design: what's in a snapshot

The project file, and every container directory the dev container builds from, following `parent-builds`, that's inside `.surf-scoter.d`.  Building one:

* reads every file with `O_NOFOLLOW`, checks it with `fstat`, and refuses symlinks, hard links and special files in container directories (the step 1 rules; a hard-linked project file only warns);
* limits each file and the whole snapshot with `scoter: values: verified-max-file-size` and `verified-max-snapshot-size` (defaults 16 MB and 256 MB).

Container directories outside `.surf-scoter.d` come from `dev-containers-search`, which the user's setup controls, such as the distribution's `containers`.  They're trusted by default, as part of the installation.  `scoter: path-lists: dev-containers-verified` (empty by default) names `dev-containers-search` directories whose containers are snapshotted and reviewed like the project's, for setups that need tighter control.

### Design: telling noise from changes

Hashing bytes means a new comment or blank line asks for a review, and reviews that change nothing train the user to accept without reading.  So each manifest entry has two hashes:

* `sha256`, of the bytes: the blob's name, and what a restore checks.  The snapshot id is made from these, so an exact match is cheap.
* `canonical`, of the file's meaning, for formats the tools understand.  Comments, blank lines and trailing whitespace don't count.

When no acceptance matches exactly, the lookup tries again on meaning: same paths, same executable bits, and for each file the same `canonical` hash (or `sha256`, for files without one).  A match on meaning carries the acceptance forward: a new acceptance pointing at the new snapshot, with `kind: carried`, the acceptance it came from, and that acceptance's expiry (carrying forward never extends it).  The user gets one line saying so, rather than a review.

The canonical forms:

* **Dockerfiles** (`Dockerfile`, `*.Dockerfile`, `Dockerfile.*`): comment lines and blank lines dropped, and trailing whitespace stripped, except inside heredocs (`RUN <<EOF`), which are kept as written, as their lines are the script.  Parser directives at the top (`# syntax=`, `# escape=`) are kept, and `escape` sets the continuation character.  Indentation is kept: inside a quoted string that spans lines, it's part of the string.
* **JSON**: parsed and written back with sorted keys and no spaces.  A file with duplicate keys has no canonical hash.
* **The project file and `dev-env.yaml` files**: Python has no YAML parser, and the tools shouldn't add one.  The generator has already parsed them: the SUC's `project` and `dev-container`.  Their canonical hash is of that data, as canonical JSON.  The snapshot is read after the generator parsed them, so the build (step 16) must check that the snapshot it builds from parses to the same data: run the generator against the staged copy, and compare.
* **Everything else**: bytes only.

### Design: `environment.json`

```json
{
    "$schema": "surf-scoter-verified-environment-1",
    "id": "client-3f9a0c17b2e4",
    "project-dir": "/home/user/mono/client",
    "acceptances": [
        {
            "id": "20261020T150405Z-7c1e",
            "kind": "reviewed",
            "from": null,
            "accepted-at": "2026-10-20T15:04:05Z",
            "expires-at": "2026-11-19T15:04:05Z",
            "exempt-until": null,
            "branch": "main",
            "suggestion": {
                "id": "sha256:91b0...",
                "files": {
                    "project.yaml": {"sha256": "5d1c...", "size": 812, "executable": false, "canonical": "sha256:0aa1..."},
                    "dev-env/Dockerfile": {"sha256": "aa07...", "size": 2310, "executable": false, "canonical": "sha256:77e2..."},
                    "dev-env/entry.sh": {"sha256": "c3d9...", "size": 140, "executable": true, "canonical": null}
                }
            },
            "setup": {
                "id": "sha256:e2f4...",
                "values": {
                    "project-files-dir": ".surf-scoter.d",
                    "dev-containers-search": ["/home/user/mono/client/.surf-scoter.d", "/opt/surf-scoter/containers"],
                    "dev-containers-verified": [],
                    "vcs-runs": {"local-clone": ["git", "clone", "..."]},
                    "gateway": null,
                    "verified-dir": "/home/user/.config/surf-scoter.d/verified",
                    "host-command-checks": {}
                }
            }
        }
    ]
}
```

* **The three versions** are `accepted.json` (what the user accepted), the `suggestion` and `suggestion.json` (the project files, and their parsed form, that the user reviewed), and the project's files now, which aren't stored until the next review.  "The project changed X" is a diff of `suggestion` against now; "you changed Y" is a diff of `accepted.json` against the settings worked out from `suggestion.json`.  A denied review stores nothing, so "the last suggestion the user saw" is the last one they accepted.
* **`setup`** is the step 5 list of setup boundaries, resolved, plus step 4's `host-command-checks` with the lower priority versions the user's checks hide, and the snapshots of `dev-containers-verified` containers.  Its `id` is the hash of `values`' canonical JSON.  A run looks for an acceptance whose `setup.id` matches, and whose `suggestion` matches exactly or on meaning.  For the first cut, any change to these asks for a new review.  Some changes don't affect a project's security, and could be told apart later, though fewer than it seems (a program a check names can be relinked, say).
* **`kind`** is `reviewed`, `carried` (above) or `blanket`.  A blanket acceptance has no `suggestion` and no `accepted.json`: each run works the settings out from the project's files as they are, and still runs the refusals (step 4) and the setup check, so a gateway change still asks again.
* **Expiry**: `scoter: values: verified-expiry-days` (default 30) sets a new acceptance's `expires-at`, which the review file can change (step 12).
* **Several acceptances** cover branch switching.  The newest valid match wins.
* **`branch`** is a note for the user, read from the project directory, never the workspace.  It isn't part of the match.

`index.json`:

```json
{
    "$schema": "surf-scoter-verified-index-1",
    "environments": {
        "/home/user/mono/client": {"id": "client-3f9a0c17b2e4"}
    }
}
```

The id comes from the path, so the index is for listing and revoking, and a cross-check: loading an environment checks that the index, the directory name and `environment.json`'s `project-dir` all agree, so a renamed or swapped directory is refused.

### Design: protecting it

* Every directory is `0700` and every file `0600`, owned by the user; every read checks both, with `O_NOFOLLOW` and `fstat`, and refuses a symlink anywhere below `verified-dir`.  On Windows, which has no mode bits, the check is that the files are under the user's profile, and the rest is left to its ACLs.
* Writes go to a temporary file in the same directory (`mkstemp`), are flushed with `fsync`, and replace the old file with `os.replace`.
* Every JSON file has a `$schema`, and a size limit when read.

**The lock** is an operating system lock on `verified-dir/lock`, held while anything in the store changes.  The operating system lets it go when the process ends, however it ends, so a crashed run never leaves the store locked, unlike a lock file that exists or doesn't (as `active_env.py` does, which also checks and then writes, so two runs can interleave).  In Python, without a library:

* Open the lock file with `os.open(path, O_RDWR | O_CREAT | O_NOFOLLOW, 0o600)`, and check it with `fstat` like any other store file.
* **POSIX:** `fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)`.  It locks the open file, so two opens of the file conflict even in one process (which makes it testable).  `fcntl.lockf` is the other choice; avoid it, as POSIX record locks are dropped when *any* descriptor for the file closes in the process.
* **Windows:** `msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)` locks the file's first byte, which works on an empty file.  Unlock it with `msvcrt.LK_UNLCK` before closing.
* Both, non-blocking, in a loop with a short sleep and a time limit (say 30 seconds), printing once that another `scoter` is updating the store.  Past the limit, fail rather than wait forever.
* Wrapped as one context manager in `fileutil`, which `active_env.py` should move to too (step 18).

`flock` on a network file system (an NFS home directory) may be emulated, or may only lock against this computer.  The store is per user, so that only matters for one user on two computers at once; the docs mention it.

### Design: the code

A new `surf_scoter_lib/verified` package, in place of `project_auth` (unused so far, and it has bugs: `RecordWindow.from_json_v1` calls `jdata.req_str` without its data, and `ProjectSet.match` throws away its `sorted`):

* `store.py`: the directory, its checks, atomic writes, the index, the blobs;
* `snapshot.py`: building a manifest from the project's files, the canonical forms, restoring into a staging directory;
* `records.py`: `environment.json` and acceptances, their validity windows (`RecordWindow`'s logic carries over), the lookup, pruning;
* `fileutil.exclusive_lock`: the lock.

The generator adds the `scoter: paths: state-dir`, with the same checks as `verified-dir`, and `running-env-config` moves under it.  Nothing calls the store yet; the review (steps 12 to 16) does.

### Completed

* **The package.**  [`verified/store.py`](../../scripts/surf_scoter_lib/verified/store.py) (the environment id, the store's checks, atomic writes, the index, the blobs), [`verified/snapshot.py`](../../scripts/surf_scoter_lib/verified/snapshot.py) (manifests, the canonical forms, taking and restoring snapshots) and [`verified/records.py`](../../scripts/surf_scoter_lib/verified/records.py) (acceptances, the lookup, carrying forward, pruning, the setup values from the SUC).  `project_auth` is gone.
* **The lock.**  `fileutil.exclusive_lock`, as described above, with `LockTimeoutErr`.
* **Settings.**  [defaults/setup.yaml](../../defaults/setup.yaml) has `scoter: paths: state-dir` (and `running-env-config` under it), `path-lists: dev-containers-verified`, and `values: verified-expiry-days`, `verified-keep`, `verified-max-file-size` and `verified-max-snapshot-size`.  The generator checks `state-dir` as it does `verified-dir` ([`system.rs`](../../host/src/config/system.rs)).
* **Docs.**  [The system configuration file](../system-config-file.md#the-verified-store) has the settings, and what the store holds, how much space it takes, and how it's kept bounded.
* `setup_values` also records the project's `vcs` section name, so a project switching VCS asks again.

The acceptance files (`accepted.json`, `suggestion.json` and `review.yaml`) are only named here: the store writes any file, and steps 12 to 15 decide what goes in them.

**Tests:** in [`s_lib/verified/`](../../scripts/tests/s_lib/verified/), a test file for each module, with a shared `helper.py`; the lock's in [`test_fileutil.py`](../../scripts/tests/s_lib/test_fileutil.py).  The environment id; the lock (a second holder in the same process waits, then gives up; a symlinked lock file); the store (private modes on what it writes, a file or directory with the wrong mode or owner, symlinked files and directories, the size limit, names with `..`, blobs checked on read and removed when unused, the index, two threads registering at once); the canonical forms (Dockerfile comments, continuations, heredocs and directives, JSON with duplicate keys); snapshots (what's in one, noise against changes, symlinks, hard links and FIFOs in a container directory, a symlinked or hard-linked project file, the limits, restoring, manifest paths with `..`, a tampered manifest id); records (validity windows, the lookup order, carrying forward keeping the expiry, pruning, JSON round trips, saving and loading with the cross-checks, the setup values).  In `system.rs`: `state-dir` relative, and in the project.

## 6. Add dev container parents found by name

A dev container's `parents` can only name a `dir` or `file` by path.  A project's container that builds on one from the installation (such as `dev-env`) can't reach it: a path from `.surf-scoter.d` into the installation isn't portable, and a path to the checkout's own `containers/` is refused unless the checkout is the distribution.  Add a parent found by name through `dev-containers-search`, for example `parents: [{name: dev-env, tag: local-surf-scoter-env/dev-env:build}]`: the first search directory holding `NAME/Dockerfile` (and, when it has one, `NAME/dev-env.yaml`), with the same confinement as the other parents.  This repository's own `.surf-scoter.d/dev-env-container/dev-env.yaml` needs it to run with the self-host copy (see [CONTRIBUTORS.md](../../CONTRIBUTORS.md#working-on-surf-scoter-with-surf-scoter)).

**Tests:** a `name` parent found in the second search directory; one missing from all of them; one found through a symlink that leads outside them; a `name` parent alongside `dir` and `file` parents, in build order.

Completed: [`dev_env.rs`](../../host/src/config/dev_env.rs) takes `{name, tag}` parents, found in the first `dev-containers-search` directory holding `NAME/Dockerfile`, then treated as a `dir` parent: built with its tag, its `dev-env.yaml` optional, confined to the project files directory and the search directories after symlinks are resolved (an error on `parents[i].name`), and in `parent-builds` in build order.  `NAME` follows `dev-container`'s rules (a single directory name).  [The developer environment Dockerfile](../project-dockerfile.md#parents) documents it.  This repository's own `.surf-scoter.d/dev-env-container` now resolves its `name: dev-env` parent to the distribution's `containers/dev-env`.

The tests above are in `dev_env.rs`, along with a `name` that isn't a single directory name, a `name` without a `tag`, `name` with `dir`, and no `dev-containers-search` at all.

## 7. Update the project file format

Change around the project file format schema to include the new features - egress registration and VCS details.

This will affect the schema, the [universal file document](universal-configuration-file.md), and the SUC generator.  The generator will include this file's contents in a new wrapped area, "current-project-setup", and it will no longer directly be used to construct the "host-brokered" section.

Land this step together with step 8, or keep the old way of building "host-brokered" behind a flag until step 8 is done, so the main branch always works.

The schema enforces:

* `program` is a name only: no `/` or `\`, no `.`, `..` or empty names, no control or non-ASCII characters, no leading `-`, no drive or stream syntax (`C:aws`, `aws:stream`), and no Windows device names.
* `egress` entries are structured (`host`, `port`, `protocol`, optional `methods`).  `methods` is only allowed with `protocol: http` or `https`.  A bare `*` and wildcards on public suffixes are refused.  Hostnames are stored and shown in their `xn--` (punycode) form.
* No `root`: the VCS root comes from step 3's discovery, never from the project.  The `file` VCS copies the project directory (the parent of `.surf-scoter.d`).
* No newlines in fields that don't need them.

The YAML parsing refuses anchors and aliases, merge keys (`<<`), explicit tags and duplicate keys, and applies a size limit.  Check how the Rust YAML library handles duplicate keys; libraries differ.

Decide whether `${section:key}` and `@NAME@` references are frozen at acceptance.  Today `scoter-host` resolves `@NAME@` when it starts, which conflicts with the design's frozen values.

**Tests:** duplicate keys; an alias bomb; a `program` with each refused form; `methods` on a `tls` rule; a non-ASCII hostname.

### Draft: what the YAML library already does

Checked against `yaml_serde` 0.10.7, which the project file is read with:

* **Duplicate keys** are refused already, at any depth (`duplicate entry with key "a"`).  Add a test so a library change can't quietly lose it.
* **Alias bombs** are refused already (`repetition limit exceeded`).
* **Plain aliases** (`b: *x`) are expanded silently.  The library doesn't expose its parser events, so refusing them would mean using `libyaml-rs` directly, through its `unsafe` C-style API.  Decided: rely on the expansion limit, with a test that an alias bomb is refused.  The review shows the expanded values, so an alias can't hide anything from it.
* **Merge keys** are never applied: `<<: *b` is just a key named `<<`.  The project file rejects unknown keys, so a `<<` is refused wherever the keys are fixed; the free-form mappings (`env: set`, `default`) need their own check.
* **Tags** are refused already (`tag_errors`).

### Draft: the format

```yaml
vcs: git
container:
  dev-container-path: ./dev-env
egress:
  - host: registry.npmjs.org        # port 443 by default
    protocol: tls
  - host: '*.pkg.dev'
    protocol: tls
  - host: mirror.example.com
    port: 8080
    protocol: http
    methods: [GET, HEAD]
env:
  set:
    HOME: '@HOME@'
commands:
  - command: aws
    args: [s3, ls]
  - command: aws-build
    program: aws                    # a name, looked up on the host
    args: [codebuild, start-build, --project-name, {any: {}}]
```

**`command` and `program` are names.**  `command` is the name the client runs as in the container.  `program` stays optional, but becomes the *name* of the host program (default: `command`), which the host resolves through the setup's `command-map` (step 4), and otherwise the `PATH`, at review time (step 11).  A path is refused, with a pointer to `command-map`.  Both get the step 4 name rules: no `/` or `\`, not `.`, `..` or empty, ASCII without control characters or spaces, no leading `-`, no `:` (drive and stream syntax), and not a Windows device name (`CON`, `NUL`, `COM1` and so on, with or without an extension, any case).

**`egress`** is a list of rules, each with exactly these keys:

* `host` (required): a host name, or `*.` and a host name for its subdomains (not the name itself).  Lower-case ASCII letters, digits and `-` in each label; no IP addresses in the first cut.  A non-ASCII name is refused, so the file always holds what the review shows (no IDNA library needed).  A bare `*` is refused, and so is `*.` on a single label (`*.com`).  `*.` on a name in the setup's `scoter: value-lists: egress-refused-wildcards` (public suffixes such as `co.uk`, and shared hosting such as `github.io`) is refused when the generator has both files.
* `protocol` (required): `tls`, `http` or `https`.  `tls` is a tunnel the gateway only sees the host of; `https` is HTTP over TLS that the gateway can look inside, so `methods` mean something, which needs a gateway that does that (the Squid gateway doesn't; step 9 refuses what the gateway can't enforce).  Plain TCP (SSH, databases) waits.
* `port` (optional): 1 to 65535; 443 for `tls` and `https`, 80 for `http` by default.
* `methods` (optional, `http` and `https` only): upper-case method names, such as `GET`.

**No control characters.**  Every string and key in the project file, newlines included, is refused if it has a control character.  None of the fields needs one.

### Draft: the SUC

The SUC's `project` becomes `current-project-setup`: the project file as written, now only a record, which steps 11 to 14 build the review from.  `host-brokered` no longer comes from it once step 8 is done.  Until then, `gen-scoter-uc --unreviewed-project-commands` builds `host-brokered` from the project file as it does today, and `scoter.py` passes it, with a warning on every run that the project's host commands haven't been reviewed.  Without the flag, `host-brokered` is empty.  The flag goes in step 8.

`scoter.py` and `user_config.py` read the new name; nothing else uses `project`.

### Draft: references

* **`${section:key}`** is resolved by the generator, and frozen at acceptance: the accepted settings hold the value, so the review shows what the command gets, and a change on the host shows up as a difference at the next review.
* **`@NAME@`** stays live: `scoter-host` reads it when it starts.  Values like `@SSH_AUTH_SOCK@` change with every login, so freezing them would ask for a review every session.  The acceptance covers the *name*: the review shows each `@NAME@` a command uses, with its value at review time for reference, and a project that starts using another name asks again.

### Draft: `value-lists`

The refused wildcards are a list of plain text, which no kind of setup value holds: `path-lists` entries are file locations (`~` and `./` expanded).  Add a seventh kind, `value-lists`: lists of text, each entry expanded like a `values` text (`${...}` references), and a whole entry that refers to another `value-lists` list putting its items in its place, as `path-lists` do.  `{x:...}` isn't allowed.  A higher priority file's list replaces the whole list, as for `path-lists`.

### Completed

* **`value-lists`.**  A seventh kind of setup value in [`system.rs`](../../host/src/config/system.rs), in the SUC's `resolved` sections and the `setup` record.  [defaults/setup.yaml](../../defaults/setup.yaml) has `scoter: value-lists: egress-refused-wildcards`, with multi-label public suffixes and shared hosting domains.
* **`egress`.**  [`egress.rs`](../../host/src/config/egress.rs): `host`, `protocol` (`tls`, `http`, `https`), `port` (defaulted by protocol) and `methods` (`http` and `https` only).  Hosts are lower-case ASCII names of two or more labels, with only a leading `*.`; no IP addresses, no bare `*`, no `*.` on a single label.  [`config.rs`](../../host/src/config.rs) refuses a wildcard on the setup's `egress-refused-wildcards`, in `load_all` and `load_universal`.
* **Names.**  `command` and `program` follow `host_commands::command_name_problem`: step 4's rules, plus no `:` and no Windows device names (which the `host-commands` names now get too).  Only in the project file: the SUC's `host-brokered` still carries the executable found as `program`.
* **Text.**  [`project.rs`](../../host/src/config/project.rs) refuses control characters in every string and key, and `<<` merge keys anywhere.  Duplicate keys and alias bombs are `yaml_serde`'s, now with tests.
* **The SUC.**  `project` is now `current-project-setup`.  `host-brokered` is empty unless `gen-scoter-uc --unreviewed-project-commands` is given; `scoter.py` passes it, and warns on every run that has host commands.  `UserConfig.project_bridge_commands` (the client links in the container) now reads `host-brokered`, so the container only gets links for commands the host would run.
* **Docs.**  [The project configuration file](../project-configuration-file.md) (egress, names, the YAML rules), [the system configuration file](../system-config-file.md#value-lists) and [the universal configuration file](universal-configuration-file.md).

**Tests:** in `egress.rs`: the rules, their errors, and host names (non-ASCII, a Cyrillic look-alike, wildcards, case, IP addresses, single labels, long labels).  In `project.rs`: duplicate keys (top level and nested), an alias bomb, a merge key, control characters in values and keys, every refused `program` and `command` form, egress against a refused list.  In `host_commands.rs`: the name rules, Windows device names included.  In `system.rs`: `value-lists` expansion, splicing, errors, and replacement by a higher priority file.  In `universal.rs`: `host-brokered` empty without the flag, and an egress wildcard the setup refuses.  In Python: `current-project-setup`, the client links from `host-brokered`, and the warning.

## 8. Update the SUC generator to also pull in the user's verified project settings

Add another CLI option to the SUC generator to read from the user's verified project settings, and integrate the results into the SUC file.  At this point, the "host-brokered" section will become valid again.

This will introduce a new section for the egress settings, and expand the VCS settings.  A consumer of the SUC should be able to recreate the original user's verified project settings.

"host-brokered" is built only from the verified settings, never from "current-project-setup".  `gen-scoter-uc --unreviewed-project-commands`, and `scoter.py`'s warning, stay as a stand-in until step 15 makes an acceptance possible; `scoter.py` passes the flag only when no acceptance matches.  This step also adds the SUC's egress section.

From this step on, the SUC is security-critical: whoever can write it decides what runs on the host, and `scoter-host` loads it without running the setup checks again.

* Write it only with `mkstemp` (mode `0600`) in a directory only the user can read, as `scoter.py` does now, and never into the setup directory the container mounts.
* Pass sub-commands its path, never its contents through the environment, and remove it when the run ends.
* Decide what `scoter-host` checks again when it loads the SUC.  At least: the file's owner and mode, that its project and dev container locations are still where the accepted settings say, and that the accepted settings it carries match the ones in `verified-dir` (by fingerprint), so an old or edited SUC can't stand in for a review.

### Draft: the accepted settings

Nothing defines the accepted data yet: step 5 named the file (`accepted.json`), and steps 12 to 14 make it from the user's review.  This step needs its shape, as the generator reads it.  The proposal: what the runtime needs, resolved, so nothing in it is worked out again from the project or the host.

```json
{
    "$schema": "surf-scoter-accepted-settings-1",
    "project-dir": "/home/user/mono/client",
    "expires-at": "2026-11-19T15:04:05+00:00",
    "vcs": "git",
    "gateway": null,
    "egress": [
        {"host": "registry.npmjs.org", "protocol": "tls", "port": 443, "methods": []}
    ],
    "commands": [
        {
            "command": "aws",
            "program": "/usr/local/bin/aws",
            "args": ["s3", "ls"],
            "env": {
                "allow": ["AWS_*"], "drop": [], "error": [],
                "set": {"HOME": "@HOME@", "AWS_CONFIG_FILE": "/home/user/.aws/config"},
                "default": {}
            }
        }
    ]
}
```

* **`commands`** have exactly the shape of today's `host-brokered` entries: `program` is the absolute executable (the review resolved it through `command-map` or the `PATH`), and `env` is the command's whole environment plan, with `${section:key}` frozen and `@NAME@` left for `scoter-host` (step 7's decision).  So `host-brokered` is `commands`, after the generator's checks.
* **`project-dir`** ties the settings to the environment they were accepted for.
* **`expires-at`** is the acceptance's expiry, so `scoter-host` can check it at start without reading the store's records.  A carried-forward acceptance (step 5) copies the file, expiry included, as carrying forward never extends it.
* **`gateway`** is step 9's; `null` until then.
* **`vcs`** is the project's VCS name.  The VCS commands themselves come from the setup, and the acceptance's `setup` values (step 5) already record them, so a change asks for a new review.  That's the "expanded VCS settings": nothing more needs to be in the accepted data.

### Draft: the flow

The store is managed in Python (step 5), so Python finds the acceptance and the generator reads the file it's given:

1. **Phase 1 and 2**, as now: the setup, then the project (`current-project-setup`, `host-command-checks`).
2. **The lookup**, by a script of its own, as the VCS root search is: the setup's `verified: runs: find-acceptance`, given the phase 2 SUC (`{x:universal}`), runs the distribution's `verified_find.py`.  It snapshots the project's files (step 5's `snapshot_project`, with the parsed files from the SUC), works out the `setup_values`, and asks the environment's record for a usable acceptance.  A match on meaning is carried forward (step 17 decides how the user is told; for now, one line).  It prints JSON: the match (`exact`, `meaning`, `blanket` or `null`), the acceptance's id, its `accepted.json`, and any notice.  `scoter.py` only reads that.
3. **Phase 3**: `gen-scoter-uc --universal PHASE2.json --no-setup-search --accepted-settings FILE`.  The generator reads `FILE` (size-limited, `O_NOFOLLOW`, regular), checks it against the schema, checks that `project-dir` is this run's, and checks each `program` as step 4 checks `command-map` programs: absolute, an executable file, and not in the project, the workspace or the repository.  It writes:
    * `accepted-settings`: `{"file": FILE, "contents": {...}}`, so the SUC alone shows what was accepted, and `scoter-host` can compare it with the store;
    * `host-brokered`: from `commands`, never from `current-project-setup`;
    * `egress`: `{"gateway": ..., "rules": [...]}`, from the accepted `egress`.

### Draft: protecting the SUC

* `scoter.py` already writes it with `mkstemp` (mode `0600`).  Make that inside a private directory (`mkdtemp`, `0700`) rather than the shared temporary directory.
* `main_run.py` saves the copy `scoter-host` reads into `.dev-env/resources/suc.json`, inside the project directory, with the default mode.  Move it to `${scoter:state-dir}/environments/HOST/suc.json` (directories `0700`, file `0600`, written with `mkstemp` and renamed into place), and remove it in `scoter kill`.
* **`scoter-host` checks, when it loads the SUC:**
    * the file belongs to the user and others can't use it (POSIX; Windows is left to the profile's ACLs, as for the store);
    * `accepted-settings.file` is inside `verified-dir`, is a private regular file, and its contents are exactly `accepted-settings.contents`.  No hash needed (the Rust side has none), and a revoked, replaced or edited acceptance is caught, as the store's file no longer matches;
    * the project directory and the dev container directory still exist where `active-config-locations` says, and the project directory is the accepted `project-dir`.
    * the accepted `expires-at` hasn't passed.  Only at start: a session that's running when its acceptance expires finishes.  `verified_find.py` only picks unexpired acceptances, too.

Decided: the shape above; the lookup in its own script; `scoter-host` checks expiry only at start; the flag stays until step 15; the SUC copy moves out of the project in this step.

A blanket acceptance has no `accepted.json`: its settings are worked out from the project's files on each run, through the checks of step 11.  Until then, `scoter.py` treats a blanket match as no match.

### Completed

* **The accepted settings.**  [`accepted.rs`](../../host/src/config/accepted.rs) reads and checks them (the schema, `expires-at` as an RFC 3339 time, `egress` through step 7's rules, `commands` in the `host-brokered` shape with absolute programs).  `gen-scoter-uc --accepted-settings FILE` checks them against the run (the project directory, and each program an executable outside the project), and the SUC gets `accepted-settings` (`file` and `contents`), `egress` (`gateway` and `rules`), and `host-brokered` from the accepted `commands` alone.
* **The lookup.**  [`verified/lookup.py`](../../scripts/surf_scoter_lib/verified/lookup.py), run by [`verified_find.py`](../../scripts/verified_find.py) through the setup's new `verified: runs: find-acceptance`.  It carries a match on meaning forward (copying the acceptance's files, `accepted.json` included).  Both install scripts include it; `mk-install.sh` now copies the library's packages too.
* **`scoter.py`.**  A third generator run, with `--accepted-settings` when the lookup finds an acceptance, and `--unreviewed-project-commands` (with the warning) when it doesn't, until step 15.  The universal files it passes along go in a private `mkdtemp` directory, and are removed when done; the sub-command now runs as a child (rather than replacing `scoter`) so its file can be removed afterwards, and Ctrl-C still reaches it.
* **`scoter-host`.**  `accepted::check_for_run`, before anything starts: the SUC is the user's alone; the accepted file is in `verified-dir`, private, and still says what the SUC does; `expires-at` hasn't passed (at start only); the project and dev container directories are still where they were.
* **The SUC copy** for `scoter-host` moved from `.dev-env/resources/suc.json` to `${scoter:state-dir}/environments/HOST/suc.json` (`fileutil.write_private`: directories `0700`, file `0600`, replaced whole), removed when `scoter run` tears down.
* **Docs.**  [The universal configuration file](universal-configuration-file.md) (the option, the third run, the new sections, `scoter-host`'s checks) and [the system configuration file](../system-config-file.md#the-verified-store).

**Tests:** in `accepted.rs`: parsing, its errors, and RFC 3339 times.  In `universal.rs`: `host-brokered` from accepted settings (whatever the project's commands, flag or not), the run checks (a program in the project, a missing program, settings for another project), and `scoter-host`'s start checks (valid; expired; a universal file others can read; the store's file changed, gone, or readable by others; the dev container gone).  In Python: the lookup (nothing accepted, exact, carried forward on meaning, a change that matters, blanket, `dev-containers-verified`), `write_private`, the private temporary files, `find_acceptance`, and running a sub-command to its end.

## 9. Add a default section for the network gateway settings

With the addition of the egress setup, the network gateway translation of those settings to the one used by the container become important.  Most likely, this will fork off to a script in the scripts directory that will manage the implementation-specific translation.  The settings will also need to include a list of mount mappings for the generated file (would need to be ingested by the docker-compose section).  First cut will limit the output to a single file.

This will also need a method to read those settings and report on what it can and can't manage.  The caller will use this reported information for end-user notices.  The information will need sufficient context to cover the above scenarios.

* The first version refuses any accepted rule the gateway can't enforce.  It never drops a condition and allows the broader traffic.  For Squid, that means `tls` on port 443 without `methods`, and plain `http` with `methods`.
* Ports other than 443 also need firewall rule changes (N4), not just Squid changes.  The first cut can refuse them.
* Keep refusing private, loopback, link-local and metadata addresses after DNS resolution.
* The verified settings record the gateway they were accepted for.  A different gateway at run time triggers a new review.
* This replaces the `allowed-domains` setup entry and `.surf-scoter/allowed-domains.txt`.  Until then, the entry in [defaults/setup.yaml](../../defaults/setup.yaml) points at the old location, and a missing list makes `main_run.py` create an empty one with mode `0666` (FILE-7).  If this step is a while off, change the default to `${scoter:project-dir}/${scoter:project-files-dir}/allowed-domains.txt` in the meantime, and refuse to start without a list instead of creating one.

**Tests:** `methods` on a `tls` rule; a rule for port 22; a listed hostname that resolves to `169.254.169.254` or a private address; a gateway change after acceptance.

## 10. Update `scoter-host` to run accepted executables

The host bridge (Rust) needs to change to match:

* Run the absolute executable path from the SUC.  Remove the `PATH` lookup in `find_on_host` ([`exec.rs`](../../host/src/host/exec.rs)), which closes BRIDGE-11.
* Match the name the client sends in the handshake exactly against the accepted names, and never look it up anywhere else.
* Decide how names compare on Windows (case, and `PATHEXT` such as `aws` against `aws.exe`).

**Tests:** a handshake name that isn't accepted; a relative `PATH` entry on the host; an executable removed after acceptance.

## 11. Add the verification checks library

A Python library, used by the verify and run scripts, that runs the checks from the design using the lists from step 4:

* **`.surf-scoter.d` contents:** refuse symlinks, hard links and special files in container directories; warn about hard links elsewhere; warn about secret-looking files; warn when a `FROM` line has no digest.
* **Program resolution:** resolve each `program` to an absolute path, then check the base name of its real target against the refused list.
* **Path checks:** refuse or flag relative `PATH` entries, programs inside the project or workspace, programs whose file or parent directories other users can write to, and version manager shims.
* **Arguments and environment:** flag `any` placeholders after options that run code, and broad `env` `allow` patterns that pass variables which steer loading.
* **Egress:** flag plain `http` and hosts that can carry data out.

Each finding carries a severity (refuse or warn) and an explanation for the generated comments.

**Tests:** `mytool -> /usr/bin/env`; a shim; a program in a directory others can write to; a symlink in a container directory; a `.env` file in a container directory.

## 12. Establish a user verification file format

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

## 13. Add the user verification file format parser

Parsing the user verification file requires YAML parsing, which we currently only parse through the SUC generator.  It makes sense to allow this to be an additional input file passed to the SUC generator as another input file format.  It would output into the SUC in the same way as the verified project settings.

The same parsing rules as step 7 apply.  After parsing, the checks library (step 11) runs on the result before anything is stored: paths the user typed get the same path checks as resolved ones, and the refused lists still apply.

A missing piece here is the fingerprint parts of the project directory.  However, that may not be included in the file, based on its design in part 5 above.

## 14. Add the main_verify.py script

Add in a script that can process the user-directory verified storage and construct the items for the user to verify.

This will need to construct the user verification file, which includes comments about the discoveries.

The script can construct a YAML file without adding a library - strings output using JSON encoded strings, everything else is simple indented dict and list.  Keep `ensure_ascii=True`, so bidirectional, zero-width and other non-ASCII characters show up as `\u` escapes, and strip control characters from anything printed to the terminal.

The code that constructs the pieces for verification should go into a library for use by other scripts.

**Tests:** a value containing a newline and `# reviewed: safe`; bidirectional override and zero-width characters; terminal escape sequences.

## 15. Add the use / deny / edit prompt

With this step, an acceptance can be made, so remove `gen-scoter-uc --unreviewed-project-commands` and `scoter.py`'s warning (step 7's stand-in, kept by step 8).

`main_verify.py` presents the verification file and asks the user to use it as-is, deny it, or edit it.

* Create the file with `mkstemp` in a directory only the user can read.  After editing, open it without following symlinks and read it once.
* Use `VISUAL`, then `EDITOR`, started with the temporary directory as its working directory, not the project.
* After the editor exits, parse and check the file again (step 13) and ask again.  Never store a file that fails.
* Pressing Enter without a choice denies.
* Without a terminal, refuse to prompt and fail.

**Tests:** the temporary file replaced by a symlink while the editor is open; an edit that adds a refused program; no terminal.

## 16. Add verification handling in main_run.py

The main_run.py script should then consume the verification section from the SUC, and use the above library to determine if the verification applies to the current settings.

Build the container from the stored, accepted copy (step 5), or from a copy staged from it, never from the live `.surf-scoter.d`.  Otherwise the directory can change between the check and the build.

In the first cut, the run script will not allow re-validation, but instead will instruct the user how to re-verify.

**Tests:** a change to `.surf-scoter.d` after verification and before the build; an expired acceptance.

## 17. Add diff verify to main_verify.py script

Add in the capability for the main_verify.py script to perform a diff if the directory changed.

Use the three stored versions (step 5) to show what the project changed separately from what the user changed, and show only those differences in the verification file.

## 18. Wrap up previous steps

Things left dangling because of dependency ordering.  These may be tackled earlier, but this tracks unfinished work.  Some of these may end up as future tasks.

- Step 2:
  - The `./` path resolution on program files remains a check the user runs, not one the tools enforce.  `scoter run` should run the same check before it starts anything, and step 11's path checks for host commands (directories others can write to, version manager shims) should cover the setup's programs too.
- Step 3:
  - The container's starting directory in the workspace should be the project directory's path relative to the VCS root.
  * The VCS commands (`local-clone`, `fetch-changes-into`, `merge-changes` and so on) run with the environment `scoter` was started with.  A project can set `GIT_DIR`, `GIT_WORK_TREE` or `GIT_CONFIG_*` when you enter its directory (say, through a `.envrc`), which would redirect them.  Run them without the VCS's own environment variables; the VCS section could list the ones to drop.
  * ~~If the project file gets a `root` setting (step 7), it may only name a directory between the project directory and the root found here.~~  Step 7 decided against a project `root`.
- Step 4:
  - The checks are only validated and combined.  Nothing applies them to the project's commands yet: that's step 11, and `host-brokered` still finds each command's `program` on the `PATH` rather than through the checks' `command-map` (steps 7, 8 and 10).
  - The project files (secret-looking files) and egress host lists are drafted above, as kinds of their own, for steps 11 and 9.
  - On Windows, matching `name-patterns` against the real file's name drops its extension, whatever it is, rather than only a `PATHEXT` one.  Revisit with step 10's Windows name decisions.
  - `env` entries match a command's `allow` entries as exact text.  Matching patterns against patterns (does `allow: [GIT_*]` pass `GIT_SSH_COMMAND`?) is future work.
  - The notices only show in `scoter user-setup`.  Decide whether the review (step 14) shows them too.
- Step 5:
  - `active_env.py`'s lock checks and then writes, which two runs can interleave, and a crashed run leaves it held.  Move it to `fileutil.exclusive_lock`.
  - Step 16 builds from a restored snapshot; it must check that the staged copy parses to the same `project` and `dev-container` as the SUC that gave the YAML files their canonical hashes (run the generator against the staged copy and compare).
  - Step 17 decides when a match on meaning carries an acceptance forward (`EnvironmentRecord.carry_forward`), and how the user is told.
  - On Windows, the store's owner and mode checks are skipped; the design's "under the user's profile" check isn't written yet.
  - Case-insensitive file systems: the environment id doesn't fold the path's case.
  - Any change to the setup values asks for a new review.  Some don't affect a project's security; telling them apart is for step 19.
- Step 7:
  - The review (steps 12 to 14) freezes `${section:key}` values at acceptance, and records the `@NAME@` names each command uses (not their values), asking again when a project starts using a new one.
  - `protocol: https` needs a gateway that looks inside TLS; step 9 refuses it, and `methods`, on the Squid gateway.
- Step 8:
  - A blanket acceptance has no `accepted.json`; `scoter.py` treats a blanket match as no match until step 11's checks can work the settings out from the project's files.
  - `scoter kill` isn't implemented; when it is, it removes `${scoter:state-dir}/environments/HOST`, as `scoter run`'s teardown does.
  - The running environments file (`running-env.json`) keeps a copy of the whole SUC for teardown; it's in `state-dir` now, but not written private.

## 19. Review the settings format

The settings files have become important for declaring how to lock down an environment.  Because of that, it must present the information in such a way that the users can get a clear idea of what it's doing.  The more complex the file, the harder time users have understanding it, and the higher chance for mistakes.

We should review the settings files and the formats and look for opportunities to make them clear and reduce the chances for mistakes.  We should also make it extremely obvious if the user follows malicous online instructions to remove important restrictions.

The user-setup sub-command can provide capabilities for exploring and disecting the setup.  It should help to identify weaknesses without overwhealming the user.

## 20. Put a bow on it

- Allow a timed exemption for working on a project.
- Allow a user to declare a project "accept" through CLI args (also used by CI/CD).  This passes an already accepted file explicitly; it never accepts a suggestion automatically.
- Allow a user to revoke a project's acceptance.
- Allow a user to list accepted projects, with their expiry and exemptions.
- Add a user cache cleanup step.
- Remove these steps from this file, and move the design and reasoning made in the steps into a separate file to indicate what exists.
- Clean up the code to remove references to steps from this document.


# Future Tasks

## Choosing the setup for a project run

`--user-setup` was removed: with the egress rules in the project file, and the accepted settings in the user's own storage, a project no longer needs its own setup file.  The remaining need is to run a project with a different docker-compose file and related images (say, an alternate gateway, or extra service containers).

Instead of reading another setup file, let the user choose, for a project run, which setup section to use for those parts, from sections their own setup files (or the distribution's) define.  The choice is the user's, recorded with the project's accepted settings, never the project's.

This also bears on the accepted egress rules: they were reviewed for one gateway (step 9), so a section with a different gateway, or a different compose file, may enforce them differently.  Switching sections should check the accepted rules against what the chosen section's gateway can enforce, and ask for a new review when it can't enforce them, or enforces them differently.  This also applies to the host command blocking rules.

## Naming by the environment id

Step 5 gives each environment an id (the project directory's name and a hash of its canonical path), and uses it for the verified storage.  Use it for the rest of what's named per environment, so more than one `.surf-scoter.d` in a repository can run at once:

* the container names, in place of the `ssh: values: host-prefix` and a reserved suffix;
* the workspace path, which then moves out of the project, as `${scoter:state-dir}/workspaces/${scoter:environment-id}` (FILE-9).

That needs the generator to work out the id, as a tool-set `${scoter:environment-id}`, with exactly the same result as step 5's Python code (or the Python code to read it from the SUC).  It changes `scoter run`, and the running environments file.

## Additional declarative prohibitions to include project files and egress hosts

The secret-looking files and the hosts that can carry data out follow the same shape (`action`, `reason`, the most restrictive action winning), each as a kind of its own with its own match field.  They can wait for the steps that use them (step 11 for the files, step 9 for the egress rules).  Their starting lists:

Project files (`.gitignore`-style patterns, warn): `.env`, `.env.*`, `*.pem`, `*.key`, `*.p12`, `*.pfx`, `id_*`, `.netrc`, `.npmrc`, `.pypirc`, `.git-credentials`, `*.kdbx`, `*.tfstate`, `.aws/`, `.ssh/`, `.docker/config.json`, `.kube/config`.

Egress hosts (all warn; several, like `github.com`, are often needed, which is why they warn rather than refuse):

| Check | Matches |
|---|---|
| `code-hosts` | `github.com`, `*.github.com`, `*.githubusercontent.com`, `gitlab.com`, `*.gitlab.com`, `bitbucket.org`, `codeberg.org` |
| `paste-sites` | `pastebin.com`, `paste.ee`, `hastebin.com`, `dpaste.org`, `ghostbin.site`, `rentry.co` |
| `object-storage` | `*.s3.amazonaws.com`, `s3.amazonaws.com`, `*.blob.core.windows.net`, `storage.googleapis.com`, `*.r2.cloudflarestorage.com`, `*.digitaloceanspaces.com` |
| `user-content` | `*.ngrok.io`, `*.ngrok-free.app`, `*.trycloudflare.com`, `*.workers.dev`, `*.pages.dev`, `*.vercel.app`, `*.netlify.app`, `*.herokuapp.com`, `*.glitch.me`, `*.repl.co` |
| `webhooks` | `webhook.site`, `*.requestbin.com`, `*.pipedream.net`, `hooks.slack.com`, `discord.com`, `*.discord.com`, `api.telegram.org` |
| `file-sharing` | `transfer.sh`, `file.io`, `wetransfer.com`, `*.dropbox.com`, `drive.google.com` |

These should go into a similar "checks" structure as the host-bridge items.

## Remove the `--bin-dir` and `--sub-cmd-dir` arguments

With the updates to self-hosting, these two arguments are now not needed.  The corresponding resolved values in the SUC are still necessary, though.
