#SPDX:MIT
"""Write the Envoy network gateway's configuration from the egress rules.

Usage: gateway_envoy.py UNIVERSAL.json OUTPUT [CA_CERT CA_KEY]

The setup's `network-gateway: runs: write-config` runs this.  It reads the
egress rules from the universal configuration file (see
`surf_scoter_lib.gateway`), works out what Envoy can enforce of them, writes
Envoy's bootstrap configuration (JSON, which Envoy reads as it does YAML) to
OUTPUT, which the docker-compose file mounts into the gateway, and prints a
JSON report of what it enforces and what it refuses.

When it refuses any rule, it writes nothing and exits 1: a rule is never
dropped, or loosened.

Envoy takes each `CONNECT`, and either passes it through or reads it:

* A `tls` rule's host is passed through: Envoy's dynamic forward proxy
  resolves the requested host itself and opens a plain TCP tunnel to it.
* An `https` rule's host is read.  The `CONNECT` goes to an internal
  listener for that host and port alone, which ends the TLS connection with
  a certificate for the host, checks each request's host and method against
  the rules, and sends it on, over a new TLS connection that checks the
  server's certificate, to that host: never to one the request names.

The certificate for each read host is made here, for this run, signed by the
run's CA (CA_CERT, CA_KEY) with the setup's `dev-env: runs:
network-gateway-mk-leaf`.  The gateway gets these certificates and their
keys, in its configuration, but never the CA's key: it can only pass as the
hosts the rules name.  So the configuration must sit inside a directory only
you can use, as the run's resources directory does by default.

A dynamic forward proxy is open to confused deputy attacks: it dials
whatever it's told to.  What keeps this one to the rules (keep all of it):

* The host that's checked is the host that's dialled.  Routing matches the
  request's `:authority` (`host:port`), and the dynamic forward proxy
  resolves and connects to that same `:authority`.  Nothing may give it a
  different one: no `host_rewrite_*`, no `allow_dynamic_host_from_filter_state`,
  no filter that sets `envoy.upstream.dynamic_host` or `dynamic_port`, and no
  port stripping in the connection manager.  The HTTP filters are exactly
  the dynamic forward proxy and the router.
* Each rule is a route matching the whole `:authority`, port included: the
  exact `host:port`, or, for `*.domain`, a regular expression for its
  subdomains on that port.  Anything else matches no route, and gets a 404.
* A name no rule allows is never resolved: the dynamic forward proxy looks a
  host up only for a route that sends it to its cluster, so a lookup can't
  carry data out to someone's DNS server.
* Only `CONNECT` is routed; the codec is HTTP/1 only.
* The tunnel is raw TCP: the cluster has no TLS of its own, so there is no
  upstream certificate check to get wrong, nor connections to share.
* A read host's requests only ever go to that host: its upstream cluster
  names it, fixed, and checks its certificate (SNI and a DNS name match, the
  system's CAs).  A request whose `:authority` isn't that host is refused, so
  the container can't send one host's request to another behind the same
  server.
* There's no admin interface: a name that resolves to loopback (which the
  gateway's firewall lets through) finds only the proxy's own listener, and
  its rules again.  The firewall keeps it off every other private address,
  and off IPv6; Envoy asks DNS for IPv4 addresses only.
"""
import json
import os
import re
import shutil
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path

from surf_scoter_lib import UserConfig, fileutil, gateway, jdata
from surf_scoter_lib.err import SurfScoterErr
from surf_scoter_lib.gateway_start import private_ancestor
from surf_scoter_lib.user_config import Capture, Echo, OnError

MAX_UNIVERSAL_BYTES = 4 * 1024 * 1024
# The configuration's mode: the gateway runs as its own user, which must
# read it.  With a read host, it holds that host's certificate key, so it
# must be inside a directory only you can use.
MODE_CONFIG = 0o644
EXIT_REFUSED = 1
EXIT_ERROR = 2

ENVOY = "envoy"

