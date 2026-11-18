# Seekrit Gateway Attack Vectors

The weak points of the seekrit network gateway ([`containers/network-gateway-seekrit`](../../containers/network-gateway-seekrit/)), measured against the [network's required properties](network.md#required-properties).  The weak points every gateway shares are on the [network attack vectors](network-attack-vectors.md) page; the default gateway's are on the [Squid gateway](network-gateway-squid-attack-vectors.md) page.  See the [attack vector overview](attack-vectors.md) for how to read the severity and status labels.

The seekrit gateway is [seekrit-proxy](https://github.com/seekritdev/proxy), built from a pinned upstream commit with Surf Scoter's patches.  Unlike Squid, it can look inside TLS: for a `tls` rule it passes the connection through untouched, as Squid does, but for an `https` rule it ends the TLS connection itself, with a certificate from the run's own CA, which the dev container trusts.  It then checks each request against the rule's methods, and replaces a placeholder, `{{seekrit:NAME}}`, with your secret's value on the way out, for the secrets the rule names.  The dev container holds only the placeholder.  That's the point of it, and it's what gives it a larger attack surface: it holds plaintext, a CA key, and your secrets.


## Attacker goals

Beyond the [network's](network-attack-vectors.md#attacker-goals), code in the dev container wants to:

* **learn a secret's value**, to use it from anywhere, not just through the gateway;
* **use a secret** against a host, or in a way, the rule didn't mean;
* **intercept** TLS it wasn't given, with the run's CA.


## Attack surface

| Input | Controlled by | Handled in |
| --- | --- | --- |
| Egress rules, and the secrets each names | Your configuration (the project's suggestion, reviewed at `scoter pull`) | [`gateway_seekrit.py`](../../py/src/dist_bin/gateway_seekrit.py) turns them into seekrit-proxy's configuration |
| `CONNECT` and plain HTTP requests: hosts, ports, methods, paths, headers, bodies, placeholders | Code in the dev container | seekrit-proxy, with [Surf Scoter's patches](../../containers/network-gateway-seekrit/patches/) |
| Responses from read hosts | The upstream servers | seekrit-proxy's response redaction |
| DNS answers for allowed hosts | Whoever runs those domains' DNS | seekrit-proxy's egress gate, [`egress.nft`](../../containers/firewall-router/egress.nft) |
| The secret values, or a seekrit.dev token | Your secret store (`project-state: paths: secret-store`), its commands | [`gateway_postrun_seekrit.py`](../../py/src/dist_bin/gateway_postrun_seekrit.py), with the store and delivery in [`gateway_secrets.py`](../../py/src/dist_bin/surf_scoter_lib/gateway_secrets.py), `docker: runs: exec-stdin`, the gateway's inject socket |
| The run's CA | `scoter run` | [`gateway_start.py`](../../py/src/dist_bin/surf_scoter_lib/gateway_start.py), [`docker-compose.yaml`](../../dist-files/defaults/docker-compose.yaml) |
| The gateway's source | seekrit's upstream, and Surf Scoter's patches | [`network-gateway-seekrit/Dockerfile`](../../containers/network-gateway-seekrit/Dockerfile) |


## Summary

| ID | Vector | Severity | Status |
| --- | --- | --- | --- |
| [SEEKRIT-1](#seekrit-1-a-secret-used-through-the-host-its-rule-allows) | A secret used through the host its rule allows | High | By design |
| [SEEKRIT-2](#seekrit-2-secrets-in-the-gateways-memory-and-their-delivery) | Secrets in the gateway's memory, and their delivery | High | Mitigated |
| [SEEKRIT-3](#seekrit-3-the-interception-ca) | The interception CA | Medium | Mitigated |
| [SEEKRIT-4](#seekrit-4-secrets-echoed-back-in-responses) | Secrets echoed back in responses | Medium | Mitigated |
| [SEEKRIT-5](#seekrit-5-a-subverted-gateway) | A subverted gateway | Medium | Mitigated |
| [SEEKRIT-6](#seekrit-6-the-secret-store-and-its-commands) | The secret store and its commands | Medium | Mitigated |
| [SEEKRIT-7](#seekrit-7-guarantees-that-rest-on-the-translation-and-the-patches) | Guarantees that rest on the translation and the patches | Medium | Mitigated |
| [SEEKRIT-8](#seekrit-8-allowed-names-resolving-to-forbidden-addresses) | Allowed names resolving to forbidden addresses | Medium | Mitigated |
| [SEEKRIT-9](#seekrit-9-the-patched-build) | The patched build | Medium | Mitigated |
| [SEEKRIT-10](#seekrit-10-what-the-log-holds) | What the log holds | Low | Mitigated |


## Vectors

### SEEKRIT-1: A secret used through the host its rule allows

**Severity:** High.  **Status:** By design.
**Where:** the project file's `egress` (`secrets:`, `methods`); the warning in [`checks.py`](../../py/src/dist_bin/surf_scoter_lib/checks.py) and the pull's reviewer; `allow` in [`gateway_seekrit.py`](../../py/src/dist_bin/gateway_seekrit.py).

The container never holds the value, but it can use it: anything in the container can send the placeholder to the host its rule names, with the methods the rule allows, and the request goes out as you.  The placeholder is filled in wherever it is in the request: a header, the path, or the body.  So a rule that sends `GITHUB_TOKEN` to `api.github.com` with every method lets the container create a public gist whose text is `{{seekrit:GITHUB_TOKEN}}`, and the gateway writes your token into it: the value leaves through the very host it was meant for.  [NET-1](network-attack-vectors.md#net-1-allowed-domains-are-open-channels) is the same risk, with your credentials attached.

The project only names a secret; you decide whether your store has a value for it, and each name, per host, is a finding at the pull, with a new id when the names change.

**Red team:** with a rule that allows `POST` or `PUT`, put the placeholder in a request body that the host stores and shows back (a gist, an issue, a comment), and read it from outside.  Try it in a query string, a path, a cookie, a multipart field, and split across two headers.

**When changing this code:** keep each secret bound to exact hosts and to its rule's methods.  Recommend `methods: [GET]` (and paths, when the gateway gets them) for every rule with `secrets`, and never treat a rule with secrets as low risk in the review.

### SEEKRIT-2: Secrets in the gateway's memory, and their delivery

**Severity:** High.  **Status:** Mitigated.
**Where:** the inject socket (patch 0002, `src/inject.rs`); [`gateway_postrun_seekrit.py`](../../py/src/dist_bin/gateway_postrun_seekrit.py) and [`gateway_secrets.py`](../../py/src/dist_bin/surf_scoter_lib/gateway_secrets.py); `docker: runs: exec-stdin`; the `/run/seekrit` tmpfs in [`docker-compose.yaml`](../../dist-files/defaults/docker-compose.yaml).

The values (or a seekrit.dev token) reach the gateway only after it starts: `scoter` runs the store's commands on your host, and sends one JSON document on the standard input of `docker exec -i GATEWAY /seekrit-proxy inject`, which passes it over a Unix socket in a private tmpfs to the proxy.  The socket checks that the caller is the gateway's own user, takes one delivery, then is closed and removed: a second `inject` can't swap the values.  The values are never in the gateway's configuration, environment, image or files, nor on any command line, so `docker inspect` and `docker cp` find nothing ([`07-seekrit-gateway`](../../integration-tests/07-seekrit-gateway/run.sh) checks).  Until the delivery, a request with a placeholder is refused with `503`, never forwarded with the placeholder in it.

What's left:

* **The container tool can reach the gateway.**  Anyone who can run `docker exec` on it (you, or a host command with the container tool) can restart it and deliver values of their own before you do, or read its memory with enough privilege.  That access already owns the environment ([BRIDGE](host-bridge-attack-vectors.md)).
* **A restart empties it.**  `restart: unless-stopped` brings the gateway back without its secrets, until `scoter gateway-post-start`; requests with placeholders fail closed meanwhile.
* **Python can't wipe memory.**  The values pass through `scoter`'s own process, as text, on their way to `docker exec`.
* **Zeroing only helps while the process runs.**  The proxy zeroes its payload buffers as it drops them, but a gateway that's killed (`SIGKILL`, the OOM killer, or a `stop` that outlasts its timeout) runs none of that, and leaves its secrets in pages the kernel takes back.  Linux clears a page before giving it to another process, so what's left is swap, a core dump, or a snapshot of the machine's memory.  The [Envoy gateway](network-gateway-envoy-attack-vectors.md#envoy-10-secrets-in-the-gateways-memory-and-their-delivery) has the same limit.

**Red team:** look for the values in `docker inspect`, `docker logs`, `docker cp` of the gateway, the run's log, `/proc` inside the gateway, and the host's process list while the post-start runs.  Restart the gateway and send an `inject` before `scoter gateway-post-start` does.  Send a second `inject` to a gateway that has its secrets.

**When changing this code:** never put a value in a command line, an environment variable or a file.  Keep the socket private, one-shot, and fail closed before its delivery.

### SEEKRIT-3: The interception CA

**Severity:** Medium.  **Status:** Mitigated.
**Where:** `make_ca`, `private_ancestor` and `gateway_ca_dir` in [`gateway_start.py`](../../py/src/dist_bin/surf_scoter_lib/gateway_start.py); `dev-env: runs: network-gateway-mk-ca` and `values: network-gateway-reads-tls` in [`setup.yaml`](../../dist-files/setup.yaml); the CA mounts in [`docker-compose.yaml`](../../dist-files/defaults/docker-compose.yaml); CA loading (patch 0006).

The dev container trusts the run's CA for every host, and the gateway holds its key, so the gateway, or anything that gets the key, can intercept any TLS the container makes, not only the hosts with `https` rules.  The CA is new for each run (EC P-256, 30 days), and removed when the run ends, is killed, or is cleaned up.  The gateway runs as its own user, which can't read a key only you can, so the key it's given is readable by others (mode 644, in a 755 directory), and `scoter run` refuses unless a directory above it is yours alone (the project state directory is, by default).  The certificate has no name constraints: it could sign for any host.

**Red team:** from the dev container, confirm a `tls` host (passed through) shows its real certificate, not one from the run's CA.  Read the key from another local account during a run.  Reuse a run's certificate after the run ends.

**When changing this code:** keep the CA new for each run, its key reachable only through a directory that's yours, and given only to a gateway that reads TLS.  Name constraints, limited to the hosts with `https` rules, would narrow it further.

### SEEKRIT-4: Secrets echoed back in responses

**Severity:** Medium.  **Status:** Mitigated.
**Where:** `[redaction]` in the configuration [`gateway_seekrit.py`](../../py/src/dist_bin/gateway_seekrit.py) writes; seekrit-proxy's `src/redact.rs`.

A host that reflects a request back (an echo endpoint, an error page quoting a header) would hand the container the value it never held.  The gateway scrubs the values it put into a request out of that request's response, as written, percent-encoded or JSON-escaped, for values of 8 bytes or more.  Another encoding (base64, hex, a compressed body), a value split across chunks the scanner misses, or a short value gets through.

**Red team:** find an endpoint on an allowed host that reflects headers or bodies, and get the value back in each encoding, compressed, or split.  Check a value shorter than 8 bytes.

**When changing this code:** keep redaction on in the gateway's configuration.  A rule's methods and paths, limiting what the container can ask for, matter more than the scrubbing.

### SEEKRIT-5: A subverted gateway

**Severity:** Medium.  **Status:** Mitigated (N4).
**Where:** [`network-gateway-seekrit/Dockerfile`](../../containers/network-gateway-seekrit/Dockerfile), [`egress.nft`](../../containers/firewall-router/egress.nft), the `gateway` service in [`docker-compose.yaml`](../../dist-files/defaults/docker-compose.yaml).

seekrit-proxy parses hostile HTTP and TLS from the dev container, and from upstream servers.  If it's subverted, it holds what [SQUID-2](network-gateway-squid-attack-vectors.md#squid-2-a-subverted-squid) can't: the plaintext of read hosts, the secrets, and the CA key ([SEEKRIT-3](#seekrit-3-the-interception-ca)).  The firewall still limits it to TCP 443 on public IPv4 addresses, so it can send them only to some public host on 443.  It runs as an unprivileged user, with no capabilities, on a `scratch` image with no shell.

**Red team:** fuzz the forward proxy with malformed `CONNECT`s, HTTP/1.1 smuggling, oversized headers, and TLS handshakes; check its dependencies (hyper, rustls) for known issues.  From inside the gateway's namespace, confirm what the firewall allows.

**When changing this code:** keep the gateway unprivileged, capability-free and shell-free, and never give it `NET_ADMIN`.  Rebuild on upstream fixes.

### SEEKRIT-6: The secret store and its commands

**Severity:** Medium.  **Status:** Mitigated.
**Where:** `read_store`, `entry_value` and `_program` in [`gateway_secrets.py`](../../py/src/dist_bin/surf_scoter_lib/gateway_secrets.py); `project-state: paths: secret-store` in [`setup.yaml`](../../dist-files/setup.yaml).

The store decides which values go out, and its `command` entries run on your host, as you.  So it's read only if it's a regular file, yours, readable by nobody else, not a symlink, and outside the project, its files directory, its clone, the repository and the project state directory.  A command's program is a name found through the absolute entries of your `PATH`, or an absolute path, and never inside any of those.  Only the secrets the configuration's rules name are read and sent; the store's other entries stay put.  The store's path is a guarded setting.  A `value` entry is your secret in a plain file: prefer a command that asks your password manager.

**Red team:** from a project, try to get the store read from inside it (a `.envrc` setting `SURF_SCOTER_HOME`, a symlink, a relative `PATH` entry with a program named as a store command).  Get a store command's output into an error message.

**When changing this code:** never read the store from anywhere a project or the container could write, never run its commands from there, and never print a value.

### SEEKRIT-7: Guarantees that rest on the translation and the patches

**Severity:** Medium.  **Status:** Mitigated.
**Where:** [`gateway_seekrit.py`](../../py/src/dist_bin/gateway_seekrit.py); the [patches](../../containers/network-gateway-seekrit/patches/).

seekrit-proxy's own defaults are looser than this gateway needs; the configuration `gateway_seekrit.py` writes, and the patches, make up the difference:

* **Every unlisted host refused** (`unmatched_host_policy = "deny"`); upstream's default passes it through.
* **`tls` hosts passed through untouched** (`intercept = false`, patch 0003); upstream reads every host with a rule.
* **Ports** (`ports`, patch 0001): a host only on its rules' ports; upstream allows any.
* **No secret over plain HTTP** (`plaintext_injection = "refuse"`, patch 0004); upstream fills a placeholder into plain `http://` too.
* **No wildcards**: seekrit-proxy matches names exactly, so the script refuses `*.` rules rather than loosening them.
* **Rules it can't tell apart refused**: one host with `tls` and `https` rules, with `https` and `http` rules, or read on two ports.

A configuration written by hand, or a patch dropped in a rebase, loses these silently: seekrit-proxy ignores keys it doesn't know.

**Red team:** with each kind of rule, try the other ports, plain HTTP with a placeholder, a CONNECT to a `tls` host expecting the run's certificate, and a subdomain of an allowed host.

**When changing this code:** keep each guarantee's test (in the fork, and [`test_gateway_seekrit.py`](../../py/tests/test_gateway_seekrit.py)), and run [`07-seekrit-gateway`](../../integration-tests/07-seekrit-gateway/run.sh) after a rebase.

### SEEKRIT-8: Allowed names resolving to forbidden addresses

**Severity:** Medium.  **Status:** Mitigated (N3, N4).
**Where:** seekrit-proxy's egress gate (`src/egress.rs`, `[egress] block_private_ips`); the `blocked_v4` set in [`egress.nft`](../../containers/firewall-router/egress.nft).

As with [SQUID-1](network-gateway-squid-attack-vectors.md#squid-1-allowed-names-resolving-to-forbidden-addresses), an allowed name whose DNS an attacker controls can point anywhere.  The gateway resolves each name once, refuses private, loopback, link-local (metadata) and special-purpose addresses, IPv4-mapped IPv6 included, and connects to the address it checked, so there's no window for DNS rebinding.  The firewall blocks the same ranges again, for IPv4; IPv6 it drops entirely.  The gateway's list and the firewall's are maintained separately.

**Red team:** as SQUID-1: point an allowed name at private, loopback, link-local, metadata and IPv4-mapped IPv6 addresses, and try rebinding.

**When changing this code:** keep `block_private_ips` on, and never widen `[egress] allow_cidr`.

### SEEKRIT-9: The patched build

**Severity:** Medium.  **Status:** Mitigated.
**Where:** [`network-gateway-seekrit/Dockerfile`](../../containers/network-gateway-seekrit/Dockerfile), [`patches/`](../../containers/network-gateway-seekrit/patches/).

The image fetches seekrit-proxy's source at a full commit hash, checks it, applies Surf Scoter's patches (stopping on any that doesn't apply cleanly), and builds with the committed `Cargo.lock`.  The build itself has the network, outside any isolation ([DEV-3](dev-container-attack-vectors.md#dev-3-image-builds-run-outside-the-isolation)), and the toolchain image is a tag, not a digest.  Keeping the patches up to date with upstream is ongoing work, and a patch change is a security change.

**Red team:** change a patch, or the pinned commit, and see what the build and the tests catch.

**When changing this code:** pin by full hash, keep `--locked`, review each patch's diff as security-sensitive code, and submit the patches upstream where they fit, so fewer have to be kept.

### SEEKRIT-10: What the log holds

**Severity:** Low.  **Status:** Mitigated.
**Where:** the access log (patch 0005); [`netlog.py`](../../py/src/dist_bin/surf_scoter_lib/netlog.py).

The gateway logs each connection, each request it reads (method, host, port, path, status, and the names of the secrets put in), and each tunnel's bytes, as JSON, for `scoter netlog`.  It never logs a value, a query string or a body (the fork's tests and `07-seekrit-gateway` check).  A path can still hold an identifier.  Everything in the log came from the dev container, so `scoter netlog` cleans every field before showing it, and suggests rules only for well-formed host names.

**Red team:** put terminal escapes, very long values and look-alike characters in hosts and paths, and look at `scoter netlog`'s output.  Look for a value in the log after an error.

**When changing this code:** never log a value, a query string or a body; keep the log's fields from the container cleaned wherever they're shown.


## Sensitive code

Changes to these files can weaken the seekrit gateway, and need a security review:

* [`containers/network-gateway-seekrit/`](../../containers/network-gateway-seekrit/): the build, the pinned commit, and the patches.
* [`py/src/dist_bin/gateway_seekrit.py`](../../py/src/dist_bin/gateway_seekrit.py): what the gateway is told to allow, read and fill in.
* [`py/src/dist_bin/surf_scoter_lib/gateway_secrets.py`](../../py/src/dist_bin/surf_scoter_lib/gateway_secrets.py), [`py/src/dist_bin/gateway_postrun_seekrit.py`](../../py/src/dist_bin/gateway_postrun_seekrit.py) and [`py/src/dist_bin/main_gateway_post_start.py`](../../py/src/dist_bin/main_gateway_post_start.py): the secret store, and the values' delivery (shared with the Envoy gateway).
* [`py/src/dist_bin/surf_scoter_lib/gateway_start.py`](../../py/src/dist_bin/surf_scoter_lib/gateway_start.py): the run's CA.
* The gateway's mounts and tmpfs in [`dist-files/defaults/docker-compose.yaml`](../../dist-files/defaults/docker-compose.yaml), and the CA and placeholder handling in [`containers/dev-env/entrypoint.sh`](../../containers/dev-env/entrypoint.sh).
