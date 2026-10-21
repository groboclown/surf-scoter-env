# Scoter System Configuration File

Scoter allows you to set specific configurations for your particular computer in the system configuration files.


## Where to find the system configuration files

The tool reads both of these, when they exist; the first takes priority over the second.

* `${scoter:user-setup-file}` (using the internal setup to find the location; see below)
* `${scoter:distribution-dir}/setup.{yaml,yml}`: the files Surf Scoter itself installs (see below).

In order to find the `${scoter:user-setup-file}`, The system tries these locations, choosing the first one found (if any):

* `$SURF_SCOTER_HOME/setup.{yaml,yml}` (using the `SURF_SCOTER_HOME` environment variable, if set)
* `~/.config/surf-scoter.setup.{yaml,yml}`
* `~/.config/surf-scoter.d/setup.{yaml,yml}`

The distribution directory is where Surf Scoter's own files are installed, much like a Linux distribution installing them into `/usr/share/surf-scoter`.  An installation gives it to the tools with `--distribution-dir`.  Without that, the tool uses the setup files read so far: if they include

```yaml
scoter:
    paths:
        distribution-dir: /my/install/dir
```

then the tool reads `/my/install/dir/setup.{yaml,yml}`, with the lowest priority.  `--distribution-dir` wins over a `distribution-dir` in the setup files.  To use a different installation, give `--distribution-dir` yourself; the setting in a setup file only counts when the tools aren't given one.  Either way, only one distribution directory's file is read, and a file is only read once, even if it's found more than one way.

The tools never read a setup file from a project.  A setup file can run any program on your computer, decides what a project may do, and changes how the containers are put together, and a project can come from anyone.  So when there's a project, these must all be outside the project directory, the project files directory, the dev container's workspace directory, and the repository holding the project (the nearest directory above the project with a `.git` or `.hg`, not counting your home directory or above); otherwise the tools stop with an error, and make no configuration:

* `$SURF_SCOTER_HOME`, whether or not it holds a setup file.  A project can set environment variables when you enter its directory (say, with a `.envrc`), so the tools check where it points.
* The user's setup file, however it was found.
* A `distribution-dir` that comes from the setup files (rather than `--distribution-dir`).  It must also be an absolute path, and must not refer to `${scoter:cwd}` (directly or through another key), as the tools usually run in the project.

Note: it is an error (will cause the tools to bail out) if you have both a `setup.yaml` and a `setup.yml` (or `surf-scoter.setup.yaml` and `surf-scoter.setup.yml`) file in the same directory.

