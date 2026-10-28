# The Universal Configuration File

Because the tooling brings configuration settings from many different places, it provides a program that finds them all and merges them into a universal configuration file, in JSON format.  This is affectionately called the Scoter Universal Configuration file, or "SUC" for short.

## Running

The program is `gen-scoter-uc` (built from the [`host`](../../host/) project, with the same configuration code that `scoter-host` uses):

```
gen-scoter-uc [--project-dir DIR] [--vcs-root DIR] [--project-branch NAME]
              [--project-clone-dir DIR]
              [--dev-container-settings-dir DIR] [--distribution-dir DIR]
              [--universal UNIVERSAL.json] [--no-setup-search]
              [--accepted-settings ACCEPTED.json] [--review REVIEW.yaml]
              [--previous-review REVIEW.yaml] [--output FILE]
```

* `--vcs-root`: the root of the repository holding the project, which `scoter` finds from the setup's VCS sections (see [Version control roots](../system-config-file.md#version-control-roots)).  It must exist and hold the project directory.  It's the `scoter` location `vcs-root`; setup files can't be read from inside it.  Without a `--project-dir` this time, a `--universal` file's carries over.
* `--project-branch`: the project's branch, which `scoter` gets from the VCS section's `runs: current-branch`.  It's made safe as one path part and set as the `scoter` value `project-branch` (see [the `scoter:` section](../system-config-file.md#the-scoter-section)).  A `--universal` file's carries over while the project directory stays the same.  The generator also works out the `scoter` value `project-id` from the project directory, the same as the verified store's name for it.
* `--project-dir`: the project: the directory holding the project files directory, with the project file in it (`scoter: values: project-files-dir` and `project-file-name` in the setup say where).  It must exist, and the setup files' `./` locations start there.  Without it, the project parts of the file (`active-config-locations`' `project-config`, `project-dir`, `vcs-root` and `dev-container-path`, `current-project-setup`, `host-brokered` and `dev-container`) are empty, unless `--universal` brings them along.
* `--project-clone-dir` and `--dev-container-settings-dir`: the host directories mounted in the launched dev container: the project's clone (writable, at the dev container's `mounts: project-clone`, or the project file's `container: mounts: project-clone`), and the settings directory with the runtime configuration files (read-only).  The program that launches the container gives these; the configuration files can't set them.  They're made absolute, but needn't exist yet.  Without them, they're `null`, unless `--universal` brings them along.  The persistent store isn't among them: it's a container volume, not a host directory.
* `--distribution-dir`: as for the [system configuration files](../system-config-file.md).  It wins over a `scoter: paths: distribution-dir` in the setup files.  The other setup file is the user's, found through `$SURF_SCOTER_HOME` or `$XDG_CONFIG_HOME` (by default `~/.config`); no setup file is ever read from a project.
* `--universal`: a universal configuration file made earlier, to start from.  Its `setup` is used without reading those setup files again.  Its `active-config-locations` `project-config`, `project-dir`, `project-clone-dir`, `dev-container-settings-dir`, `user-setup-file`, `user-dir` and `distribution-dir` carry over, unless this run gives or finds others.  Its `distribution-bin-dir` carries over too, as long as the distribution directory stays the same, so another program can change it in the file.  (`dev-container-path` and `cwd` are worked out again.)  Setup files found by the search that it doesn't already have are added, with their usual priority.
* `--no-setup-search`: don't look for the user's setup file (`$SURF_SCOTER_HOME`, `$XDG_CONFIG_HOME` or `~/.config`), or for the distribution directory's file when it comes from the setup files.  The `--distribution-dir` file is still read.
* `--accepted-settings`: the user's accepted settings for the project: an `accepted.json` in the verified store (`verification: paths: store`; see step 8 of [Projects in v1](projects-v1.md)).  It must be for this run's project, and each command's program must still be an executable file outside the project, its clone and the repository.  `host-brokered` is then built from its `commands`, and only from them; the file goes into `accepted-settings`, and its egress rules into `egress`.  When its `dev-container: use` names one of the user's containers, that container (found in `dev-containers-search` directories outside the project directory and the VCS root, as are its parents) becomes the dev container: `dev-container-path` and `dev-container` are its, not the project's.
* `--previous-review`: an earlier acceptance's review file, from the verified store, which a new review starts from (step 17 of [Projects in v1](projects-v1.md)).  It's parsed as `--review` is, but not checked against the run: it has usually expired.  The file's `previous-review` gets its `file`, and its `data` as the user left it, `$comment`s and `$override`s included.  Nothing is accepted from it.
* `--review`: the user's review file for the project (step 12 of [Projects in v1](projects-v1.md)).  It's read as the project file is, its `project-dir` must be this run's, its `expires-at` in the future, and a `dev-container: use: {name: ...}` one of the user's containers.  The file's `review` gets its `result`, and, unless it's `reject`, the accepted settings it gives, in `accepted.json`'s shape.  Nothing runs from a review: `host-brokered` doesn't change.
* `--output`: the file to write; without it, the JSON goes to stdout.

