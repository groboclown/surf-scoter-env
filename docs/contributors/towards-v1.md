# Towards v1

The basic ideas for this tool exist, so now they just need some solidification and consolidation before a V1 release.

## Center on `scoter.py`

The [`scoter.py`](../../scoter.py) script (will be named `scoter` when installed) works as the user's front-end to everything.  It replaces the existing top-level shell scripts.  It works as glue code for file modification and running other programs.  Because of that, it's left as a script so that people can open it up for inspection and do things manually if they want.

## Configuration setup

Rather than pass all the values to `scoter`, it should collect them from the installation directory, the user configuration directory, and other places.  However, this becomes a hassle to handle, especially in a script that may not have proper format loader libraries.

As an alternative, the tooling uses a "universal configuration file" that joins all the different pieces into a single place.  The installation comes with a program that constructs this from all the different kinds of invocation arguments and settings.

### Configurable as much as possible

The tools should read everything they need from the universal configuration file.  Only the barest defaults are used to find the setup locations, and everything else leads from that discovery.

The developer environment construction also comes from this.

## Installation directory

This leads us to a proper installation directory:

* `surf-scoter/scoter` - the user script.  Expect to link this into `~/.local/bin` or equivalent user-land space.
* `surf-scoter/bin` - hosts the binary files specific to Surf Scoter.  These aren't intended for direct end-user invocation.  Right now, this will just contain the `scoter-host` executable.
* `surf-scoter/containers` - the [`containers`](../../containers/) directory.
* `surf-scoter/defaults` - files that the `scoter` script reads at runtime if the user doesn't have an override.
* `surf-scoter/docs` - tasty end-user documents.

## Changes to the development environment container

The dev-env container will need to be configurable, so that the user can replace it with an appropriate custom container if needed.

With this change, the user will need to provide a configuration file that describes how to work with the container.

Additionally, the container will now take 2 mount points - the project workspace directory (writable), and a read-only setup directory that contains runtime configuration files.  Along with the SSH and SSL connection information, it will also take a list of the replaced commands from the project configuration file tha the container should run `ln "client-bin" "/usr/local/bin/cmd-name`.

The `scoter` script will read the JSON to build the setup files.

* It needs the username and SSHD port  to ensure the `~/.ssh/config` has a Host entry with the right username and port.
* The mount points.
* The location of the broker process in the container.

## Changes to the project configuration file

The project configuration file needs additional handlers for environment variables.  Host commands now start from an empty environment, and an `env` block (top-level for every command, and per command, added to the top-level one) says what goes in it:

* `allow:` a list of glob-matching environment variable names that may pass through from the container.  Nothing passes through unless it's listed, so letting a value through is always intentional.
* `drop:` a list of glob-matching names that don't pass through after all, as an addendum to `allow:`.  For example, `allow: ["foo*"]` with `drop: ["foo*bar"]`.
* `set:` a map of `name: value` settings to put into the execution's environment variables.
* `default:` a map of `name: value` to set a value if the container's value didn't pass through.
* `error:` a list of glob-matching environment variable names to trigger a failure if the container tried to set them.  For example, `AWS_ACCESS_KEY`.

The `set:` and `default:` values can use `${section:key}` references to the universal configuration file's scalar values (`paths`, `programs` and `values`), plus `@ENV_NAME@` for a host environment variable.  That falls back to the system configuration's `host` section value when the host doesn't have it, and is empty when neither does.  On Windows hosts, the environment variable lookup ignores case, as Windows does, but the `host` section lookup and the `allow` / `drop` / `error` patterns are case sensitive.

There's deliberately no built-in list of protected variables: it would lead to subtle differences in developer experiences.

See [the project configuration file](../project-configuration-file.md#host-command-environment).
