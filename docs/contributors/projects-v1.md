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
* `scoter` lists the store's size with the accepted projects (step 21), and can prune or revoke them.

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
* **`dev-container`** (added by step 12): `use` (`"project"`, or `{"name": NAME}` for one of the user's containers) and `images` (each image the build starts from, and what to build from instead).
* **`overrides`** (added by step 12): `{"commands": [{"index": N, "checks": [...]}], "egress": [...]}`, the setup checks the user set aside for each item.
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

Completed:

* **The setup.**  [defaults/setup.yaml](../../defaults/setup.yaml) has a `network-gateway` section: `values: name` (`squid`), `values: config-mount` (where the gateway reads its file), and `runs: write-config`, given `{x:universal}` and `{x:output}`.  `dev-env: paths: allowed-domains` is gone, and so is `.surf-scoter/allowed-domains.txt` (this repository's list moved into its project file's `egress`, as exact hosts).
* **The translation.**  [`gateway_squid.py`](../../scripts/gateway_squid.py) (the Squid translation), with [`gateway.py`](../../scripts/surf_scoter_lib/gateway.py) (reading the rules, and the report every gateway gives): the rules come from the SUC's accepted `egress`, or, until step 15, the project file's (the file and `scoter run` say so).  Each is checked again (a host that isn't a host name, an unknown protocol or port, is refused outright).  The report (JSON on stdout) lists each rule enforced, and each refused with its reason; any refusal means nothing's written, exit 1.  For Squid, only `tls` on 443 is enforced: `https` needs a gateway that looks inside TLS, `http` needs port 80 through the firewall, and other ports need firewall changes (N4).
* **Squid** reads one file, `/user-space/egress-hosts.txt`, with `dstdom_regex -n -i`: `^host$` for a host, `^.+\.domain$` for `*.domain`.  Squid's `dstdomain` can't say "subdomains but not the domain itself", which a `*.` rule means, so the regular expressions keep it from being broader.  The image's default list allows nothing.  Private, loopback, link-local and metadata destinations are still refused after resolution (`blocked-destinations.txt`, and the firewall), whatever the list says.
* **`scoter run`** runs `write-config` into `resources/gateway-config` (mode `0644`, as the gateway's own user reads it), shows any refusals and stops, and the docker-compose file mounts it with `GATEWAY_CONFIG_HOST` and `GATEWAY_CONFIG_CONTAINER`.  The setup directory no longer carries it, so the dev container doesn't see it.
* **The gateway in acceptances.**  `setup_values` records the gateway's name and `write-config`, so a different gateway asks for a new review.  The generator refuses accepted settings whose `gateway` isn't the setup's.
* **Docs:** [network](../security/network.md), [settings](../security/settings.md), [files](../security/files.md), the attack vector lists, the [project file](../project-configuration-file.md#egress), the [system configuration](../system-config-file.md), the README and the new project guide.

**Tests:** in `test_gateway.py`: the rules' source (accepted, then the project's), and rules refused outright; in [`test_gateway_squid.py`](../../scripts/tests/test_gateway_squid.py): Squid's enforced and refused rules (`https` with `methods`, `http` with `methods`, port 22), the patterns (no broader than the rule), and the script (writes, refuses with nothing written, bad input).  In `universal.rs`: settings accepted for another gateway.  Checked by hand against the real Squid image: the configuration parses (with a list, and the empty default), the patterns allow and refuse the right hosts in a running Squid (`x.registry.npmjs.org`, `pkg.dev` and `evilpkg.dev` refused; `us.pkg.dev` and `a.b.pkg.dev` allowed), and a listed host that resolves to loopback (`localtest.me`) is refused.  A test that runs the containers would make those repeatable.

## 10. Update `scoter-host` to run accepted executables

The host bridge (Rust) needs to change to match:

* Run the absolute executable path from the SUC.  Remove the `PATH` lookup in `find_on_host` ([`exec.rs`](../../host/src/host/exec.rs)), which closes BRIDGE-11.
* Match the name the client sends in the handshake exactly against the accepted names, and never look it up anywhere else.
* Decide how names compare on Windows (case, and `PATHEXT` such as `aws` against `aws.exe`).

**Tests:** a handshake name that isn't accepted; a relative `PATH` entry on the host; an executable removed after acceptance.

Completed:

* **Absolute programs only.**  `scoter-host` runs `host-brokered`'s `program` as it is: `accepted_executable` in [`exec.rs`](../../host/src/host/exec.rs) refuses one that isn't absolute, or isn't an executable file any more (removed or changed since acceptance), and never looks it up; `find_on_host` is gone.  Loading the universal file refuses a `program` that isn't absolute or `null`, too.  This closes [BRIDGE-11](../security/host-bridge-attack-vectors.md#bridge-11-the-hosts-path-lookup).
* **No relative `PATH` entries.**  `find_executable` skips relative and empty `PATH` entries, so the generator (finding the setup's programs, the host command checks' programs, and, until step 15, the unreviewed commands') never resolves a program against the directory it runs in.
* **Names.**  The handshake name (the client's `argv[0]`, its last path part) is matched exactly against the accepted `command` names, and nothing else (the existing `denies_lookalike_commands` test covers `AWS` and `aws.exe` against `aws`).  On Windows too: names are case sensitive (step 4), and `PATHEXT` doesn't apply at run time, as the accepted program is an absolute path with its extension.

**Tests:** in `exec.rs`: only absolute executables run (a bare name, a relative path, a missing program, one removed after acceptance, one not executable).  In `command.rs`: relative `PATH` entries are skipped, and a later absolute one still counts.  In `config.rs`: a relative `host-brokered` program is refused when the universal file loads.  The host's own tests now give their commands absolute programs, as the universal file does.

## 11. Add the verification checks library

A Python library, used by the verify and run scripts, that runs the checks from the design using the lists from step 4:

* **`.surf-scoter.d` contents:** refuse symlinks, hard links and special files in container directories; warn about hard links elsewhere; warn about secret-looking files; warn when a `FROM` line has no digest.
* **Program resolution:** resolve each `program` to an absolute path, then check the base name of its real target against the refused list.
* **Path checks:** refuse or flag relative `PATH` entries, programs inside the project or workspace, programs whose file or parent directories other users can write to, and version manager shims.
* **Arguments and environment:** flag `any` placeholders after options that run code, and broad `env` `allow` patterns that pass variables which steer loading.
* **Egress:** flag plain `http` and hosts that can carry data out.

Each finding carries a severity (refuse or warn) and an explanation for the generated comments.

**Tests:** `mytool -> /usr/bin/env`; a shim; a program in a directory others can write to; a symlink in a container directory; a `.env` file in a container directory.

Completed: [`checks.py`](../../scripts/surf_scoter_lib/checks.py), `check_project(config)` (a `UserConfig`, whose accessors read the universal file, with `jdata`'s typed lookups for the setup's combined checks; a badly formed file is a `ConfigErr`), giving a `Report`: how each command's program resolved, and the findings, each with a severity (`refuse`, `warn`, or `note` for what the review shows without judging), a key in the project file's terms (`commands[0].args[1]`, `egress[2]`, `env/Dockerfile`), an explanation, and the setup checks it came from.

* **Program resolution:** each command's `program` (or `command`) through the setup's combined `command-map`, or else the `PATH`'s absolute entries.  A program that isn't there is refused.
* **The `host-commands` checks** (step 4), from the SUC's `host-command-checks`: by name, by the file the program really is (so `mytool -> env` is `env`), and by `name-patterns` on the name or the real file's name; `options` when a value the container fills in follows the option (or is joined to it, `--eval=`); `env` entries against the project's and the command's `allow`, exactly; `paths` (version manager shims) against the real executable.  The most restrictive applies first, then the overrides, as step 4 says; each matching check is a finding with its own reason, and an override is a note naming what it replaced.  A command no check covers is a note.
* **Path checks:** relative or empty `PATH` entries (a warning: the tools skip them); a program in the project, its repository or its workspace (refused); a program whose file or any parent directory belongs to another user, or anyone can write to (refused; a sticky directory, such as `/tmp`, is fine), or its group can write to (a warning).  Both the path found and its real target are checked; a symlink's own mode isn't.
* **Egress:** plain `http` (a warning), and hosts anyone can upload to (a warning), from the new `scoter: value-lists: egress-data-out-hosts`.
* **The project's files:** in container directories inside `.surf-scoter.d`, symlinks, hard links and special files are refused, every problem reported rather than the first; files that look like secrets warn, from the new `scoter: value-lists: secret-file-patterns`; a `FROM` without a digest warns (unless it's `scratch`, a local parent's tag, or an earlier stage), and one from a build argument warns that it can't be told.  A hard-linked project file warns.  Container directories outside it are trusted, as step 5 decided.

The secret-looking files and the hosts that can carry data out are `value-lists` for now; the future task below would make them check kinds, with an action and a reason each.

**Tests:** in [`test_checks.py`](../../scripts/tests/s_lib/test_checks.py): the step's five, and resolution (`command-map`, the `PATH`, a missing program, a relative `PATH` entry), name patterns, notes for allowed and unknown commands, overrides in two steps, options, `env`, a program in the project, a group-writable and a sticky directory, symlinks' modes ignored, `http` and data-out hosts (exactly and under a wildcard), hard links and FIFOs, container directories outside the project files directory, and `FROM` lines (stages, local tags, digests, build arguments, continuations).  Run against this repository's configuration, a pretend `sh -c {any}` is refused as a shell and warned about for `-c`.

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

### Design: data is what's accepted; everything else is a comment

The file holds two kinds of content:

* **YAML data**: only what becomes the accepted settings (`accepted.json`, step 8): the egress rules, the commands, their environment, the dev container choices (below), and `expires-at`.  It reads like the project file, with everything resolved: each program's absolute path, each `${section:key}` frozen to its value.
* **Comments**: everything the user needs to judge it, but can't change here: the summary, each finding (step 11), how each program was found, the dev container's files, the setup it's reviewed under.  The parser (step 13) drops every comment, so editing one changes nothing, and the next review writes them afresh.

Generated explanations start `#scoter:`.  What the user might want to switch on is generated as plain YAML commented out with `# `, so removing the `# ` makes it data: the `result: reject` line, and the refused items.

The user keeps a note with `$comment:`, a key any mapping in the data can have (the file as a whole, a rule, a command).  It's data, so it's parsed, stored with the review, and carried into the next one; a plain `#` comment the user writes isn't kept, and the file's header says so.

```yaml
#scoter: Surf Scoter review: /home/user/mono/client
#scoter:   project files  .surf-scoter.d (snapshot sha256:91b0...)
#scoter:   branch         main
#scoter:   gateway        squid
#scoter:
#scoter: Summary: 1 refused (commented out below), 2 warnings, 3 notes.
#scoter:   refused  commands: sh -- a shell runs whatever command it's given (host-bridge:shells)
#scoter:   warn     egress[1] github.com -- lets the container send data out
#scoter:   warn     dev-env/Dockerfile -- `FROM debian:bookworm` has no digest
#scoter:
#scoter: To say no, remove the `# ` before `result: reject`.  To accept less, delete an
#scoter: egress rule or a command, narrow a command's arguments, or shorten `expires-at`.
#scoter: To keep a note with this review, add a `$comment:` to any entry; `#` comments
#scoter: aren't kept.
review: "surf-scoter-review-1"
# result: reject
project-dir: "/home/user/mono/client"
expires-at: "2026-11-19T15:04:05+00:00"

egress:
  - {host: "registry.npmjs.org", protocol: "tls", port: 443}
  #scoter: warn: `github.com` lets the container send data out (code hosts take uploads).
  - host: "github.com"
    protocol: "tls"
    port: 443
    $comment: "Needed for the release workflow; the token only has read access."

env:
  allow: ["LANG"]
  set:
    #scoter: from `${ssh:config-file}`
    "AWS_CONFIG_FILE": "/home/user/.aws/config"
    "HOME": "@HOME@"

commands:
  #scoter: note: no check in your setup covers `aws`.
  #scoter: runs /usr/local/bin/aws (found on the PATH)
  - command: "aws"
    program: "/usr/local/bin/aws"
    args: ["s3", "ls", {any: {prefix: "s3://my-bucket/"}}]
  #scoter: refused: a shell runs whatever command it's given (host-bridge:shells).
  #scoter: To accept it for this project anyway, uncomment it, keeping `$override`.
  # - command: "sh"
  #   program: "/usr/bin/sh"
  #   args: ["-c", {any: {}}]
  #   $override: ["host-bridge:shells"]

dev-container:
  #scoter: The project's container: .surf-scoter.d/dev-env
  #scoter:   dev-env/Dockerfile  2310 bytes  sha256:aa07...
  #scoter:     | FROM debian:bookworm
  #scoter:     | RUN apt-get update && apt-get install -y build-essential
  #scoter:   dev-env/dev-env.yaml  210 bytes
  #scoter:   dev-env/bin/entry.sh  140 bytes  executable
  #scoter:   parents: dev-env (the installation's, trusted)
  use: "project"
  images:
    #scoter: warn: no digest; the tag can point at new content at any time.
    "debian:bookworm": "debian:bookworm"
```

### Design: what each part does

* **`review`** names the format, so a later version can tell an old file.
* **`result`** is commented out: the file says yes (with the step 15 prompt's "use") unless the user uncomments `result: reject`.  `reject` is the only value; any other is an error, so a typo can't accept.  Rejecting stores nothing, as a "deny" doesn't.
* **`project-dir`** is read-only: the parser refuses the file if it's changed.  It says which project this is, and the generator checks it again (step 8).
* **`expires-at`** defaults to `verified-expiry-days` from now.  The user can shorten or lengthen it; it must be in the future.  `exempt-until` isn't here: exemptions are their own command (step 21).
* **`egress`**, **`env`** and **`commands`** are the project file's, resolved, in its shape, `env` split as the project has it (a top-level `env`, and each command's own).  That keeps the next review's diff readable: what the user changed against what the project changed, item by item (step 17).  `${section:key}` is frozen, with its source in a comment; `@NAME@` stays as written (step 7).  The parser builds each command's whole environment plan, as `accepted.json` holds it.
* **Refused items** are generated commented out, with their reasons, so the file can be accepted as it stands, minus what can't be.  The parser runs the checks again (step 13), so an item the user uncomments is refused again, unless it carries an `$override` (below).
* **Findings** come before the item they're about, worst first.
* **The branch and the gateway** are comments: they come from the project and the setup.

### Design: editing

The user may change any data, narrowing or widening: remove a rule or a command, narrow an argument (a literal for an `any`, a longer `prefix`), point a `program` at another executable, add a command or an egress rule the project didn't ask for (the user may have uses the project doesn't anticipate).  Everything typed gets the same checks as the suggestion (step 13): a typed `program` gets the path checks, and the refused lists still apply.  The next review marks what the user added or changed, against the project's suggestion, as "added by you" or "changed by you" (step 17), so edits stay visible.

### Design: overriding a refusal for this project

Refusals come from two places, and only one can be set aside here:

* **Policy refusals** come from the user's own setup: a `host-commands` check with `action: refuse` (`host-bridge:shells`, say).  The user owns that policy, so they can make an exception to it.
* **Safety refusals** come from facts that accepting can't change: the program isn't on this computer; it's in the project, the workspace or the repository; another user owns it, or anyone can write to it or a directory above it; a container directory has a symlink, a hard link or a special file.  These can't be overridden.

A policy-refused item is generated commented out with an `$override` line, naming the checks that refused it.  Uncommenting the item with its `$override` accepts it for this project; uncommenting it without one, it's refused again, so a refusal is never undone by accident.

* **`$override`** is a list of `section:key` check names, on a command (or an egress rule, when the later egress checks can refuse).  The named checks don't apply to that item; every other check still does.
* **Exact names only**, as the setup's `overrides` (step 4): a check the list doesn't name still refuses, including one a later version of the setup adds.  A name that isn't a check refusing the item is an error, so a stale or misspelled override is caught.
* **This project, this item, this acceptance.**  It doesn't change the setup, and it doesn't carry to other projects; an exception for every project belongs in the setup, as an `overrides` check.
* **Recorded and visible.**  The acceptance records each override (in `accepted.json`, with the command, so the generator can tell an accepted override from a smuggled command), and the next review shows it: "you overrode `host-bridge:shells` here".  `scoter user-setup` lists the overrides in force, with the setup's own.
* **Safety refusals get no `$override` line**, and the parser refuses one written on an item refused for safety.

`$override` is data, like `$comment`, so it's kept with the review and carried into the next one on its item, as long as the item hasn't changed.  If it has, the override is dropped, and the item is refused again until the user looks at it: an override covers what the user saw.

### Design: comments the user keeps

`$comment` is text, on any mapping in the data.  The parser keeps them with the review (`review.yaml`, step 5), not in `accepted.json` (the runtime doesn't need them).  The next review puts each back on its item: the file's own on the file; a rule's or a command's on the item it was on, matched by what the item was (its accepted data, ignoring the `$comment`).  An item that changed keeps its note, marked "on the earlier version"; a note whose item is gone is listed in the header, so it isn't lost silently.

### Design: the dev container

As drafted earlier, the container was only described: the user could accept it or reject the whole review.  A user who doesn't trust what the project builds should have choices short of rejecting:

* **`use`**: `"project"` (the project's container, the default), or the name of a container in the user's own `dev-containers-search` directories (`{name: "my-env"}`), which the user trusts as part of their setup.  The project's commands and egress still apply, but its Dockerfile isn't built.  The user's container must still be one whose `dev-env.yaml` gives what the tools need (mounts, the broker); the parser checks it's there and loads, as for any container.
* **`images`**: each image the build starts from (the `FROM` lines, the project's and its local parents'), mapped to what to build from instead.  It starts as each image itself; the user can pin one to a digest (`"debian@sha256:4c1e..."`), or point it at an image they already have (`"local/debian-hardened:12"`).  The build passes them as named build contexts (`--build-context debian:bookworm=docker-image://debian@sha256:...`, which BuildKit and recent Podman read), so the project's Dockerfile needn't change.  The `docker` section gets a `runs` entry for the build context argument, so another container tool can say how it does it.

Both go into `accepted.json` (step 8's shape gets a `dev-container` with `use` and `images`), and into the setup of the run: step 16 builds from the user's choice.  The fingerprint of the project's container (step 5) still matters with `use: "project"`; with the user's own, a change to the project's container is noted, but doesn't need a new review.

### Design: writing it safely

Step 14 writes it without a YAML library, so the format is a small subset that's easy to write and parse:

* every string double-quoted, as JSON writes it, with `ensure_ascii=True` (so bidirectional, zero-width and look-alike characters show as `\u` escapes);
* placeholders and short lists in flow style (`{any: {}}`, `["s3", "ls"]`), the rest in block style;
* every key the parser expects written, in a fixed order; no anchors, aliases, tags or block scalars;
* every comment written from data that's been stripped of control characters, one line each.

### Design: the branch

A `runs: current-branch` in each VCS section, like `find-root`: the distribution's script for Git reads `.git/HEAD` (following a `gitdir:` file, for worktrees) from the project directory, without running Git, which would read the repository's configuration.  It's a comment, and a note in `environment.json` (step 5), not part of the match.

### Open items

* **Image digests.**  Not looked up by default: asking a registry needs the network and credentials, and can be slow.  A quick `docker images` inventory, to say which of the `images` are already stored locally (and their digests), could be a sub-command of its own later.
* **The dev container choices** (`use` and `images`) are in the first cut.  Step 8's `accepted.json` gains a `dev-container` with them.
* **Refused items the user wants anyway**: an `$override` on the item, for policy refusals only (above).

## 13. Add the user verification file format parser

Parsing the user verification file requires YAML parsing, which we currently only parse through the SUC generator.  It makes sense to allow this to be an additional input file passed to the SUC generator as another input file format.  It would output into the SUC in the same way as the verified project settings.

The same parsing rules as step 7 apply.  After parsing, the checks library (step 11) runs on the result before anything is stored: paths the user typed get the same path checks as resolved ones, and the refused lists still apply.

A missing piece here is the fingerprint parts of the project directory.  However, that may not be included in the file, based on its design in part 5 above.

Completed:

* **The parser.**  [`review.rs`](../../host/src/config/review.rs): step 7's rules (no tags, merge keys, duplicate keys or control characters); `review` must be the format's name; `result: reject` says no, whatever else the file says (the rest isn't read); `project-dir` and `expires-at` are required; `egress`, `env` and `commands` are checked as the project file's are, except that each `program` must be an absolute path, and a `${...}` in an `env` value is refused (the review's values are as accepted); `$comment` (text) and `$override` (`section:key` names) are allowed on the file and on each egress rule and command, and taken off before the checks; `dev-container` takes `use` (`"project"`, or `{name: NAME}`) and `images` (each mapped to an image reference, which can't start with `-` or hold spaces or `=`).
* **What it gives.**  `Review::accepted` builds `accepted.json`'s contents: each command with its whole environment plan (the top-level `env` merged with its own, as written), the normalised egress rules, the `dev-container` choices and the `overrides`, with the project's `vcs` and the setup's gateway.  [`accepted.rs`](../../host/src/config/accepted.rs) reads `dev-container` and `overrides` back, checking each override's index.
* **The generator.**  `gen-scoter-uc --review FILE` checks the review against the run (this project, an unexpired `expires-at`, a `{name: ...}` container that's in `dev-containers-search`) and writes the SUC's `review`: `file`, `result`, and the accepted settings (`null` when rejected).  Nothing runs from it.
* **The checks.**  [`checks.check_review`](../../scripts/surf_scoter_lib/checks.py) runs step 11's checks on what the review gives: each program as given (nothing looked up; a missing one is refused), each command's whole plan for `env`, the review's egress rules, and the project's container files when `use` is `"project"`.  Then `apply_overrides`: a refusal whose checks are all named becomes a note ("You overrode ..."); a name that doesn't refuse the item, or an override on an item refused for safety, is refused.

**Tests:** in `review.rs`: a full review, `reject` winning over a broken file, `result` other than `reject`, the format name, unknown keys, a relative `program`, a Windows device name, a `${...}` value, a bad `$override`, bad `use` and `images`, a merge key, a duplicate key, a control character, an unknown command field; and what it gives reading back as accepted settings.  In `universal.rs`: `--review` accepted, rejected, and refused (another project, expired, a container that isn't there), and `host-brokered` unchanged.  In `test_checks.py`: a policy refusal with and without an override, an override naming a check that doesn't refuse, a safety refusal that can't be overridden, the user's own container skipping the project's files, and no review or a rejected one.  Run on this repository, a review accepting `sh -c {any}` with `$override: [host-bridge:shells]` gives a note for the shell and keeps the `-c` and `github.com` warnings.

## 14. Add the main_verify.py script

Add in a script that can process the user-directory verified storage and construct the items for the user to verify.

This will need to construct the user verification file, which includes comments about the discoveries.

The script can construct a YAML file without adding a library - strings output using JSON encoded strings, everything else is simple indented dict and list.  Keep `ensure_ascii=True`, so bidirectional, zero-width and other non-ASCII characters show up as `\u` escapes, and strip control characters from anything printed to the terminal.

The code that constructs the pieces for verification should go into a library for use by other scripts.

**Tests:** a value containing a newline and `# reviewed: safe`; bidirectional override and zero-width characters; terminal escape sequences.

Completed:

* **`scoter verify [--output FILE]`** ([`main_verify.py`](../../scripts/main_verify.py)) writes the review file for the project (to standard output, or to a file only the user can read, not following a symlink), and says how many items are refused and warned about.  Step 15 adds the editor and the prompt.
* **The library**, [`review.py`](../../scripts/surf_scoter_lib/review.py): `build(config, context)` lays the file out as step 12 says: the header (the project, its snapshot, its branch, the gateway, any acceptance that already matches, and a summary of the refusals and warnings); `result: reject` commented out; `project-dir`; `expires-at` (`verified-expiry-days` from now); the egress rules (ports filled in); the top-level `env` and each command's own, resolved, with each `${...}` source as a comment; the commands, each with its findings, how its program was found, and its absolute program; refused commands commented out, with `$override` naming the refusing checks when every refusal is a setup check's, and without when any isn't; and `dev-container`, listing the project's container files (Dockerfiles in full), its parents, `use: "project"` and `images` (each `FROM`, mapped to itself).
* **Writing it safely:** strings as JSON writes them with `ensure_ascii`; keys plain only when they're simple names; `clean` takes terminal escape sequences and other control characters out of every comment, and escapes anything outside ASCII, so the whole file is ASCII.
* **The resolved `env`.**  The generator now gives `current-project-resolved`: the project file's `env` blocks, each worked out against the setup on its own, not merged (step 13's carry-over).
* **The branch.**  The setup's `git-vcs: runs: current-branch`, running the new [`vcs_git_branch.py`](../../scripts/vcs_git_branch.py) (with [`vcs_branch.py`](../../scripts/surf_scoter_lib/vcs_branch.py)), reads `.git/HEAD` without running Git.
* **Found on the way:** a sticky directory (like `/tmp`) no longer draws a group-writable warning in the path checks, as it already didn't draw the refusal; `checks.from_images` lists a Dockerfile's base images, for both the `FROM` check and the review's `images`.

**Tests:** in [`test_review.py`](../../scripts/tests/s_lib/test_review.py): the step's three (a value with a newline and `# reviewed: safe` stays one quoted line; bidirectional, zero-width and look-alike characters show as escapes; terminal escape sequences go, from a setup check's reason and from a Dockerfile), the YAML pieces, the layout (refused items with and without `$override`, `${...}` sources, `images`), and the branch (a branch, detached, unreadable, a worktree's `.git` file).  In `universal.rs`: `current-project-resolved`.  By hand, on a scratch project: `scoter verify` writes a review that the generator's `--review` reads back as written, and again with the refused shell uncommented, where the checks turn the refusal into "You overrode `host-bridge:shells`".

## 15. Add the use / deny / edit prompt

With this step, an acceptance can be made, so remove `gen-scoter-uc --unreviewed-project-commands` and `scoter.py`'s warning (step 7's stand-in, kept by step 8).

`main_verify.py` presents the verification file and asks the user to use it as-is, deny it, or edit it.

* Create the file with `mkstemp` in a directory only the user can read.  After editing, open it without following symlinks and read it once.
* Use `VISUAL`, then `EDITOR`, started with the temporary directory as its working directory, not the project.
* After the editor exits, parse and check the file again (step 13) and ask again.  Never store a file that fails.
* Pressing Enter without a choice denies.
* Without a terminal, refuse to prompt and fail.

**Tests:** the temporary file replaced by a symlink while the editor is open; an edit that adds a refused program; no terminal.

Completed:

* **The stand-in is gone.**  `gen-scoter-uc --unreviewed-project-commands` is removed, and so are `scoter.py`'s warning and `main_run.py`'s egress warning.  Without an acceptance, `host-brokered` is empty and so are the gateway's rules: `UserConfig.egress_rules` gives only the accepted rules, and the gateway report has no `source`.  The checks read the project file's own rules through the new `project_egress`.
* **`scoter verify`** ([`main_verify.py`](../../scripts/main_verify.py)) now asks.  It refuses without a terminal (both standard input and output), and `--output FILE` still only writes the review.  It snapshots the project's files first, so the acceptance is for the files the review shows.  It writes the review with `mkstemp` in a `mkdtemp` directory, prints it, and checks it, then asks `[u]se / [e]dit / [D]eny`.  Enter, the end of input and Ctrl-C deny.
  * **Edit** runs `VISUAL`, or else `EDITOR` (split as a shell would, keeping a Windows path's backslashes), in the temporary directory.  With neither set, Windows falls back to Notepad, by its full path under `SystemRoot`; elsewhere, editing asks you to set one.  The file is then read once: opened with `O_NOFOLLOW` (and `O_NONBLOCK`, so a FIFO can't hang it), and it must be a regular file, owned by the user, with one link, within the generator's size limit.  A link or anything else in its place stops the review, and nothing is stored.
  * **Every check** writes the bytes read to a fresh private file, so what's checked is what's stored.  It runs `gen-scoter-uc --universal ... --no-setup-search --project-dir ... [--vcs-root ...] --review COPY`, then `checks.check_review`.  The generator's complaints name the file being edited, not the copy.
  * **Use** is refused while the generator complains or anything is refused; `result: reject` denies.
* **Storing** ([`verified/accept.py`](../../scripts/surf_scoter_lib/verified/accept.py)): `store_acceptance` puts the snapshot's blobs in the store, under its lock, and writes `accepted.json` (the SUC's `review: accepted`), `review.yaml` (the bytes the user left) and `suggestion.json` (`project` and `dev-container`).  It adds a `reviewed` acceptance, newest first, with the review's `expires-at`, the branch, the snapshot's manifest and the setup values.  Saving prunes to `verified-keep`.  The lookup's snapshot and setup helpers are now public for it (`project_snapshot`, `vcs_section`, `verified_containers`), and so is `records.parse_time`.
* **By hand**, on a scratch project and distribution, through a pseudo-terminal: used as-is, it's stored, the next lookup matches exactly, and the third generator run brokers the accepted `ls` with its egress rule.  Uncommenting the refused `sh` is refused until it's denied; a symlink swap stops the review with the store unchanged; a broken edit shows the generator's complaint; `result: reject` denies.

**Tests:** in [`test_main_verify.py`](../../scripts/tests/test_main_verify.py): the step's three (the file replaced by a link while the editor is open, an edit that uncomments the refused shell, no terminal); the same edit with its `$override`, stored; use, deny, a broken edit, `result: reject`; the choices; the editor's choice (Notepad on Windows); and `read_once` on a link, a FIFO, a hard-linked file and an oversized one.  The prompt tests run the debug generator, and skip without it.  In [`test_accept.py`](../../scripts/tests/s_lib/verified/test_accept.py): storing, with private files and an exact lookup afterwards; newest first; no `verified-dir`.  In `universal.rs`: `host-brokered` empty without accepted settings.  In `test_gateway.py`: only the accepted rules.

## 16. Add verification handling in main_run.py

The main_run.py script should then consume the verification section from the SUC, and use the above library to determine if the verification applies to the current settings.

Build the container from the stored, accepted copy (step 5), or from a copy staged from it, never from the live `.surf-scoter.d`.  Otherwise the directory can change between the check and the build.

In the first cut, the run script will not allow re-validation, but instead will instruct the user how to re-verify.

**Tests:** a change to `.surf-scoter.d` after verification and before the build; an expired acceptance.

### Draft: what the run has to go on

By the time `main_run.py` starts, `scoter.py` has run the lookup (step 8) and, if it matched, the generator's third run has loaded the acceptance's `accepted.json`.  So the SUC's `accepted-settings` says whether there is one; its `host-brokered` and `egress` already come only from it (step 15).  What the run doesn't have is the snapshot the acceptance was made for, and nothing yet stops it building from the live `.surf-scoter.d`.

### Draft: no acceptance, no run

Without `accepted-settings`, the run stops before building anything, and says how to go on:

> You haven't accepted this project's settings.  Run `scoter verify` to review them, then run again.

Saying *why* (no acceptance for this project; the matching one expired; the files or the setup changed since) is left for later work.

This holds for a project with no host commands and no egress rules too: its container files (`dev-env.yaml`'s mounts, [DEV-2](../security/dev-container-attack-vectors.md#dev-2-mount-points-are-written-into-the-compose-env-file), and the Dockerfiles) are part of what's reviewed.

A blanket acceptance (step 8) has no snapshot to build from, so it still counts as no match; that stays a future task.

### Draft: finding the snapshot

`main_run.py` finds the acceptance from the environment's record: the `accepted.json` the SUC loaded must be one of the project's acceptances in the store, still usable, and with a snapshot.  (The draft had the run do the lookup again, but with `use: {name}` the SUC's dev container is the user's, not the one the snapshot was taken of, so the lookup can't match on it.  For the same reason, `scoter verify` now stops after the generator's second run: it reviews what the project suggests.)

### Draft: staging and checking the copy

* **Where.**  `${scoter:state-dir}/environments/HOST/build/`, next to `scoter-host`'s `suc.json`, private, and removed with it at teardown.  The manifest's files are restored (step 5's `restore`, checking each blob's hash) under `build/project/<project-files-dir>/`, so the copy is laid out like a project.
* **Checking.**  The SUC's `current-project-setup` and `dev-container` were parsed from the live files, before the snapshot was read, so the YAML files' canonical hashes vouch only for that parse (step 5).  Run the generator on the copy: `gen-scoter-uc --universal SUC --no-setup-search --project-dir build/project`.  Its `current-project-setup` must equal the SUC's, and so must its `dev-container`, once `parent-builds` directories inside the live files directory are mapped to the copy.  A difference stops the run ("the project's files changed while `scoter` was starting; run again").  Only these two sections are compared: a different project directory changes every `./` location in the setup, so nothing else is taken from this run.
* **Building.**  The dev container and its parents inside `.surf-scoter.d` build from the copy (the checking run's `dev-container-path` and `parent-builds`).  Parents outside it come from `dev-containers-search`, which the user's setup controls, and are trusted (step 5).  Those in `dev-containers-verified` had their manifest ids checked by the lookup a moment earlier; they still build from the live directory, which leaves a short window (a carry-over).  The firewall and gateway images are the distribution's.

### Draft: the acceptance's container choices

The review lets the user choose the container (step 12): `accepted.json` keeps `dev-container: use` and `images`, and nothing applies them yet.

* **`images`.**  Each mapping that isn't to itself rewrites the matching `FROM` in the staged Dockerfiles; the copy is private, so the live files are never touched.  The image is matched as the review listed it (`checks.from_images`: not `scratch`, a local parent's tag, or an earlier stage).  A `FROM` that spans lines with continuations isn't rewritten: the run stops and says so, rather than guessing.  The staged files are checked (above) before they're rewritten.
* **`use: {name: NAME}`.**  The user's own container from `dev-containers-search` replaces the project's.  Its `dev-env.yaml` gives the container's settings (user, mounts, `broker-bin`, sshd), so it has to come through the generator: given accepted settings that name a container, `dev-container-path` becomes that container's directory, and `dev-container` is its merged `dev-env.yaml`.  It's outside `.surf-scoter.d`, so it builds from the live directory, like any trusted parent.  The project's container files aren't built at all, though they're still in the snapshot.

### Design decisions

* `use: {name}` and `images` are applied in this step.
* The run finds the acceptance from the record, not by the lookup (above); `scoter verify` reviews without the accepted settings.
* Without an acceptance, the run only says to run `scoter verify`; later work refines the message (why there's no match).
* Building from the copy is fine: the build only ever runs within the container's own directory.

Completed:

* **No acceptance, no run.**  `scoter run` stops before anything else without `accepted-settings`: "You haven't accepted this project's settings.  Run `scoter verify` to review them, then run again."
* **Staging** ([`verified/staging.py`](../../scripts/surf_scoter_lib/verified/staging.py)), called by `main_run.py` once the environment's host name is reserved, into `${scoter:state-dir}/environments/HOST/build/` (private; removed at teardown, when the build fails, and when the run aborts; a stale one from a crashed run is removed first):
  * `find_accepted`: the loaded `accepted.json` must be `ENV-ID/acceptances/ID/accepted.json` in the store, for this project's environment id, with the acceptance in the record, usable now, and with a snapshot.  Otherwise the run stops, with the reason and "run `scoter verify`".
  * `stage`: restores the snapshot under `build/project/<project-files-dir>/`.  Restored files now get the usual modes (0644, 0755 when executable, directories 0755) inside the private root, whatever the umask, since a build's `COPY` keeps them in the image; before, they were 0600 and 0700.
  * `build_plan`: runs the generator on the copy ([`generator.py`](../../scripts/surf_scoter_lib/generator.py), now shared with `main_verify.py`).  Its `current-project-setup` must equal the SUC's, and, for the project's container, so must its `dev-container-path` and `dev-container` with `parent-builds` mapped back to the live files directory.  A difference stops the run with "the project's files changed while `scoter` was starting ... run again" (no new review needed).  The project's container and its parents inside `.surf-scoter.d` build from the copy; parents outside it from where they are.
  * `apply_images`: each `images` mapping that isn't to itself rewrites the `FROM` word in the staged Dockerfiles (`rewrite_images`); local parents' tags and earlier stages are left alone, and a `FROM` continued over lines is refused when its image would change.  Only for the project's container.
* **`use: {name}`.**  The generator, given accepted settings naming a container, makes it the dev container (`dev-container-path` and `dev-container`) in place of the project's.  It, and its parents by name, are looked for only in `dev-containers-search` directories outside the project directory and the VCS root (the default list starts with the project's own files directory), and the review check (step 13) uses the same rule.  The run builds it from where it is, and never builds the project's container.
* **`scoter verify`** loads without the accepted settings (`load_config(scope=ConfigScope.SUGGESTED)`), so it reviews the project's suggestion even when an acceptance uses the user's container.

**Tests:** in [`test_main_run_staging.py`](../../scripts/tests/test_main_run_staging.py), with the generator and acceptances made through the review prompt: the step's two (a change to `.surf-scoter.d` after the run read it doesn't reach the build, and one before it stops the run; an expired acceptance asks for a review), no acceptance, an `accepted.json` outside the store, a parent inside the project, `images` rewriting only the copy, and the user's own container.  `rewrite_images` on flags, stages, local tags, a mapping to itself, and continued lines.  In `universal.rs`: accepted settings using the user's container (with its parent the user's, not the project's same-named one), the project's, and a name only found in the project refused.  In `test_snapshot.py`: the restored modes.  In `test_scoter.py`: `verify` loads without the acceptance.  By hand: `scoter run` on a project with no acceptance stops with the message.  A full run wasn't tried: the scratch setup's state directory is the real one in `~/.config`.

## 17. Add diff verify to main_verify.py script

Add in the capability for the main_verify.py script to perform a diff if the directory changed.

Use the three stored versions (step 5) to show what the project changed separately from what the user changed, and show only those differences in the verification file.

### Draft: the three versions

A re-review compares, item by item:

1. **What the project suggested last time**: the earlier acceptance's `suggestion.json` (`project`, `dev-container`), and its snapshot's files (manifest and blobs).
2. **What the user accepted**: the earlier acceptance's `review.yaml`, as the user left it, in the project file's shape (`env` split, `$comment` and `$override` on their items).  `accepted.json` isn't enough: its commands carry the whole merged environment plan, and no notes.
3. **What the project suggests now**: the SUC's `current-project-setup`, `current-project-resolved` and `dev-container`, and the current snapshot.

Python has no YAML parser, so (2) comes through the generator: `gen-scoter-uc --previous-review FILE` parses the stored `review.yaml` with step 13's rules, but without the checks against the run (its `expires-at` has usually passed; it's the user's own file, from the store), and writes the SUC's `previous-review`: its data as written, in the project file's shape (`egress`, `env`, `commands`, `dev-container`, with each `$comment` and `$override`).  Nothing is accepted from it: it only seeds the new review.

### Draft: which acceptance is "last time"

The project's newest acceptance on the current branch (`Acceptance.branch`), or, without one, its newest of any branch, expired ones included: an expiry is the commonest reason to re-review, and the diff is most useful then.  A blanket acceptance has no snapshot, so it's skipped.  The header names it: "Compared with your acceptance 2026...-ab12 (branch main, expired 2026-10-01)."

### Draft: matching items

Egress rules match by `host`, `protocol` and `port`.  A top-level `env` entry matches by variable name, in each of its lists.  Commands match by `command` name when the name appears once on each side; otherwise only an identical entry matches, and the rest count as removed and added.  (Commands with several allowed forms are the case where "changed" can't be told from "removed one, added another", and reading them as removed and added is the safe reading.)

### Draft: what each item becomes

| Project last time → now | The user had | The new review's data | Marked |
|---|---|---|---|
| unchanged | kept it as suggested | as before | one line: "unchanged since you accepted it" |
| unchanged | edited it | the user's version, with its `$override` and `$comment` | "your change, kept" |
| unchanged | removed it | left out, the suggestion written commented out | "you removed this before" |
| changed | (anything) | the project's new version | "the project changed this", with what changed, and the user's earlier version commented out beside it; its `$override` is dropped (step 12: an override covers what the user saw), its `$comment` kept, "on the earlier version" |
| added | — | the project's new version, with its findings | "new: the project added this" |
| removed | (anything) | left out, the user's version written commented out | "the project removed this" |
| — | added it | the user's version | "added by you" |

Findings are worked out afresh for every item, and every refusal is shown in full whether the item changed or not, since a setup change can refuse what was accepted before.  Unchanged items keep their data but drop their other `#scoter:` detail to the one line, so the file shows only what changed; the summary at the top counts only the changes and the refusals.

### Draft: the files

Each container file in both snapshots that differs is shown as a unified diff (`difflib`, from the stored blob to the current file), cleaned as every comment is, for text files under a size limit; others show their old and new sizes and hashes.  A new file is listed as now (Dockerfiles in full); a removed one says so; an unchanged one is one line.

### Draft: the setup

The acceptance's setup values (step 5) are compared with the current ones, and each that changed is listed in the header ("`host-bridge:shells` changed since you accepted it"), since a setup change alone also asks for a review.

### Design decisions

* Unchanged items stay in the file as data, collapsed to one line each.
* An item the project changed after the user edited it shows the project's setting, with the user's edit commented out beside it.
* The base includes expired acceptances.  So expiring doesn't remove an acceptance: saving no longer prunes (step 5's `verified-keep`); cleaning up is step 21's, which may also clean up projects whose directory is gone.
* Carrying forward on meaning stays automatic, with its notice: asking would bother the user about noise.

Completed:

* **The base** ([`verified/previous.py`](../../scripts/surf_scoter_lib/verified/previous.py)): the project's newest acceptance on the current branch, or else its newest, expired ones included, with a snapshot and a stored suggestion.  Its files are read from the blobs, and its setup values compared with the current ones (`setup_changes`, listed in the header).
* **What the user accepted:** `gen-scoter-uc --previous-review FILE` parses the stored `review.yaml` with step 13's rules, without the checks against the run, and gives the SUC's `previous-review`: `file`, and `data` as written (`Review::as_written`).  If it can't be read, the review is a full one, and says why.
* **What the project suggested:** each acceptance now stores the suggestion as the review wrote it (`review.suggested`) in `suggestion.json`'s `review`, so the comparison is between like and like (resolved programs and `env`).
* **Comparing** ([`review.py`](../../scripts/surf_scoter_lib/review.py)): `compare` matches items (egress by host, protocol and port; commands by name when it's on each side once, else only identical ones), and `show` writes each as the design's table says.
  * Unchanged items keep the user's data (with `$comment` and `$override`) on one line.  If the setup now refuses one, the refusal is shown in full; an override covering it keeps it, and otherwise it's commented out.
  * An item that changed shows the project's setting and the differences, with the user's earlier edit commented out beside it; its `$override` is dropped and its `$comment` kept, marked.
  * New items get their full findings; removed ones are offered back commented out; the user's own additions are kept, marked.  A refused item the user hadn't taken is shown as the project's, refused, not as "removed by you".
  * The top-level `env` is one item, compared the same way.  The file's own `$comment` is carried, and the dev container's `use` and `images` choices too.
  * Container files: unchanged ones are one line; changed text files show a unified diff (up to 200 lines, for files up to 256 KB); new and removed ones are marked.
  * The header names the base, and the summary counts what changed since.
* **No more pruning on save:** `save_environment` only writes; `prune_environment` keeps the old pruning for step 21's cleanup.  `verified-keep` is for that cleanup.

**Tests:** in [`test_main_verify_diff.py`](../../scripts/tests/test_main_verify_diff.py), with the generator and acceptances made through the prompt: a first review is full; a re-review after the user's edits (a note, a narrower argument, an override) and the project's changes (a new rule, a changed command, a changed Dockerfile) shows each as it should, and can be used as it stands; nothing changed keeps the user's edits; a removed item is offered back with its note; the base prefers the branch and takes an expired acceptance; the suggestion is stored.  In [`test_review.py`](../../scripts/tests/s_lib/test_review.py): `compare` for each kind and the command matching, `differences`, `setup_changes`.  In `universal.rs`: `--previous-review` reads an expired review for another directory, keeping its notes, and still refuses a broken one.  By hand: accepted a scratch project, changed it, and `scoter verify --output` showed the unchanged rule on one line, the new rule, the changed `ls` and the Dockerfile's diff.

## 18. Wrap up previous steps

Things left dangling because of dependency ordering, grouped into batches by what they touch.  Some may end up as future tasks.

### Closed

Done by later steps, or only notes:

- ~~Step 4: the checks are only validated and combined; `host-brokered` finds each `program` on the `PATH` rather than through `command-map`.~~  Steps 11 and 14 apply the checks, the review resolves programs through `command-map`, and `host-brokered` comes only from the accepted settings (steps 8 and 15).
- ~~Step 4: the project files (secret-looking files) and egress host lists, for steps 11 and 9.~~  Step 11's `check_project_files` and `egress-data-out-hosts`; making them check kinds is a future task (below).
- ~~Step 7: the review freezes `${section:key}` values at acceptance, and records the `@NAME@` names each command uses.~~  The review writes each `${...}` resolved (step 14), and keeps `@NAME@` as written; a project that starts using a new name changes its files, which asks for a new review.
- ~~Step 9: accepted settings with `"gateway": null` are accepted by any gateway.~~  A note: step 13 always writes the gateway.
- ~~Step 15: `scoter verify` snapshots the files after the generator parsed them.~~  Step 16's check that the staged copy parses the same catches a change in between.
- ~~Step 17: acceptances made before step 17 have no stored suggestion.~~  A note: they get a full review.
- ~~Step 2 and step 11: the `./` path resolution on program files is a check the user runs, not one the tools enforce, and step 11's path checks (relative `PATH` entries, directories others can write to, version manager shims) cover only the project's commands.~~  Done in batch B: `setup_check.program_findings` gives step 2's checks (`check_programs`: `./` values, project locations, programs found in the project) as refusals, and a relative `PATH` entry as a warning (the tools skip those); then, for each program as found, step 11's owner and mode checks (`checks.writable_by_others`), and the `host-commands` checks that match by `paths` (`checks.path_findings`: the version manager shims; checks by name or option are for the project's commands, so they don't apply, and nor do overrides).  `scoter user-setup` reports them with their severity; `scoter run` checks them first, and won't start on a refusal.  **Tests:** in `test_setup_check.py`: refusals and the `PATH` warning, programs anyone or the group can write to, a shim (and a check by name not applying).
- ~~Step 5: `active_env.py`'s lock checks and then writes, which two runs can interleave, and a crashed run leaves it held.~~  Done in batch C: each change to the running environments file is made in `ActiveEnvironmentSet.locked`, under `fileutil.exclusive_lock`, held only while the file is read, changed and written back (on an error in the block, nothing is written).  `scoter run` makes three short changes instead of holding a lock while it builds: choosing the port, writing the ssh configuration and reserving the host name together (a reservation now records the port, user, project and process, so another run choosing its port sees it); recording the environment once it's set up (an error if `scoter kill` removed it meanwhile); and removing it at teardown.  A run that fails before it starts its containers gives up its reservation.  A read-only lock file an earlier version left behind is made writable again.  **Tests:** in `test_active_env.py`: reserving and updating, an update after removal, an error changing nothing, a held lock timing out and then being free, an old lock file.
- ~~Step 8: the running environments file (`running-env.json`) keeps a copy of the whole SUC for teardown, but isn't written private.~~  Done in batch C: it's written with `fileutil.write_private` (0600, its directory 0700), and read without following a link.  **Tests:** in `test_active_env.py`: the modes, and a link refused.
- ~~Step 8: `scoter kill` isn't implemented.~~  Done in batch C ([`main_kill.py`](../../scripts/main_kill.py)): it stops the environment `--host` names (from anywhere), or the project's one (listing them when there's none, or more than one), with the configuration it ran with (its record's SUC): `compose-down`; then `--fetch BRANCH` runs the VCS's `fetch-changes-into` from the workspace into the repository; then `--force-clean` removes the workspace, refusing one that is, or holds, the project directory, the repository or the home directory.  A reservation (a run that crashed before starting containers) is only forgotten.  If the containers won't stop, or the fetch fails, it stays listed.  Last, its entry and `${scoter:state-dir}/environments/HOST` go.  `scoter` loads it without the acceptance lookup.  **Tests:** in `test_main_kill.py`: the project's environment, by host from outside a project, choosing (unknown, none, two), a reservation, `compose-down` failing, fetching and cleaning, and a workspace that holds the project.
- ~~Step 16 and step 9: a full `scoter run` against the staged copy hasn't been tried end to end, and no test runs the gateway containers.~~  Done in batch C: [`integration-tests`](../../integration-tests/README.md), run by hand (not from the `Makefile`), each test in a sandbox with its own `HOME`, from this checkout's debug build and scripts, with shared functions in `common/lib.sh`.  `01-verify-and-revoke` and `02-re-review` need no containers; `03-run-and-kill` runs `user-setup`, a real `scoter run` (the images built, the workspace cloned, the containers started), checks the gateway lets out the accepted host and not another, that an ssh session finds the host command's bridge first, and that `scoter kill` cleans up after the run is killed as a crash would.  All three pass.  Found and fixed on the way, mostly on a home that's never run the tools:
  * `scoter user-setup` didn't make `~/.ssh` before `ssh-keygen`; with `--non-interactive` it didn't create the empty ssh config its prompt defaults to (`[Yn]`, but the code's default was no), so the run couldn't start; and the known hosts prompt took only `y`, not `yes`.  **Tests:** `test_main_user_setup.py`.
  * The running environments lock failed when the state directory didn't exist yet; it's made, private.  **Tests:** `test_active_env.py`.
  * `dev-container: sshd-port: null` (the distribution's, chosen at run time) failed `UserConfig.dev_container_sshd_port`, which also refused a number (the run sets it as one); `require_value` refused numbers too (`start-sshd-port`, `cpus`, `pids-limit`).  **Tests:** `test_user_config.py`.
  * Step 16's check of the staged copy compared the `sshd-port` the run had just chosen with the files' `null`, so every run with a port chosen at run time stopped with "the project's files changed".  **Tests:** `test_main_run_staging.py`.
  * The dev-env container's entrypoint hard-linked the bridge client into `/usr/local/bin`, which its user can't write, so any project with host commands exited at start.  The links now go in `/usr/local/scoter-bridge` (the container user's, first on the image's `PATH`, and on ssh sessions' through `SetEnv`), and are symbolic: the kernel refuses a hard link to a file the user doesn't own, and the client takes its name from `argv[0]`, which a symbolic link keeps.  The entrypoint skips names that are blank or paths.
  * `scoter run` found `scoter-host` through `args.bin_dir` and `args.distribution_dir`, which are `scoter.py`'s options, not the sub-command's; it's the setup's `distribution-bin-dir` now.  And only Ctrl-C reached the teardown: any other error after the containers started left them running.  Now any error tears them down, and the run fails.
- ~~Step 15: the whole review is printed before the prompt; with long Dockerfiles it scrolls away.~~  Done in batch D: `scoter verify` shows the review through `PAGER` when it's set and the output is a terminal (split as `EDITOR` is, started in the review's private directory), or prints it; `[v]iew` shows it again, as it is after any edit.  **Tests:** in `test_main_verify.py`: the pager on a terminal and printing without one, the command split, `view` showing it again.
- ~~Step 4: the setup's notices only show in `scoter user-setup`.~~  Done in batch D: the review's header lists them (`setup_check.host_command_report`: the generator's notices about checks removed or replaced, and each override in force), or says the setup changes none of the checks; `scoter verify --skip-setup-review` leaves them out.  **Tests:** in `test_review.py`, and in `integration-tests/01-verify-and-revoke`.
- ~~Step 16: without an acceptance, the run doesn't say why.~~  Done in batch D: `lookup.no_match_reason` (read only, unlike `find`, which carries acceptances forward) says the project was never accepted; or the acceptance of these files expired (when); or the setup changed since (which values, `records.setup_changes`, moved there from `previous.py`); or the files changed since the newest acceptance (which files).  `scoter run` prints it before "Run `scoter verify`".  **Tests:** in `test_lookup.py`, and in `integration-tests/02-re-review`.
- ~~Step 17: the top-level `env` is compared as one item.~~  Done in batch D: `review.merge_env` merges it by entry (each `allow`, `drop` and `error` name, each `set` and `default` variable): one the project didn't change keeps the user's value, or their removal; one it changed takes the project's, with the user's earlier value commented out below when they'd edited it; new ones are added, removed ones dropped and noted.  **Tests:** in `test_review.py`.
- ~~Step 17: a `${...}` that resolves differently on the host shows as "changed since", as a project change does.~~  Done in batch D: the earlier acceptance's project file (`suggestion.json`'s `project`, now on `Previous.project`) tells them apart: an `env` value whose `${...}` didn't change says "your setup now gives X (was Y), from `${...}`"; a command whose only change is its `program` says it's "found somewhere else since you accepted it (the project didn't change it)".  **Tests:** in `test_review.py`.
- ~~Step 17: a `$comment` on an item gone from both the project and the user's acceptance is lost.~~  Done in batch D: the review where the project removes the item offers it back commented out, with its note, and now also lists the note in the header, saying it goes with the item unless the user keeps it; accepting without it is then the user's choice, made in view of the note.  **Tests:** in `test_main_verify_diff.py`.
- ~~Step 16: containers in `dev-containers-verified` build from their live directories, leaving a short window after the lookup checked their manifests.~~  Done in batch E: an acceptance now keeps them as it keeps the project's files: `containers.json` beside `accepted.json` (each directory's manifest, `lookup.verified_container_snapshots`), with their contents in the store.  The run's staging (`staging.stage_containers`) restores each one the build uses and builds from that; an acceptance made before they were kept asks for a new review.  **Tests:** in `test_main_run_staging.py`: a verified parent changed after the acceptance builds as accepted, and an acceptance without `containers.json`.
- ~~Step 16: `images` only applies to the project's container; with `use: {name}`, the user's container builds as it is.~~  Done in batch E: the user's own container is trusted, as their setup is, and still builds from where it is, unless the acceptance's `images` change a `FROM`: then from a copy in the staging directory, which they're applied to (the user's own is left as it is).  `images` now rewrites every staged Dockerfile.  Its parents, found by name outside the project, build as they are.  **Tests:** in `test_main_run_staging.py`.
- ~~Step 16: a `FROM` naming an image through an `ARG` isn't matched.~~  Done in batch E: `images` can't map it, as the image isn't known until the build, so the review notes each one.  **Tests:** in `test_review.py`.
- ~~Step 8 and step 11: blanket acceptances.~~  Folded into step 21's timed exemption.
- ~~Step 15: `--accept-file`.~~  For automation, out of scope for now: a future task, with its draft.
- ~~Step 3: the VCS commands (`local-clone`, `fetch-changes-into`, `merge-changes` and so on) run with the environment `scoter` was started with, which a project's `.envrc` could set (`GIT_DIR`, `GIT_WORK_TREE`, `GIT_CONFIG_*`).~~  Done in batch B: any section's `value-lists: drop-env` names the variables its `runs` commands don't get (a name, or a prefix ending in `*`; case-insensitive on Windows).  `UserConfig.run_command` (the run's clone, fetch and merge), `scoter verify`'s `current-branch` and `scoter.py`'s `find-root` use it.  The distribution's `git-vcs` drops `GIT_*`; [the system configuration file](../system-config-file.md#value-lists) says how to add to it or drop less, and why `docker` has none (a future task, below).  **Tests:** in `test_user_config.py`: `drop_env`, and `run_command` without a dropped variable, another section's command with it.  In `test_scoter.py`: `find-root` without `GIT_DIR`.

## 19. Review the settings format

The settings files have become important for declaring how to lock down an environment.  Because of that, it must present the information in such a way that the users can get a clear idea of what it's doing.  The more complex the file, the harder time users have understanding it, and the higher chance for mistakes.

We should review the settings files and the formats and look for opportunities to make them clear and reduce the chances for mistakes.  We should also make it extremely obvious if the user follows malicous online instructions to remove important restrictions.

The user-setup sub-command can provide capabilities for exploring and disecting the setup.  It should help to identify weaknesses without overwhealming the user.

Adjustments:

- `network-gateway`: The contents should move into the `dev-env` section, as it relates to the gateway image and docker-compose file.
- `verified`: this isn't a user adjustable command, but a helper internal tool.  It should stay in the setup file to reduce hard-coded values in the `scoter.py` script, but where it sits makes it look like a user-adjustable program.  This should belong in the `scoter:` section instead.
- `scoter: value-lists:` entries `secret-file-patterns`, `egress-data-out-hosts`, and `egress-refused-wildcards` with the `verified-*` values, plus the `host-bridge` section: these belong together as part of the verification values.  See below for the note on `egress-refused-wildcards` and the public suffix list, but that's out of scope for this step.
- Consider `workspace`: it's fine where it is, but it may better belong with the dev-env values.  While here, it would be nice to add in a `scoter: values: project-id` (unique per project directory, not verification id) so the user can use this for the `workspace: paths: workspace-parent-dir` value for a path outside the project directory, which would handle a set of red team issues.  Also adding in a `scoter: values: project-branch` (comes from the VCS discovery) would also be nice, so a user could have a workspace per branch if needed.

### Review of the adjustments, and more suggestions

On the notes above:

* **`network-gateway` into `dev-env`.**  Agreed.  `dev-env` already names the gateway's container directory (`paths: network-gateway`) and image (`values: network-gateway-image`); the rest follows the same prefix: `values: network-gateway-name` (was `network-gateway: values: name`), `network-gateway-config-mount`, and `runs: network-gateway-write-config`.  The gateway name is also a setup value an acceptance is reviewed under (step 5's `gateway`), and the Rust side reads it (`accepted.rs`'s `GATEWAY_SECTION` and `GATEWAY_NAME`), so both change together.
* **`verified` into `scoter`.**  Agreed: `scoter: runs: find-acceptance`.  The same holds for every `runs` entry that runs one of the distribution's own scripts and that a user has no reason to change: `find-acceptance`, and the gateway's `write-config`.  The VCS sections' `find-root` and `current-branch` are the exception: they're per VCS, and a user adding a VCS writes their own.
* **One section for the review's values.**  Agreed.  I'd name it `verification` (the sub-command is `verify`, and the store `verified-dir`), holding:
  * `host-commands`: the checks now in `host-bridge`, so their ids become `verification:shells` and so on;
  * `value-lists`: `secret-file-patterns`, `egress-data-out-hosts`, `egress-refused-wildcards`;
  * `values`: `expiry-days`, `keep`, `max-file-size`, `max-snapshot-size` (the `verified-` prefix goes, as the section says it);
  * `paths: store` (was `scoter: paths: verified-dir`), and `path-lists: verified-containers` (was `scoter: path-lists: dev-containers-verified`), as both decide what a review covers.
* **`workspace`.**  I'd keep it a section of its own: it's where the run puts the project's files, which is what a user changes most, and folding it into `dev-env` would put the most edited values among the least edited.  Its default should move out of the project (FILE-9; the comment there still says "See ticket" with no number): `workspace-parent-dir: ${scoter:state-dir}/workspaces/${scoter:project-id}`.
* **`${scoter:project-id}`.**  The step 5 environment id is already unique per project directory (its name and a hash of its canonical path), and is what names the project's directory in the verified store; it isn't an acceptance's id.  So I'd make `project-id` that value, worked out by the generator, and have the Python store read it from the SUC rather than work it out again: one source, which is what the "Naming by the environment id" future task asks for (it can then fold into this step).
* **`${scoter:project-branch}`.**  It comes from the VCS section's `current-branch`, which `scoter.py` would run before the generator's second run and pass in (`--project-branch`), since the setup's paths are worked out there.  SECURITY: a branch name is the project's to choose (`../../.ssh`, or one with a newline), so as a path part it must be made safe: one name, with `/` and anything outside a small set of characters replaced (`feature/x` becomes `feature-x`), `.` and `..` refused, and a length limit.  Without a branch (a detached checkout, or a VCS with none), it's empty, so a path using it stays the project's.

More suggestions:

* **All the checks in one place.**  `docker` and `git-vcs` also have `host-commands` (the `docker` and `git` checks, with `command-map: ${exec}`).  Auditing the restrictions means reading every section.  Moving them into `verification` (with `command-map: {docker: ${docker:exec}}`) puts every restriction in one list, which is what someone checking for a weakened setup reads.
* **One way to run the distribution's scripts.**  `${python:exec} -E -s ${scoter:distribution-bin-dir}/X.py` is written out five times; a copy that drops `-E` lets a project's `PYTHONPATH` in.  A `python: runs: script: [${exec}, -E, -s]`, used as `[${python:script}, ${scoter:distribution-bin-dir}/X.py, ...]` (a whole-argument reference to a list puts its items in place), keeps the flags in one place.
* **The tools' own state files** (`dev-env: paths: running-env-config` and `-lock`) aren't anything to adjust either: `scoter: paths: running-envs` and its lock, beside `state-dir`, or fixed names inside `state-dir`.
* **The tool-set `scoter` locations.**  `scoter: paths` mixes what the user sets (`verified-dir`, `state-dir`) with what the generator sets (`project-dir`, `vcs-root`, ...; only in a comment).  With `verified-dir` moving to `verification`, `scoter: paths` keeps `state-dir` and the tool-set ones; the comment should say plainly which is which.
* **Making a weakened setup obvious.**  `scoter user-setup` (and now the review) reports checks a setup removes, replaces or overrides.  A setup can also weaken things outside the checks: `python: runs: sub-command` without `-E`; `git-vcs: runs: local-clone` without `--no-local` (FILE-2); a shorter `drop-env`; a project location in `dev-containers-search`; a smaller `egress-refused-wildcards`.  The defaults mark these with `SECURITY:` comments, which a user never sees.  Suggest a `verification: value-lists: guarded` list of `section:kind:key` names in the defaults, and `user-setup` (and the review's setup notes) reporting each that a user's setup file changes, with the default beside it.  Lists that only add (more `secret-file-patterns`) needn't be reported.
* **Words.**  The code and docs say "verified", "accepted" and "review" for overlapping things.  With the new section: *verification* for the setup's policy, *review* for the file, *acceptance* for what's stored.  The rename is a good moment to settle it in the docs too.

### Plan

Breaking changes are fine (no release yet), so no migration of setup files, acceptances or stored state: an acceptance made before is simply asked for again.

1. **The new layout in `defaults/setup.yaml`**, as above: `verification` (checks, lists, values, store, verified containers), `dev-env` taking the gateway, `scoter` taking `find-acceptance` and the state files, the checks from `docker` and `git-vcs` moved into `verification`, and `python: runs: script` used by every distribution script.
2. **The code**, by name: the Python accessors' section and key names (`checks.py`, `review.py`, `verified/*.py`, `main_run.py`, `main_kill.py`, `scoter.py`'s `find_acceptance`); the Rust constants (`config.rs`'s `EGRESS_REFUSED_WILDCARDS` section, `accepted.rs`'s gateway, `system.rs`'s `verified-dir` checks); the step 5 setup values (`records.setup_values`) with the new names; and the tests, whose fixtures write these names.
3. **`project-id` and `project-branch`**: the generator works out `project-id` (step 5's algorithm, as Rust) as a tool-set `scoter` location; `store.environment_id` reads it from the SUC; `scoter.py` runs `current-branch` before the second generator run and passes `--project-branch`, which the generator makes safe as a path part.  Then the workspace default moves to `${scoter:state-dir}/workspaces/${scoter:project-id}`, and the container names can follow (the "Naming by the environment id" future task).
4. **Guarded settings** in `user-setup` and the review's setup notes.
5. **The docs**: [the system configuration file](../system-config-file.md) (each section), [the universal configuration file](universal-configuration-file.md), the security docs that name `host-bridge:` checks, and the integration tests' expectations; then a run of all three integration tests.

### Design decisions

* The section is `verification`.
* Every `host-commands` check moves into it, `docker`'s too, so none is missed by being elsewhere.  `git` has no check of its own: the `git-vcs` section's `git` and `git-options` go, as `verification:version-manager` already covers `git` (having both is the confusion splitting them leads to).
* `workspace` stays its own section.  Its default moves out of the project, using `${scoter:project-id}`; `${scoter:project-branch}` is there for a user who wants a workspace per branch, but the default doesn't use it.
* The generator works out `project-id`.
* The guarded settings report is a step of its own (step 20).

### Completed

* **The layout** of [`defaults/setup.yaml`](../../defaults/setup.yaml):
  * `verification`: every `host-commands` check (the old `host-bridge` ones, ids now `verification:NAME`, and `docker`'s, with `command-map: {docker: ${docker:exec}}`); `paths: store` (was `scoter: paths: verified-dir`); `path-lists: verified-containers` (was `scoter: path-lists: dev-containers-verified`); `value-lists: secret-file-patterns`, `egress-data-out-hosts`, `egress-refused-wildcards` (were `scoter`'s); `values: expiry-days`, `keep`, `max-file-size`, `max-snapshot-size` (were `scoter: values: verified-*`).  `git-vcs`'s `git` and `git-options` checks are gone: `verification:version-manager` covers `git`.
  * `dev-env`: `values: network-gateway-name` and `network-gateway-config-mount`, `runs: network-gateway-write-config` (were the `network-gateway` section).
  * `scoter`: `runs: find-acceptance` (was the `verified` section); `paths: running-envs` and `running-envs-lock` (were `dev-env: paths: running-env-config` and its lock).
  * `python: runs: script` (`${exec} -E -s`): every distribution script's `runs`, `sub-command` included, starts with it.
  * `workspace: paths: workspace-parent-dir` defaults to `${scoter:state-dir}/workspaces/${scoter:project-id}`, outside the project (FILE-9 is mitigated by default).  `scoter run` makes it private, and adds the work directories to the project's ignore file only when they're inside the project.
* **The code**: the Rust constants (`system.rs`'s `VERIFICATION_SECTION` and `VERIFIED_STORE`, which the "outside the project" check now takes with `scoter:state-dir`; `accepted.rs`'s gateway; `config.rs`'s refused wildcards), the Python accessors (`checks`, `review`, `verified/*`, `main_run`, `main_kill`, `scoter.py`), and `records.setup_values`' names (`store`, `verified-container-dirs`; the second avoids a clash with the containers' ids, which ruff caught).
* **`${scoter:project-id}`**: the generator works it out (`system::environment_id`, the step 5 algorithm in Rust, with the `sha2` crate), as a tool-only `scoter` value, also in `active-config-locations`.  The scripts' store takes it from there (`store.project_id`), falling back to its own `environment_id` for a file without one.  Both have the same known values in their tests, so they can't drift apart.
* **`${scoter:project-branch}`**: `scoter.py` runs the VCS section's `current-branch` before the generator's second run and gives it to `gen-scoter-uc --project-branch`; the generator makes it safe as one path part (`system::path_part`: letters, digits, `.`, `_` and `-`, each run of anything else made one `-`, ends trimmed of `.` and `-`, at most 64), and carries it over from `--universal` while the project directory stays the same (the review's and the run's later generator runs give it again).  Not set without one.
* **The docs**: [the system configuration file](../system-config-file.md) (the sections, the `scoter:` locations, the checks' examples, the store), [the universal configuration file](universal-configuration-file.md), and the security docs (`files.md`, FILE-9, `settings.md`, `network.md`).

**Tests:** in Rust: the new layout's fixtures throughout; the store's "outside the project" check under its new name; `environment_id` against the Python implementation's values; `path_part` on hostile branch names (`../../.ssh`, `..`, a newline, `${...}`); `project-id` and `project-branch` in the universal file, carried over by `--universal` with and without the same project, and not settable by a setup file; `--project-branch` parsed.  In Python: the fixtures throughout; `store.project_id`; the same id values; `scoter.vcs_current_branch`.  All three integration tests pass, `03` with the workspace outside the project.

## 20. Report guarded settings that a setup weakens

From step 19: a setup can weaken the tools' protections outside the `host-commands` checks, which `scoter user-setup` already reports when removed, replaced or overridden.  For example: `python: runs: script` without `-E`; `git-vcs: runs: local-clone` without `--no-local` (FILE-2); a shorter `drop-env`; a project location in `dev-containers-search`; a smaller `egress-refused-wildcards`.  The defaults mark these with `SECURITY:` comments, which a user never sees.

Suggested: a `verification: value-lists: guarded` list of `section:kind:key` names in the defaults, and `user-setup` (and the review's setup notes, step 18) reporting each that a user's setup file changes, with the default beside it.  Lists that only add (more `secret-file-patterns`) needn't be reported.  This is what makes it obvious when a user follows instructions that remove restrictions.

### Completed

* **The guards**, in the defaults' `verification` section: `value-lists: guarded` (any change reported: how the distribution's scripts run, the workspace's clone and the VCS's root and branch scripts, `dev-containers-search`, where the store, the state and the workspace are, the gateway and its configuration, the compose file and network containers, the container limits, `ssh: runs: proxy-command`, `docker: runs: broker`, and every `project-files` and `egress-hosts` check) and `value-lists: guarded-removals` (deny-lists, reported only for a default entry left out: `drop-env`, `egress-refused-wildcards`, `public-suffix-lists`, `verified-containers`, and the two guard lists).  Entries are `section:kind:key`, `*` for every key of a kind.
* **The report** ([`setup_check.py`](../../scripts/surf_scoter_lib/setup_check.py)): `guarded_changes` compares each user setup file's settings, as written (the universal file's `setup.files`), with the distribution's, for each guarded key the user's file sets: a change, a removal (`null`), or, for `guarded-removals`, the default's entries left out.  A `*` guard covers the keys the distribution sets, so a user's own check (an addition) isn't reported, but `simple_check_overrides` lists every `project-files` and `egress-hosts` override in effect.  The guards used are the distribution's and the user's additions: a user file can't take one out, and one that tries is reported ("they still apply").  `setup_report` puts them with `host_command_report`.
* **Where it shows**: `scoter user-setup` (after the host command checks; it doesn't fail the setup), and the review's setup notes, which `--skip-setup-review` leaves out (worded now for all of it: "How your setup changes the defaults' protections").
* This also does what the simple checks' future task left: their replacements, removals and overrides are reported.

**Tests:** in `test_setup_check.py`: unchanged and default values not reported; changes to a run and a list; a default check replaced and removed, and a user's own not; a deny-list that adds and one that leaves an entry out; a guard list emptied (its guards still used, and the attempt reported); a user's own guard; no user file.  In `test_review.py` and `integration-tests/01-verify-and-revoke`: the review's wording.  By hand, on the real defaults with a user setup that drops `-E`, `--no-local`, `GIT_*` and the guard list, and adds an override: each is reported, the override once.

## 21. Allow a timed exemption for working on a project.

Allow a timed exemption for working on a project.

It covers blanket acceptances too (moved from step 18).  The kind exists in the records and the lookup (step 5), for a private project nobody else contributes to, but no command creates one, and `scoter.py` treats a match as none (steps 8 and 11).  Step 16 also changed what one would have to mean: the run builds only from a stored, accepted copy, and a blanket acceptance has none.  One way, drafted in step 18: "accept as suggested" on each run, recorded by a `scoter verify` option: each run takes a fresh snapshot and, if the checks refuse nothing, stores an acceptance of the suggestion as it stands (as using the review unedited would), and builds from that; refusals, setup changes and a gateway change still stop it.  A timed exemption ("don't ask me again for a week") covers much of the same need with less trust handed out.

### Draft: what an exemption is

The [configuration weaknesses](../security/configuration-weaknesses.md#authorization-for-a-project) design: "for a time period disable the checksum analysis.  This can be useful when the user actively works on making a running environment."  That's someone editing their project's Dockerfile, running it, editing again: each edit is a change to the files, and asks for a review.

So an exemption is a window, on one project, in which a run with changed files doesn't stop for a review.  It never builds from the live files (step 16): instead each run carries the user's last acceptance forward over the changed files, as a match on meaning does (step 5).  The new acceptance keeps the last one's accepted settings (`accepted.json`: the host commands, egress rules and container choices) and its expiry; only the files it's for are the current ones.  It's marked as made by the exemption, so the next review compares with the last acceptance the user reviewed, not one the exemption made.

### Draft: what it covers

1. **Container files only** (proposed).  Within the window, a change to the project's container directories (Dockerfiles, files they copy) is carried forward.  A change to the project file, or to a `dev-env.yaml` (its mounts and user are DEV-2), still asks for a review: those change what the accepted settings mean, and a pulled change to them is where another contributor's commands come in.
2. **Everything**: within the window, any change is carried forward, the project file's too.  But the accepted settings stay as they were (the host commands and egress rules aren't the project's new ones), so a changed project file would build with the old settings, which is confusing rather than unsafe.

Either way, the review's checks still run on the carried copy: a refusal (a symlink in a container directory, say) stops the run as it would a review.

### Draft: the command

`scoter exempt [--for DURATION | --end]`, in the project:

* Starts (or extends) the window from the project's newest usable acceptance; `--for` defaults to a day (`1d`, `8h`, `30m`), up to the setup's `verification: values: max-exempt-days` (default 7).  It can't outlast that acceptance's expiry.
* `--end` closes it now.  Revoking the acceptance (`scoter revoke`) closes it too.
* Without an acceptance there's nothing to exempt from: it says to run `scoter verify` first.

The window is recorded in the environment's record (`environment.json`), with the acceptance it carries from; each run in it says so ("exempt until 15:30: carried your acceptance forward over env/Dockerfile").  `scoter list-accepted` shows it.

### Draft: blanket acceptances

The exemption covers their use case for a while, with an end.  Proposed: remove the `blanket` kind from the records and the lookup, rather than leave a kind nothing creates; a permanent exemption is a future task, if it's wanted.

### Design decisions

* **Container files are used as they are.**  Within the window, each run copies the project's container files off as they are now (read as a snapshot is: no links, no special files), and builds from that copy; the review's file checks run on them.
* **The project file and the `dev-env.yaml` files keep their accepted versions.**  Their changes are ignored, and each run says which it's ignoring, until `scoter verify --with-exemption` picks them up.
* **`scoter verify --with-exemption`** accepts the project's suggestion as it stands, without the prompt (so automation can use it), only while an exemption holds for the branch, and only if the checks refuse nothing.  The exemption then carries from the new acceptance.  So the blanket acceptance's use case, mostly automation, is two steps: `scoter exempt`, then `scoter verify --with-exemption`.  The `blanket` kind goes.
* **One branch.**  An exemption holds for the branch it was started on, when the VCS gives one.
* **`scoter exempt`**, for `verification: values: exempt-days` (1) unless `--for` says, at most `max-exempt-days` (7), set in the defaults.

### Completed

* **The record** ([`records.py`](../../scripts/surf_scoter_lib/verified/records.py)): the environment's record (`environment.json`) has an `exemption`: the acceptance it uses, its end (`until`), its branch (none without a VCS branch) and when it started.  `find` tries an exact match, then one on meaning, then the acceptance of an exemption that holds for the run's branch (`scoter:project-branch`), which must be usable and under the same setup: a setup change still needs a review.  The `blanket` kind, the per-acceptance `exempt_until` and `Validity.EXEMPT` are gone; every acceptance has its suggestion.  A new kind, `exempted`, is an acceptance `verify --with-exemption` made.
* **One acceptance per branch** (after review): `EnvironmentRecord.add` replaces the branch's others (expired or not; `branch` None is one "branch"), and an exemption using a replaced one moves to the new one; `records.remove_replaced` removes their files and the blobs nothing uses, once the record is saved.  So a review, a carry-forward (which now replaces the acceptance it came from) and `verify --with-exemption` each leave the branch one acceptance, which the next review compares with, whatever its kind.  Expired acceptances on other branches stay.  Revoke's "carried ones go too" is gone, as nothing is left to follow.  `scoter exempt` starts from the branch's acceptance, else the project's newest.
* **`scoter exempt [--for LENGTH | --end]`** ([`main_exempt.py`](../../scripts/main_exempt.py), with [`verified/exempt.py`](../../scripts/surf_scoter_lib/verified/exempt.py)): starts (or moves the end of) the exemption from the newest usable acceptance, for `--for` (`1d`, `8h`, `30m`, `1d12h`) or `exempt-days`, at most `max-exempt-days`, and never past the acceptance's expiry; `--end` ends it.  `scoter` loads it without the accepted settings, as `verify` and `revoke`.  Revoking the exemption's acceptance ends it; `list-accepted` shows it under its project ("exempt until T (holds)  branch B  from ID").
* **The lookup** ([`lookup.py`](../../scripts/surf_scoter_lib/verified/lookup.py)): an exempt match gives the acceptance's `accepted.json` and a notice, which `scoter` prints for every command that loads the accepted settings: "Exempt on main until T: the run uses your container files as they are; it ignores your changes to env/dev-env.yaml, project.yaml until you run `scoter verify --with-exemption`."  The settings files are the project file and the `dev-env.yaml` files (`is_settings_file`).
* **The run** ([`staging.py`](../../scripts/surf_scoter_lib/verified/staging.py), [`main_run.py`](../../scripts/main_run.py)): when the run's acceptance is its exemption's, `adopt_accepted` first restores the accepted copy, runs the generator on it, and puts its `current-project-setup` and `dev-container` (with the parents' paths as the live project's) in the run's configuration, before the run reads the port, the user or the mounts: the live `dev-env.yaml` can't change them.  `stage` then lays the live files of the containers those settings name (read as a snapshot is: no links, no special files) over the accepted project file and `dev-env.yaml` files, and `prepare` runs the `project-files` checks on the live files (for what a copy can't show) and on the copy; a refusal stops the run.  `build_plan`'s comparison holds as before, since the run's view is now the copy's.  With `dev-container: use: {name}`, only the project file is adopted.  `snapshot.write_files` is `restore` with the contents from a function.
* **`scoter verify --with-exemption`** ([`main_verify.py`](../../scripts/main_verify.py)): needs an exemption that holds for the branch; builds the review as `scoter verify --output` would, without the earlier acceptance's edits, and accepts it unseen only if the suggestion's checks refuse nothing and the review's checks pass.  `accept.store_acceptance(exemption=...)` stores it as `exempted` and moves the exemption to it (same end and branch) under the store's lock, or stores nothing if the exemption changed meanwhile.  It can't be given with `--output`.
* **Defaults**: `verification: values: exempt-days: 1` and `max-exempt-days: 7`; `max-exempt-days` is guarded (`exempt-days` is bounded by it).

**Tests:** [`test_records.py`](../../scripts/tests/s_lib/verified/test_records.py): the exempt match (its branch, its end, the same setup, any branch for an exemption without one, a missing acceptance) and the record's JSON.  [`test_lookup.py`](../../scripts/tests/s_lib/verified/test_lookup.py): the exempt match and its notice, with the ignored `dev-env.yaml`, and another branch.  [`test_main_exempt.py`](../../scripts/tests/test_main_exempt.py): `parse_duration`; nothing to exempt from; the newest acceptance, the branch, the setup's lengths, the acceptance's expiry and the maximum; the branch's acceptance first; `--for` and `--end`.  One per branch: `test_records.py` (replacing expired and usable ones, the exemption moving, other branches kept), `test_accept.py` (the replaced files gone), `test_main_revoke.py` (a carry-forward replacing its original), and `integration-tests/02-re-review` and `04-exempt` (one acceptance after the second).  [`test_main_run_staging.py`](../../scripts/tests/test_main_run_staging.py): adopting the accepted settings, and staging the live Dockerfile and a new file with the accepted `dev-env.yaml`; no exemption; a hard link and a check's refusal stopping the run.  [`test_main_verify.py`](../../scripts/tests/test_main_verify.py): `--with-exemption` without an exemption, accepted and the exemption moved, and refused.  [`test_main_revoke.py`](../../scripts/tests/test_main_revoke.py): the listing, and revoking the exemption's acceptance (but not another) ending it.  [`integration-tests/04-exempt`](../../integration-tests/04-exempt/run.sh): all of it, with a stand-in container tool.

**Carried over:**

* An `exempted` acceptance accepts the suggestion as it stands: the user's edits to an earlier review (a removed egress rule, say) aren't kept.  Keeping them, as the re-review does (step 17), would need the earlier review's edits applied without a prompt.

~~Allow a user to revoke a project's acceptance.~~  Done, after step 16: `scoter revoke` ([`main_revoke.py`](../../scripts/main_revoke.py), with [`verified/revoke.py`](../../scripts/surf_scoter_lib/verified/revoke.py)).

Without arguments, it lists the project's acceptances: id, kind, branch, fingerprint (the snapshot id, first 12 characters), expiry and validity.

Positional arguments name acceptance ids (as `scoter list-accepted` shows them), for any project and from anywhere; or, for the current project, branches or fingerprints (the whole id, with or without `sha256:`, or its first 12 hex characters or more, in any case).  `--all` takes every one of the current project's, and can't be given with names.  An acceptance carried forward from a chosen one goes too, as it holds the same settings.

A name that chooses nothing revokes nothing, and shows the list, so a typo can't revoke half of what was meant.

Revoking removes the acceptances from `environment.json` (written first, so it never names a missing acceptance), then their directories, then the blobs nothing uses, all under the store's lock.  A project left with none is taken out of the index, then its directory removed.  The store now also removes blob prefix directories it empties.

`scoter` loads `revoke` without the accepted settings, as it does `verify`: the lookup could otherwise carry an acceptance forward just before it's revoked.

Run inside a project, it still needs a project file the generator can read; outside one, ids work for any project, including one whose directory is gone.

**Tests:** [`test_main_revoke.py`](../../scripts/tests/test_main_revoke.py): nothing accepted, the listing, by branch (several), by fingerprint (whole, short and upper case; too short refused), carried acceptances going too, an unmatched name revoking nothing, `--all` (and the blobs it frees), and `--all` with names.

## 22. Allow a user to list accepted projects, with their expiration and exemption.

Done: `scoter list-accepted` ([`main_list_accepted.py`](../../scripts/main_list_accepted.py)).

Every project in the store's index, by directory (noting one whose directory is gone), and under each its acceptances: id, kind, branch, fingerprint, expiry, validity, and what it was carried from, then its exemption, if it has one (step 21).  Then how many, and how much the blobs take.

`-q` shows only the acceptance ids, one per line, which `scoter revoke` takes: `scoter revoke $(scoter list-accepted -q)` revokes every acceptance, and empties the store.

It needs no project: `scoter` loads only the setup for it, as for `help`.

This needs user documentation in the `docs` directory for guidance on how to deal with old runs and cached files.  Part of this is covered under `new-project-setup.md`, but an end-user needs the information, too.

### Completed

* **User documentation**: [`docs/cleaning-up.md`](../cleaning-up.md), for end users: what Surf Scoter keeps (the running environments' records and configuration, each project's workspace directory, the verified store, the images, the ssh entries), where by default, and how to clear each: `scoter kill` (with `--host`, `--fetch`, `--force-clean`), `scoter run --clean-workspace` or deleting a workspace by hand, `scoter list-accepted` and `scoter revoke` (one acceptance per branch, expired ones kept), the container tool's `image` and `builder` commands, and the ssh entries.  It says there's no single cleanup command yet (step 23).  Linked from the user guide's index, the README's "Starting a Developer Environment", and `new-project-setup.md`.

**Tests:** in [`test_main_revoke.py`](../../scripts/tests/test_main_revoke.py): the listing of two projects, `-q`, an exemption and a gone directory, nothing accepted, revoking by id across projects from outside one, an unknown id, and the whole clean-out (index and blobs empty afterwards).  By hand, on the scratch distribution: two projects accepted through the prompt, listed, and revoked with `scoter revoke $(scoter list-accepted -q)` from outside both.

## 23. Add a user cache cleanup program

The `kill` and `revoke` allow for cleaning up parts of the system, but the user should also have a method to sweep up all the caches.  If these sub-commands already cover the bases, then ignore this step.

### Draft: what's left behind that nothing removes

`kill` and `revoke` work on one environment or acceptance the user names.  What neither finds (see [`docs/cleaning-up.md`](../cleaning-up.md)):

1. **Stale run records.**  An entry in `running-env.json` whose run's process (`pid`) is gone: a crash, a closed terminal, a reboot.  `kill` handles one, if the user knows its host.
2. **Orphaned state.**  `state/environments/HOST/` without a run record; `state/exempt-*` directories from a run that crashed while adopting (step 21).
3. **Workspaces of projects that are gone.**  `state/workspaces/PROJECT-ID/`, for a project directory that was moved or deleted.  Nothing records which project a workspace is for, so today the user has to guess from the name.  A workspace can hold work not yet fetched back.
4. **The verified store's leftovers.**  Projects whose directory is gone (`list-accepted` marks them; `revoke ID` removes them one id at a time), blobs nothing uses (normally removed as acceptances go, but not after a crash between the record and the blobs), and the store's own temporary files.
5. **Images.**  `local-surf-scoter/*` images, their replaced layers, and the build cache.  The tags are reused; what they replace isn't.
6. **SSH entries.**  `Host scoter-USER-PORT-*` blocks and `known_hosts` lines.  Harmless while unused.

### Draft: `scoter cleanup`

Needs no project (like `list-accepted`).  It lists everything it finds, by kind, with sizes, then removes it on confirmation:

* **Always offered**, as nothing in them can be lost: stale run records (1: their containers stopped through the recorded configuration, as `kill` does, the workspace kept), orphaned state (2), unused blobs and temporary files (4).
* **Gone projects** (4): their acceptances.
* **Opt-in**: `--workspaces` (every workspace not in use by a running environment: they can hold unfetched work), `--images` (the `local-surf-scoter/` images not used by a running environment, through `docker: runs:` commands the setup adds, then the container tool's prune).
* **Not touched**: expired acceptances (kept for the next review's comparison, step 17; `revoke` removes them), SSH entries (left to `user-setup`).
* `--dry-run` only lists; `--yes` skips the confirmation, for scripts.  Without a terminal and without `--yes`, it only lists.

`records.prune`, `prune_environment` and `verification: values: keep` (for expired acceptances beyond a count) have no caller now that each branch keeps one acceptance (step 21).  Proposed: remove them.

### Design decisions

* **Confirmation**: it lists what it found, then asks; `--dry-run` never asks, and removes nothing; `--force` never asks, and removes it all.  Without a terminal, and neither, it only lists.
* **Workspaces** only with `--workspaces`: every workspace no running environment uses.  No marker file: the run doesn't record a workspace's project.
* **Images** with `--images`, through new `docker: runs:` commands.
* **`prune` and `keep` go.**

### Completed

* **`scoter cleanup [--workspaces] [--images] [--dry-run | --force]`** ([`main_cleanup.py`](../../scripts/main_cleanup.py), with [`cleanup.py`](../../scripts/surf_scoter_lib/cleanup.py)).  `scoter` loads it without a project.  It finds everything, lists it by kind (with sizes), then asks `Remove these? [y/N]`; `--dry-run` only lists, `--force` doesn't ask, and without a terminal it only lists.  Each `find_*` only reads; each removal looks again under the lock that guards it (the running environments' lock for the state and the workspaces, the store's for its leftovers), and removes only what's still as it was found.
* **What it finds**: run records whose `scoter run` process is gone (`process_alive`: `os.kill(pid, 0)` on POSIX; on Windows, or with no `pid`, it can't tell, and leaves them), stopped through `main_kill.run_kill` after checking again that they're still stale; `state/environments/HOST` directories with no record, and `state/exempt-*` directories over an hour old; projects whose directory is gone (`revoke.revoke` of all their acceptances); the store's leftovers (environment directories the index doesn't name, acceptance directories a record doesn't, unused blobs, `.tmp-*` files: `VerifiedStore.temp_files`); with `--workspaces`, `state/workspaces/*` that no record's workspace, setup or resources directory is in, and that no starting run (a reservation's project id) is for; with `--images`, the `dev-env-image-prefix` images no record uses (its dev container, and the network's while any runs).
* **Defaults**: `docker: runs: list-images`, `remove-image` and `prune-images`.  `run_command` takes `quiet`, so the listing isn't mixed with "Running ..." lines.
* **Removed**: `records.prune`, `prune_environment`, and `verification: values: keep`.
* **Docs**: [`docs/cleaning-up.md`](../cleaning-up.md) has "The Cleanup Command", and each section says what it covers; the README mentions it.

**Tests:** [`test_main_cleanup.py`](../../scripts/tests/test_main_cleanup.py): `process_alive`; nothing to clean; a stale reservation stopped and a running one kept, orphaned state (and a fresh `exempt-*` kept), with `--dry-run` first; a gone project revoked and another kept; the store's leftovers found and removed, the record's acceptance kept; asking (no, and no terminal); workspaces only with the flag, keeping one in use and one starting; images only with the flag, keeping the running one's and the network's.  [`integration-tests/05-cleanup`](../../integration-tests/05-cleanup/run.sh): the real command, end to end, with a stand-in container tool.

**Carried over:**

* Workspaces outside `state/workspaces` (a setup that moves `workspace: paths: workspace-parent-dir`) aren't found.
* A run record from another computer sharing the home directory (an NFS home) looks stale here: its `pid` is checked on this one.
* SSH entries, and the container tool's build cache, are left to the user (the docs say how).

## 24. Clean up the code to follow good coding practices.

Follow the new [code style](code-style.md).

Reuse existing helper functions that make clear what's happening, even if it's just a one-liner.

- example: Python `sys.platform == "win32"` vs. `fileutils.is_windows()` (this follows from the "lower level mechanics" abstraction rule).
- example: use the `jdata` library for handling JSON-like lookups with type safety.

Clean up the code to remove references to steps from this document.

### Design decisions

* Booleans become enums for public APIs only: functions called from another module, the scripts' `run_*` functions, Rust `pub fn`s.  Private helpers keep their booleans; struct and dataclass fields aren't parameters.
* Every function name is under 30 characters, tests included; test names don't start with an article.
* The broker crate (`containers/dev-env/src`) follows the same rules.

### Completed

* **Step references** are gone from the code, comments and docstrings (about 52, in Python and `host/`; none in the broker, shell or YAML); where one carried meaning, the text says the thing itself.
* **Names**: every function is under 30 characters: in Python, `main_run._write_docker_compose_env_file` became `_write_compose_env_file` and 27 test methods were renamed; in `host/`, 110 tests; in the broker, 41 tests.  No Rust public function changed.  Test names no longer start with an article.
* **Enums for public booleans** (Python; no Rust public function took one):
  * `scoter.load_config(scope=ConfigScope.SETUP | SUGGESTED | ACCEPTED)`, with `config_scope(sub_cmd)`.
  * `UserConfig.run_command(on_error=OnError.RAISE | RETURN, capture=Capture.NONE | STDOUT | STDERR | BOTH, echo=Echo.SHOW | QUIET)`; `search_path(kind=PathKind.FILE | DIR)`.
  * `fileutil.restore(without_backup=WithoutBackup.KEEP | DELETE)`; `VerifiedStore.open(if_missing=IfMissing.REFUSE | CREATE)`; `revoke.select(pick=Pick.NAMED | ALL)`, which `main_revoke.run_revoke` takes too.
  * `main_kill.run_kill(workspace=Workspace.KEEP | REMOVE, ssh=SshEntries.RELEASE | KEEP)`; `main_cleanup.run_cleanup(extras=Extras.WORKSPACES | IMAGES, confirm=Confirm.ASK | DRY_RUN | FORCE)`; `main_exempt.run_exempt(action=Action.START | END)`; `main_list_accepted.run_list(listing=Listing.FULL | IDS)`; `main_user_setup`'s `Interaction.ASK | DEFAULTS`; `main_verify.run_verify(terminal=Terminal..., setup_notes=SetupNotes..., via=AcceptVia.REVIEW | EXEMPTION)` and `Platform.POSIX | WINDOWS` for the editor command.  The command-line flags are unchanged: each script converts them.
* **Helpers**: `fileutil.is_windows()` everywhere in the library and tests (`scoter.py`, which can't import the library, has its own `_is_windows`); new `fileutil.O_NONBLOCK` and `MODE_NOT_PRIVATE`, and `MODE_USER_RW`/`MODE_USER_RWX` in place of `0o600`/`0o700`; `argutil.can_ask()` and `records.now_utc()` reused.  `jdata` wasn't brought in: the hand-written lookups skip a value of the wrong type, which `jdata`'s `find_*` would raise on.
* **Constants**: about 30 in Python (sizes as `*_BYTES`, timeouts as `*_SECONDS`, the id and fingerprint lengths, exit codes, ports, modes), 14 in `host/` (among them `system.rs`'s `ID_NAME_MAX_CHARS`, `ID_HASH_BYTES` and `PATH_PART_MAX_CHARS`, which must match the Python store's; `read.rs`'s `MAX_CONFIG_FILE_BYTES` and `MAX_UNIVERSAL_FILE_BYTES`), and 6 in the broker (`STDIN_CHUNK_BYTES`, `CLIENT_CHUNK_BYTES`, `SOCKET_UMASK`, ...); the wire protocol's are unchanged.
* **Python rules**: every `noqa` and `nosec` says why (the two `SIM113` counters became `enumerate`); 15 data-driven test loops use `subTest`; missing type hints added (and `main_fetch.run_fetch`'s `branch` corrected to `str | None`); bare `except:` became `except BaseException:`.
* **Non-ASCII**: test data in both Rust crates uses `\u{...}` escapes (an egress test's comment named the wrong Cyrillic letter, and now says U+043E); [`project-configuration-file.md`](../project-configuration-file.md)'s example is an escape too.
* **Shell and config**: `${...}` for variables; quoted `$( dirname "$0" )`; a new `it_tty` in `integration-tests/common/lib.sh` gives the review prompt a terminal with util-linux's or BSD's `script`; `sed -i` out of `04-exempt`; the `Makefile` runs `mk-install.sh` with `sh`; comment fixes throughout (one said `ls` for `ln -s`, another named a script that doesn't exist); `defaults/setup.yaml` parses to the same values.  `03-run-and-kill` still needs `setsid`, which macOS lacks; it says so.
* **Docs**: the broker's `broker_host` docs said exit code 1 for a socket I/O error, where the broker sends 122; stale `run-container.sh` references in [`future-ideas.md`](future-ideas.md) and [`files-attack-vectors.md`](../security/files-attack-vectors.md); the renamed names in the security docs and this file.

**Tests:** both Rust crates' suites (`host/`: 207 + 7 + 111; the broker: 48 + 29 + 24), `cargo fmt --check` and clippy with no warnings; the 257 Python tests, ruff and bandit clean; integration tests 01, 02, 04 and 05.  `03` got as far as building the dev-env image, where this network's TLS interception refused crates.io again (the broker's sources changed, so the build couldn't use its cached layer); it needs a run on a network that reaches crates.io.

## 25. Clean up this file.

Remove these steps from this file, and move the design and reasoning made in the steps into a separate file to indicate what exists.