The `scoter` section is special: the tool fills in some of its values.  See [The `scoter:` section](#the-scoter-section).

## Format

Each top-level key is a *section*.  A section holds a mapping for each kind of value, all optional:

```yaml
docker:
    programs:
        exec: /usr/sbin/podman
    runs:
        build: ${exec} -t {x:tag} -f {x:dockerfile} {x:dirname}
    path-lists:
        compose:
            - ./.scoter/compose
            - ${scoter:user-dir}/compose
    values:
        retries: 3
ssh:
    paths:
        home: ~/.ssh
        config-file: ${ssh:home}/config
        key-file: ${home}/scoter-dev-container
```

* `paths`: file locations (see [File Locations](#file-locations)).
* `programs`: executables, found on the `PATH` (see [Programs](#programs)).
* `runs`: command patterns (see [Defining Executables](#defining-executables)).
* `path-lists`: lists of file locations (see [Defining Path Lists](#defining-path-lists)).
* `value-lists`: lists of plain text (see [Value Lists](#value-lists)).
* `values`: plain values (see [Plain Values](#plain-values)).
* `host-commands`: checks on the commands a project may run on your computer (see [Host Command Checks](#host-command-checks)).

Each key in a section has exactly one kind of value: setting the same key in two of the mappings (say, `paths: retries` and `values: retries`) is an error.  That holds across files too: the kind of value is part of what a key means, so a higher priority file can change a key's value, but not its kind.  You can think of a section as a mapping of keys, where each key has a value and a kind.

Sections from different files are merged key by key, with the higher priority file's value winning.

Each file may be at most 2 MB.  YAML tags (such as `!include`) aren't supported, and the tools report them as errors.  When the files have problems, the tools report all the problems they find, rather than just the first.

Inside the values, you can refer to any other key with the `${[section:]key}` format.  In the example above, `${ssh:home}` is the `ssh` section's `home` path, so `config-file` becomes `~/.ssh/config` (with `~` expanded to your home directory).  In the same way, the `key-file` key assumes the `${home}` reference refers to its own section, which expands into `~/.ssh/scoter-dev-container`.

What a reference gives depends on the kind of value it refers to:

* `paths`, `programs` and `values` are text, and can be used anywhere.  A `null` value can't be used.  A reference to a program gives the program found (see [Programs](#programs)).
* `host-commands` checks can't be referred to at all: `${verification:shells}` is an error.
* `runs`, `path-lists` and `value-lists` are lists.  A reference that is a whole argument of a `runs` command, or a whole entry of a `path-lists` or `value-lists` list, puts the list's items in its place; for example, `${exec} ${common-args} build`, or a list entry `${docker:compose}`.  Anywhere else, a reference to a list is an error.

Referring to a key that isn't set anywhere is an error (except for the `scoter` locations the tool sets; see [below](#the-scoter-section)), as is a set of values that refer to each other in a loop.  You can escape a `${` expression by doubling up the dollars - `$${` becomes `${`; you don't need to double-up a `$` unless it's followed by a `{`.

### The `scoter:` section

The `scoter` section is special, in that the tool fills in the `paths` that say where the configuration came from, and two `values` about the project, so you can refer to them from other places.  These are the same values as the universal configuration file's `active-config-locations` (see [The Universal Configuration File](contributors/universal-configuration-file.md)):

* `project-config` - the project configuration file: `values: project-file-name` in the project directory's `values: project-files-dir` (below).  Not set without a project.
* `project-dir` - the project directory, where `./` locations start (see [File Locations](#file-locations)): the directory holding the project files directory.  `scoter` finds it (see [Version control roots](#version-control-roots)) and gives it to `gen-scoter-uc --project-dir`.  Not set without a project.
* `vcs-root` - the root of the repository holding the project.  The `scoter` tool finds it (see [Version control roots](#version-control-roots)) and gives it to `gen-scoter-uc --vcs-root`; it must hold the project directory.  Not set without a project, or when the project isn't in a repository.  Setup files can't be read from inside it.
* `dev-container-path` - the project's dev container directory, with its `Dockerfile` and `dev-env.yaml`.  Not set without a project configuration file.  The tool finds it using `path-lists: dev-containers-search` (see below), so that list can't refer to it.
* `dev-container-workspace-dir` - the project workspace, mounted (writable) in the launched dev container.  The program that launches the container gives it (`gen-scoter-uc --dev-container-workspace-dir`).  Not set without that.
* `dev-container-settings-dir` - the settings directory, holding the runtime configuration files, mounted read-only in the launched dev container.  Like `dev-container-workspace-dir`, the launching program gives it (`gen-scoter-uc --dev-container-settings-dir`).
* `user-setup-file` - the user's setup file that the tool found, as described [above](#where-to-find-the-system-configuration-files).  If the tool didn't find a user setup file, this isn't set.
* `user-dir` - the directory the user's setup file was found in: `$SURF_SCOTER_HOME`, or `~/.config/surf-scoter.d`.  It isn't set when the file was found as `~/.config/surf-scoter.setup.{yaml,yml}` (that file isn't in a directory of its own), or when no user setup file was found.
* `distribution-dir` - the Surf Scoter distribution directory, as described [above](#where-to-find-the-system-configuration-files): `--distribution-dir`, or else the highest priority setup file's `distribution-dir`.  Not set without either.
* `distribution-bin-dir` - the distribution's programs: `${scoter:distribution-dir}/bin`.  A program working with the universal configuration file may change it there, and a later `gen-scoter-uc --universal` keeps the change while the distribution directory stays the same.  Not set without a distribution directory.
* `cwd` - the directory the tool was run in.
* `project-id` (a value, not a path) - the project's id: the project directory's name (lower-cased, with anything but letters, digits and `-` made `-`, at most 32 characters) and the first 12 hex digits of the SHA-256 of its canonical path, such as `client-3f9a0b1c2d4e`.  The same project directory always gets the same id; another clone gets another.  It names the project's directory in the verified store, and the default workspace (`workspace: paths: workspace-parent-dir`).  Not set without a project.
* `project-branch` (a value) - the branch checked out, from the project's VCS section's `runs: current-branch` (below), which `scoter` runs and gives to `gen-scoter-uc --project-branch`.  The project chooses its branch names, so it's made safe as one path part: only letters, digits, `.`, `_` and `-` are kept, each run of anything else made one `-` (`feature/x` becomes `feature-x`), `.` and `-` trimmed from the ends, and at most 64 characters.  Not set without a project, outside a repository, or when the VCS can't tell; a path that refers to it then has no value.  `--universal` carries it over while the project stays the same.

### Version control roots

The `scoter` tool finds the repository holding your project with the setup's VCS sections (`NAME-vcs`).  A section that can find its root has a `runs: find-root` command: given `{x:start}`, it prints the root of the repository holding it, or nothing when it isn't in one.  It should find the root from the repository's files alone; running the VCS itself (such as `git rev-parse --show-toplevel`) reads the repository's configuration, which the project controls.  The distribution has two scripts for this, in its `bin` directory, each given the name of the file or directory that marks a checkout:

* `vcs_root_nearest.py MARKER START`: the nearest directory, at or above `START`, holding `MARKER`.  For `.git` (a directory, or a file in a Git worktree or submodule), `.hg` or `.svn`.
* `vcs_root_outermost.py MARKER START`: the highest directory of the unbroken run, from the nearest one up, holding `MARKER`.  For a VCS that keeps a directory in every directory of a checkout, such as CVS with its `CVS` directories.

Both stop below your home directory (so a home directory kept in a repository doesn't count) and at a file system boundary, and use no environment variables (such as `GIT_DIR`).  The `git-vcs` section runs the first with `.git`, using the `python` section's interpreter.

`scoter` runs every VCS section's `find-root` from where the search starts: the current directory, or `--project-dir`.  The nearest root wins, so a Git submodule, with its own `.git` file, is its own repository when you run the tool inside it; above it, you're in the owning repository.

The project directory is then looked for in the search's start, and each directory above it up to the repository root, never above it: a project planted above the repository isn't found.  Outside any repository, only the start is searched.  A project directory is one holding `values: project-files-dir`, with `values: project-file-name` in it.  A project whose `vcs` has a `find-root` must be in a repository of that VCS; one without (such as `file`) can be anywhere.

A VCS section can also have a `runs: current-branch`: given `{x:root}`, the repository's root, it prints the branch checked out, or nothing when it can't tell.  `scoter verify` shows it in the review file, and `scoter` gives it to the generator as `${scoter:project-branch}`.  Like `find-root`, it should read the repository's files rather than run the VCS.  The distribution's `vcs_git_branch.py ROOT` does this for Git: it reads `.git/HEAD` (following a `.git` file's `gitdir:`, for worktrees and submodules), and prints the branch, or `detached at COMMIT`.

All of the paths are absolute.

`distribution-dir` is the only one of these that you can set in a setup file.  The tool needs the others to find the configuration, so changing them afterwards would be a lie.  Setting one is an error (such as `scoter.paths.user-dir: is set by the tool; remove it`).

A run doesn't always have all of these: `project-dir` isn't set without a project, for example.  Referring to one that isn't set (say, `${scoter:user-dir}` when your setup file is `~/.config/surf-scoter.setup.yaml`) isn't an error, so one setup file can serve runs with and without a project:

* A `path-lists` entry that needs it is left out, as a `./` entry is without a project.  So `dev-containers-search: ['${scoter:project-dir}/${scoter:project-files-dir}', '${scoter:distribution-dir}/containers']` is just the distribution's directory when there's no project.
* Any other value that needs it has no value (`null`), like a value that needs a program that isn't there.  The tool using that value reports the problem, if it needs it.

A misspelled location, such as `${scoter:project-dirr}`, is still an error.

When the tool doesn't look for setup files (`gen-scoter-uc --no-setup-search`), `user-setup-file` and `user-dir` aren't set, unless they come from an earlier run's universal configuration file (`--universal`).  A `--universal` file also brings along its `project-config`, `project-dir`, `dev-container-workspace-dir`, `dev-container-settings-dir` and `distribution-dir`, unless this run gives others.

The rest of the `scoter` section is yours to set.  The tools read its `path-lists: dev-containers-search` (see [Defining Executables](#defining-executables)).

## File Locations

Every `paths` value is a file location.  The tool expands a leading `~` to your home directory, and a leading `./` makes the location relative to the project directory, `${scoter:project-dir}` (without one, a `./` location stays as written).  References to it, such as `${ssh:home}`, get the expanded location.  A file location can't use `{x:...}` values.

A file location that doesn't exist isn't an error, and is kept as it is (in `path-lists` too): configurations often name files that are only needed now and then, or that a tool creates.  The tool that uses the location checks whether it exists, when that matters.

## Programs

A section's `programs` name the executables it runs.  The key's name doesn't matter: anything in `programs` is a program, and nothing in `paths` is, whatever its name.  By convention, a section's main program is called `exec`.

```yaml
ssh:
    programs:
        exec: ssh
        keygen: ~/bin/ssh-keygen
```

A program's value is expanded as a file location is (a leading `~` and `./`, and `${...}` references), and then:

* a bare name, such as `ssh`, is searched for in the `PATH`'s absolute directories.  Relative and empty entries (such as `.`) are skipped: they'd be relative to wherever the tool runs, which is likely the project;
* anything else, such as `~/bin/ssh-keygen` or `/usr/bin/ssh`, is used as is, as long as it's an executable file.

References to a program, such as `${exec}`, get the program found.  If a program can't be found, it has no value, and neither does any value that refers to it (such as a `runs` command that uses it).  The rest of the section is still usable.  Like a file location, a program can't use `{x:...}` values.

## Defining Executables

The tool makes heavy use of running other commands.  In order to remain flexible enough to work with your particular environment, it sets up executables based on the tool name and the uses for it.  In the example above, when the tool builds a container image, it will use the `docker` section's `runs: build` key.  The execution will include external values `{x:tag}`, `{x:dockerfile}`, and `{x:dirname}`, and the `${exec}` comes from the `docker` section's `programs: exec`.

When a `runs` pattern is a single string, the tool splits it into arguments at the spaces *before* it replaces the `${...}` references, so a referenced value with spaces in it (such as `C:\Program Files\...`) stays a single argument.  To put spaces into an argument yourself, quote it with `'...'` or `"..."`; inside `"..."`, write `\"` for a `"` and `\\` for a `\`.  A backslash anywhere else is an ordinary character.

A `runs` pattern can also be a list, with one entry per argument.  The tool uses each entry as is, with no splitting and no quote handling, though it still replaces the `${...}` references and keeps the `{x:...}` values:

```yaml
docker:
    runs:
        build:
            - ${exec}
            - build
            - --label=built by "Surf Scoter"
            - -t
            - "{x:tag}"
```

Quote list entries that start with `{`, such as `"{x:tag}"`; otherwise YAML reads them as a mapping.

The tools use these sections:

* `docker`: the container tool.
    * `programs: exec`: the program; defaults to `docker`.
    * `runs: broker`: how `scoter-host` starts the broker inside the development container.  It defaults to `${exec} exec -i {x:container} {x:broker}`, where `{x:container}` is the container name and `{x:broker}` is the `broker-bin` from the container's `dev-env.yaml`.  It must not ask for a TTY.
* `python`: the interpreter that runs the distribution's scripts: the sub-commands (`bin/main_*.py`) and the helpers the other sections' `runs` name.
    * `programs: exec`: the interpreter, normally `python3`.  Point it at a virtual environment's interpreter, such as `${scoter:distribution-dir}/.venv/bin/python`, to give the scripts installed libraries.  It runs on your computer with your privileges, so never point it into a project.
    * `runs: script`: how the tools run one of the distribution's scripts: `${exec} -E -s`.  Every `runs` entry that starts one begins with it (`- ${python:script}`, a reference that puts its items in place), so the flags are in one place: `-E` ignores the `PYTHON*` environment variables (which a project could set when you enter its directory) and `-s` your user site-packages.
    * `runs: sub-command`: how the tool starts a sub-command script: `${script} {x:script}`.  `{x:script}` is the script, and the tool adds the universal configuration file, the sub-command name and its arguments after it.  It must start with the interpreter.
* `git-vcs`: the Git VCS (see [Version control roots](#version-control-roots)).  Its `value-lists: drop-env` is `GIT_*`, so the Git commands the tools run (`local-clone`, `fetch-changes-into`, `merge-changes`) don't get Git's environment variables, which a project could set as you enter its directory to point Git at another repository, configuration or program.  Set what you need in your Git configuration instead (`core.sshCommand` in place of `GIT_SSH_COMMAND`).
* `workspace`: where a project's run keeps its files.
    * `paths: workspace-parent-dir`: the directory holding them.  The default is `${scoter:state-dir}/workspaces/${scoter:project-id}`: outside the project, so the project's own files and the container's stay apart, named by the project's id (the same for the same project directory).  For a workspace per branch, use `${scoter:state-dir}/workspaces/${scoter:project-id}/${scoter:project-branch}`.
    * `paths: workspace-dir`, `setup-dir` and `resources-dir`: the clone the container works in (mounted read-write), the run's settings (mounted read-only), and its resources (not mounted), in `workspace-parent-dir`.
* `dev-env`: the containers of a development environment.
    * `paths: docker-compose`, `firewall-router` and `network-gateway`: the compose file, and the distribution's firewall and gateway container directories.
    * `values`: the images' names, the containers' limits (`mem-limit`, `cpus`, `pids-limit`, `logging-max-size`), and `start-sshd-port`, the first port tried for a container's sshd when its `dev-env.yaml` leaves it to the run.
    * `values: network-gateway-name`: the network gateway's name (`squid`): the gateway is the only way out of the development container.  Your acceptance of a project's egress rules is for this gateway; a different one asks for a new review.
    * `values: network-gateway-config-mount`: where the gateway container reads the configuration `network-gateway-write-config` makes.
    * `runs: network-gateway-write-config`: given the universal configuration file (`{x:universal}`) and where to write (`{x:output}`), writes the gateway's configuration from the egress rules, and prints JSON saying what it enforces and what it refuses (each with its reason).  It exits with 1, writing nothing, when it refuses any rule: `scoter run` then shows the refusals and doesn't start.  The distribution runs its `bin/gateway_squid.py`.
* `verification`: what a review of a project's settings checks (`scoter verify`), and where your acceptances are kept (see [The verified store](#the-verified-store)).
    * `host-commands`: every [host command check](#host-command-checks): shells, interpreters, programs that run other programs, options and environment variables that run code, version manager shims, and the `docker` command (mapped to the `docker` section's program).  Keeping them in one section means none is missed by being somewhere else.
    * `paths: store`: where your acceptances are kept.  They decide what each project may do, so it must be an absolute path, outside the project directory, the project files directory, the dev container's workspace and the repository holding the project; otherwise the tools stop with an error.  The default is `~/.config/surf-scoter.d/verified`, whether or not a user setup file is found there.
    * `path-lists: verified-containers`: the `scoter: path-lists: dev-containers-search` directories whose containers are reviewed like a project's own: kept in the store with each acceptance, and built from that copy.  The others (such as the distribution's `containers`) are trusted as part of your installation.  Empty by default; add `${scoter:distribution-dir}/containers` to review the distribution's containers too.
    * `project-files`: checks on the files in a project's container directories, as [project file and egress host checks](#project-file-and-egress-host-checks).  The distribution's `secret-files` warns about names that look like secrets, such as `.env`, `*.pem` or `.aws/`, as everything in a container directory goes into the image.
    * `egress-hosts`: checks on the hosts a project's egress rules reach, the same way.  The distribution's warn about hosts anyone can upload to (`code-hosts`, `paste-sites`, `object-storage`, `user-content`, `webhooks`, `file-sharing`): a rule that reaches one lets the container send data out, whatever the other rules say.
    * `path-lists: public-suffix-lists`: [Public Suffix List](https://publicsuffix.org/) files: a project's egress rule can't allow every subdomain (`*.DOMAIN`) of a public suffix, where anyone can have a name.  None by default.  To use the published list, download it (`curl -o ~/.config/surf-scoter.d/public_suffix_list.dat https://publicsuffix.org/list/public_suffix_list.dat`), name the file here, and refresh it now and then.  Its plain, wildcard (`*.ck`) and exception (`!www.ck`) rules are all used, its private section (shared hosting) too; entries outside ASCII are skipped, as egress hosts are written in their `xn--` form.  A file named here that can't be read is an error.  The refusal applies to the project file's rules, to those typed in a review, and to accepted settings at each run, so a refreshed list can refuse a wildcard accepted before.
    * `value-lists: egress-refused-wildcards`: the domains a project's egress rule can't allow every subdomain of, such as `github.io` or `co.uk`: public suffixes and shared hosting, where anyone can have a name.  A `*.` on a single label (`*.com`) is always refused.  The distribution gives a starting list; to add to it, copy it into your setup and add your own.
    * `value-lists: guarded` and `guarded-removals`: the settings that keep the tools' protections in place; see [Guarded settings](#guarded-settings).
    * `values: expiry-days`: how long an acceptance lasts, in days.  The default is 30.
    * `values: keep`: how many expired acceptances to keep for each project when you ask for a cleanup; nothing removes them on its own.  The default is 10.
    * `values: max-file-size` and `max-snapshot-size`: the largest project file, and the most for all of a project's files, that the tools copy into the store, in bytes.  The defaults are 16 MB and 256 MB.  A project over either can't be accepted.
* `ssh`: the SSH client used to connect to the development container.
    * `programs: exec`: the program, normally `ssh`.
    * `paths: home`: your SSH directory, normally `~/.ssh`.
    * `paths: config-file`: the SSH configuration file the tool adds the container's `Host` entry to.
    * `paths: key-file`: the private key used to connect to the container.
* `scoter`: the tool's own settings.
    * `paths`: where the configuration came from, set by the tool (except that you can set `distribution-dir`), as described in [The `scoter:` section](#the-scoter-section).
    * `values: project-id` and `project-branch`, set by the tool for a project: see [The `scoter:` section](#the-scoter-section).
    * `paths: state-dir`: where the tools keep their own state, such as the running environments, and the workspaces by default.  It has the same rules as `verification: paths: store`.  The default is `~/.config/surf-scoter.d/state`.
    * `paths: running-envs` and `running-envs-lock`: the running environments file, in `state-dir`.  It holds each environment's whole configuration, so it's written readable only by you; each change to it is made under the lock, which is let go when the command ends, however it ends.  `scoter kill` stops an environment its `scoter run` left behind (after a crash, say): it stops its containers, and with `--fetch BRANCH` fetches the workspace's branch into the project, and with `--force-clean` removes the workspace (never one that is, or holds, the project, its repository or your home directory).  Without `--host`, it stops the project's one.
    * `runs: find-acceptance`: given the universal configuration file with the project (`{x:universal}`), prints JSON saying which of your acceptances matches the project's files, if any, and its accepted settings.  The distribution runs its `bin/verified_find.py`.  There's no reason to change it.
    * `path-lists: dev-containers-search`: where the project configuration's `dev-container: NAME` looks for the `NAME` directory, with its `Dockerfile` and `dev-env.yaml`.  The first directory that has both is used.  A dev container, and every parent it builds on, must be inside the project files directory or one of these directories, so don't list directories a project controls, such as `${scoter:project-dir}` itself.  The default is `${scoter:project-dir}/${scoter:project-files-dir}` and the distribution's `containers` directory.
    * `values: project-files-dir`: the name of the project files directory, which holds the project configuration file and the project's dev containers.  The project directory is the one holding it.  It must be a single directory name; the default is `.surf-scoter.d`.
    * `values: project-file-name`: the name of the project configuration file, in the project files directory.  It must be a single file name; the default is `project.yaml`.

If you find yourself using very obscure versions or alternatives of these tools, you may find yourself needing to use a translator script to convert between what Surf Scoter expects and what your tool does.

## Defining Path Lists

The tool relies upon search paths to find files it uses for running parts of its task.  This allows you to override parts of the configuration to better suit your project.  These lists go in a section's `path-lists`.  Note that, based on the load order above, if a higher level sets a list, this *replaces* the whole lower level list.

Each entry is a file location, like a `paths` value, so a leading `~` is expanded to your home directory and a leading `./` is relative to the project directory (`${scoter:project-dir}`).  When there is no project directory, `./` entries are left out.

```yaml
docker:
    path-lists:
        compose:
            - ./.scoter/compose
            - ${scoter:user-dir}/compose
```

## Value Lists

A section's `value-lists` hold lists of plain text: names, patterns, domains.  Unlike `path-lists`, the entries aren't file locations, so `~` and `./` mean nothing special.  Each entry is expanded like a `values` text (`${...}` references, `$${` for `${`), and an entry that's a whole reference to another list puts its items in its place.  Numbers and `true`/`false` become text.  An entry that needs a `scoter` location this run doesn't have is left out, as for `path-lists`.

```yaml
verification:
    value-lists:
        egress-refused-wildcards:
            - ${my-lists:company-shared-domains}
            - github.io
```

As with `path-lists`, a higher priority file's list replaces the whole list.

Any section can have a `value-lists: drop-env`: the environment variables its `runs` commands don't get, each a name, or a prefix ending in `*` (`GIT_*`).  On Windows, names match ignoring case.

To change a section's `drop-env`, set it in your own setup file.  Your list replaces the distribution's whole, and the rest of the section (its `runs`, `values` and so on) stays as the distribution has it, so you only write the list:

```yaml
# Drop more: copy the distribution's entries, and add yours.
git-vcs:
    value-lists:
        drop-env:
            - GIT_*
            - PAGER
```

```yaml
# Drop less: list only what you want dropped.  (`drop-env: []` drops nothing.)
git-vcs:
    value-lists:
        drop-env:
            - GIT_DIR
            - GIT_WORK_TREE
            - GIT_CONFIG_*
```

A list can't refer to the distribution's copy of itself, so adding means copying its entries; check them again when you update Surf Scoter, as yours won't pick up a new one.  Dropping less lets a project's environment reach those commands again: keep at least the variables that point Git at another repository (`GIT_DIR`, `GIT_WORK_TREE`, `GIT_COMMON_DIR`, `GIT_INDEX_FILE`, `GIT_OBJECT_DIRECTORY`, `GIT_ALTERNATE_OBJECT_DIRECTORIES`), another configuration (`GIT_CONFIG*`), or another program (`GIT_SSH`, `GIT_SSH_COMMAND`, `GIT_ASKPASS`, `GIT_EXEC_PATH`, `GIT_EDITOR`, `GIT_PAGER`, `GIT_EXTERNAL_DIFF`).

The `docker` section has no `drop-env`.  A project could set `DOCKER_HOST`, `DOCKER_CONTEXT` or `DOCKER_CONFIG` as you enter its directory, to point the tools' Docker commands at another daemon, or at a configuration whose credential helpers run programs.  But `DOCKER_HOST` is also how Podman, and remote Docker daemons, are commonly set up, so dropping it by default would break those setups.  If your setup doesn't rely on them, consider adding one in your setup file, such as `docker: value-lists: drop-env: [DOCKER_HOST, DOCKER_CONTEXT, DOCKER_CONFIG, DOCKER_CERT_PATH, DOCKER_TLS_VERIFY]`, and setting what you need in the section's `runs` instead (`--host`, `--config`).

The programs your setup runs are checked before `scoter run` starts anything, and by `scoter user-setup`: a program can't be written relative to the project (`./`), refer to a project location, or be found inside the project, the workspace or the repository; nor can another user own it, or anyone write to it or a directory above it (one its group can write to is warned about); nor can it be under a `host-commands` check's `paths`, such as the version manager shims.  A relative `PATH` entry is warned about: the tools skip it when finding programs.

## Plain Values

A section's `values` hold single values that aren't file locations: text, numbers, `true`/`false`, or `null`.  The tool keeps each value's type, and doesn't treat any of them as a file location or a program.  Text may refer to other keys with `${...}`, just like elsewhere; other kinds of values are used as written.

```yaml
ssh:
    values:
        port: 22022
        strict-host-key-checking: false
        banner: Connecting with ${ssh:config-file}
```

## Host Command Checks

A project can ask to run commands on your computer (its `commands`, in the [project configuration file](project-configuration-file.md)).  A section's `host-commands` are checks on those commands: what to refuse, what to warn you about, and what you've decided to allow.  Each key is one check:

```yaml
verification:
    host-commands:
        shells:
            action: refuse
            reason: >-
                A shell runs whatever command it's given, so the container could
                run anything on your computer.
            commands: [sh, bash, zsh, ksh]
        docker:
            action: warn
            reason: An allowed `docker` command can mount your home directory.
            command-map:
                docker: ${docker:exec}
docker:
    programs:
        exec: podman
```

`gen-scoter-uc` checks, expands and combines these (see the universal configuration file's `host-command-checks`), and `scoter verify` applies them to a project's commands.

The distribution keeps all its checks in the `verification` section, and so should your setup: someone checking what's refused reads one list.  (Any section can still have them, and their names are `section:key`.)  A check has:

* `action` (required): `refuse`, `warn` or `allow`, from most to least restrictive.
* `reason` (required): why, shown when the check matches a project's command.
* What it matches: one or more of these.  With more than one, all of them must match: `commands: [git]` with `options: [-c]` matches `git -c`, not `git` alone.
    * `commands`: command names, as the project file's `command` gives them.  Each is found on the `PATH`, as a `programs` value is.
    * `command-map`: command names, each with the program it runs, written as a `programs` value is (`docker: ${docker:exec}`, or `docker: podman`).  The command `docker` then runs `podman`.  A name can't be in both `commands` and `command-map`.
    * `name-patterns`: patterns for command names, with `*` (any characters), `?` (one character) and `[...]` (one of a set; `[!...]` is one not in it), such as `python*`.
    * `options`: options that run code, such as `-c`.  They match when the project lets the container fill in what follows them.  Without names, they apply to every command.
    * `env`: environment variables, matched against a command's `env` `allow` entries as exact text: `LD_*` matches an `allow: ['LD_*']` entry, but not `allow: ['L*']`.
    * `paths`: directories, written as `path-lists` entries are.  They match a command whose program is in one of them.  `paths` can't go with the other matches.
* `overrides` (optional): the checks (`section:key`) this one replaces, for the commands it names (see below).

Names are case sensitive.  A check doesn't look only at the name: a command whose program is a link to the same file as one a check names matches that check too, and `name-patterns` are also matched against the name of the file the program really is.  So a command `mytool` that's a link to `env` is refused as `env` is.

Only `command-map` values and `paths` entries are expanded.  The other fields are used as written, and a `${` in them is an error.  Every field is checked: a misspelled field, or a value that can't be right (a `commands` entry with a `/` in it, say), is an error.

A command's program can't be in the project directory, the project files directory, the workspace or the repository holding the project, and two checks can't map the same command to different programs; `gen-scoter-uc` reports either as an error.

### When checks disagree

A command's action is worked out in two steps:

1. Every check without `overrides` that matches the command counts, and the most restrictive of their actions wins.  So adding a check can only add to what's refused or warned about.  `allow` matters only for a command that no other check matches: it says you've decided about it.
2. Then each [override](#overrides) for the command takes the place of the check it overrides, and the action is again the most restrictive of what's left.  An override changes what one check says about a command; it doesn't decide the command on its own.  The other checks that match still count, and an override for a command no check matches does nothing.

You're shown the reason of every check that counts, and which checks an override replaced.

Your setup file's checks merge with the distribution's key by key, like the other kinds of values:

* to **add** a check, give it a name of your own;
* to **relax** one for some commands, add a check that names those commands and `overrides` it (below);
* to **replace** one, set it again under the same name (the whole check: its fields aren't merged with the distribution's);
* to **remove** one, set it to `null`.

Prefer `overrides` to replacing or removing a check.  A replaced check is frozen: when a later Surf Scoter version adds a newly found shell to `verification: shells`, your replacement doesn't get it.  `scoter user-setup` lists the checks your setup replaces or removes, and what each gives up.

### Overrides

To allow `ksh` without allowing the other shells:

```yaml
verification:
    host-commands:
        ksh-is-mine:
            action: warn
            reason: I review the ksh scripts this project's commands run.
            commands: [ksh]
            overrides: [verification:shells]
```

For `ksh`, this check's `warn` takes the place of `verification: shells`' `refuse`.  The rest of `shells` still applies, including what later versions add to it, and so does any other check that matches `ksh`: if one refuses it, it's still refused.  So an override can't undo more than you meant:

* It names the checks it overrides exactly.  Any other check that matches `ksh`, including one a later version adds, still applies.
* It names its commands exactly, with `commands` or `command-map`, and can't use `name-patterns` or `paths`.  Its `env` entries must be exact names, too.
* A command (or option, or environment variable) the overridden check doesn't match is reported, as the override does nothing for it.  So is an override of a check that isn't set.
* A check can only override checks from its own setup file or a lower priority one: the distribution's can't override yours.
* It can only override checks without `overrides` of their own.
* Two checks can't override the same check for the same command (or, with `options` or `env`, for the same option or variable): only one can say what the check becomes.  `gen-scoter-uc` reports it as an error, as it does the two above.

## Project File and Egress Host Checks

A section's `project-files` and `egress-hosts` are checks of the same kind as [host command checks](#host-command-checks), each matching one thing:

```yaml
verification:
    project-files:
        keys:
            action: refuse
            reason: A private key doesn't belong in an image.
            patterns: ['*.pem', id_*]
    egress-hosts:
        our-git:
            action: allow
            reason: Our own code host; the container's token only reads.
            hosts: [git.example.com, github.com]
            overrides: [verification:code-hosts]
```

* `action` (required): `refuse`, `warn` or `allow`.  Each check that counts is shown, with its reason, and the most restrictive decides: a refused file or egress rule can't be accepted as it is.
* `reason` (required): why, shown when the check matches.
* `patterns` (`project-files`): names of files, and, ending in `/`, directories, with `*`, `?` and `[...]`, matched against each file's and directory's own name in the project's container directories.
* `hosts` (`egress-hosts`): host names, or `*.` and a domain for its subdomains.  A check's host matches an egress rule that reaches it: `github.com` matches a rule for `github.com`, `*.githubusercontent.com` one for `raw.githubusercontent.com`, and a rule for `*.github.com` matches a check for `gist.github.com`.
* `overrides` (optional): the checks (`section:key`, of the same kind) this one takes the place of, for what it matches.  An override of a check that doesn't match does nothing; it must name a check that's set, and one that isn't an override itself.

They merge across your setup files key by key, as the other checks do: add a check with a name of your own, set one again to replace it, or set it to `null` to remove it.  Prefer `overrides` to replacing or removing one, for the same reason.

An egress rule an `egress-hosts` check refuses is written commented out in the review, with an `$override` naming the check: uncommenting it with its `$override` accepts it for that project only.  A refused file has no such override: change the file, choose your own container (`use: {name}`), or change the check.

## Guarded Settings

Some settings keep the tools' protections in place: `-E` on the Python scripts (so a project's `PYTHONPATH` stays out), `--no-local` on the workspace's clone, Git's `drop-env`, the network gateway and its configuration, where the store and the workspace are, the review's checks.  The defaults mark them, in `verification: value-lists: guarded`, as `section:kind:key` (`*` for every key of a kind), and `scoter user-setup` and the review list each one your setup file changes, with the default beside it:

```
Your setup changes these guarded settings; check each is what you want:
  * ~/.config/surf-scoter.d/setup.yaml sets `python: runs: script` to ["${exec}", "-s"]; the default is ["${exec}", "-E", "-s"]
  * ~/.config/surf-scoter.d/setup.yaml leaves out of `git-vcs: value-lists: drop-env` the default's `GIT_*`
```

So a change copied in from instructions you didn't look at closely doesn't go unnoticed.  Neither fails the setup: they're your choices to make.

* `guarded` settings are reported for any change, and so is a check of the distribution's that your setup replaces or removes (`verification:egress-hosts:*`); a check of your own isn't, as adding one can only add to what's checked, except that each override is listed too.
* `guarded-removals` are deny-lists, where adding entries only strengthens (`drop-env`, `egress-refused-wildcards`): they're reported only when your setup leaves out one of the default's entries.
* Your setup can add guards of its own to either list.  It can't take the distribution's out: they're always used, and a setup that tries is reported too.

`scoter verify --skip-setup-review` leaves this, and the host command checks' report, out of the review.

## The Verified Store

`verification: paths: store` holds the project settings you've accepted, and a copy of the project files you accepted them from: the project file, and the project's container directories (with `verification: path-lists: verified-containers`, some of the installation's too).  Each project's are in a directory named by its `${scoter:project-id}`.  The tools build and run a project from that copy, so what runs is what you reviewed.

The copies take disk space.  Each file is kept once, however many acceptances share it, so each new acceptance costs a copy of the files that changed since the last one; but container directories can hold large files copied into the image.  To keep it bounded:

* Each acceptance expires after `verification: values: expiry-days`.  Expired acceptances are kept, so a review can show what changed since; nothing removes them on its own.  `scoter revoke` removes them (`scoter list-accepted` lists them), with the files nothing else uses.
* `verification: values: max-file-size` and `max-snapshot-size` limit what one acceptance can copy.

Everything in it must belong to you, with no access for anyone else (directories `0700`, files `0600`), and contain no symlinks; the tools refuse to read it otherwise.  On Windows, which has no file modes, that's left to your user profile's permissions.

Each `scoter` run with a project looks for the acceptance that matches the project's files, as they are now, and the setup values it was accepted under.  A change that only touches comments or blank lines (in a Dockerfile, a JSON file, or the YAML files the tools read) carries the acceptance forward, with its settings and its expiry; any other change needs a new review.

`scoter run` keeps `scoter-host`'s copy of the configuration in `${scoter:state-dir}/environments/HOST/suc.json`, private to you, and removes it when the environment stops.

The tools lock the store while they change it, so two `scoter` commands can't interleave their changes.  If the store is on a network file system (such as an NFS home directory), the lock may only work between commands on the same computer.

