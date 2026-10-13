# The Developer Environment `Dockerfile`

Your [project configuration file](project-configuration-file.md) defines the development environment container (defaults to the [ini](user-ini-file.md) file stack setting).  This container runs in a small container network to isolate it from the rest of your system.

You can optionally specify a `Dockerfile` for that environment that lays on top of the base [`dev-env`](../containers/dev-env) container.  Please take care when doing so, to better make your journey safe.


# The `Dockerfile`


## Dos

While the actual execution of the container will force the user to run as a non-privileged one, it's good practice to finish off your container with:

```Dockerfile
USER ubuntu
```

## Don'ts


## The `workspace` directory

The container will have the host's "workspace" directory mounted to it.  This contains a clone of the project root.

## The `setup` directory

The container will have a special "setup" directory mounted to it as read-only.  This will contain these files:

* `ssh_host_ed25519_key` - the host key for the SSHD server to report.
* `authorized_keys` - the user-space SSH authorized key file, which contains the public key for the host connecting to the container.
* `host_cmds.txt` - list of command names (one per line) to link the client-broker binary as.
* *Future updates will also include a root authority certificate for the SSL proxy.*


# The `dev-env.json` file

Beside the `Dockerfile` must exist the `dev-env.json` file.  This JSON formatted file provides basic information for the `scoter` tool to understand how to configure and run the container cluster around it.

The file must provide at a minimum these values:

```json
{
    "user": "USERNAME",
    "mounts": {
        "workspace": "PATH",
        "setup": "PATH"
    },
    "broker-bin": "PATH",
    "sshd-port": 22022
}
```

The keys are:

* `"user"` - the user that the container runs as.  In particular, this must also align as the user that the `sshd` process will accept requests.
* `"mounts": "workspace"` - the directory in the container to mount the [workspace directory](#the-workspace-directory).
* `"mounts": "setup"` - the directory in the container to mount the [setup directory](#the-setup-directory).
* `"broker-bin"` - the path to the broker binary.  The host service will connect to this file.
* `"sshd-port"` - the port number on the loopback (localhost) interface that the SSHD process listens for connections.