# Inside the gateway container, which shares the firewall's network
# namespace: the firewall takes port 3128 only from the isolated network.
LISTEN_ADDRESS = "0.0.0.0"  # nosec: B104  # The firewall limits who reaches it.
LISTEN_PORT = 3128
CLUSTER = "dynamic_forward_proxy_cluster"
DNS_CACHE = "dynamic_forward_proxy_cache"
# The system's CAs, in Envoy's image: what a read host's certificate must chain to.
TRUST_STORE = "/etc/ssl/certs/ca-certificates.crt"
# The setup's run that makes a read host's certificate.
MK_LEAF = "network-gateway-mk-leaf"

# Limits: on the hosts the DNS cache holds, the lookups waiting, and the
# connections (in and out).
MAX_DNS_HOSTS = 1024
MAX_PENDING_DNS = 256
MAX_CONNECTIONS = 1024
MAX_REQUEST_HEADERS_KB = 16
# A request's headers must arrive promptly; a tunnel may sit idle a while
# (as Squid's 15 minute read timeout).
REQUEST_HEADERS_TIMEOUT = "10s"
STREAM_IDLE_TIMEOUT = "900s"
CONNECT_TIMEOUT = "10s"
# HTTP/2 flow control on a read host's connections, both ways.  Envoy's
# default stream window (256 MiB) lets one side send a whole response into
# the gateway's memory while the other reads it slowly; these keep each
# stream to about a mebibyte, which still moves data quickly.
HTTP2_WINDOWS: dict[str, int] = {
    "initial_stream_window_size": 1024 * 1024,
    "initial_connection_window_size": 4 * 1024 * 1024,
}
MAX_CONCURRENT_STREAMS = 100

# The route name prefix for a `CONNECT` to a read host, and the name of the
# route that refuses a read host's request (both for `scoter netlog`).
INTERCEPT = "intercept "
REFUSED = "refused"

_TYPE = "type.googleapis.com/envoy.extensions."
_TLS_SOCKET = "envoy.transport_sockets.tls"
_TLS_PARAMS = {"tls_minimum_protocol_version": "TLSv1_2"}

# What each access log line records, for `scoter netlog`
# (`surf_scoter_lib.netlog.parse_envoy`).  For each `CONNECT`, one line when
# a tunnel opens, and one when it ends or is refused (`plane: connect`); for
# each request to a read host, one line when it ends (`plane: request`, with
# the rule's host and port).
ACCESS_LOG_FORMAT: dict[str, str] = {
    "time": "%START_TIME%",
    "type": "%ACCESS_LOG_TYPE%",
    "authority": "%REQ(:AUTHORITY)%",
    "method": "%REQ(:METHOD)%",
    "path": "%REQ(:PATH)%",
    "code": "%RESPONSE_CODE%",
    "flags": "%RESPONSE_FLAGS%",
    "route": "%ROUTE_NAME%",
    "upstream": "%UPSTREAM_HOST%",
    "bytes_up": "%BYTES_RECEIVED%",
    "bytes_down": "%BYTES_SENT%",
}

# A read host's certificate and key (PEM), by host.
Leaves = dict[str, tuple[str, str]]

# ---------------------------------------------------------------------
# The translation


def refusal(rule: gateway.Rule, ports: frozenset[int], tls_ports: frozenset[int]) -> str | None:
    """Why the Envoy gateway can't enforce `rule` on its own, if it can't.

    Its firewall lets out only `ports`, the setup's `network-gateway-ports`,
    and it reads TLS only on `tls_ports` (`network-gateway-tls-ports`).  It
    passes `tls` rules through, and reads `https` rules for exact hosts.
    """
    if rule.secrets:
        return (
            "the Envoy gateway can't fill in secrets "
            f"(`secrets: {', '.join(rule.secrets)}` needs the seekrit gateway)"
        )
    if rule.protocol == "http":
        return (
            "the Envoy gateway only takes `CONNECT`, never plain HTTP "
            "(use `tls` or `https` on 443 if the host has it)"
        )
    if rule.protocol == "https" and rule.host.startswith("*."):
        return (
            "the Envoy gateway only reads TLS for a host named exactly; "
            "list each host, or use `tls` for the wildcard"
        )
    if rule.port not in ports:
        return (
            f"the gateway's firewall only lets TCP {gateway.ports_text(ports)} out, not {rule.port} "
            f"(the setup's `dev-env: value-lists: {gateway.GATEWAY_PORTS}`)"
        )
    if rule.protocol == "https" and rule.port not in tls_ports:
        return (
            f"the gateway only looks inside TLS on TCP {gateway.ports_text(tls_ports)}, not {rule.port} "
            f"(the setup's `dev-env: value-lists: {gateway.GATEWAY_TLS_PORTS}`)"
        )
    if rule.protocol == "tls" and rule.methods:
        return "the gateway can't see the methods inside `tls`"
    return None


