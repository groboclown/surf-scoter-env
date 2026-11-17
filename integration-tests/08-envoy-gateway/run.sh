#!/bin/sh
#SPDX:MIT
# A full run with the Envoy gateway in place of Squid: the user's setup
# chooses it, and the project's egress rules read one host (GET only), and
# pass the subdomains of another through.  Checks, from an ssh session in the
# dev container:
#   * the read host works, through the gateway's certificate for it, made
#     for the run; a POST, or a request naming another host on its
#     connection, is refused by the gateway (403);
#   * a subdomain of the wildcard is reachable, passed through;
#   * the wildcard's own domain, another host, another port on an allowed
#     host, and plain HTTP to an allowed host are refused, by Envoy's routes
#     (404, `NR`), and Envoy never looked the refused names up;
#   * a tunnel outlives Envoy's default route timeout (15 seconds);
#   * the gateway never has the run's CA key, only the read host's certificate;
#   * `scoter netlog` reads Envoy's log, and suggests rules for the refused;
#   * `scoter kill` stops it all.
#
# Like 03, it builds the real images, runs containers, and needs the network.
. "$( dirname "$0" )/../common/lib.sh"

IT_RUN_TIMEOUT="${IT_RUN_TIMEOUT:-1800}"

it_require setsid ssh "${IT_CONTAINER_TOOL}"
it_init
it_project "${IT_TEST_DIR}/project" git
tool="${IT_SANDBOX}/tools/container-tool"
running="${HOME}/.config/surf-scoter.d/state/running-env.json"

running_field() {
    python3 -I - "${running}" "$1" <<'PY'
import json, sys
try:
    envs = json.load(open(sys.argv[1]))
except (OSError, ValueError):
    envs = []
print(next((e[sys.argv[2]] for e in envs if e.get("suc")), ""))
PY
}

is_running() {
    [ "$( "${tool}" inspect -f '{{.State.Running}}' "$1" 2> /dev/null )" = "true" ]
}

start_run() {
    setsid "${IT_DIST}/scoter" --project-dir "${IT_PROJECT}" run > "$1" 2>&1 &
    run_pid=$!
    host=""
    waited=0
    while [ "${waited}" -lt "${IT_RUN_TIMEOUT}" ]; do
        kill -0 "${run_pid}" 2> /dev/null || { cat "$1" >&2; it_fail "the run ended before the environment started"; }
        host=$( running_field host )
        if [ -n "${host}" ] && is_running "${host}" && is_running "${host}-ng"; then
            return 0
        fi
        sleep 5
        waited=$(( waited + 5 ))
    done
    cat "$1" >&2
    it_fail "the environment didn't start in ${IT_RUN_TIMEOUT} seconds"
}

# in_ssh COMMAND: run the shell COMMAND in an ssh session in the dev
# container; its output on stdout.  sshd may still be starting: tries a while.
in_ssh() {
    tries=0
    until ssh -F "${HOME}/.ssh/config" -o "UserKnownHostsFile=${HOME}/.ssh/known_hosts" \
            -o BatchMode=yes -o ConnectTimeout=10 "${host}" "$1" 2> "${IT_SANDBOX}/ssh.err"; do
        status=$?
        # 255: ssh itself failed; anything else is the command's own status.
        [ "${status}" -eq 255 ] || return "${status}"
        tries=$(( tries + 1 ))
        [ "${tries}" -lt 12 ] || { cat "${IT_SANDBOX}/ssh.err" >&2; it_fail "couldn't ssh into the environment"; }
        sleep 5
    done
}

# status_of ARGS...: the HTTP status curl gets in the dev container, through
# the gateway; 000 when the connection itself is refused.
status_of() {
    in_ssh "curl -s -o /dev/null -w '%{http_code}' --max-time 30 $*" || true
}

# connect_of URL: the status the gateway answers curl's CONNECT with, in the
# dev container (`%{http_connect}`); 000 when there was none.
connect_of() {
    in_ssh "curl -s -o /dev/null -w '%{http_connect}' --max-time 30 $1" || true
}

# gateway_log TEXT: the gateway's log, once it has TEXT (Envoy writes its
# access log every second, not line by line).
gateway_log() {
    waited=0
    until "${tool}" logs "${host}-ng" > "${IT_SANDBOX}/gateway.log" 2>&1 \
            && grep -qF -- "$1" "${IT_SANDBOX}/gateway.log"; do
        waited=$(( waited + 1 ))
        [ "${waited}" -lt 15 ] || break
        sleep 1
    done
}

it_say "the user's setup chooses the Envoy gateway"
cat >> "${SURF_SCOTER_HOME}/setup.yaml" <<'YAML'
dev-env:
    paths:
        network-gateway: ${scoter:distribution-dir}/containers/network-gateway-envoy
    values:
        network-gateway-image: ${dev-env-image-prefix}network-gateway-envoy
        network-gateway-name: envoy
        network-gateway-config-mount: /user-space/gateway/envoy.json
        network-gateway-reads-tls: 'no'
    runs:
        network-gateway-write-config:
            - ${python:script}
            - ${scoter:distribution-bin-dir}/gateway_envoy.py
            - '{x:universal}'
            - '{x:output}'
            - '{x:ca-cert}'
            - '{x:ca-key}'
YAML

it_say "user setup"
scoter user-setup --non-interactive > "${IT_SANDBOX}/user-setup.log" 2>&1 \
    || { cat "${IT_SANDBOX}/user-setup.log" >&2; it_fail "user setup failed"; }

