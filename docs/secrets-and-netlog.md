# Secrets, and Seeing the Network

A tool in the dev container often needs a credential: a token for a package registry, or for an API.  Put the token in the container, and anything in the container can read it, and send it anywhere it can reach.  The seekrit network gateway gives you another way: the container holds only a placeholder, such as `{{seekrit:GITHUB_TOKEN}}`, and the gateway puts your real value in its place as the request leaves, only toward the host your configuration names.

This page shows how to set it up, and how to see what the container does through the network with `scoter netlog`, whichever gateway you run.  The [seekrit gateway's attack vectors](security/network-gateway-seekrit-attack-vectors.md) cover what it protects, and what it doesn't.


## Choosing the seekrit gateway

Squid is the default gateway.  It only sees which host a connection goes to, and can't fill in secrets.  To use the seekrit gateway, copy its settings, which the distribution's `setup.yaml` has in a comment under `dev-env`, into your own [setup file](system-config-file.md):

```yaml
dev-env:
    paths:
        network-gateway: ${scoter:distribution-dir}/containers/network-gateway-seekrit
    values:
        network-gateway-image: ${dev-env-image-prefix}network-gateway-seekrit
        network-gateway-name: seekrit
        network-gateway-config-mount: /user-space/gateway/seekrit-proxy.toml
        network-gateway-reads-tls: 'yes'
    runs:
        network-gateway-write-config:
            - ${python:script}
            - ${scoter:distribution-bin-dir}/gateway_seekrit.py
            - '{x:universal}'
            - '{x:output}'
        network-gateway-post-start:
            - ${python:script}
            - ${scoter:distribution-bin-dir}/gateway_postrun_seekrit.py
            - '{x:universal}'
            - '{x:gateway-container}'
```

The gateway is part of what a pull records, so pull your projects again after the change.  The first run builds the gateway's image, which compiles seekrit-proxy: it takes a while.

What changes:

* **`tls` rules** work as with Squid: the connection passes through untouched.
* **`https` rules** are read: the gateway checks each request's method, and can fill in secrets.  For that, the dev container trusts a certificate authority made for each run; tools find it through `SSL_CERT_FILE`, `REQUESTS_CA_BUNDLE`, `CURL_CA_BUNDLE`, `GIT_SSL_CAINFO` and `NODE_EXTRA_CA_CERTS`.  A tool with its own trust store, such as Java's, needs the CA (`$SCOTER_GATEWAY_CA`) added to it.
* **Wildcard hosts** (`*.example.com`) are refused: list each host.


## Naming a secret in the project

A rule names the secrets the gateway may put into requests to its host:

```yaml
egress:
  - host: api.github.com
    protocol: https
    methods: [GET]
    secrets: [GITHUB_TOKEN]
```

Only an `https` rule, for a host named exactly, can carry secrets.  The review at `scoter pull` shows which secrets go to which host, and asks again when that changes.

Give a rule with secrets the fewest methods it needs.  Anything in the container can send the placeholder to that host, with those methods, and the gateway fills it in wherever it is in the request, the body included: with `POST`, code could put your token into a public gist.


## Your secret store

The values come from your secret store, a TOML file that's yours alone, never the project's.  By default it's `~/.config/surf-scoter.d/secrets/PROJECT-ID.toml` (the project id is the project directory's name and a hash, such as `my-project-c593c7ec2d55`; the setup's `project-state: paths: secret-store` sets it).  Make it readable only by you:

```toml
[secrets]
# A command that prints the value, run on your computer as you:
GITHUB_TOKEN = { command = ["gh", "auth", "token"] }
# Or the value itself (it's then in a plain file: prefer a command):
OTHER_KEY = { value = "..." }
```

```shell
chmod 600 ~/.config/surf-scoter.d/secrets/my-project-c593c7ec2d55.toml
```

Instead of `[secrets]`, it can hold a seekrit.dev service token, which gives the values itself: `seekrit-token = { command = ["pass", "show", "seekrit/my-project"] }`.

When the environment starts, `scoter` reads the names the rules give, gets each value, and hands them to the gateway, into its memory only.  A name the store doesn't have stops the run before anything is sent.  The store must be outside the project and its clone, a file you own, and not a symlink; a command's program must be found outside the project.


## Using a secret in the container

Each ssh session has an environment variable for each secret the rules name, holding its placeholder:

```shell
$ printenv GITHUB_TOKEN
{{seekrit:GITHUB_TOKEN}}
$ curl -H "Authorization: Bearer $GITHUB_TOKEN" https://api.github.com/user
```

The gateway replaces the placeholder in the request on its way to `api.github.com`.  Toward any other host, or with a method the rule doesn't give, the request is refused.  If the host echoes the value back, the gateway scrubs it from the response.

If the gateway restarts, it loses the values: a request with a placeholder gets `503 secrets not loaded yet` until you give them again:

```shell
scoter gateway-post-start
```


## Seeing the network: `scoter netlog`

While an environment runs, `scoter netlog` shows what the container did through the gateway:

```
Hosts:
  api.github.com:443                       intercept     2
  example.com:443                          deny          1 (no_rule)
  pypi.org:443                             tunnel        3
Requests the gateway read:
  GET api.github.com/user -> 200  x1  with GITHUB_TOKEN
  POST api.github.com/user -> 403  x1
Secrets put into requests:
  GITHUB_TOKEN                       1
Refused hosts.  If the container needs one, its rule goes in the project's `egress` (and is reviewed at the next pull); for example:
  - {host: example.com, protocol: tls}
```

With Squid, it shows the hosts reached and refused; with the seekrit gateway, also the requests it read, what they got, and the secrets put in, by name only.  For each refused host, it suggests the rule that would allow it; it never changes your configuration.

* `scoter netlog --follow` shows each event as it happens, until you stop it.  Where your container tool can't follow a log (Podman on WSL2), it reads the log again every few seconds; to follow it directly, set the setup's `docker: runs: logs-follow` to `${exec} logs --follow {x:name}`.
* `scoter netlog --json` gives the events as data.
* `--host` picks an environment other than the project's.
