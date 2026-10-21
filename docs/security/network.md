# The Runtime Network

The development container runs code you didn't write: packages pulled from registries, build plugins, and agents acting on your behalf.  Surf Scoter treats the network around it as a closed room with one supervised door.  This document describes the properties that room must have while an environment runs, and how traffic moves through it.

Related pages:

* [Network attack vectors](network-attack-vectors.md): what's at risk, and the weak points we know about.
* [The development container](dev-container.md): the room's occupant, and the one place untrusted code runs.
* [User and default settings](settings.md): the compose file, firewall and gateway that build the room.  You can replace all of them.


## Required properties

Whatever settings you run with, the network must keep these properties.  The [settings page](settings.md) shows how the shipped defaults provide each one, and what to recheck if you change them.

| | Property | Meaning |
| --- | --- | --- |
| **N1** | Single exit | The dev container has no route anywhere except the gateway.  A program that ignores the proxy settings fails rather than connecting directly. |
| **N2** | Named destinations | Outbound connections go only to the hosts the project's egress rules allow, on TCP port 443. |
| **N3** | No private reach | Nothing in the environment reaches private, loopback, link-local, multicast or reserved addresses: not your host, not your LAN, not cloud metadata services. |
| **N4** | Defense in depth | Kernel firewall rules, not just the proxy, enforce the port and address limits of N2 and N3.  A subverted proxy can't widen them. |
| **N5** | No listeners | Nothing outside the environment can open a connection into it.  No container publishes a port. |
| **N6** | Fail closed | The firewall rules are in place before the gateway carries traffic.  If a piece is missing or misconfigured, traffic stops rather than flowing freely. |
| **N7** | Separate environments | Each running environment has its own isolated network, which no other environment can join. |
| **N8** | No side channels | DNS and other services the container runtime provides don't offer another path out. |


## The layout

Each environment runs three containers:

```
  +---------------- isolated network (internal) ----------------+
  |                                                             |
  |   dev ---- HTTP CONNECT :3128 ----> firewall namespace      |
  |   (your tools)                      +- gateway  (Squid)     |
  |                                     +- firewall (nftables)  |
  +---------------------------------------------+---------------+
                                                | uplink network
                                                V
                                       public internet, TCP/443 only
```

* **`dev`**, the [development container](dev-container.md), is attached only to the `isolated` network, which the container runtime gives no route out (N1).
* **`firewall`** owns a network namespace attached to both networks.  It is the only container allowed to change network rules, and it installs them at startup (N4, N6).
* **`gateway`** runs a proxy inside the firewall's namespace without any privileges.  It checks each request against the host list made from the egress rules (N2), and every connection it makes passes through the firewall's rules (N3, N4).

The [settings page](settings.md) describes each piece's configuration in detail.


## How traffic moves

### Outbound: through the gateway only

Tools in the dev container find the gateway through the standard proxy environment variables (`HTTPS_PROXY` and friends).  Those variables are a convenience, not a control.  Code in the container can unset or change them, but it gains nothing by doing so, because the gateway is the only place it can reach (N1).

The gateway accepts only `CONNECT` requests for an allowed hostname on port 443.  There's no TLS interception and no private certificate authority.  Once the tunnel opens, the traffic is an opaque TLS stream between the tool and the server, which keeps private certificates out of the container and means the gateway sees *where* data goes, not *what* it is.

The allowed hosts come from the project's egress rules (`egress` in its [project file](../project-configuration-file.md#egress)), as you accepted them with `scoter verify`.  Until you accept them, there are none, and nothing gets out.  At launch, `scoter` has the setup's `network-gateway: runs: write-config` turn them into the gateway's configuration, which the gateway mounts read-only from the resources directory; the dev container never sees it.  A rule the gateway can't enforce stops the launch: it's never dropped, or loosened into something broader.  The Squid gateway enforces `tls` rules on port 443; it refuses `http`, `https` and other ports, which need more than it has.  The project file in the workspace has no effect (see [file access](files.md)).

### Inbound: SSH through the runtime

Your IDE and terminal reach the container over SSH, but not through a published port (N5).  The SSH `ProxyCommand` that `scoter` writes into `~/.ssh/config` runs `docker exec -i CONTAINER socat STDIO TCP:127.0.0.1:PORT`.  The connection travels through the container runtime's exec channel to the container's loopback interface, where `sshd` listens.  Nothing on your network can connect to it.  The [dev container page](dev-container.md#ssh) covers what the SSH server allows.

### Port forwarding

IDE port forwarding, such as VS Code's **Forward a Port**, rides inside that SSH connection.  It lets your host reach a server running in the container.  It doesn't let the container reach your host, but whatever the container serves on that port then reaches your host's `localhost`, and your browser if you point it there.

### The host bridge

The [host bridge](host-bridge.md) also uses the container runtime's exec channel, not the network.  It isn't a network path out, but a command it runs on the host has the host's full network access.

### Image builds

The network described here exists only while an environment runs.  Building the dev container image happens outside it; see [the dev container](dev-container.md#building-the-image).