If the configuration has problems, the program writes all of them to stderr, one per line, writes no JSON, and exits with 1.  Bad arguments exit with 2.

### Building up the configuration in steps

The `scoter` program needs the system setup to find the project, and the project to find the user's acceptance of it.  Rather than knowing the setup rules itself, it runs `gen-scoter-uc` three times:

```shell
gen-scoter-uc --output first.json
# ... run first.json's VCS `find-root` commands, then look for the project
#     directory up to the root ...
gen-scoter-uc --universal first.json --no-setup-search --project-dir DIR --vcs-root ROOT --output second.json
# ... run second.json's `scoter: runs: find-acceptance`, given second.json ...
gen-scoter-uc --universal second.json --no-setup-search --project-dir DIR --vcs-root ROOT \
    --accepted-settings ACCEPTED.json
```

The second run doesn't read the setup files again, but it does expand their settings again, so `./` locations are now relative to the project directory.  The third adds the accepted settings `find-acceptance` named.  Without any (no acceptance matches), there are no host commands and no egress rules until the user accepts a review with `scoter verify`.

`scoter` writes each universal file it passes along into a new directory only the user can use, as a file only they can read, and removes it when it's done.

### Starting `scoter-host`

`scoter-host` takes only a universal configuration file and the dev container's name:

```shell
gen-scoter-uc --project-dir DIR --project-clone-dir CLONE --output universal.json
scoter-host --universal universal.json CONTAINER_NAME
```

Before anything else, it checks:

