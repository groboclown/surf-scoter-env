# The Universal Configuration File

Because the tooling brings configuration settings from many different places, it provides a program that finds them all and merges them into a universal configuration file, in JSON format.  This is affectionately called the Scoter Universal Configuration file, or "SUC" for short.

## Running

The program is `gen-scoter-uc` (built from the [`host`](../../host/) project, with the same configuration code that `scoter-host` uses):

```
gen-scoter-uc [--project-dir DIR] [--vcs-root DIR]
              [--dev-container-workspace-dir DIR]
              [--dev-container-settings-dir DIR] [--distribution-dir DIR]
              [--universal UNIVERSAL.json] [--no-setup-search] [--output FILE]
```

* `--vcs-root`: the root of the repository holding the project, which `scoter` finds from the setup's VCS sections (see [Version control roots](../system-config-file.md#version-control-roots)).  It must exist and hold the project directory.  It's the `scoter` location `vcs-root`; setup files can't be read from inside it.  Without a `--project-dir` this time, a `--universal` file's carries over.
* `--project-dir`: the project: the directory holding the project files directory, with the project file in it (`scoter: values: project-files-dir` and `project-file-name` in the setup say where).  It must exist, and the setup files' `./` locations start there.  Without it, the project parts of the file (`active-config-locations`' `project-config`, `project-dir`, `vcs-root` and `dev-container-path`, `project`, `host-brokered` and `dev-container`) are empty, unless `--universal` brings them along.
* `--dev-container-workspace-dir` and `--dev-container-settings-dir`: the directories mounted in the launched dev container: the project workspace (writable), and the settings directory with the runtime configuration files (read-only).  The program that launches the container gives these; the configuration files can't set them.  They're made absolute, but needn't exist yet.  Without them, they're `null`, unless `--universal` brings them along.
* `--distribution-dir`: as for the [system configuration files](../system-config-file.md).  It wins over a `scoter: paths: distribution-dir` in the setup files.  The other setup file is the user's, found through `$SURF_SCOTER_HOME` or `~/.config`; no setup file is ever read from a project.
* `--universal`: a universal configuration file made earlier, to start from.  Its `setup` is used without reading those setup files again.  Its `active-config-locations` `project-config`, `project-dir`, `dev-container-workspace-dir`, `dev-container-settings-dir`, `user-setup-file`, `user-dir` and `distribution-dir` carry over, unless this run gives or finds others.  Its `distribution-bin-dir` carries over too, as long as the distribution directory stays the same, so another program can change it in the file.  (`dev-container-path` and `cwd` are worked out again.)  Setup files found by the search that it doesn't already have are added, with their usual priority.
* `--no-setup-search`: don't look for the user's setup file (`$SURF_SCOTER_HOME`, `~/.config`), or for the distribution directory's file when it comes from the setup files.  The `--distribution-dir` file is still read.
* `--output`: the file to write; without it, the JSON goes to stdout.

If the configuration has problems, the program writes all of them to stderr, one per line, writes no JSON, and exits with 1.  Bad arguments exit with 2.

### Building up the configuration in steps

The `scoter` program needs the system setup to find the project.  Rather than knowing the setup rules itself, it runs `gen-scoter-uc` twice:

```shell
gen-scoter-uc --output first.json
# ... run first.json's VCS `find-root` commands, then look for the project
#     directory up to the root ...
gen-scoter-uc --universal first.json --no-setup-search --project-dir DIR --vcs-root ROOT
```

The second run doesn't read the setup files again, but it does expand their settings again, so `./` locations are now relative to the project directory.

### Starting `scoter-host`

`scoter-host` takes only a universal configuration file and the dev container's name:

```shell
gen-scoter-uc --project-dir DIR --dev-container-workspace-dir WORKSPACE --output universal.json
scoter-host --universal universal.json CONTAINER_NAME
```

It reads the universal configuration file and **no other file**:

* the commands it runs are the `host-brokered` list, exactly as written there: each entry's `program` is the program run (`null` makes the command unavailable, and its requests are refused), and its `env` is the command's whole environment plan;
* the system settings come from the file's `setup` record, expanded again;
* the dev container's `dev-env.yaml` from its `dev-container`;
* the locations, `cwd` included, from its `active-config-locations`.