def _covers(wildcard: str, host: str) -> bool:
    """Whether the `*.domain` rule's host takes `host`."""
    return host.endswith(wildcard[1:])


def _overlap_refusal(rule: gateway.Rule, tunnels: list[gateway.Rule]) -> str | None:
    """Why an `https` rule can't be enforced beside the `tls` rules, if it can't.

    The gateway decides whether to read a connection or pass it through from
    its `CONNECT`, before any request.  A host and port a `tls` rule passes
    through can't also be read.
    """
    for other in tunnels:
        if other.port != rule.port:
            continue
        if other.host == rule.host or (other.host.startswith("*.") and _covers(other.host, rule.host)):
            return (
                f"the Envoy gateway either reads a host's traffic or passes it through untouched, "
                f"and the `tls` rule for {other.host} passes {rule.host}:{rule.port} through"
            )
    return None


def plan(rules: list[gateway.Rule], ports: frozenset[int], tls_ports: frozenset[int]) -> gateway.Report:
    """The Envoy gateway's report: what it enforces, and what it refuses, and why."""
    report = gateway.Report(ENVOY)
    alone: list[gateway.Rule] = []
    for rule in rules:
        reason = refusal(rule, ports, tls_ports)
        if reason is None:
            alone.append(rule)
        else:
            report.refused.append((rule, reason))
    tunnels = [r for r in alone if r.protocol == "tls"]
    for rule in alone:
        reason = _overlap_refusal(rule, tunnels) if rule.protocol == "https" else None
        if reason is None:
            report.enforced.append(rule)
        else:
            report.refused.append((rule, reason))
    return report


def read_hosts(rules: list[gateway.Rule]) -> dict[tuple[str, int], frozenset[str] | None]:
    """Each read host and port, with the methods its rules allow, together; None for any."""
    out: dict[tuple[str, int], frozenset[str] | None] = {}
    for rule in rules:
        if rule.protocol != "https":
            continue
        key = (rule.host, rule.port)
        if not rule.methods:
            out[key] = None
        elif key not in out:
            out[key] = frozenset(rule.methods)
        elif out[key] is not None:
            out[key] = out[key] | frozenset(rule.methods)  # type: ignore[operator]
    return out


def wildcard_regex(host: str, port: int) -> str:
    """The RE2 expression for a `*.domain` rule's `:authority`: any of the
    domain's subdomains, at any depth, but not the domain itself, on `port`.

    The host is already checked (`gateway.host_is_valid`): only lower-case
    letters, digits, `-` and `.`, so escaping the dots is enough.  RE2 runs
    in linear time, whatever the input.
    """
    domain = host.removeprefix("*.").replace(".", "\\.")
    return f"(?i)^[a-z0-9-]+(\\.[a-z0-9-]+)*\\.{domain}:{port}$"


def authority_match(host: str, port: int) -> jdata.DictJsonData:
    """The `CONNECT` route's header matcher: the whole `:authority`, port included."""
    if host.startswith("*."):
        matcher: jdata.DictJsonData = {"safe_regex": {"regex": wildcard_regex(host, port)}}
    else:
        matcher = {"exact": f"{host}:{port}", "ignore_case": True}
    return {"name": ":authority", "string_match": matcher}


def request_authority_regex(host: str, port: int) -> str:
    """The RE2 expression for a read host's requests' `:authority`: the host,
    and its port, which may be left out when it's 443."""
    escaped = host.replace(".", "\\.")
    return f"(?i)^{escaped}(:{port})?$" if port == 443 else f"(?i)^{escaped}:{port}$"


