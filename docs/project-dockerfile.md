# The Developer Environment `Dockerfile`

Your [project configuration file](project-configuration-file.md) defines the development environment container (defaults to the [ini](user-ini-file.md) file stack setting).  This container runs in a small container network to isolate it from the rest of your system.

You can optionally specify a `Dockerfile` for that environment that lays on top of the base [`dev-env`](../containers/dev-env) container.  Please take care when doing so, to better make your journey safe.


# The `Dockerfile`

The build reads the file named `Dockerfile` in the container directory, and only that one: a `Containerfile` beside it, or a `Dockerfile.dev`, is just another file in the build context.

When you accept the project, a later change to the `Dockerfile` that only adds, removes or edits comments or blank lines doesn't ask for another review.  Any other change does, trailing whitespace included.  A `Dockerfile` with a control character (such as a lone carriage return), a Unicode line separator, or a heredoc Surf Scoter can't follow (such as `<<"END-1"`) asks for a review on any change, even to a comment.  Use plain `<<EOF`-style heredoc delimiters, and LF or CRLF line endings.

## Dos

While the actual execution of the container will force the user to run as a non-privileged one, it's good practice to finish off your container with:

```Dockerfile
USER ubuntu
```

## Don'ts


## The project clone

The container will have a clone of your project mounted to it, read-write, at its `mounts: project-clone` (or where the project file's `container: mounts: project-clone` says).  The clone is of the repository holding the project, from its root.  It's the only directory host commands can work on.

## The persistent store

The container will have its *persistent store* mounted to it, read-write, at its `mounts: persistent-store`.  It's a volume in the container tool, kept between runs, for the container's own files: its IDE server, caches, shell history.  It isn't a directory on the host, so host commands can't reach it.  The image must have a directory at the mount point that the container's user can write, since a new volume takes its owner from it.

The distribution's `dev-env` container keeps the home directory entries listed in `/etc/scoter/persistent-store.txt` in the store, linked from the home directory, and takes everything else in the home directory fresh from the image on each start.  An image built on it can add lines to that file:

```Dockerfile
USER root
RUN printf '.cargo/registry/\n.cargo/git/\n' >> /etc/scoter/persistent-store.txt
USER ubuntu
```

Keep the store to state and caches.  Anything there that the container runs, such as shell start-up files or programs on the `PATH`, outlives a rebuild of the image, so an earlier session's change to it outlives the session.

## The `setup` directory

The container will have a special "setup" directory mounted to it as read-only.  This will contain these files:

* `ssh_host_ed25519_key` - the host key for the SSHD server to report.
* `authorized_keys` - the user-space SSH authorized key file, which contains the public key for the host connecting to the container.
* `host_cmds.txt` - list of command names (one per line) to link the client-broker binary as.
* *Future updates will also include a root authority certificate for the SSL proxy.*


# The `dev-env.yaml` file

Beside the `Dockerfile` must exist the `dev-env.yaml` file.  This YAML formatted file provides basic information for the `scoter` tool to understand how to configure and run the container cluster around it.

Once its [parents](#parents) are merged in, the file must provide at a minimum these values:

```yaml
user: USERNAME
mounts:
    project-clone: "PATH"
    persistent-store: "PATH"
    setup: "PATH"
broker-bin: "PATH"
sshd-port: 22022
```

The keys are:

* `"user"` - the user that the container runs as.  In particular, this must also align as the user that the `sshd` process will accept requests.  It's written into your `~/.ssh/config`, so it must be a plain user name: a letter or `_`, then letters, digits, `.`, `_` or `-`, at most 32 characters.
* `"mounts": "project-clone"` - the directory in the container to mount the [project clone](#the-project-clone).
* `"mounts": "persistent-store"` - the directory in the container to mount the [persistent store](#the-persistent-store).
* `"mounts": "setup"` - the directory in the container to mount the [setup directory](#the-setup-directory).
* `"broker-bin"` - the path to the broker binary.  The host service will connect to this file.

The mount points and `broker-bin` are written into files and command lines on your host, so each must be an absolute container path: `/`-separated names of ASCII letters, digits, `.`, `_` and `-`, without `.` or `..` names, a trailing `/`, or more than 255 characters, and not `/` itself.  The three mount points must differ, and none may be inside another.
* `"sshd-port"` - the port number on the loopback (localhost) interface that the SSHD process listens for connections.  It may be `null` (or left out) only when `sshd-port-env` is set; the container then picks the port itself when it starts.
* `"sshd-port-env"` - optional: the name of an environment variable given to the container, set to the SSHD port.  With a `sshd-port`, the container is told that port; without one, it is told the port picked for it at run time.
* `"parents"` - optional: the dev-env images this one builds on.  See [below](#parents).

Other keys are allowed, and passed along as written.


## Parents

A dev-env image often builds on another: a common base that every developer needs, with the project's own tools on top.  The `parents` list says what this image builds on, so its `dev-env.yaml` need only give what differs.

```yaml
parents:
    # A parent built locally, from its container directory, before this one.
    # The tag must match the `FROM` line in this Dockerfile.
    - dir: ../base-env
      tag: my-base:latest
    # A parent pulled from a repository: only its dev-env.yaml is read, and
    # this Dockerfile's `FROM` pulls the image.
    - file: ../vendor/published-env/dev-env.yaml
    # A parent built locally, found by name in the `dev-containers-search`
    # directories, such as one your Surf Scoter installation provides.
    - name: dev-env
      tag: local-surf-scoter-env/dev-env:build
# Overrides of the parents' values.
sshd-port: 2222
```

Each entry is one of:

* `dir` and `tag` - a local container directory, with its own `Dockerfile`, built with that tag before this image.  Use several for a multi-stage `Dockerfile` with several local parents.  Its `dev-env.yaml` is optional: without one, the parent adds no values and has no parents to build; say, a project's CI build image, with this `dev-env.yaml` adding the SSH setup on top.
* `name` and `tag` - a local parent like `dir`, found by name: the first of the system configuration's `dev-containers-search` directories holding `NAME/Dockerfile` (its `NAME/dev-env.yaml` is optional, as for `dir`).  Use it to build on a container your installation, or your own setup, provides, without a path that only works on one computer.  `NAME` is a single directory name.
* `file` - a published parent's `dev-env.yaml`.  It isn't built, and neither are any parents it lists, as its image already holds them.  (Reading it from a URL may come later.)

Paths are relative to the directory holding this `dev-env.yaml`.  Once symlinks are resolved, each `dir`, `name` and `file` parent must be inside the project's `.surf-scoter.d` or one of the system configuration's `dev-containers-search` directories (see [the system configuration](system-config-file.md)), so a project can't build from, or read, anything else on your computer.

Parents may have parents of their own, and the same parent may be reached more than once, but no image may be its own parent, and a local parent always has the same tag.

The values merge in order: each parent's (with its own parents' merged in), in the order listed, then this file's.  A later value wins.  Mappings, such as `mounts`, merge key by key; any other value replaces the earlier one, and an explicit `null` replaces it with `null` (so `sshd-port: null` undoes a parent's port).

Some common shapes:

* **Self-contained** - no `parents`; the file gives every value.
* **A local base doing the work** - only `parents: [{dir: ..., tag: ...}]`.
* **A published base** - only `parents: [{file: ...}]`.
* **A local base with changes** - the parent, plus the values that differ, such as `sshd-port`.
* **Several local bases** - several `dir` entries, for a multi-stage `Dockerfile`.

The tools resolve all of this when reading the file: the merged values, with `parents` replaced by `parent-builds`, the list of local parents to build as `{"dir": "/absolute/dir", "tag": "image-tag"}`, in build order: the lowest first, the immediate parents last.  Don't write `parent-builds` yourself.
