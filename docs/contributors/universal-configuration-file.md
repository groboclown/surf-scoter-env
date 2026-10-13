# The Universal Configuration File

Because the tooling brings configuration settings from many different places, it provides a program that finds them all and merges them into a universal configuration file, in JSON format.

The program is `gen-scoter-uc` (built from the [`host`](../../host/) project, with the same configuration code that `scoter-host` uses):

```
gen-scoter-uc [--config PROJECT.yaml] [--user-setup SETUP.yaml] [--distribution-dir DIR] [--output FILE]
```

* `--config`: the project configuration file.  Without it, the project parts of the file (`project-config`, `dev-container-path`, `host-brokered` and `dev-container`) are empty.
* `--user-setup` and `--distribution-dir`: as for the [system configuration files](../system-config-file.md).
* `--output`: the file to write; without it, the JSON goes to stdout.

If the configuration has problems, the program writes all of them to stderr, one per line, writes no JSON, and exits with 1.  Bad arguments exit with 2.

The general layout:

```json
{
    // Provenance for where these settings came from.  Helps with user debugging.
    "project-config": "/home/user/my-project/.scoter/settings.yml",  // might be `null`
    "system-configs": ["/home/user/.config/surf-scoter.settings.yaml"], // ordered list
    "dev-container-path": "/home/user/my-project/.scoter/dev-env", // might be `null`.

    // Lookup paths pull from the system configuration, the '*-paths' keys.
    "lookup-paths": {
        "docker-compose": ["/home/user/my-project/.scoter/compose", "/etc/surf-scoter.d/compose"]
    },

    // Every section of the system configuration, including the generated 'scoter' section.
    // 'paths' are expanded file locations; the executables ('exec' and '*-cmd') are replaced
    // with the best-match executable from the PATH.  'run' holds the cli patterns, each as its
    // list of arguments.  If any
    // executable in a section can't be found, the whole section is set to `null` to indicate
    // that it was declared but not found.
    "sections": {
        "docker": {
            "paths": {"exec": "/usr/sbin/podman"},
            "run": {"build": ["/usr/sbin/podman", "-t", "{x:tag}", "-f", "{x:dockerfile}", "{x:dirname}"]}
        },
        "ssh": {
            "paths": {
                "config-file": "/home/user/.ssh/config",
                "exec": "/usr/bin/ssh",
                "home": "/home/user/.ssh",
                "key-file": "/home/user/.ssh/scoter-dev-container"
            },
            "run": {}
        },
        "scoter": {
            "paths": {
                "install-dir": "/home/user/.local/share/surf-scoter",
                "user-setup-file": "/home/user/.config/surf-scoter.setup.yaml"
            },
            "run": {}
        },
        // Couldn't find the 'my-hoodoo' command.
        "my-hoodoo": null
    },

    "host-brokered": [
        // This is the `commands` section from the project configuration file.
        // It's nearly verbatim, except for the 'program' field, which will be filled with
        // either the path to the executable, or 'null' if not found.
    ],

    "dev-container": {
        // Information straight from the 'dev-env.yaml' file.
        // If no such file exists, this section is instead 'null'.
    }
}
```

Notes on the values:

* Paths are absolute.  `dev-container-path` has symbolic links resolved.
* Every `sections` entry has both `paths` and `run`, possibly empty.
* `lookup-paths` are keyed without their `-paths` suffix, with `~` and `./` already resolved.
* Each `run` command is a list of arguments, the first being the program.  The quotes from the setup file are already removed, and the `{x:name}` placeholders are left in place: fill them in within each argument, so a value with spaces stays a single argument.
* `host-brokered` and `dev-container` keep their keys as written, though not necessarily in the same order.