def _connect_route(name: str, host: str, port: int, cluster: str) -> jdata.DictJsonData:
    return {
        "name": name,
        "match": {"connect_matcher": {}, "headers": [authority_match(host, port)]},
        "route": {
            "cluster": cluster,
            # No limit on a tunnel's length; the route's default (15s) would
            # cut every one.  The idle timeout still ends a quiet one.
            "timeout": "0s",
            # Ends the CONNECT here, and sends what follows as plain TCP.
            "upgrade_configs": [{"upgrade_type": "CONNECT", "connect_config": {}}],
        },
    }


def route(rule: gateway.Rule) -> jdata.DictJsonData:
    """The route for one `tls` rule: its `CONNECT`s, tunnelled to the host."""
    return _connect_route(f"{rule.host}:{rule.port}", rule.host, rule.port, CLUSTER)


def _intercept_name(host: str, port: int) -> str:
    return f"{INTERCEPT}{host}:{port}"


def _upstream_name(host: str, port: int) -> str:
    return f"upstream {host}:{port}"


def dns_cache_config() -> jdata.DictJsonData:
    """The DNS cache the filter and the cluster share (they must be the same)."""
    return {
        "name": DNS_CACHE,
        # The gateway's firewall drops IPv6.
        "dns_lookup_family": "V4_ONLY",
        "max_hosts": MAX_DNS_HOSTS,
        "dns_cache_circuit_breaker": {"max_pending_requests": MAX_PENDING_DNS},
    }


def _circuit_breakers() -> jdata.DictJsonData:
    return {"thresholds": [{
        "max_connections": MAX_CONNECTIONS,
        "max_pending_requests": MAX_CONNECTIONS,
        "max_requests": MAX_CONNECTIONS,
    }]}


def _access_log(fields: dict[str, str]) -> list[jdata.DictJsonData]:
    return [{
        "name": "envoy.access_loggers.stdout",
        "typed_config": {
            "@type": _TYPE + "access_loggers.stream.v3.StdoutAccessLog",
            "log_format": {"json_format": fields},
        },
    }]


def _manager_limits() -> jdata.DictJsonData:
    return {
        "max_request_headers_kb": MAX_REQUEST_HEADERS_KB,
        "request_headers_timeout": REQUEST_HEADERS_TIMEOUT,
        "stream_idle_timeout": STREAM_IDLE_TIMEOUT,
        "common_http_protocol_options": {"headers_with_underscores_action": "REJECT_REQUEST"},
    }


def _intercept_listener(host: str, port: int, methods: frozenset[str] | None, leaf: tuple[str, str]) -> jdata.DictJsonData:
    """The internal listener for one read host and port: it ends the TLS
    connection with the host's certificate, and sends on only the requests
    for that host, with the rules' methods, to that host."""
    headers: list[jdata.DictJsonData] = [{
        "name": ":authority",
        "string_match": {"safe_regex": {"regex": request_authority_regex(host, port)}},
    }]
    if methods is not None:
        headers.append({
            "name": ":method",
            "string_match": {"safe_regex": {"regex": "^(" + "|".join(sorted(methods)) + ")$"}},
        })
    manager: jdata.DictJsonData = {
        "@type": _TYPE + "filters.network.http_connection_manager.v3.HttpConnectionManager",
        "stat_prefix": "intercept",
        # HTTP/1.1 or HTTP/2, as the client's TLS (ALPN) chooses.
        "codec_type": "AUTO",
        # Add nothing of its own to the request, nor change the response's server.
        "generate_request_id": False,
        "server_header_transformation": "PASS_THROUGH",
        "http2_protocol_options": {**HTTP2_WINDOWS, "max_concurrent_streams": MAX_CONCURRENT_STREAMS},
        **_manager_limits(),
        "route_config": {
            "name": _intercept_name(host, port),
            "virtual_hosts": [{"name": "intercept", "domains": ["*"], "routes": [
                {
                    "name": f"{host}:{port}",
                    "match": {"prefix": "/", "headers": headers},
                    "route": {"cluster": _upstream_name(host, port), "timeout": "0s"},
                },
                {
                    # Anything else: refused here, never sent on.
                    "name": REFUSED,
                    "match": {"prefix": "/"},
                    "direct_response": {"status": 403, "body": {
                        "inline_string": "Refused by the Surf Scoter network gateway: no egress rule allows it.\n",
                    }},
                },
            ]}],
        },
        "http_filters": [{
            "name": "envoy.filters.http.router",
            "typed_config": {"@type": _TYPE + "filters.http.router.v3.Router", "suppress_envoy_headers": True},
        }],
        "access_log": _access_log({**ACCESS_LOG_FORMAT, "plane": "request", "host": host, "port": str(port)}),
    }
    cert, key = leaf
    return {
        "name": _intercept_name(host, port),
        # Reached only from the `CONNECT` route for this host and port.
        "internal_listener": {},
        "filter_chains": [{
            "transport_socket": {"name": _TLS_SOCKET, "typed_config": {
                "@type": _TYPE + "transport_sockets.tls.v3.DownstreamTlsContext",
                "common_tls_context": {
                    "tls_params": dict(_TLS_PARAMS),
                    "alpn_protocols": ["h2", "http/1.1"],
                    "tls_certificates": [{
                        "certificate_chain": {"inline_string": cert},
                        "private_key": {"inline_string": key},
                    }],
                },
            }},
            "filters": [{"name": "envoy.filters.network.http_connection_manager", "typed_config": manager}],
        }],
    }