The `project` contents are only a record of what the `host-brokered` list was made from, which, with `scoter-host`'s log, makes the universal file the main artifact for debugging a run.  `scoter-host` still checks them as a project file, but doesn't take its commands from them.

So `scoter-host` runs with exactly the configuration its invoker made, even if a file changes after the universal file was written, and the universal file alone recreates the run for debugging or testing.  The contents are checked as the files would be, and each must go with its location (`project` with `project-config`, `dev-container` with `dev-container-path`).

What isn't in the file comes from where `scoter-host` runs: `@NAME@` environment values are its environment variables (expanded once, when it starts), and the system settings it expands again find programs on its `PATH`, with `~` as its home directory.

The file must have a project and a `dev-container-workspace-dir`.  The workspace is the `dev-container-workspace-dir` on the host, mounted at the `dev-container`'s `mounts: workspace` in the container.

(`gen-scoter-uc --universal`, on the other hand, reads the project file and `dev-env.yaml` again, so it can pick up their changes.)

## Layout

The general layout:

```json
{
    // Where the configuration in effect came from.  Each might be `null`.  These are also
    // the 'scoter' section's 'paths' of the same names, in 'resolved' below.  (Where the
    // system settings came from are in 'setup', and a history of the runs' inputs is in
    // 'loaded-from', below.)
    "active-config-locations": {
        // The project configuration file, the project directory (--project-dir, or the
        // directory holding the project file's .surf-scoter.d) and the project's dev container.
        "project-config": "/home/user/my-project/.surf-scoter.d/project.yaml",
        "project-dir": "/home/user/my-project",
        "vcs-root": "/home/user/my-project",
        "dev-container-path": "/home/user/my-project/.surf-scoter.d/dev-env",
        // The host directories mounted in the dev container:
        //   --dev-container-workspace-dir - the cloned project workspace
        //   --dev-container-settings-dir - the read-only settings directory
        "dev-container-workspace-dir": "/home/user/my-project",
        "dev-container-settings-dir": "/home/user/.cache/surf-scoter/my-project",
        // The user's setup file found, and the user directory it's in (`null` when the file
        // was found as `~/.config/surf-scoter.setup.yaml`).
        "user-setup-file": "/home/user/.config/surf-scoter.d/setup.yaml",
        "user-dir": "/home/user/.config/surf-scoter.d",
        // The distribution directory: --distribution-dir, or else the setup files' setting.
        "distribution-dir": "/home/user/.local/share/surf-scoter",
        // The distribution's programs: `${distribution-dir}/bin`, unless another program
        // changed it in an earlier universal file with the same distribution directory.
        "distribution-bin-dir": "/home/user/.local/share/surf-scoter/bin",
        // The directory gen-scoter-uc ran in.
        "cwd": "/home/user/my-project"
    },

    // Every section of the system configuration, including the 'scoter' section with the
    // tool's 'paths'.
    // 'paths' are expanded file locations.  'programs' are the best-match executables from
    // the PATH.  'runs' holds the cli patterns, each as its list of arguments.  'path-lists'
    // holds lists of expanded file locations.  'values' holds plain values, with their JSON
    // types.  A program that can't be found is `null`, as is every value that needs it; the
    // tool using a `null` value reports the problem.  Paths are given whether or not they
    // exist: the tool using them checks.
    "resolved": {
        "docker": {
            "paths": {},
            "programs": {"exec": "/usr/sbin/podman"},
            "runs": {"build": ["/usr/sbin/podman", "-t", "{x:tag}", "-f", "{x:dockerfile}", "{x:dirname}"]},
            "path-lists": {
                "compose": ["/home/user/my-project/.scoter/compose", "/etc/surf-scoter.d/compose"]
            },
            "values": {"retries": 3}
        },
        "ssh": {
            "paths": {
                "config-file": "/home/user/.ssh/config",
                "home": "/home/user/.ssh",
                "key-file": "/home/user/.ssh/scoter-dev-container"
            },
            "programs": {"exec": "/usr/bin/ssh"},
            "runs": {},
            "path-lists": {},
            "values": {"port": 22022, "strict-host-key-checking": false}
        },
        "scoter": {
            "paths": {
                "cwd": "/home/user/my-project",
                "dev-container-path": "/home/user/my-project/.surf-scoter.d/dev-env",
                "dev-container-settings-dir": "/home/user/.cache/surf-scoter/my-project",
                "dev-container-workspace-dir": "/home/user/my-project",
                "distribution-bin-dir": "/home/user/.local/share/surf-scoter/bin",
                "distribution-dir": "/home/user/.local/share/surf-scoter",
                "project-config": "/home/user/my-project/.surf-scoter.d/project.yaml",
                "project-dir": "/home/user/my-project",
                "user-dir": "/home/user/.config/surf-scoter.d",
                "user-setup-file": "/home/user/.config/surf-scoter.d/setup.yaml"
            },
            "programs": {},
            "runs": {},
            "path-lists": {
                "dev-containers-search": [
                    "/home/user/my-project/.surf-scoter.d",
                    "/home/user/.local/share/surf-scoter/containers"
                ]
            },
            "values": {}
        },
        "my-hoodoo": {
            // Couldn't find the 'my-hoodoo' command, so it and the command using it are `null`.
            "paths": {"config": "/home/user/.config/hoodoo.conf"},
            "programs": {"exec": null},
            "runs": {"cast": null},
            "path-lists": {},
            "values": {}
        }
    },

    // Every section's `host-commands` checks, put together (see the system configuration
    // file's "Host command checks").  Checks a setup file removes (null) aren't here.
    "host-command-checks": {
        // Each check, by `section:key`, expanded: 'commands' holds `commands` and `command-map`
        // together, each name with the executable it runs (`null` when it isn't there), and
        // 'paths' are expanded file locations.  'files' are the setup files that set this
        // `section:key`, highest priority first: the check is the first one's alone (a check's
        // fields aren't merged), and the others hold the versions it hides.
        "checks": {
            "host-bridge:shells": {
                "files": [
                    "/home/user/.config/surf-scoter.d/setup.yaml",
                    "/home/user/.local/share/surf-scoter/setup.yaml"
                ],
                "action": "refuse",
                "reason": "A shell runs whatever command it's given, ...",
                "commands": {"bash": "/usr/bin/bash", "sh": "/usr/bin/sh", "ksh": "/usr/bin/ksh"},
                "name-patterns": [], "options": [], "env": [], "paths": [], "overrides": []
            },
            "host-bridge:ksh-is-mine": {
                "files": ["/home/user/.config/surf-scoter.d/setup.yaml"],
                "action": "warn",
                "reason": "I review the ksh scripts this project's commands run.",
                "commands": {"ksh": "/usr/bin/ksh"},
                "name-patterns": [], "options": [], "env": [], "paths": [],
                "overrides": ["host-bridge:shells"]
            },
            "docker:docker": {
                "files": ["/home/user/.local/share/surf-scoter/setup.yaml"],
                "action": "warn",
                "reason": "An allowed `docker` command can start a container ...",
                "commands": {"docker": "/usr/sbin/podman"},
                "name-patterns": [], "options": [], "env": [], "paths": [], "overrides": []
            }
        },
        // Each command name the checks map, with the program it runs and the checks that map it.
        "command-map": {
            "bash": {"program": "/usr/bin/bash", "checks": ["host-bridge:shells"]},
            "docker": {"program": "/usr/sbin/podman", "checks": ["docker:docker"]}
        },
        // For each mapped command, what the checks that match on its name alone make of it, in
        // two steps.  First, the checks without `overrides` that match it: the most restrictive
        // of their actions.  Then each override for the command takes the place of the check it
        // overrides, and the action is the most restrictive of what's left.  'checks' are the
        // checks the action comes from; 'overridden' are the matching checks an override
        // replaced.  Checks with 'options', 'env' or 'paths' depend on the project's command,
        // so they're left to whoever checks it.
        "commands": {
            "bash": {"action": "refuse", "checks": ["host-bridge:shells"], "overridden": []},
            "docker": {"action": "warn", "checks": ["docker:docker"], "overridden": []},
            "ksh": {
                "action": "warn",
                "checks": ["host-bridge:ksh-is-mine"],
                "overridden": [{"check": "host-bridge:shells", "by": "host-bridge:ksh-is-mine"}]
            }
        },
        // Things to tell the user: a check removed or replaced (which misses what the lower
        // priority file adds to it later), or an override that does nothing.  See below.
        "notices": [
            {
                "message": "/home/user/.config/surf-scoter.d/setup.yaml replaces `host-bridge:shells` from /home/user/.local/share/surf-scoter/setup.yaml without its commands `nu`; ...",
                "id": "check-replaced-without",
                "parameters": {"check": "host-bridge:shells", "field": "commands", "entries": ["nu"]},
                "sources": [
                    "/home/user/.config/surf-scoter.d/setup.yaml",
                    "/home/user/.local/share/surf-scoter/setup.yaml"
                ]
            }
        ]
    },

    "host-brokered": [
        // This is the `commands` section from the project configuration file.
        // It's nearly verbatim, except for two fields, so each entry says exactly what it does:
        //  * 'program' is filled with either the path to the executable, or 'null' if not
        //    found.
        //  * 'env' is the command's whole environment: the project's top-level `env` with
        //    the command's own added, and its `${section:key}` references expanded.  All
        //    five parts are always there.  `@NAME@` host environment values are left as
        //    written: `scoter-host` expands them with its own environment when it starts.
        //    (An `@` from an expanded reference is written `@@`, so it isn't taken for one.)
        {
            "command": "aws",
            "args": ["s3", "ls"],
            "program": "/usr/local/bin/aws",
            "env": {
                "allow": ["LANG", "AWS_*"],
                "drop": ["AWS_SESSION_TOKEN"],
                "error": ["AWS_SECRET_*"],
                "set": {"HOME": "@HOME@", "AWS_CONFIG_FILE": "/home/user/.aws/config"},
                "default": {"AWS_REGION": "us-east-1"}
            }
        }
    ],

    // The project configuration file, as written.  If there's none, this is 'null'.
    "project": {
        // `vcs` is for the `scoter` script; the host tools don't use it.
        "vcs": "git",
        "container": {"dev-container-path": "./dev-env"},
        "commands": [
            // ...
        ]
    },

    // The 'dev-env.yaml' file, with its parents merged in.  If no such file
    // exists, this section is instead 'null'.
    "dev-container": {
        "user": "ubuntu",
        "mounts": {"workspace": "/workspace", "setup": "/opt/surf-scoter-setup"},
        "broker-bin": "/usr/local/bin/scoter-container",
        // 'null' when the container picks the port; then 'sshd-port-env' is set.
        "sshd-port": 22022,
        "sshd-port-env": null,
        // In place of 'parents': the local parent images to build, lowest first.
        "parent-builds": [
            {"dir": "/home/user/my-project/.surf-scoter.d/base-env", "tag": "my-base:latest"}
        ]
    },

    // The system setup files' settings, as written (nothing expanded), so a later
    // `gen-scoter-uc --universal` can start from them without reading the files again.
    "setup": {
        // Highest priority first.  'source' is how the file was found: 'user' or
        // 'distribution'.
        "files": [
            {
                "file": "/home/user/.config/surf-scoter.d/setup.yaml",
                "source": "user",
                "sections": {
                    "docker": {
                        "paths": {},
                        "programs": {"exec": "/usr/sbin/podman"},
                        "runs": {"build": "${exec} -t {x:tag} -f {x:dockerfile} {x:dirname}"},
                        "path-lists": {},
                        "values": {},
                        // Only when the file has some; a null check removes a lower
                        // priority file's.
                        "host-commands": {"shells": null}
                    }
                }
            }
        ]
    },

    // A history of the runs' inputs, most recent first.  These are only a record: the
    // values in effect are the `active-config-locations` keys of the same names.  Each holds
    // only the values actually given or found; duplicates are allowed, but removed when
    // written.  A later `gen-scoter-uc --universal` adds to them.
    "loaded-from": {
        // The user setup files found, and the user directories found.
        "user-setup-file": [
            "/home/user/.config/surf-scoter.d/setup.yaml",
            "/home/user/.config/surf-scoter.setup.yaml"
        ],
        "user-dir": ["/home/user/.config/surf-scoter.d"],
        // The --distribution-dir values.
        "distribution-dir": ["/home/user/.local/share/surf-scoter"],
        // The project files of the --project-dir projects.
        "project-config": [
            "/home/user/my-project/.surf-scoter.d/project.yaml",
            "/home/user/other-project/.surf-scoter.d/project.yaml"
        ]
    }
}
```

