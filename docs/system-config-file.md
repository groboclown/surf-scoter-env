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
* `host-commands` checks can't be referred to at all: `${host-bridge:shells}` is an error.
* `runs`, `path-lists` and `value-lists` are lists.  A reference that is a whole argument of a `runs` command, or a whole entry of a `path-lists` or `value-lists` list, puts the list's items in its place; for example, `${exec} ${common-args} build`, or a list entry `${docker:compose}`.  Anywhere else, a reference to a list is an error.

Referring to a key that isn't set anywhere is an error (except for the `scoter` locations the tool sets; see [below](#the-scoter-section)), as is a set of values that refer to each other in a loop.  You can escape a `${` expression by doubling up the dollars - `$${` becomes `${`; you don't need to double-up a `$` unless it's followed by a `{`.

### The `scoter:` section

The `scoter` section is special, in that the tool fills in the `paths` that say where the configuration came from, so you can refer to them from other places.  These are the same values as the universal configuration file's `active-config-locations` (see [The Universal Configuration File](contributors/universal-configuration-file.md)):

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

### Version control roots

The `scoter` tool finds the repository holding your project with the setup's VCS sections (`NAME-vcs`).  A section that can find its root has a `runs: find-root` command: given `{x:start}`, it prints the root of the repository holding it, or nothing when it isn't in one.  It should find the root from the repository's files alone; running the VCS itself (such as `git rev-parse --show-toplevel`) reads the repository's configuration, which the project controls.  The distribution has two scripts for this, in its `bin` directory, each given the name of the file or directory that marks a checkout:

* `vcs_root_nearest.py MARKER START`: the nearest directory, at or above `START`, holding `MARKER`.  For `.git` (a directory, or a file in a Git worktree or submodule), `.hg` or `.svn`.
* `vcs_root_outermost.py MARKER START`: the highest directory of the unbroken run, from the nearest one up, holding `MARKER`.  For a VCS that keeps a directory in every directory of a checkout, such as CVS with its `CVS` directories.

Both stop below your home directory (so a home directory kept in a repository doesn't count) and at a file system boundary, and use no environment variables (such as `GIT_DIR`).  The `git-vcs` section runs the first with `.git`, using the `python` section's interpreter.

`scoter` runs every VCS section's `find-root` from where the search starts: the current directory, or `--project-dir`.  The nearest root wins, so a Git submodule, with its own `.git` file, is its own repository when you run the tool inside it; above it, you're in the owning repository.

The project directory is then looked for in the search's start, and each directory above it up to the repository root, never above it: a project planted above the repository isn't found.  Outside any repository, only the start is searched.  A project directory is one holding `values: project-files-dir`, with `values: project-file-name` in it.  A project whose `vcs` has a `find-root` must be in a repository of that VCS; one without (such as `file`) can be anywhere.

All of them are absolute paths.

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

* a bare name, such as `ssh`, is searched for on the `PATH`;
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
    * `host-commands: docker`: a project's `docker` command runs `${exec}`, and is warned about.
    * `runs: broker`: how `scoter-host` starts the broker inside the development container.  It defaults to `${exec} exec -i {x:container} {x:broker}`, where `{x:container}` is the container name and `{x:broker}` is the `broker-bin` from the container's `dev-env.yaml`.  It must not ask for a TTY.
* `python`: the interpreter that runs the tool's sub-commands (the distribution's `bin/main_*.py` scripts).
    * `programs: exec`: the interpreter, normally `python3`.  Point it at a virtual environment's interpreter, such as `${scoter:distribution-dir}/.venv/bin/python`, to give the scripts installed libraries.  It runs on your computer with your privileges, so never point it into a project.
    * `runs: sub-command`: how the tool starts a sub-command script: normally `${exec} -E -s {x:script}`.  `{x:script}` is the script, and the tool adds the universal configuration file, the sub-command name and its arguments after it.  It must start with the interpreter; `-E` ignores the `PYTHON*` environment variables (which a project could set when you enter its directory) and `-s` your user site-packages.
