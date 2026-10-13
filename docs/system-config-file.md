# Scoter System Configuration File

Scoter allows you to set specific configurations for your particular computer in the system configuration files.


## Where to find the system configuration files

The tool will look in *all* these places, in order, not just the first one found.  That means all of the files that exist will load into the configuration.  Earlier files take priority over later ones.

* The `--user-setup` argument.
* `${scoter:user-setup-file}` (using the internal setup to find the location; see below)
* Some distributions may also install Surf Scoter into a system path (given to the tools as `--distribution-dir`); its `setup.{yaml,yml}` is read next.

In order to find the `${scoter:user-setup-file}`, The system tries these locations, choosing the first one found (if any):

* `$SURF_SCOTER_HOME/setup.{yaml,yml}` (using the `SURF_SCOTER_HOME` environment variable, if set)
* `~/.config/surf-scoter.setup.{yaml,yml}`
* `~/.config/surf-scoter.d/setup.{yaml,yml}`

Once these are loaded, it will then try to load one more setup file, based on the already loaded configuration.  If the configuration includes the path:

```yaml
scoter:
    paths:
        install-dir: /my/install/dir
```

then the script will try to load the additional file `${scoter:install-dir}/setup.{yaml,yml}`, with the lowest priority.  (A file is only read once, even if it's found more than one way.)

Note: it is an error (will cause the tools to bail out) if you have both a `setup.yaml` and a `setup.yml` (or `surf-scoter.setup.yaml` and `surf-scoter.setup.yml`) file in the same directory.

The `scoter` block is special.  If its `paths: install-dir` is not set, it will be set by the distribution system path (if any).  Its `paths` will also have the `user-setup-file` and `user-dir` set, based on how they were discovered (using `~/.config/surf-scoter.setup.yaml` means the `user-dir` goes unset).

## Format

Each top-level key is either a *section* or a *lookup path* list (see below).  A section has two parts, both optional:

```yaml
docker:
    paths:
        exec: /usr/sbin/podman
    run:
        build: ${exec} -t {x:tag} -f {x:dockerfile} {x:dirname}
ssh:
    paths:
        home: ~/.ssh
        config-file: ${ssh:home}/config
        key-file: ${home}/scoter-dev-container
```

* `paths`: file locations (see [File Locations](#file-locations)).
* `run`: command patterns (see [Defining Executables](#defining-executables)).

Each value is a single value (text, numbers or `true`/`false`).  Sections from different files are merged key by key, with the higher priority file's value winning.

Each file may be at most 2 MB.  YAML tags (such as `!include`) aren't supported, and the tools report them as errors.  When the files have problems, the tools report all the problems they find, rather than just the first.

Inside the values, you can refer to a `paths` value with the `${[section:]key}` format.  In the example above, `${ssh:home}` is the `ssh` section's `home` path, so `config-file` becomes `~/.ssh/config` (with `~` expanded to your home directory).  In the same way, the `key-file` key assumes the `${home}` reference refers to its own section, which expands into `~/.ssh/scoter-dev-container`.

Only `paths` values can be referred to; `run` values can't.  Referring to a key that isn't set anywhere is an error, as is a set of values that refer to each other in a loop.  You can escape a `${` expression by doubling up the dollars - `$${` becomes `${`; you don't need to double-up a `$` unless it's followed by a `{`.

The `scoter` section follows the same pattern.  The tool fills in its `paths` (see [above](#where-to-find-the-system-configuration-files)), though you can set `install-dir` yourself.

## File Locations

Every `paths` value is a file location.  The tool expands a leading `~` to your home directory, and a leading `./` makes the location relative to the project file (without a project file, a `./` location stays as written).  References to it, such as `${ssh:home}`, get the expanded location.  A file location can't use `{x:...}` values.

A `paths` key named `exec`, or ending with `-cmd` (such as `keygen-cmd`), is an executable.  When its value is a bare name, the tool searches the `PATH` for it; a full path is used as is, as long as it's an executable file.  References to it, such as `${exec}`, get the program found.  If any executable in a section can't be found, the whole section can't be used.

## Defining Executables

The tool makes heavy use of running other commands.  In order to remain flexible enough to work with your particular environment, it sets up executables based on the tool name and the uses for it.  In the example above, when the tool builds a container image, it will use the `docker` section's `run: build` key.  The execution will include external values `{x:tag}`, `{x:dockerfile}`, and `{x:dirname}`, and the `${exec}` comes from the `docker` section's `paths: exec`.

When a `run` pattern is a single string, the tool splits it into arguments at the spaces *before* it replaces the `${...}` references, so a referenced value with spaces in it (such as `C:\Program Files\...`) stays a single argument.  To put spaces into an argument yourself, quote it with `'...'` or `"..."`; inside `"..."`, write `\"` for a `"` and `\\` for a `\`.  A backslash anywhere else is an ordinary character.

A `run` pattern can also be a list, with one entry per argument.  The tool uses each entry as is, with no splitting and no quote handling, though it still replaces the `${...}` references and keeps the `{x:...}` values:

```yaml
docker:
    run:
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
    * `paths: exec`: the program; defaults to `docker`.
    * `run: broker`: how `scoter-host` starts the broker inside the development container.  It defaults to `${exec} exec -i {x:container} {x:broker}`, where `{x:container}` is the container name and `{x:broker}` is the `broker-bin` from the container's `dev-env.yaml`.  It must not ask for a TTY.
* `ssh`: the SSH client used to connect to the development container.
    * `paths: exec`: the program, normally `ssh`.
    * `paths: home`: your SSH directory, normally `~/.ssh`.
    * `paths: config-file`: the SSH configuration file the tool adds the container's `Host` entry to.
    * `paths: key-file`: the private key used to connect to the container.

If you find yourself using very obscure versions or alternatives of these tools, you may find yourself needing to use a translator script to convert between what Surf Scoter expects and what your tool does.

## Defining Lookup Paths

The tool relies upon the search path to find files it uses for running parts of its task.  This allows you to override parts of the configuration to better suit your project.  All lookup paths end with a `-paths`.  Note that, based on the load order above, if a higher level sets a path list, this *replaces* any lower level path list.

Setting a path element with a leading './' means that it's relative to the project file.

```yaml
docker-compose-paths:
    - ./.scoter/compose
    - ${scoter:user-dir}/compose
```

A leading `~` is expanded to your home directory.  When there is no project file, `./` entries are left out.

The tools use these lookup paths:

* `dev-container-paths`: where the project configuration's `dev-container: NAME` looks for the `NAME` directory, with its `Dockerfile` and `dev-env.yaml`.  The first directory that has both is used.