The `resolved` section retains the value type above the value name, because users of the values have an expectation for the value's purpose.  They can fetch the value with a required expectation of the returned shape and usage pattern.

The file is written as compact JSON, on one line, to keep it small; `jq .` (or any JSON formatter) lays it out for reading.  The tools refuse a universal configuration file over 4 MB, and `gen-scoter-uc` won't write one.  (The other configuration files are limited to 2 MB each.)

Notes on the values:

* Paths are absolute.  `dev-container-path` has symbolic links resolved.
* `active-config-locations` and the `resolved` `scoter` section's `paths` of the same names always hold the same values (a `null` location is left out of `paths`).
* Every `resolved` entry has `paths`, `programs`, `runs`, `path-lists` and `values`, possibly empty.  A section's `host-commands` checks aren't in `resolved`: they're in `host-command-checks`, combined with every other section's.
* Every `host-command-checks` check has all its fields, possibly empty.  A check a setup file removes (with `null`) isn't in `checks`; the `check-removed` notice says so.  `commands` and `command-map` give the program found, as a `programs` value does, and `gen-scoter-uc` refuses (with an error) a program in the project directory, the project files directory, the workspace or the repository, or a command two checks map to different programs.
* `setup` is for `gen-scoter-uc`; read the expanded values from `resolved`.
* `paths` and `path-lists` entries have `~` and `./` already resolved, and are given whether or not they exist.
* `values` keep their types: text, numbers, `true`/`false` and `null`.  Only text has its `${...}` references expanded.
* Each `runs` command is a list of arguments, the first being the program.  The quotes from the setup file are already removed, and the `{x:name}` placeholders are left in place: fill them in within each argument, so a value with spaces stays a single argument.
* `project`, `host-brokered` and `dev-container` keep their keys as written, though not necessarily in the same order.  `dev-container` is the merge of the `dev-env.yaml` and its parents, so always has `sshd-port`, `sshd-port-env` and `parent-builds` (possibly `null` or empty), and never `parents`.

