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
  # Or, instead, the name of one found in the system configuration's `scoter` `path-lists: dev-containers-search`.
  # dev-container: dev-env
# optional; the environment of every host command (see "Host command environment")
env:
  set:
    HOME: '@HOME@'
# What clients may run: one entry per allowed form of a command.
commands:
  - command: aws
    args: [s3, cp, {any: {prefix: "s3://some-name/"}}, {path: {within: cwd}}]
  - command: aws
    # optional; the executable to run on the host (default: `command`, found on the PATH)
    program: /usr/local/bin/aws
    args: [codebuild, start-build, --project-name, {any: {}}]
    # optional; added to the top-level `env`
    env:
      allow: ['AWS_*']
      drop: [AWS_SESSION_TOKEN]
      error: ['AWS_SECRET_*']
      default:
        AWS_REGION: us-east-1
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
* `dev-container`: the name of such a directory, found in one of the `scoter` section's `path-lists: dev-containers` directories of the [system configuration](system-config-file.md).  The standard installation provides `dev-env`.

The `dev-env.yaml` file says how to run the container, including where the broker is inside it (`broker-bin`).  The command that starts the broker comes from the system configuration's `docker` section (its `runs: broker` key).

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

## Host command environment

A host command starts with an **empty** environment: it doesn't get the host's environment variables, and it gets none of the container's unless you allow them.  The `env` blocks say what it gets.  A top-level `env` applies to every command, and a command's own `env` adds to it.

```yaml
env:
  allow: [LANG, 'LC_*']
  drop: [LC_IDENTIFICATION]
  error: ['AWS_SECRET_*']
  set:
    HOME: '@HOME@'
    PATH: '@PATH@'
  default:
    LANG: C.UTF-8
```

All four parts are optional:

* `allow`: the container's variables that come through, as name patterns.
* `drop`: name patterns of allowed variables that don't come through after all.  For example, `allow: ['AWS_*']` with `drop: [AWS_SESSION_TOKEN]` passes every `AWS_` variable but the session token.
* `error`: name patterns that refuse the request when the container sends a matching variable, whether or not it's allowed.  Use this for values the container should never have, such as credentials.
* `set`: variables always set, whatever the container sends.
* `default`: variables set when the container's value doesn't come through (because the container didn't send it, or it isn't allowed).

In the patterns, `*` matches any characters and `?` matches one character; a pattern matches the whole name, case and all.  Quote patterns that start with `*`, as YAML reads a bare `*...` as an alias.

A command's `env` adds its `allow`, `drop` and `error` patterns to the top-level ones, and its `set` and `default` values replace the top-level ones of the same name.

`set` and `default` values can refer to the [system configuration](system-config-file.md) as `${section:key}`, which gives a `paths` value, the program found for a `programs` value, or a `values` value.  That keeps settings for a particular computer out of this shared file.  `$${` stands for `${`.  A reference to something that isn't set is an error when the configuration is loaded.

`@NAME@` is the host's `NAME` environment variable:

* On Windows hosts, the name is matched without regard to case, as Windows does (`@PATH@` finds `Path`).
* When the host doesn't have the variable, it's the system configuration's `host` section's `NAME` key, matched exactly, if that's set.  So each computer can supply values its environment doesn't have.
* Otherwise it's empty.
* `NAME` is letters, digits, `_`, `(` and `)`.  Any other `@` is kept as it is (so `me@example.com` is fine), and `@@` stands for `@`.
* YAML can't start a plain value with `@`, so quote such values: `HOME: '@HOME@'`.

So a command that needs the host's `HOME` or `PATH` must be given them with `set`, as in the example above.  The command itself is still found on the host's `PATH`, whatever its environment says.

## Keys

Keys are kebab-case.  Unknown keys are rejected, so typos don't silently fall back to defaults.