def _intercept_clusters(host: str, port: int) -> list[jdata.DictJsonData]:
    """The clusters for one read host and port: into its internal listener,
    and out to the host itself, over TLS, checking its certificate."""
    inward = _intercept_name(host, port)
    outward = _upstream_name(host, port)
    return [
        {
            "name": inward,
            "connect_timeout": CONNECT_TIMEOUT,
            "load_assignment": {"cluster_name": inward, "endpoints": [{"lb_endpoints": [{"endpoint": {
                "address": {"envoy_internal_address": {"server_listener_name": inward}},
            }}]}]},
        },
        {
            "name": outward,
            # The host, fixed: never one a request names.
            "type": "LOGICAL_DNS",
            "dns_lookup_family": "V4_ONLY",
            "connect_timeout": CONNECT_TIMEOUT,
            "load_assignment": {"cluster_name": outward, "endpoints": [{"lb_endpoints": [{"endpoint": {
                "address": {"socket_address": {"address": host, "port_value": port}},
            }}]}]},
            "transport_socket": {"name": _TLS_SOCKET, "typed_config": {
                "@type": _TYPE + "transport_sockets.tls.v3.UpstreamTlsContext",
                "sni": host,
                "common_tls_context": {
                    "tls_params": dict(_TLS_PARAMS),
                    "validation_context": {
                        "trusted_ca": {"filename": TRUST_STORE},
                        "match_typed_subject_alt_names": [{"san_type": "DNS", "matcher": {"exact": host}}],
                    },
                },
            }},
            # HTTP/2 or HTTP/1.1, as the server's TLS (ALPN) chooses.
            "typed_extension_protocol_options": {"envoy.extensions.upstreams.http.v3.HttpProtocolOptions": {
                "@type": _TYPE + "upstreams.http.v3.HttpProtocolOptions",
                "auto_config": {"http_protocol_options": {}, "http2_protocol_options": dict(HTTP2_WINDOWS)},
            }},
            "circuit_breakers": _circuit_breakers(),
        },
    ]


