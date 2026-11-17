#!/bin/sh
#SPDX:MIT
# A full run with the seekrit gateway in place of Squid: the user's setup
# chooses it, and the project's egress rules pass one host through untouched
# and let the gateway read another, filling in a secret from the user's
# secret store.  Checks, from an ssh session in the dev container:
#   * the session has the secret's placeholder, never its value;
#   * the passed-through host works with no CA involved;
#   * the read host gets the secret, only with the methods its rule gives,
#     and nothing else gets out;
#   * the container tool shows no value: not in the gateway's environment,
#     configuration, or logs;
#   * after the gateway restarts, a placeholder gets 503 until
#     `scoter gateway-post-start` gives the secrets again;
#   * `scoter kill` removes the run's CA.
#
# Like 03, it builds the real images (the gateway compiles seekrit-proxy, with
# Surf Scoter's patches), runs containers, and needs the network: GitHub's API
# answers the request the secret goes into (401, for the made-up value).
. "$( dirname "$0" )/../common/lib.sh"

IT_RUN_TIMEOUT="${IT_RUN_TIMEOUT:-1800}"
# A made-up value: it must never show anywhere but the gateway's memory.
SECRET_VALUE="it-07-not-a-real-token-5f3a9c"

it_require setsid ssh "${IT_CONTAINER_TOOL}"
it_init
it_project "${IT_TEST_DIR}/project" git
tool="${IT_SANDBOX}/tools/container-tool"
running="${HOME}/.config/surf-scoter.d/state/running-env.json"

running_host() {
    python3 -I - "${running}" <<'PY'
import json, sys
try:
    envs = json.load(open(sys.argv[1]))
except (OSError, ValueError):
    envs = []
print(next((e["host"] for e in envs if e.get("suc")), ""))
PY
}