* the universal configuration file belongs to the user, and nobody else can use it (it decides what runs on the host);
* with `accepted-settings`: its `file` is in the verified store (`verification: paths: store`), private, and still says exactly what `contents` does (a revoked, replaced or edited acceptance doesn't), and the acceptance's `expires-at` hasn't passed.  Only when it starts: a session that's running when its acceptance expires carries on;
* the project directory is the accepted one, and it and the dev container directory are still there.

It reads the universal configuration file and **no other file** (but for the accepted settings' file in the store, to compare):

* the commands it runs are the `host-brokered` list, exactly as written there: each entry's `program` is the program run (`null` makes the command unavailable, and its requests are refused), and its `env` is the command's whole environment plan;
* the system settings come from the file's `setup` record, expanded again;
* the dev container's `dev-env.yaml` from its `dev-container`;
* the locations, `cwd` included, from its `active-config-locations`.

The `current-project-setup` contents are only a record of the project's file, which the review is made from, and which, with `scoter-host`'s log, makes the universal file the main artifact for debugging a run.  `scoter-host` still checks them as a project file, but doesn't take its commands from them.

So `scoter-host` runs with exactly the configuration its invoker made, even if a file changes after the universal file was written, and the universal file alone recreates the run for debugging or testing.  The contents are checked as the files would be, and each must go with its location (`current-project-setup` with `project-config`, `dev-container` with `dev-container-path`).

What isn't in the file comes from where `scoter-host` runs: `@NAME@` environment values are its environment variables (expanded once, when it starts), and the system settings it expands again find programs on its `PATH`, with `~` as its home directory.

The file must have a project and a `project-clone-dir`.  The project clone is the `project-clone-dir` on the host, mounted at the `dev-container`'s `mounts: project-clone` in the container, or at the project file's `container: mounts: project-clone` when it gives one.  Host commands only ever see paths in it.

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
        // The project's id, and its branch made safe as a path part (`scoter` values).
        "project-id": "my-project-3f9a0b1c2d4e",
        "project-branch": "feature-x",
        "dev-container-path": "/home/user/my-project/.surf-scoter.d/dev-env",
        // The host directories mounted in the dev container:
        //   --project-clone-dir - the project's clone
        //   --dev-container-settings-dir - the read-only settings directory
        "project-clone-dir": "/home/user/.config/surf-scoter.d/state/projects/my-project-3f9a0b1c2d4e/project-clone",
        "dev-container-settings-dir": "/home/user/.config/surf-scoter.d/state/projects/my-project-3f9a0b1c2d4e/setup",
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
    // holds lists of expanded file locations, and 'value-lists' lists of expanded text.  'values' holds plain values, with their JSON
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
                "dev-container-settings-dir": "/home/user/.config/surf-scoter.d/state/projects/my-project-3f9a0b1c2d4e/setup",
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
            "verification:shells": {
                "files": [
                    "/home/user/.config/surf-scoter.d/setup.yaml",
                    "/home/user/.local/share/surf-scoter/setup.yaml"
                ],
                "action": "refuse",
                "reason": "A shell runs whatever command it's given, ...",
                "commands": {"bash": "/usr/bin/bash", "sh": "/usr/bin/sh", "ksh": "/usr/bin/ksh"},
                "name-patterns": [], "options": [], "env": [], "paths": [], "overrides": []
            },
            "verification:ksh-is-mine": {
                "files": ["/home/user/.config/surf-scoter.d/setup.yaml"],
                "action": "warn",
                "reason": "I review the ksh scripts this project's commands run.",
                "commands": {"ksh": "/usr/bin/ksh"},
                "name-patterns": [], "options": [], "env": [], "paths": [],
                "overrides": ["verification:shells"]
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
            "bash": {"program": "/usr/bin/bash", "checks": ["verification:shells"]},
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
            "bash": {"action": "refuse", "checks": ["verification:shells"], "overridden": []},
            "docker": {"action": "warn", "checks": ["docker:docker"], "overridden": []},
            "ksh": {
                "action": "warn",
                "checks": ["verification:ksh-is-mine"],
                "overridden": [{"check": "verification:shells", "by": "verification:ksh-is-mine"}]
            }
        },
        // Things to tell the user: a check removed or replaced (which misses what the lower
        // priority file adds to it later), or an override that does nothing.  See below.
        "notices": [
            {
                "message": "/home/user/.config/surf-scoter.d/setup.yaml replaces `verification:shells` from /home/user/.local/share/surf-scoter/setup.yaml without its commands `nu`; ...",
                "id": "check-replaced-without",
                "parameters": {"check": "verification:shells", "field": "commands", "entries": ["nu"]},
                "sources": [
                    "/home/user/.config/surf-scoter.d/setup.yaml",
                    "/home/user/.local/share/surf-scoter/setup.yaml"
                ]
            }
        ]
    },

    // The user's accepted settings, from the verified store, which 'host-brokered' and
    // 'egress' were made from: the store's file, and its contents (see step 8 of
    // projects-v1.md).  'null' without them.
    "accepted-settings": {
        "file": "/home/user/.config/surf-scoter.d/verified/my-project-3f9a0c17b2e4/acceptances/20261020T150405Z-7c1e/accepted.json",
        "contents": {
            "$schema": "surf-scoter-accepted-settings-1",
            "project-dir": "/home/user/my-project",
            "expires-at": "2026-11-19T15:04:05+00:00",
            "vcs": "git",
            "gateway": null,
            "egress": [{"host": "registry.npmjs.org", "protocol": "tls", "port": 443, "methods": []}],
            "commands": []
        }
    },

    // The accepted egress rules, and the gateway they were accepted for.  'null' without
    // accepted settings.
    "egress": {
        "gateway": null,
        "rules": [{"host": "registry.npmjs.org", "protocol": "tls", "port": 443, "methods": []}]
    },

    // The user's review file, when --review gives it: its `result` ('accept' or 'reject'),
    // and the accepted settings it gives ('null' when rejected), for the checks to judge
    // before anything is stored.  'null' without one.
    "review": {
        "file": "/tmp/surf-scoter-x1/review.yaml",
        "result": "accept",
        "accepted": {"$schema": "surf-scoter-accepted-settings-1", "...": "..."}
    },

    // With --previous-review: an earlier acceptance's review file, and its data as the user
    // left it, which a new review starts from.  'null' without one.
    "previous-review": {
        "file": "/home/user/.config/surf-scoter.d/verified/proj-1a2b3c4d5e6f/acceptances/.../review.yaml",
        "data": {"review": "surf-scoter-review-1", "egress": [], "commands": [], "...": "..."}
    },

    // The commands `scoter-host` runs: the accepted settings' `commands`.  Without accepted
    // settings, empty.
    "host-brokered": [
        // This is the `commands` section from the project configuration file.
        // It's nearly verbatim, except for two fields, so each entry says exactly what it does:
        //  * 'program' is filled with either the absolute path of the executable (which
        //    `scoter-host` runs as it is, never looking it up), or 'null' if not
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

    // The project configuration file, as written: the project's suggestion, which the review
    // is made from.  If there's none, this is 'null'.
    "current-project-setup": {
        // `vcs` is for the `scoter` script; the host tools don't use it.
        "vcs": "git",
        // `container: mounts: project-clone`, when given, is where the clone is mounted in
        // the container, in place of the dev container's own.
        "container": {"dev-container-path": "./dev-env", "mounts": {"project-clone": "/src/my-project"}},
        "egress": [{"host": "registry.npmjs.org", "protocol": "tls"}],
        "commands": [
            // ...
        ]
    },

    // The 'dev-env.yaml' file, with its parents merged in.  If no such file
    // exists, this section is instead 'null'.
    "dev-container": {
        "user": "ubuntu",
        "mounts": {
            "project-clone": "/project",
            "persistent-store": "/persistent-store",
            "setup": "/opt/surf-scoter-setup"
        },
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

* Paths are absolute.  `dev-container-path` has symbolic links resolved.  `project-clone-dir` is the host path of the clone; in the container, it's at the `current-project-setup`'s `container: mounts: project-clone` when that's given, else at the `dev-container`'s `mounts: project-clone`.
* `active-config-locations` and the `resolved` `scoter` section's `paths` of the same names always hold the same values (a `null` location is left out of `paths`).
* Every `resolved` entry has `paths`, `programs`, `runs`, `path-lists`, `value-lists` and `values`, possibly empty.  A section's `host-commands` checks aren't in `resolved`: they're in `host-command-checks`, combined with every other section's.
* Every `host-command-checks` check has all its fields, possibly empty.  A check a setup file removes (with `null`) isn't in `checks`; the `check-removed` notice says so.  `commands` and `command-map` give the program found, as a `programs` value does, and `gen-scoter-uc` refuses (with an error) a program in the project directory, the project files directory, the project's clone or the repository, or a command two checks map to different programs.
* `setup` is for `gen-scoter-uc`; read the expanded values from `resolved`.
* `paths` and `path-lists` entries have `~` and `./` already resolved, and are given whether or not they exist.
* `values` keep their types: text, numbers, `true`/`false` and `null`.  Only text has its `${...}` references expanded.
* Each `runs` command is a list of arguments, the first being the program.  The quotes from the setup file are already removed, and the `{x:name}` placeholders are left in place: fill them in within each argument, so a value with spaces stays a single argument.
* `current-project-setup`, `host-brokered` and `dev-container` keep their keys as written, though not necessarily in the same order.  `dev-container` is the merge of the `dev-env.yaml` and its parents, so always has `sshd-port`, `sshd-port-env` and `parent-builds` (possibly `null` or empty), and never `parents`.

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

