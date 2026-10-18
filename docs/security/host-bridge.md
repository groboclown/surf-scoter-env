# The Host Bridge

Some work needs privileges the development container deliberately doesn't have: cloud credentials, the Docker daemon, a hardware token.  The host bridge lets a process in the container ask your host to run a command on its behalf.  It is the one intended way for the container to cause something to happen *outside* it, which makes it the most sensitive part of Surf Scoter.

The bridge is built to say no.  It runs only the exact command forms listed in the project configuration, gives them only the files and environment variables that configuration allows, and treats every byte from the container as hostile.  This document describes how it does that.  [Host bridge attack vectors](host-bridge-attack-vectors.md) analyzes what remains open, and what's your responsibility.


## The pieces

```
  dev container                                   your host
 +--------------------------------------+        +------------------------------+
 | aws s3 cp ...                        |        |                              |
 |  +- scoter-client (linked as "aws")  |        |  scoter-host                 |
 |       | Unix socket (mode 0600)      |        |   +- analyze: allowed?       |
 |       V                              | stdin/ |   +- exec: run on the host   |
 |  scoter-container (the broker) <-----+-stdout-+---- started with             |
 |                                      |        |     `docker exec -i`         |
 +--------------------------------------+        +------------------------------+
```

* **`scoter-client`** ([`client.rs`](../../containers/dev-env/src/client.rs)) is hard-linked in the container under the name of each bridged command.  It sends its working directory, arguments and environment to the host, then relays standard input, output, error, signals and the exit code, so the command feels local.
* **The broker** ([`broker.rs`](../../containers/dev-env/src/broker.rs)) listens on a Unix socket in the container and multiplexes client connections over its standard input and output.  It doesn't read the messages it passes along.
* **`scoter-host`** ([`host.rs`](../../host/src/host.rs)) starts the broker with `docker exec -i`, decides whether each request may run ([`analyze.rs`](../../host/src/host/analyze.rs)), and runs the approved ones ([`exec.rs`](../../host/src/host/exec.rs)).

The command forms come from the `commands` section of the [project configuration file](../project-configuration-file.md#host-commands).


## The controls

### The host makes the connection

`scoter-host` reaches *into* the container through the container runtime's exec channel.  Nothing listens on the host, so there's no port for the container, or anything on your network, to connect to.  When `scoter-host` stops, the bridge is gone.

`scoter-host` reads its settings only from the universal configuration file that `scoter run` generates from your project directory's configuration.  The container's copy of the project configuration, in the workspace, has no effect.

### Every request is untrusted

The command name, arguments, working directory and environment all come from the container.  The client binary and its hard links are a convenience, not a boundary: any process in the container can connect to the socket and send whatever it likes, including a made-up `argv[0]`.  So the host validates the request itself, and the broker sits on the untrusted side.

The wire protocol ([`comm`](../../containers/dev-env/src/comm.rs)) caps every message at 32 MiB, rejects malformed input by closing the connection, and uses length-prefixed binary framing so data can't be mistaken for commands.

### Exact allowlist matching

A request runs only if an entry in `commands` matches it ([`analyze.rs`](../../host/src/host/analyze.rs)):

* The command name must match, and the arguments must match the entry's `args` one-to-one, with the same count.
* Plain string tokens must match exactly.
* An `any` placeholder refuses values starting with `-` unless the entry sets `allow-leading-dash`, so a placeholder can't be used to slip in an option such as `--endpoint-url`.  A `prefix` pins the start of the value.
* The first matching entry wins.  Anything that matches no entry is refused with exit code 126 and a message on the client's standard error.

The container never picks the program.  The host runs the entry's `program`, or the command name found on the *host's* `PATH`.

### Files stay in the workspace

Path arguments (`path` placeholders) are the main defense against using a host command to reach host files:

* A container path must be inside the workspace mount.  It's mapped to the host's workspace directory and resolved, following symlinks, and the result must still be inside the workspace and match the entry's `within` rule.  A symlink in the workspace pointing at `~/.ssh` is refused.
* The host process gets the resolved host path, not the container's text.
* `within: temp` gives the process a fresh file in a private temporary directory instead, which is deleted when the process exits.  The container picks at most the file's name, and only with `keep-name`, after checks for separators, `.`, `..` and Windows device names.

The working directory follows the same rules.  Without a `cwd` rule the process runs in a new, empty temporary directory.  With one, the client's directory is resolved the same way and must match an `allowed-workspace-dirs` pattern.

### An empty environment

A host command starts with no environment variables at all: none of the host's, and none of the container's.  The entry's `env` block adds them back on purpose: `allow` and `drop` choose container variables to pass, `set` and `default` supply values, and `error` refuses the whole request if the container sends a variable it should never have, such as a secret key.  See [host command environment](../project-configuration-file.md#host-command-environment).

### Lifetime

If the client disconnects, the host kills its process.  When the broker's stream closes, `scoter-host` kills every running process and exits.  The broker command never requests a TTY, and the container name is checked so the runtime can't mistake it for an option.
