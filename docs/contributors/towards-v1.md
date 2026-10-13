# Towards v1

The basic ideas for this tool exist, so now they just need some solidification and consolidation before a V1 release.

## Center on `scoter.py`

The [`scoter.py`](../../scoter.py) script (will be named `scoter` when installed) works as the user's front-end to everything.  It replaces the existing top-level shell scripts.  It works as glue code for file modification and running other programs.  Because of that, it's left as a script so that people can open it up for inspection and do things manually if they want.

This leads us to a proper installation directory:

* `surf-scoter/scoter` - the user script.  Expect to link this into `~/.local/bin` or equivalent user-land space.
* `surf-scoter/bin` - hosts the binary files specific to Surf Scoter.  These aren't intended for direct end-user invocation.  Right now, this will just contain the `scoter-host` executable.
* `surf-scoter/containers` - the [`containers`](../../containers/) directory.
* `surf-scoter/defaults` - files that the `scoter` script reads at runtime if the user doesn't have an override.
* `surf-scoter/docs` - tasty end-user documents.

With this change, the way the configuration settings also changes:

* User-wide configuration file.  For easy out-of-the-box Python usage, it's stored as an `.ini` file, with a default path at `~/.config/surf-scoter.ini`.  It's not required, and, not having it, causes `scoter` to assume defaults.  It stores:
    * Executable locations and usage patterns.  For example, which 'docker' executable to use, and how to use it (e.g. podman vs. docker, docker compose, openssh).  The focus here is "use this executable for tool X" and then "run action Y like this with tool X."
    * Installation location.
    * Default overrides, like the name of the project configuration file.
    * Names of the built images in the local docker environment.
* Associate the concept of "a project has a developer environment":
    * The project contains a configuration file + project-relative referenced files.  This allows the Surf Scoter project configuration to ship with the code, and have all contributors use the same setup.
    * The parent dev-env container.  We recommend the `dev-env` `Dockerfile`, but the user can choose something different.  This becomes important if they need to develop with a different set of tools, say `musl` libraries instead of `glibc`.  The parent dev-env container, if overwritten, needs to point to the `Dockerfile` and give the local tag.
    * An optional `Dockerfile`.  It **must** have a `FROM` with the `dev-env` parent container.  The `scoter` tool can optionally change that when building the container, though that's risky.  Instead, the tool should just ensure the two match, and that the project `Dockerfile` just has one `FROM` statement.
    * The file mustn't allow override the user's installation directory.  I expect this file to be shared between project contributors, so it needs to stay relative to the project.
        * This means the host executable paths need to move into the `surf-scoter.ini` file.
    * The existing "allowed commands".
    * Because `scoter` needs to parse this file, the pieces it needs to parse should remain in "trivial yaml format".
      Without this, `scoter` needs a full yaml parser, or the host program + scoter need to use something they both can handle.  `ini` is the easiest for Python, but would make the allowed commands list ugly.

Because the executable forms now live outside the configuration, this means the `host` executable needs to read them some other way.  This can come from the assembled workspace directory.

**Late Note:** the script currently reads `ini` files, because they're easy for users to work with, and Python has a ini parser baked in.  JSON would be better, for its richness, but they're harder for people to work with (lack of comments, for example).  This may be a good reason to require a Yaml parser, or at least create a super trivial in-line one.  Additionally, due to the need of the script to parse the project configuration (a yaml file), it may need it anyway.  Alternatively, the `host` directory could also bundle a config load-up parser that spits out a JSON file; that would unify the configuration code for the host program and make that code cleaner in the script.

## Changes to the development environment container

The dev-env container will need to be configurable, so that the user can replace it with an appropriate custom container if needed.

With this change, the user will need to provide a configuration file that describes how to work with the container.

Additionally, the container will now take 2 mount points - the project workspace directory (writable), and a read-only setup directory that contains runtime configuration files.  Along with the SSH and SSL connection information, it will also take a list of the replaced commands from the project configuration file tha the container should run `ls "client-bin" "/usr/local/bin/cmd-name`.

The `scoter` script will read the JSON to build the setup files.

* It needs the username and SSHD port  to ensure the `~/.ssh/config` has a Host entry with the right username and port.
* The mount points.
* The location of the broker process in the container.