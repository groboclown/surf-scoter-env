# The Configuration File

Surf Scoter stores its configuration in a YAML formatted file, `project.yaml`, in a `.surf-scoter.d` directory.  Everything Surf Scoter reads from your project lives in `.surf-scoter.d`: this file, and your development container directories.  The *project directory* is the one holding `.surf-scoter.d`; it's usually the root of your repository, but needn't be.  The directory's name comes from the [system configuration](system-config-file.md)'s `scoter` `values: project-files-dir`; `.surf-scoter.d` is the default, and the name these pages use.

The `scoter` tool looks for a project directory, one holding `.surf-scoter.d/project.yaml`, from the current directory (or the one given with `--project-dir`) up to the root of the repository it's in, never above it (see [Version control roots](system-config-file.md#version-control-roots)).  The file's name comes from the system configuration's `values: project-file-name`; `project.yaml` is the default.

For safety, the tools refuse to read the project file when it, or its `.surf-scoter.d` directory, is a symlink, or when the file isn't a regular file (a FIFO or device, say).

This file tells the Scoter tools which development container the project uses.  It's meant to be shared by everyone working on the project, so settings for a particular computer (such as which executables to use) belong in the [system configuration files](system-config-file.md) instead.

The file is only a suggestion: its host commands and egress rules do nothing until you accept them.  `scoter verify` shows them, resolved for your computer (each command's program as an absolute path, each `${section:key}` worked out), with what the checks found as `#scoter:` comments, and asks whether to use them, edit them or deny them.  When you've accepted the project before (on this branch, if you have; expired acceptances count), the review compares with that acceptance: what changed since is shown in full, with your earlier version beside anything the project changed; what didn't change is one line; and your own changes, notes (`$comment`) and overrides are kept.  The review starts with what your setup changes about the host command checks (`--skip-setup-review` leaves that out), and is shown through `PAGER` when it's set; `[v]iew` shows it again.  Edit opens the review in `VISUAL`, or else `EDITOR` (on Windows, or else Notepad), and after you save it, it's read and checked again before you're asked again.  Pressing Enter denies.  It needs a terminal; `scoter verify --output FILE` only writes the review.  Your acceptance is kept in your `verification: paths: store`, never in the project, and later runs use the settings exactly as you accepted them.  `scoter revoke` lists your acceptances of the project, and revokes them by branch, by fingerprint, or all (`--all`), so the next run asks for a review again.  `scoter list-accepted` lists every project you've accepted, with each acceptance's id and expiry, and any exemption; `scoter revoke ID` revokes one from anywhere, and `scoter revoke $(scoter list-accepted -q)` revokes them all.  `scoter run` won't start a project you haven't accepted, and says why (never accepted, expired, your setup changed, or which files changed), and builds its container from the copy of `.surf-scoter.d` kept with your acceptance, not from the files in the project.  The review can also choose one of your own containers instead of the project's (`dev-container: use: {name: NAME}`, found outside the project), or replace the images the project's Dockerfiles start from (`images`).  While you work on the project's container, `scoter exempt` lets runs use the container files as they are, without a review, for a day (on the current branch); `scoter verify --with-exemption` picks up changes to the project file and `dev-env.yaml` files meanwhile, when the checks refuse nothing.  See [Exemptions](system-config-file.md#exemptions).

You can also use this with the [scoter-host](../host/) executable to describe ways the container can communicate with the host ("host bypass").  This allows you to grant very limited access to additional privileges that the container normally shouldn't do (access to credentials, the Docker daemon, and so on).  Because those actions run on the host, everything they touch on the file system is restricted to the project's clone (or a throwaway temporary directory).

**Caution!**  Only allow commands that truly need the host's privileges: a command that works inside the container, or that the user can run on the host themselves, doesn't belong here.  Don't allow `find` just because it makes things a little easier.

## Example

```yaml
# optional; how the project's files are kept, for the `scoter` script (see "Version control")
vcs: git
container:
  # The development container: a directory with a Dockerfile and dev-env.yaml,
  # relative to this file, inside .surf-scoter.d.
  dev-container-path: ./dev-env
  # Or, instead, the name of one found in the system configuration's `scoter` `path-lists: dev-containers-search`.
  # dev-container: dev-env
  # optional; where the project's clone is mounted in the container
  # (default: the dev container's `mounts: project-clone`)
  mounts:
    project-clone: /src/my-project
# optional; the network destinations the container needs (see "Egress")
egress:
  - host: registry.npmjs.org
    protocol: tls
  - host: '*.pkg.dev'
    protocol: tls
# optional; the environment of every host command (see "Host command environment")
env:
  set:
    HOME: '@HOME@'
# What clients may run: one entry per allowed form of a command.
commands:
  - command: aws
    args: [s3, cp, {any: {prefix: "s3://some-name/"}}, {path: {within: cwd}}]
  - command: aws-build
    # optional; the name of the program to run on the host (default: `command`)
    program: aws
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
      allowed-project-clone-dirs:
        - services/*
        - infra/**
```

## Version control

`vcs` is optional text (not empty) that tells the `scoter` script how the project's files are kept, such as `git`, so it can set up the project for the development container.  The host tools don't interpret it; they check that it's text, and pass it along in the universal configuration file's `project`.  Quote values that YAML would otherwise read as a number or `true`/`false`.

## The development container

The `container` section sets exactly one of:

* `dev-container-path`: a directory, relative to this file, holding the container's `Dockerfile` and its `dev-env.yaml` file (see [the developer environment Dockerfile](project-dockerfile.md)).
* `dev-container`: the name of such a directory, found in one of the `scoter` section's `path-lists: dev-containers-search` directories of the [system configuration](system-config-file.md).  The standard installation provides `dev-env`.

The container directory, and every parent its `dev-env.yaml` builds on, must be inside `.surf-scoter.d` or one of the `dev-containers-search` directories, once symlinks are resolved.  The directory is the container's build context, so keep files you don't want in the image, such as `.env` files and keys, out of it.

`mounts: project-clone` (optional) says where the project's clone is mounted in the container, in place of the dev container's own `mounts: project-clone` (`/src/my-project`, say, rather than `/project`).  It's a container path, held to the same rules as the dev container's mount points (see [the `dev-env.yaml` file](project-dockerfile.md#the-dev-envyaml-file)), and it can't be at, or inside, the container's other mounts.  It only moves where the container sees the clone: on the host, the clone is always the setup's `project-state: paths: project-clone-dir` itself.

The `dev-env.yaml` file says how to run the container, including where the broker is inside it (`broker-bin`).  The command that starts the broker comes from the system configuration's `docker` section (its `runs: broker` key).

## Egress

`egress` lists the network destinations the development container needs.  The rules say what's needed, not how a gateway provides it; the gateway enforces what it can of each rule, and refuses to start with a rule it can't enforce.  The standard gateway (Squid) enforces `tls` rules on port 443; it refuses `http`, `https` and other ports for now.  Each rule has:

* `host` (required): a host name, such as `registry.npmjs.org`, or `*.` and a host name for its subdomains (`*.pkg.dev` allows `us.pkg.dev`, not `pkg.dev` itself).  Lower-case letters, digits and `-`, in at least two parts.  No IP addresses.  Write a name with non-ASCII letters in its `xn--` form, so the file says exactly what the review shows (a Cyrillic `a`, `\u0430`, in `github.com` can't hide).  A bare `*` is refused, and so is `*.` on a top-level domain (`*.com`), or on a domain where anyone can have a name (`*.github.io`, `*.co.uk`; the system configuration's `verification: value-lists: egress-refused-wildcards`).
* `protocol` (required):
    * `tls`: a TLS connection, such as HTTPS, that the gateway sees only the host name of.
    * `https`: HTTP over TLS that the gateway looks inside, so it can enforce `methods`.  It needs a gateway that does that; the standard one doesn't.
    * `http`: plain HTTP.  Anyone between you and the host can read and change it, downloads included, so prefer `tls`.
* `port` (optional): 443 for `tls` and `https`, 80 for `http`, by default.
* `methods` (optional, `http` and `https` only): the HTTP methods allowed, in upper case, such as `[GET, HEAD]`.  A `GET` still carries data in its address and headers, so this limits what the container can do (no uploads, say); it doesn't stop it sending data out.

## Host commands

`command` is the name the client was run as in the container.  `program` is the name of the program the host runs (by default, the same as `command`).  Both are names, never paths: the host finds the program by its name, through your system configuration's [`command-map`](system-config-file.md#host-command-checks), or else on its `PATH`.  A name is ASCII without spaces or control characters, doesn't start with `-`, and has no `/`, `\` or `:`, and isn't a Windows device name (such as `NUL` or `COM1`).  A request is allowed by the first entry with that command whose `args` match (and whose current working directory (`cwd`) allows the client's directory, when the entry has one).

Without `cwd`, the process runs in a new temporary directory.  With `cwd`, it runs in the host directory matching the client's working directory.  That directory must be inside the project clone once symlinks are resolved, and its resolved path must match one of the `allowed-project-clone-dirs` patterns:

* Patterns are relative to the project clone; `.` is the clone itself, and a leading `./` is allowed.
* `*` matches any characters within one directory name, and `?` matches one character.
* `**`, as a whole directory name, matches any number of directories, including none: `infra/**` allows `infra` too, and `**` allows the whole project clone.
* Quote patterns that start with `*`; YAML reads a bare `*...` as an alias.

`args` is a list of tokens, matched one-to-one against the client's arguments.  A string token must match exactly.  A map token is a placeholder, `any` or `path`.

`any` matches a value, with these options:

* `prefix`: the value must start with this text.
* `allow-leading-dash`: allow values starting with '-' (normally refused, so a placeholder can't be used to slip in an option).  Text given in `prefix` doesn't count; only the part the client chose is checked.
* `allow-empty`: allow an empty value (or nothing after the prefix).

`path` matches a file path, which the host process receives as the resolved host path.  `within` says where it may be:

* `{path: {within: cwd}}`: at or below the client's working directory.
* `{path: {within: project-clone, allowed: [build/**, '*.txt']}}`: in the project clone, matching one of `allowed` (patterns like `allowed-project-clone-dirs`, but they may name files too).
* `{path: {within: temp}}`: a throwaway file in a temporary directory, deleted when the process exits.  The client's value only picks the file name, and only with `keep-name: true`; otherwise the name is generated.  Kept names may not contain separators or characters Windows refuses, nor be `.`, `..` or a Windows device name like `NUL`.

Client paths are relative to the client's working directory, or absolute container paths; either way they must be inside the project clone's mount.  They are resolved on the host following symlinks, and the result must still be in the project clone (and match the `within` rule).  Paths in the persistent store, or anywhere else in the container, are refused: there's no host directory to map them to.  A path that doesn't exist yet (such as an output file) is allowed when its directory exists.  `allow-leading-dash` is also a `path` option.

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

`@NAME@` is the host's `NAME` environment variable, as `scoter-host` sees it when it starts (it's left as written in the universal configuration file, which another program makes):

* On Windows hosts, the name is matched without regard to case, as Windows does (`@PATH@` finds `Path`).
* When the host doesn't have the variable, it's the system configuration's `host` section's `NAME` key, matched exactly, if that's set.  So each computer can supply values its environment doesn't have.
* Otherwise it's empty.
* `NAME` is letters, digits, `_`, `(` and `)`.  Any other `@` is kept as it is (so `me@example.com` is fine), and `@@` stands for `@`.
* YAML can't start a plain value with `@`, so quote such values: `HOME: '@HOME@'`.

So a command that needs the host's `HOME` or `PATH` must be given them with `set`, as in the example above.  The command's program is found by its name on the host (see [Host commands](#host-commands)), whatever its environment says.

## Keys

Keys are kebab-case.  Unknown keys are rejected, so typos don't silently fall back to defaults.

No key or value can have a control character, such as a newline or a tab: none of them needs one, and a newline in a value could look like another line when you review the file.  Duplicate keys are refused, and so are YAML merge keys (`<<`), which the tools don't apply.  YAML aliases (`*name`) are expanded, within a limit, so a file can't grow into something huge.