def config(rules: list[gateway.Rule], leaves: Leaves | None = None) -> jdata.DictJsonData:
    """Envoy's bootstrap configuration, allowing only `rules` (all enforceable).

    `leaves` has the certificate and key for each read host (`https` rule).
    """
    reads = read_hosts(rules)
    missing = {host for host, _ in reads} - set(leaves or {})
    if missing:
        raise ValueError(f"no certificate for {', '.join(sorted(missing))}")
    # The read hosts first: a `CONNECT` goes to the first route it matches.
    routes: list[jdata.DictJsonData] = [
        _connect_route(_intercept_name(host, port), host, port, _intercept_name(host, port))
        for host, port in reads
    ]
    seen: set[tuple[str, int]] = set()
    for rule in rules:
        if rule.protocol == "tls" and (rule.host, rule.port) not in seen:
            seen.add((rule.host, rule.port))
            routes.append(route(rule))
    manager: jdata.DictJsonData = {
        "@type": _TYPE + "filters.network.http_connection_manager.v3.HttpConnectionManager",
        "stat_prefix": "egress",
        # HTTP/1 only: no HTTP/2 extended CONNECT, no h2c.
        "codec_type": "HTTP1",
        "upgrade_configs": [{"upgrade_type": "CONNECT"}],
        **_manager_limits(),
        "route_config": {
            "name": "egress",
            # One virtual host for every name: the routes match the whole
            # `:authority`.  A request no route matches gets a 404, and its
            # host is never resolved.
            "virtual_hosts": [{"name": "egress", "domains": ["*"], "routes": routes}],
        },
        "http_filters": [
            {
                "name": "envoy.filters.http.dynamic_forward_proxy",
                "typed_config": {
                    "@type": _TYPE + "filters.http.dynamic_forward_proxy.v3.FilterConfig",
                    "dns_cache_config": dns_cache_config(),
                },
            },
            {
                "name": "envoy.filters.http.router",
                "typed_config": {"@type": _TYPE + "filters.http.router.v3.Router"},
            },
        ],
        "access_log": _access_log({**ACCESS_LOG_FORMAT, "plane": "connect"}),
        "access_log_options": {"flush_log_on_tunnel_successfully_established": True},
    }
    listeners: list[jdata.DictJsonData] = [{
        "name": "egress",
        "address": {"socket_address": {"address": LISTEN_ADDRESS, "port_value": LISTEN_PORT}},
        "filter_chains": [{"filters": [{
            "name": "envoy.filters.network.http_connection_manager",
            "typed_config": manager,
        }]}],
    }]
    clusters: list[jdata.DictJsonData] = [{
        "name": CLUSTER,
        "connect_timeout": CONNECT_TIMEOUT,
        "lb_policy": "CLUSTER_PROVIDED",
        "cluster_type": {
            "name": "envoy.clusters.dynamic_forward_proxy",
            "typed_config": {
                "@type": _TYPE + "clusters.dynamic_forward_proxy.v3.ClusterConfig",
                "dns_cache_config": dns_cache_config(),
            },
        },
        "circuit_breakers": _circuit_breakers(),
    }]
    for (host, port), methods in reads.items():
        listeners.append(_intercept_listener(host, port, methods, (leaves or {})[host]))
        clusters.extend(_intercept_clusters(host, port))
    bootstrap: jdata.DictJsonData = {
        "static_resources": {"listeners": listeners, "clusters": clusters},
        "overload_manager": {"resource_monitors": [{
            "name": "envoy.resource_monitors.global_downstream_max_connections",
            "typed_config": {
                "@type": _TYPE + "resource_monitors.downstream_connections.v3.DownstreamConnectionsConfig",
                "max_active_downstream_connections": MAX_CONNECTIONS,
            },
        }]},
    }
    if reads:
        bootstrap["bootstrap_extensions"] = [{
            "name": "envoy.bootstrap.internal_listener",
            "typed_config": {"@type": _TYPE + "bootstrap.internal_listener.v3.InternalListener"},
        }]
    return bootstrap


def config_text(rules: list[gateway.Rule], leaves: Leaves | None = None) -> str:
    """The configuration, as the file holds it."""
    return json.dumps(config(rules, leaves), indent=2, sort_keys=True, ensure_ascii=True) + "\n"


# ---------------------------------------------------------------------
# The certificates

_PEM = re.compile(r"-----BEGIN [A-Z ]+-----\n[A-Za-z0-9+/=\n]+-----END [A-Z ]+-----\n?")


def _pem(path: Path, what: str) -> str:
    text = path.read_text(encoding="ascii")
    if not _PEM.fullmatch(text.strip() + "\n"):
        raise SurfScoterErr(f"`dev-env: runs: {MK_LEAF}` didn't write a PEM {what}")
    return text.strip() + "\n"