### Notices

Each `host-command-checks` notice is an object:

* `message`: the text to show the user.
* `id`: what kind of notice it is, below.  With `parameters`, it says the same as `message`, so a tool can show it in another language.  (The tools don't translate them yet; the format is ready for it.)
* `parameters`: the notice's values, by name: each a text, or a list of texts.
* `sources`: the setup files it's about, highest priority first.

| `id` | When | `parameters` | `sources` |
|---|---|---|---|
| `override-target-unset` | A check overrides one that isn't set, so the override does nothing. | `check`, `target` | the override's file |
| `override-not-matched` | A check overrides another for a command, option or environment variable the other doesn't match, so it does nothing for it. | `check`, `target`, `kind` (`command`, `option` or `env`), `name` | the override's file, the target's |
| `check-removed` | A setup file sets a lower priority file's check to `null`. | `check` | the file, the lower priority one |
| `check-replaced-without` | A setup file replaces a lower priority file's check without some of its entries.  One notice for each field. | `check`, `field` (`commands`, `name-patterns`, `options`, `env` or `paths`), `entries` (a list) | the file, the lower priority one |
| `check-replaced-less-restrictive` | A setup file replaces a lower priority file's check with a less restrictive action. | `check`, `action`, `replaced-action` | the file, the lower priority one |

A new kind of notice gets a new `id`.  Tools that don't know an `id` show its `message`.