* `git-vcs`: the Git VCS (see [Version control roots](#version-control-roots)).  Its `host-commands` map a project's `git` command to `${exec}`, and warn about it, and about the options that set Git configuration.
* `host-bridge`: the [host command checks](#host-command-checks) that aren't about one tool: shells, interpreters, programs that run other programs, options and environment variables that run code, and version manager shims.
* `ssh`: the SSH client used to connect to the development container.
    * `programs: exec`: the program, normally `ssh`.
    * `paths: home`: your SSH directory, normally `~/.ssh`.
    * `paths: config-file`: the SSH configuration file the tool adds the container's `Host` entry to.
    * `paths: key-file`: the private key used to connect to the container.
* `scoter`: the tool's own settings.
    * `paths`: where the configuration came from, set by the tool (except that you can set `distribution-dir`), as described in [The `scoter:` section](#the-scoter-section).
    * `path-lists: dev-containers-search`: where the project configuration's `dev-container: NAME` looks for the `NAME` directory, with its `Dockerfile` and `dev-env.yaml`.  The first directory that has both is used.  A dev container, and every parent it builds on, must be inside the project files directory or one of these directories, so don't list directories a project controls, such as `${scoter:project-dir}` itself.  The default is `${scoter:project-dir}/${scoter:project-files-dir}` and the distribution's `containers` directory.
    * `values: project-files-dir`: the name of the project files directory, which holds the project configuration file and the project's dev containers.  The project directory is the one holding it.  It must be a single directory name; the default is `.surf-scoter.d`.
    * `values: project-file-name`: the name of the project configuration file, in the project files directory.  It must be a single file name; the default is `project.yaml`.
    * `paths: verified-dir`: where your accepted (verified) project settings are kept.  They decide what each project may do, so it must be an absolute path, outside the project directory, the project files directory, the dev container's workspace and the repository holding the project; otherwise the tools stop with an error.  The default is `~/.config/surf-scoter.d/verified`, whether or not a user setup file is found there.  See [The verified store](#the-verified-store).
    * `paths: state-dir`: where the tools keep their own state, such as the running environments (`dev-env: paths: running-env-config`).  It has the same rules as `verified-dir`.  The default is `~/.config/surf-scoter.d/state`.
    * `value-lists: egress-refused-wildcards`: the domains a project's egress rule can't allow every subdomain of, such as `github.io` or `co.uk`: public suffixes and shared hosting, where anyone can have a name.  A `*.` on a single label (`*.com`) is always refused.  The distribution gives a starting list; to add to it, copy it into your setup and add your own.
    * `path-lists: dev-containers-verified`: the `dev-containers-search` directories whose containers are reviewed like a project's own.  The others (such as the distribution's `containers`) are trusted as part of your installation.  Empty by default; add `${scoter:distribution-dir}/containers` to review the distribution's containers too.
    * `values: verified-expiry-days`: how long an accepted project's settings stay accepted, in days.  The default is 30.
    * `values: verified-keep`: how many expired acceptances to keep for each project; older ones are removed.  The default is 10.
    * `values: verified-max-file-size` and `verified-max-snapshot-size`: the largest project file, and the most for all of a project's files, that the tools copy into the verified store, in bytes.  The defaults are 16 MB and 256 MB.  A project over either can't be accepted.

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
scoter:
    value-lists:
        egress-refused-wildcards:
            - ${my-lists:company-shared-domains}
            - github.io
```

As with `path-lists`, a higher priority file's list replaces the whole list.

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
host-bridge:
    host-commands:
        shells:
            action: refuse
            reason: >-
                A shell runs whatever command it's given, so the container could
                run anything on your computer.
            commands: [sh, bash, zsh, ksh]
docker:
    programs:
        exec: podman
    host-commands:
        docker:
            action: warn
            reason: An allowed `docker` command can mount your home directory.
            command-map:
                docker: ${exec}
```

Note: for now, `gen-scoter-uc` only checks, expands and combines these (see the universal configuration file's `host-command-checks`).  Applying them to a project's commands comes with the review of the project file, which is still being built ([Projects in v1](contributors/projects-v1.md)).

Any section can have them, so a check can sit with the tool it's about; the distribution's checks that aren't about one tool are in `host-bridge`.  A check has:

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

Prefer `overrides` to replacing or removing a check.  A replaced check is frozen: when a later Surf Scoter version adds a newly found shell to `host-bridge: shells`, your replacement doesn't get it.  `scoter user-setup` lists the checks your setup replaces or removes, and what each gives up.

### Overrides

To allow `ksh` without allowing the other shells:

```yaml
host-bridge:
    host-commands:
        ksh-is-mine:
            action: warn
            reason: I review the ksh scripts this project's commands run.
            commands: [ksh]
            overrides: [host-bridge:shells]
```

For `ksh`, this check's `warn` takes the place of `host-bridge: shells`' `refuse`.  The rest of `shells` still applies, including what later versions add to it, and so does any other check that matches `ksh`: if one refuses it, it's still refused.  So an override can't undo more than you meant:

* It names the checks it overrides exactly.  Any other check that matches `ksh`, including one a later version adds, still applies.
* It names its commands exactly, with `commands` or `command-map`, and can't use `name-patterns` or `paths`.  Its `env` entries must be exact names, too.
* A command (or option, or environment variable) the overridden check doesn't match is reported, as the override does nothing for it.  So is an override of a check that isn't set.
* A check can only override checks from its own setup file or a lower priority one: the distribution's can't override yours.
* It can only override checks without `overrides` of their own.
* Two checks can't override the same check for the same command (or, with `options` or `env`, for the same option or variable): only one can say what the check becomes.  `gen-scoter-uc` reports it as an error, as it does the two above.

## The Verified Store

`scoter: paths: verified-dir` holds the project settings you've accepted, and a copy of the project files you accepted them from: the project file, and the project's container directories (with `dev-containers-verified`, some of the installation's too).  The tools build and run a project from that copy, so what runs is what you reviewed.

The copies take disk space.  Each file is kept once, however many acceptances share it, so each new acceptance costs a copy of the files that changed since the last one; but container directories can hold large files copied into the image.  To keep it bounded:

* Each acceptance expires after `verified-expiry-days`.  Expired acceptances are kept for a while, so a review can show what changed since; past `verified-keep` of them, the oldest are removed, with the files nothing else uses.
* `verified-max-file-size` and `verified-max-snapshot-size` limit what one acceptance can copy.

Everything in it must belong to you, with no access for anyone else (directories `0700`, files `0600`), and contain no symlinks; the tools refuse to read it otherwise.  On Windows, which has no file modes, that's left to your user profile's permissions.

The tools lock the store while they change it, so two `scoter` commands can't interleave their changes.  If `verified-dir` is on a network file system (such as an NFS home directory), the lock may only work between commands on the same computer.