def make_leaves(config_: UserConfig, hosts: Sequence[str], ca_cert: str, ca_key: str) -> Leaves:
    """A new certificate and key for each host, signed by the run's CA, with
    the setup's `dev-env: runs: network-gateway-mk-leaf`.

    They're made in a directory only you can use, read, and removed.
    """
    leaves: Leaves = {}
    directory = Path(tempfile.mkdtemp(prefix="surf-scoter-leaf-"))
    try:
        for host in hosts:
            # Already checked (`gateway.host_is_valid`): a plain DNS name.
            if not gateway.host_is_valid(host) or host.startswith("*."):
                raise SurfScoterErr(f"{host!r} can't have a certificate")
            cert, key = directory / "leaf.pem", directory / "leaf-key.pem"
            res = config_.run_command(
                "dev-env", MK_LEAF,
                template={
                    "ca-cert": ca_cert, "ca-key": ca_key,
                    "leaf-cert": str(cert), "leaf-key": str(key),
                    "subject": f"/CN={host}", "san": f"subjectAltName=DNS:{host}",
                },
                capture=Capture.BOTH, on_error=OnError.RETURN, echo=Echo.QUIET,
            )
            if res.returncode != 0 or not cert.is_file() or not key.is_file():
                detail = (res.stderr or b"").decode("utf-8", "replace").strip()
                raise SurfScoterErr(f"couldn't make a certificate for {host} (`dev-env: runs: {MK_LEAF}`): {detail}")
            leaves[host] = (_pem(cert, "certificate"), _pem(key, "key"))
            cert.unlink()
            key.unlink()
    finally:
        shutil.rmtree(directory, ignore_errors=True)
    return leaves


# ---------------------------------------------------------------------
# The command


def main(argv: Sequence[str]) -> int:
    """CLI entrypoint."""
    if len(argv) not in (3, 5):
        print(
            f"Usage: {argv[0] if argv else 'gateway_envoy.py'} UNIVERSAL.json OUTPUT [CA_CERT CA_KEY]",
            file=sys.stderr,
        )
        return EXIT_ERROR
    try:
        with open(argv[1], "rb") as inp:
            data = inp.read(MAX_UNIVERSAL_BYTES + 1)
        if len(data) > MAX_UNIVERSAL_BYTES:
            raise SurfScoterErr(f"{argv[1]}: larger than {MAX_UNIVERSAL_BYTES} bytes")
        suc = json.loads(data)
        if not isinstance(suc, dict):
            raise SurfScoterErr(f"{argv[1]}: not a universal configuration file")
        config_ = UserConfig(suc)
        rules = gateway.rules_from(config_)
        ports = gateway.setup_ports(config_, gateway.GATEWAY_PORTS)
        tls_ports = gateway.setup_ports(config_, gateway.GATEWAY_TLS_PORTS)
    except (OSError, ValueError, SurfScoterErr) as err:
        print(f"{argv[0]}: {err}", file=sys.stderr)
        return EXIT_ERROR
    report = plan(rules, ports, tls_ports)
    print(json.dumps(report.to_json()))
    if report.refused:
        return EXIT_REFUSED
    output = Path(argv[2])
    hosts = sorted({host for host, _ in read_hosts(report.enforced)})
    try:
        leaves: Leaves = {}
        if hosts:
            if len(argv) != 5:
                raise SurfScoterErr("reading TLS needs the run's CA: give CA_CERT and CA_KEY")
            # The keys in the configuration are readable by the gateway's user,
            # so by others: only a directory above, that's yours alone, keeps them.
            if not fileutil.is_windows() and private_ancestor(output) is None:
                raise SurfScoterErr(
                    f"{output} would hold the read hosts' certificate keys, readable by others, "
                    "and nothing above it is yours alone; keep `project-state: paths: resources-dir` "
                    "inside a directory only you can use (`chmod 700`)"
                )
            leaves = make_leaves(config_, hosts, argv[3], argv[4])
        text = config_text(report.enforced, leaves)
    except (OSError, ValueError, SurfScoterErr) as err:
        print(f"{argv[0]}: {err}", file=sys.stderr)
        return EXIT_ERROR
    # The gateway runs as its own user, which must be able to read it.
    # Replaced whole, so it's never half-written.
    temp = argv[2] + ".tmp"
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | fileutil.O_NOFOLLOW, MODE_CONFIG)
    with os.fdopen(fd, "w", encoding="utf-8") as out:
        out.write(text)
    os.chmod(temp, MODE_CONFIG)
    os.replace(temp, argv[2])
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