running_field() {
    python3 -I - "${running}" "$1" <<'PY'
import json, sys
envs = json.load(open(sys.argv[1]))
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
        host=$( running_host )
        if [ -n "${host}" ] && is_running "${host}" && is_running "${host}-ng" \
                && "${tool}" logs "${host}-ng" 2>&1 | grep -q "secrets injected"; then
            return 0
        fi
        sleep 5
        waited=$(( waited + 5 ))
    done
    cat "$1" >&2
    it_fail "the environment didn't start, with its secrets, in ${IT_RUN_TIMEOUT} seconds"
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
# the gateway; 000 when the connection itself is refused.  The ARGS are read
# by the session's shell, so `"$GITHUB_TOKEN"` there is its placeholder.
status_of() {
    in_ssh "curl -s -o /dev/null -w '%{http_code}' --max-time 30 $*" || true
}

it_say "the user's setup chooses the seekrit gateway"
cat >> "${SURF_SCOTER_HOME}/setup.yaml" <<'EOF'
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
EOF

it_say "user setup"
scoter user-setup --non-interactive > "${IT_SANDBOX}/user-setup.log" 2>&1 \
    || { cat "${IT_SANDBOX}/user-setup.log" >&2; it_fail "user setup failed"; }

it_say "pulling the project"
it_approve pull

it_say "the secret store, only the user's"
store=$( "${IT_DIST}/bin/gen-scoter-uc" --distribution-dir "${IT_DIST}" --project-dir "${IT_PROJECT}" \
    | python3 -I -c 'import json, sys; print(json.load(sys.stdin)["resolved"]["project-state"]["paths"]["secret-store"])' )
case "${store}" in
    "${SURF_SCOTER_HOME}"/secrets/*.toml) ;;
    *) it_fail "the secret store isn't where it should be: ${store}" ;;
esac
mkdir -p -m 700 "$( dirname "${store}" )"
printf '[secrets]\nGITHUB_TOKEN = { command = ["printf", "%%s\\n", "%s"] }\n' "${SECRET_VALUE}" > "${store}"
chmod 600 "${store}"

it_say "starting the run (building the images; this can take a while)"
start_run "${IT_SANDBOX}/run.log"
it_say "running as ${host}"
resources=$( running_field resources_dir )

it_say "the gateway was given its CA, and the secret, once"
"${tool}" exec "${host}-ng" /seekrit-proxy --version > /dev/null || it_fail "the gateway isn't seekrit-proxy"
[ "$( stat -c %a "${resources}/ca/gateway-ca-key.pem" )" = 644 ] \
    || it_fail "the gateway can't read its CA's key"

it_say "an ssh session has the placeholder, never the value"
in_ssh 'printenv GITHUB_TOKEN' > "${IT_SANDBOX}/env.log" || it_fail "no GITHUB_TOKEN in the session"
it_expect "${IT_SANDBOX}/env.log" "{{seekrit:GITHUB_TOKEN}}"
in_ssh 'env' > "${IT_SANDBOX}/all-env.log" || true
it_expect_not "${IT_SANDBOX}/all-env.log" "${SECRET_VALUE}"

it_say "the passed-through host works, with no CA involved"
[ "$( status_of https://pypi.org/simple/ )" = 200 ] || it_fail "pypi.org wasn't reachable"
"${tool}" logs "${host}-ng" > "${IT_SANDBOX}/gateway.log" 2>&1
it_expect "${IT_SANDBOX}/gateway.log" '"host":"pypi.org","port":443,"action":"tunnel"'

it_say "the read host gets the secret, with the methods its rule gives, and nothing else gets out"
[ "$( status_of -H '"Authorization: Bearer $GITHUB_TOKEN"' https://api.github.com/user )" = 401 ] \
    || it_fail "api.github.com didn't answer the request (with the made-up value, 401)"
"${tool}" logs "${host}-ng" > "${IT_SANDBOX}/gateway.log" 2>&1
it_expect "${IT_SANDBOX}/gateway.log" '"method":"GET","host":"api.github.com","port":443,"path":"/user","status":401'
it_expect "${IT_SANDBOX}/gateway.log" '"injected":"GITHUB_TOKEN"'
[ "$( status_of -X POST https://api.github.com/user )" = 403 ] || it_fail "a POST got past the rule's methods"
[ "$( status_of https://example.com/ )" = 000 ] || it_fail "a host without a rule was reachable"

it_say "the container tool shows no value"
"${tool}" inspect "${host}-ng" > "${IT_SANDBOX}/inspect.log" 2>&1
"${tool}" logs "${host}-ng" > "${IT_SANDBOX}/gateway.log" 2>&1
for file in inspect.log gateway.log run.log; do
    it_expect_not "${IT_SANDBOX}/${file}" "${SECRET_VALUE}"
done
it_expect_not "${resources}/gateway-config" "${SECRET_VALUE}"

it_say "scoter netlog shows what it did, and suggests a rule for the refused host"
scoter netlog < /dev/null > "${IT_SANDBOX}/netlog.log" 2>&1 || { cat "${IT_SANDBOX}/netlog.log" >&2; it_fail "netlog failed"; }
it_expect "${IT_SANDBOX}/netlog.log" "GET api.github.com/user -> 401"
it_expect "${IT_SANDBOX}/netlog.log" "with GITHUB_TOKEN"
it_expect "${IT_SANDBOX}/netlog.log" "POST api.github.com/user -> 403"
it_expect "${IT_SANDBOX}/netlog.log" "{host: example.com, protocol: tls}"
it_expect_not "${IT_SANDBOX}/netlog.log" "${SECRET_VALUE}"
scoter netlog --json < /dev/null > "${IT_SANDBOX}/netlog.json" 2>&1 || it_fail "netlog --json failed"
python3 -I -c 'import json, sys; e = json.load(open(sys.argv[1])); assert any(x["injected"] == ["GITHUB_TOKEN"] for x in e)' \
    "${IT_SANDBOX}/netlog.json" || it_fail "netlog --json doesn't list the injected request"
# --follow, reading the log again (the setup has no `logs-follow`): a new event shows up.
( cd "${IT_PROJECT}" && exec "${IT_DIST}/scoter" netlog --follow ) < /dev/null > "${IT_SANDBOX}/follow.log" 2>&1 &
follow_pid=$!
sleep 8
status_of https://pypi.org/simple/ > /dev/null
sleep 8
kill "${follow_pid}" 2> /dev/null || true
wait "${follow_pid}" 2> /dev/null || true
[ "$( grep -c 'tunnel pypi.org:443' "${IT_SANDBOX}/follow.log" )" -ge 2 ] \
    || { cat "${IT_SANDBOX}/follow.log" >&2; it_fail "netlog --follow didn't show the new connection"; }

it_say "after the gateway restarts, a placeholder waits for scoter gateway-post-start"
"${tool}" restart "${host}-ng" > /dev/null || it_fail "couldn't restart the gateway"
waited=0
until [ "$( status_of -H '"Authorization: Bearer $GITHUB_TOKEN"' https://api.github.com/user )" = 503 ]; do
    waited=$(( waited + 5 ))
    [ "${waited}" -lt 120 ] || it_fail "the restarted gateway didn't refuse the placeholder with 503"
    sleep 5
done
scoter gateway-post-start < /dev/null > "${IT_SANDBOX}/post-start.log" 2>&1 \
    || { cat "${IT_SANDBOX}/post-start.log" >&2; it_fail "gateway-post-start failed"; }
it_expect_not "${IT_SANDBOX}/post-start.log" "${SECRET_VALUE}"
[ "$( status_of -H '"Authorization: Bearer $GITHUB_TOKEN"' https://api.github.com/user )" = 401 ] \
    || it_fail "the secret wasn't given again"
it_say "a second gateway-post-start finds it has its secrets"
# Long enough after the restart that a missing socket means it's been used.
sleep 65
scoter gateway-post-start < /dev/null > "${IT_SANDBOX}/post-start2.log" 2>&1 \
    && it_fail "a second delivery was taken"
it_expect "${IT_SANDBOX}/post-start2.log" "already has its secrets"

it_say "the run crashes, and scoter kill cleans up, the CA with it"
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
