# v0 Architecture

The first version of Surf Scoter contains an assembly of pieces to allow for construction of the containers and remote access.  I expect the project to eventually automate this construction.

## Layers

Basic terminology:

* **Host** the computer you interact with.
* **Container** a virtual computer running through a technology like Docker or Podman.
* **Container mount** a directory on the *host* computer that the *container* also has access to.  This can have various read-write access restrictions.
* **Remote clone** when using a distributed version control system, you have a clone of the repository that connects to the remote copy.

### Development Environment

At the root is the development environment.  It's a container that comes with all the build goodies installed.  `javac`, `cargo`, `npm`, `python3`, or whatever you need to build, test, and run your project.

This runs a service back-end, like SSH, that allows the IDE to connect into the container.  VS Code and JetBrains allow for remote coding, and they connect through this interface.  Some IDEs, like VS Code, allow for direct interaction with the container.  JetBrains Gateway supports connecting to a Linux environment over SSH.

For external network connections, it can only connect to the [network gateway](#network-gateway).  This requires the environment to install a local root certificate (CA) to allow for outbound SSL connections.

The development environment shares a working directory of the source, so that you can then commit it back to your main repository.

One thing to note with this environment: we don't want it to contain credentials.  Agents can use these in ways you don't expect, and malware can steal it.  To help with this, the environment includes:

* The container runs as a local user without admin escalation.  If you want extra system tools installed, you need to rebuild the image.  However, things like `npm` and Python's virtual environments allow for user-space installation of tools.
* The working directory that stores the source runs with a container mount with read/write access.  However, we store it as a local clone of the host's remote clone of that repository.
    * This requires extra steps to cherry pick the commits then commit them as signed.
* Allow inbound connections for service testing, so that you can still use your local browser to try out code you're developing.
* The container does not grant access to the host's docker socket.

### Network Gateway

Rather than giving the environment raw access to the Internet, we want to put tight controls around what we allow it to do.  Additionally, we want to have methods to monitor what it tries to do.  For v1, a simple approach works better.


### Firewall + Router

The network gateway doesn't directly connect to the Internet.  Instead, it connects to yet another container that has low-level firewall + routing rules for the network traffic.  This runs in the same network as the gateway, in order to separate privileges - the Squid server doesn't have network admin access.

The current firewall rules have:

| Traffic | Action |
| --- | --- |
| Outbound TCP/443 to public IPv4 | Allow |
| Outbound to private IPv4 networks | Deny |
| Outbound to loopback/link-local addresses | Deny, except local namespace traffic |
| Outbound UDP/443 (QUIC) | Deny |
| Outbound arbitrary TCP/UDP ports | Deny |
| Docker embedded DNS | Allow |
| IPv6 external traffic | Deny |
| Inbound proxy connections from isolated subnet | Allow TCP/3128 |
| Other inbound connections | Deny |
| Forwarded traffic | Deny |
