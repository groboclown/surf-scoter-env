# The Universal Configuration File

Because the tooling brings configuration settings from many different places, it provides a program that finds them all and merges them into a universal configuration file, in JSON format.

## Running

The program is `gen-scoter-uc` (built from the [`host`](../../host/) project, with the same configuration code that `scoter-host` uses):

```
gen-scoter-uc [--config PROJECT.yaml] [--project-dir DIR] [--user-setup SETUP.yaml]
              [--distribution-dir DIR] [--universal UNIVERSAL.json] [--no-setup-search]
              [--output FILE]
```

* `--config`: the project configuration file.  Without it, the project parts of the file (`active-config-locations`' `project-config`, `project-dir` and `dev-container-path`, `host-brokered` and `dev-container`) are empty, unless `--universal` brings one along.
* `--project-dir`: the project directory, where the setup files' `./` locations start, in place of the project configuration file's directory.  It must exist.  It doesn't change where the project configuration file's own relative paths (such as `dev-container-path`) start.
* `--user-setup` and `--distribution-dir`: as for the [system configuration files](../system-config-file.md).  `--distribution-dir` wins over a `scoter: paths: distribution-dir` in the setup files.
* `--universal`: a universal configuration file made earlier, to start from.  Its `setup` is used without reading those setup files again.  Its `active-config-locations` `project-config`, `project-dir`, `user-setup-file`, `user-dir` and `distribution-dir` carry over, unless this run gives or finds others; a `--config` without `--project-dir` uses that file's directory.  (`dev-container-path` and `cwd` are worked out again.)  Setup files found by the search that it doesn't already have are added, with their usual priority.
* `--no-setup-search`: don't look for the user's setup file (`$SURF_SCOTER_HOME`, `~/.config`), or for the distribution directory's file when it comes from the setup files.  Files named with `--user-setup` or `--distribution-dir` are still read.
* `--output`: the file to write; without it, the JSON goes to stdout.

If the configuration has problems, the program writes all of them to stderr, one per line, writes no JSON, and exits with 1.  Bad arguments exit with 2.

### Building up the configuration in steps

The `scoter` program, when it isn't told which project file to use, needs the system setup to find it.  Rather than knowing the setup rules itself, it runs `gen-scoter-uc` twice:

```shell
gen-scoter-uc --output first.json
# ... read first.json to find the project file ...
gen-scoter-uc --universal first.json --no-setup-search --config PROJECT.yaml
```

The second run doesn't read the setup files again, but it does expand their settings again, so `./` locations are now relative to the project directory.

## Layout

The general layout:

```json
{
    // Where the configuration in effect came from.  Each might be `null`.  These are also
    // the 'scoter' section's 'paths' of the same names, in 'resolved' below.  (Where the
    // system settings came from, and a history of the runs' inputs, are in 'setup', below.)
    "active-config-locations": {
        // The project configuration file, the project directory (--project-dir, or the
        // project file's directory) and the project's dev container.
        "project-config": "/home/user/my-project/.scoter/settings.yml",
        "project-dir": "/home/user/my-project/.scoter",
        "dev-container-path": "/home/user/my-project/.scoter/dev-env",
        // The user's setup file found, and the user directory it's in (`null` when the file
        // was found as `~/.config/surf-scoter.setup.yaml`).
        "user-setup-file": "/home/user/.config/surf-scoter.d/setup.yaml",
        "user-dir": "/home/user/.config/surf-scoter.d",
        // The distribution directory: --distribution-dir, or else the setup files' setting.
        "distribution-dir": "/home/user/.local/share/surf-scoter",
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
                "dev-container-path": "/home/user/my-project/.scoter/dev-env",
                "distribution-dir": "/home/user/.local/share/surf-scoter",
                "project-config": "/home/user/my-project/.scoter/settings.yml",
                "project-dir": "/home/user/my-project/.scoter",
                "user-dir": "/home/user/.config/surf-scoter.d",
                "user-setup-file": "/home/user/.config/surf-scoter.d/setup.yaml"
            },
            "programs": {},
            "runs": {},
            "path-lists": {
                "dev-containers": ["/home/user/.local/share/surf-scoter/containers"]
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

    "host-brokered": [
        // This is the `commands` section from the project configuration file.
        // It's nearly verbatim, except for the 'program' field, which will be filled with
        // either the path to the executable, or 'null' if not found.
    ],

    "dev-container": {
        // Information straight from the 'dev-env.yaml' file.
        // If no such file exists, this section is instead 'null'.
    },

    // The system setup files' settings, as written (nothing expanded), so a later
    // `gen-scoter-uc --universal` can start from them without reading the files again.
    "setup": {
        // Highest priority first.  'source' is how the file was found: 'user-setup'
        // (--user-setup), 'user' or 'distribution'.
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
                        "values": {}
                    }
                }
            }
        ],

        // A history of the runs' inputs, most recent first.  These are only a record: the
        // values in effect are the `active-config-locations` keys of the same names.  Each holds only the
        // values actually given or found; duplicates are allowed, but removed when written.
        // The user setup files found, and the user directories found.
        "user-setup-file": [
            "/home/user/.config/surf-scoter.d/setup.yaml",
            "/home/user/.config/surf-scoter.setup.yaml"
        ],
        "user-dir": ["/home/user/.config/surf-scoter.d"],
        // The --distribution-dir values.
        "distribution-dir": ["/home/user/.local/share/surf-scoter"],
        // The --config project files.
        "project-config": [
            "/home/user/my-project/.scoter/settings.yml",
            "/home/user/other-project/.scoter-config.yaml"
        ]
    }
}
```

The `resolved` section retains the value type above the value name, because users of the values have an expectation for the value's purpose.  They can fetch the value with a required expectation of the returned shape and usage pattern.

Notes on the values:

* Paths are absolute.  `dev-container-path` has symbolic links resolved.
* `active-config-locations` and the `resolved` `scoter` section's `paths` of the same names always hold the same values (a `null` location is left out of `paths`).
* Every `resolved` entry has `paths`, `programs`, `runs`, `path-lists` and `values`, possibly empty.
* `setup` is for `gen-scoter-uc`; read the expanded values from `resolved`.
* `paths` and `path-lists` entries have `~` and `./` already resolved, and are given whether or not they exist.
* `values` keep their types: text, numbers, `true`/`false` and `null`.  Only text has its `${...}` references expanded.
* Each `runs` command is a list of arguments, the first being the program.  The quotes from the setup file are already removed, and the `{x:name}` placeholders are left in place: fill them in within each argument, so a value with spaces stays a single argument.
* `host-brokered` and `dev-container` keep their keys as written, though not necessarily in the same order.
