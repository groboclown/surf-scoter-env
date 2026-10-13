# The Configuration File

Surf Scoter stores its configuration in a YAML formatted file.  By convention, this resides in your project directory named `.scoter-config.yaml`.

This file tells the Scoter tools which development container the project uses.  It's meant to be shared by everyone working on the project, so settings for a particular computer (such as which executables to use) belong in the [system configuration files](system-config-file.md) instead.

You can also use this with the [scoter-host](../host/) executable to describe ways the container can communicate with the host ("host bypass").  This allows you to grant very limited access to additional privileges that the container normally shouldn't do (access to credentials, the Docker daemon, and so on).  Because those actions run on the host, everything they touch on the file system is restricted to the workspace (or a throwaway temporary directory).

**Caution!**  Only allow commands that truly need the host's privileges: a command that works inside the container, or that the user can run on the host themselves, doesn't belong here.  Don't allow `find` just because it makes things a little easier.

## Example

```yaml
container:
  # The development container: a directory with a Dockerfile and dev-env.yaml,
  # relative to this file.
  dev-container-path: ./.scoter/dev-env
  # Or, instead, the name of one found in the system configuration's `dev-container-paths`.
  # dev-container: dev-env
# What clients may run: one entry per allowed form of a command.
commands:
  - command: aws
    args: [s3, cp, {any: {prefix: "s3://some-name/"}}, {path: {within: cwd}}]
  - command: aws
    # optional; the executable to run on the host (default: `command`, found on the PATH)
    program: /usr/local/bin/aws
    args: [codebuild, start-build, --project-name, {any: {}}]
  - command: docker
    args: [compose, up]
    # optional; run in the client's directory, when it matches one of these
    cwd:
      allowed-workspace-dirs:
        - services/*
        - infra/**
```

## The development container

The `container` section sets exactly one of:

* `dev-container-path`: a directory, relative to this file, holding the container's `Dockerfile` and its `dev-env.yaml` file (see [the developer environment Dockerfile](project-dockerfile.md)).
* `dev-container`: the name of such a directory, found in one of the `dev-container-paths` directories of the [system configuration](system-config-file.md).  The standard installation provides `dev-env`.

The `dev-env.yaml` file says how to run the container, including where the broker is inside it (`broker-bin`).  The command that starts the broker comes from the system configuration's `docker` section (its `run: broker` key).

## Host commands

`command` is the name the client was run as in the container.  A request is allowed by the first entry with that command whose `args` match (and whose current working directory (`cwd`) allows the client's directory, when the entry has one).

Without `cwd`, the process runs in a new temporary directory.  With `cwd`, it runs in the host directory matching the client's working directory.  That directory must be inside the workspace once symlinks are resolved, and its resolved path must match one of the `allowed-workspace-dirs` patterns:

* Patterns are relative to the workspace directory; `.` is the workspace itself, and a leading `./` is allowed.
* `*` matches any characters within one directory name, and `?` matches one character.
* `**`, as a whole directory name, matches any number of directories, including none: `infra/**` allows `infra` too, and `**` allows the whole workspace.
* Quote patterns that start with `*`; YAML reads a bare `*...` as an alias.

`args` is a list of tokens, matched one-to-one against the client's arguments.  A string token must match exactly.  A map token is a placeholder, `any` or `path`.

`any` matches a value, with these options:

* `prefix`: the value must start with this text.
* `allow-leading-dash`: allow values starting with '-' (normally refused, so a placeholder can't be used to slip in an option).  Text given in `prefix` doesn't count; only the part the client chose is checked.
* `allow-empty`: allow an empty value (or nothing after the prefix).

`path` matches a file path, which the host process receives as the resolved host path.  `within` says where it may be:

* `{path: {within: cwd}}`: at or below the client's working directory.
* `{path: {within: workspace, allowed: [build/**, '*.txt']}}`: in the workspace, matching one of `allowed` (patterns like `allowed-workspace-dirs`, but they may name files too).
* `{path: {within: temp}}`: a throwaway file in a temporary directory, deleted when the process exits.  The client's value only picks the file name, and only with `keep-name: true`; otherwise the name is generated.  Kept names may not contain separators or characters Windows refuses, nor be `.`, `..` or a Windows device name like `NUL`.

Client paths are relative to the client's working directory, or absolute container paths; either way they must be inside the workspace mount.  They are resolved on the host following symlinks, and the result must still be in the workspace (and match the `within` rule).  A path that doesn't exist yet (such as an output file) is allowed when its directory exists.  `allow-leading-dash` is also a `path` option.

Keys are kebab-case.  Unknown keys are rejected, so typos don't silently fall back to defaults.