it_say "pulling the project"
it_approve pull

it_say "starting the run (building the images; this can take a while)"
start_run "${IT_SANDBOX}/run.log"
it_say "running as ${host}"
resources=$( running_field resources_dir )

it_say "the gateway is Envoy, without the run's CA key, with the read host's certificate"
"${tool}" exec "${host}-ng" /usr/local/bin/envoy --version > /dev/null || it_fail "the gateway isn't Envoy"
ca_source=$( "${tool}" inspect -f '{{range .Mounts}}{{if eq .Destination "/user-space/ca"}}{{.Source}}{{end}}{{end}}' "${host}-ng" )
[ -n "${ca_source}" ] || it_fail "the gateway has no /user-space/ca mount"
[ -z "$( ls -A "${ca_source}" )" ] || it_fail "the Envoy gateway was given the CA's directory (${ca_source})"
config="${resources}/gateway-config"
it_expect "${config}" "BEGIN CERTIFICATE"
it_expect "${config}" "intercept registry.npmjs.org:443"
it_expect_not "${config}" "$( sed -n 2p "${resources}/ca/gateway-ca-key.pem" )"
[ "$( stat -c %a "${resources}/ca/gateway-ca-key.pem" )" = 600 ] || it_fail "the CA's key isn't private"

it_say "the read host works, with the rule's methods, for itself only"
[ "$( status_of https://registry.npmjs.org/ )" = 200 ] || it_fail "registry.npmjs.org wasn't reachable through the gateway's certificate"
[ "$( status_of -X POST https://registry.npmjs.org/ )" = 403 ] || it_fail "a POST got past the rule's methods"
[ "$( status_of -H "'Host: example.com'" https://registry.npmjs.org/ )" = 403 ] \
    || it_fail "a request naming another host was sent on"

it_say "a subdomain of the wildcard is passed through"
[ "$( connect_of https://api.github.com/ )" = 200 ] || it_fail "api.github.com (*.github.com) wasn't reachable"

it_say "everything else is refused by Envoy's routes, without a lookup"
for url in https://github.com/ https://example.com/ https://registry.npmjs.org:8443/; do
    [ "$( connect_of "${url}" )" = 404 ] || it_fail "${url} wasn't refused by the gateway (404)"
done
[ "$( in_ssh "curl -s -o /dev/null -w '%{http_code}' --max-time 30 http://registry.npmjs.org/" || true )" = 404 ] \
    || it_fail "plain HTTP to an allowed host wasn't refused (404)"
gateway_log '"authority":"registry.npmjs.org","bytes_down"'
it_expect "${IT_SANDBOX}/gateway.log" '"authority":"example.com:443"'
it_expect "${IT_SANDBOX}/gateway.log" '"authority":"registry.npmjs.org:8443","bytes_down":0,"bytes_up":0,"code":404,"flags":"NR"'
# A refused request never got an upstream: Envoy didn't resolve it.
python3 -I - "${IT_SANDBOX}/gateway.log" <<'PY' || it_fail "Envoy reached an upstream for a refused request"
import json, sys
for line in open(sys.argv[1]):
    if line.startswith("{"):
        e = json.loads(line)
        assert e["code"] != 404 or e["upstream"] is None, line
PY

it_say "a tunnel outlives the default route timeout"
status=0
in_ssh "curl -s -o /dev/null --limit-rate 3k --max-time 25 https://registry.npmjs.org/react" || status=$?
[ "${status}" -eq 28 ] || it_fail "a slow download through the tunnel ended early (curl exit ${status}, not 28)"

it_say "scoter netlog reads Envoy's log"
gateway_log '"path":"/react","plane":"request"'
scoter netlog < /dev/null > "${IT_SANDBOX}/netlog.log" 2>&1 || { cat "${IT_SANDBOX}/netlog.log" >&2; it_fail "netlog failed"; }
it_expect "${IT_SANDBOX}/netlog.log" "registry.npmjs.org:443"
it_expect "${IT_SANDBOX}/netlog.log" "GET registry.npmjs.org/ -> 200"
it_expect "${IT_SANDBOX}/netlog.log" "POST registry.npmjs.org/ -> 403"
it_expect "${IT_SANDBOX}/netlog.log" "intercept"
it_expect "${IT_SANDBOX}/netlog.log" "{host: example.com, protocol: tls}"
it_expect "${IT_SANDBOX}/netlog.log" "{host: github.com, protocol: tls}"
it_expect "${IT_SANDBOX}/netlog.log" "{host: registry.npmjs.org, protocol: tls, port: 8443}"
it_expect "${IT_SANDBOX}/netlog.log" "{host: registry.npmjs.org, protocol: http}"

it_say "scoter kill stops it all"
kill -KILL -- "-${run_pid}" 2> /dev/null || kill -KILL "${run_pid}"
wait "${run_pid}" 2> /dev/null || true
scoter kill < /dev/null > "${IT_SANDBOX}/kill.log" 2>&1 || { cat "${IT_SANDBOX}/kill.log" >&2; it_fail "kill failed"; }
it_expect "${IT_SANDBOX}/kill.log" "Stopped ${host}."
for name in "${host}" "${host}-fr" "${host}-ng"; do
    if is_running "${name}"; then
        it_fail "${name} is still running"
    fi
done
[ ! -e "${resources}/ca" ] || it_fail "the run's gateway CA is still there"
"${tool}" volume rm "${host}-store" > /dev/null 2>&1 || true

it_pass
