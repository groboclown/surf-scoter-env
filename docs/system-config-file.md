# Scoter System Configuration File

Scoter allows you to set specific configurations for your particular computer in the system configuration files.


## Where to find the system configuration files

The tool will look in *all* these places, in order, not just the first one found.  That means all of the files that exist will load into the configuration.  Earlier files take priority over later ones.

* The `--user-setup` argument.
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
* `values`: plain values (see [Plain Values](#plain-values)).

Each key in a section has exactly one kind of value: setting the same key in two of the mappings (say, `paths: retries` and `values: retries`) is an error.  That holds across files too: the kind of value is part of what a key means, so a higher priority file can change a key's value, but not its kind.  You can think of a section as a mapping of keys, where each key has a value and a kind.

Sections from different files are merged key by key, with the higher priority file's value winning.

Each file may be at most 2 MB.  YAML tags (such as `!include`) aren't supported, and the tools report them as errors.  When the files have problems, the tools report all the problems they find, rather than just the first.

Inside the values, you can refer to any other key with the `${[section:]key}` format.  In the example above, `${ssh:home}` is the `ssh` section's `home` path, so `config-file` becomes `~/.ssh/config` (with `~` expanded to your home directory).  In the same way, the `key-file` key assumes the `${home}` reference refers to its own section, which expands into `~/.ssh/scoter-dev-container`.

What a reference gives depends on the kind of value it refers to:

* `paths`, `programs` and `values` are text, and can be used anywhere.  A `null` value can't be used.  A reference to a program gives the program found (see [Programs](#programs)).
* `runs` and `path-lists` are lists.  A reference that is a whole argument of a `runs` command, or a whole entry of a `path-lists` list, puts the list's items in its place; for example, `${exec} ${common-args} build`, or a list entry `${docker:compose}`.  Anywhere else, a reference to a list is an error.

Referring to a key that isn't set anywhere is an error, as is a set of values that refer to each other in a loop.  You can escape a `${` expression by doubling up the dollars - `$${` becomes `${`; you don't need to double-up a `$` unless it's followed by a `{`.

### The `scoter:` section

The `scoter` section is special, in that the tool fills in the `paths` that say where the configuration came from, so you can refer to them from other places.  These are the same values as the universal configuration file's `active-config-locations` (see [The Universal Configuration File](contributors/universal-configuration-file.md)):

* `project-config` - the project configuration file.  Not set without one.
* `project-dir` - the project directory, where `./` locations start (see [File Locations](#file-locations)).  This is the directory with the project configuration file in it, unless `gen-scoter-uc --project-dir` gives another.  Not set without either.
* `dev-container-path` - the project's dev container directory, with its `Dockerfile` and `dev-env.yaml`.  Not set without a project configuration file.  The tool finds it using `path-lists: dev-containers` (see below), so that list can't refer to it.
* `user-setup-file` - the user's setup file that the tool found, as described [above](#where-to-find-the-system-configuration-files).  This is the file the tool found by looking, not the `--user-setup` file.  If the tool didn't find a user setup file, this isn't set.
* `user-dir` - the directory the user's setup file was found in: `$SURF_SCOTER_HOME`, or `~/.config/surf-scoter.d`.  It isn't set when the file was found as `~/.config/surf-scoter.setup.{yaml,yml}` (that file isn't in a directory of its own), or when no user setup file was found.
* `distribution-dir` - the Surf Scoter distribution directory, as described [above](#where-to-find-the-system-configuration-files): `--distribution-dir`, or else the highest priority setup file's `distribution-dir`.  Not set without either.
* `cwd` - the directory the tool was run in.

All of them are absolute paths.

`distribution-dir` is the only one of these that you can set in a setup file.  The tool needs the others to find the configuration, so changing them afterwards would be a lie.  Setting one is an error (such as `scoter.paths.user-dir: is set by the tool; remove it`).

Like any other key, referring to one of these when it isn't set (say, `${scoter:user-dir}` when your setup file is `~/.config/surf-scoter.setup.yaml`) is an error.

When the tool doesn't look for setup files (`gen-scoter-uc --no-setup-search`), `user-setup-file` and `user-dir` aren't set, unless they come from an earlier run's universal configuration file (`--universal`).  A `--universal` file also brings along its `project-config`, `project-dir` and `distribution-dir`, unless this run gives others.

The rest of the `scoter` section is yours to set.  The tools read its `path-lists: dev-containers` (see [Defining Executables](#defining-executables)).

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
    * `runs: broker`: how `scoter-host` starts the broker inside the development container.  It defaults to `${exec} exec -i {x:container} {x:broker}`, where `{x:container}` is the container name and `{x:broker}` is the `broker-bin` from the container's `dev-env.yaml`.  It must not ask for a TTY.
* `ssh`: the SSH client used to connect to the development container.
    * `programs: exec`: the program, normally `ssh`.
    * `paths: home`: your SSH directory, normally `~/.ssh`.
    * `paths: config-file`: the SSH configuration file the tool adds the container's `Host` entry to.
    * `paths: key-file`: the private key used to connect to the container.
* `scoter`: the tool's own settings.
    * `paths`: where the configuration came from, set by the tool (except that you can set `distribution-dir`), as described in [The `scoter:` section](#the-scoter-section).
    * `path-lists: dev-containers`: where the project configuration's `dev-container: NAME` looks for the `NAME` directory, with its `Dockerfile` and `dev-env.yaml`.  The first directory that has both is used.

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

## Plain Values

A section's `values` hold single values that aren't file locations: text, numbers, `true`/`false`, or `null`.  The tool keeps each value's type, and doesn't treat any of them as a file location or a program.  Text may refer to other keys with `${...}`, just like elsewhere; other kinds of values are used as written.

```yaml
ssh:
    values:
        port: 22022
        strict-host-key-checking: false
        banner: Connecting with ${ssh:config-file}
```
