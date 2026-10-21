#!/bin/sh
#SPDX:MIT
# A full run: set up the user, accept the project, start it with
# `scoter run`, check the gateway lets out only the accepted host, then
# stop the run the hard way (as a crash would) and clean up with `scoter kill`.
#
# This builds the real images (the dev-env container compiles the broker),
# pulls their base images, and runs containers: it needs the container tool
# (IT_CONTAINER_TOOL, docker by default, with compose) and the network, and
# can take a while the first time.
. "$( dirname "$0" )/../common/lib.sh"

# How long to wait for the environment to start, in seconds.
IT_RUN_TIMEOUT="${IT_RUN_TIMEOUT:-1800}"

it_require setsid ssh "${IT_CONTAINER_TOOL}"
it_init
it_project "${IT_TEST_DIR}/project" git
tool="${IT_SANDBOX}/tools/container-tool"
running="${HOME}/.config/surf-scoter.d/state/running-env.json"

# The host of the one running environment, once its run has recorded it.
running_host() {
    python3 -I - "${running}" <<'PY'
import json, sys
try:
    envs = json.load(open(sys.argv[1]))
except (OSError, ValueError):
    envs = []
print(next((e["host_prefix"] + e["host_suffix"] for e in envs if e.get("suc")), ""))
PY
}

is_running() {
    [ "$( "${tool}" inspect -f '{{.State.Running}}' "$1" 2> /dev/null )" = "true" ]
}

it_say "user setup"
scoter user-setup --non-interactive > "${IT_SANDBOX}/user-setup.log" 2>&1 \
    || { cat "${IT_SANDBOX}/user-setup.log" >&2; it_fail "user setup failed"; }

it_say "accepting the project"
it_accept

it_say "starting the run (building the images; this can take a while)"
log="${IT_SANDBOX}/run.log"
setsid "${IT_DIST}/scoter" --project-dir "${IT_PROJECT}" run > "${log}" 2>&1 &
run_pid=$!
host=""
waited=0
while [ "${waited}" -lt "${IT_RUN_TIMEOUT}" ]; do
    kill -0 "${run_pid}" 2> /dev/null || { cat "${log}" >&2; it_fail "the run ended before the environment started"; }
    # (Not from the run's log: the sub-commands' output to a file is buffered.)
    host=$( running_host )
    if [ -n "${host}" ] && is_running "${host}" && is_running "${host}-ng"; then
        break
    fi
    sleep 5
    waited=$(( waited + 5 ))
done
[ -n "${host}" ] && is_running "${host}" || { cat "${log}" >&2; it_fail "the environment didn't start in ${IT_RUN_TIMEOUT} seconds"; }
it_say "running as ${host}"

it_say "the gateway lets out the accepted host, and nothing else"
"${tool}" exec "${host}" curl -sS -o /dev/null --max-time 30 https://registry.npmjs.org/ \
    || it_fail "the accepted host (registry.npmjs.org) wasn't reachable"
if "${tool}" exec "${host}" curl -sS -o /dev/null --max-time 30 https://example.com/ 2> /dev/null; then
    it_fail "a host that wasn't accepted (example.com) was reachable"
fi

it_say "an ssh session finds the host command's bridge first"
# ssh finds ~ from the password database, not HOME: give it the sandbox's files.
# sshd may still be starting: try for a while.
tries=0
until ssh -F "${HOME}/.ssh/config" -o "UserKnownHostsFile=${HOME}/.ssh/known_hosts" \
        -o BatchMode=yes -o ConnectTimeout=10 \
        "${host}" 'command -v ls' > "${IT_SANDBOX}/ssh.log" 2>&1; do
    tries=$(( tries + 1 ))
    [ "${tries}" -lt 12 ] || { cat "${IT_SANDBOX}/ssh.log" >&2; it_fail "couldn't ssh into the environment"; }
    sleep 5
done
it_expect "${IT_SANDBOX}/ssh.log" "/usr/local/scoter-bridge/ls"

it_say "the run crashes: its containers are left behind"
kill -KILL -- "-${run_pid}" 2> /dev/null || kill -KILL "${run_pid}"
wait "${run_pid}" 2> /dev/null || true
is_running "${host}" || it_fail "the containers went with the run; nothing to clean up"
[ "$( running_host )" = "${host}" ] || it_fail "the environment isn't listed any more"

it_say "scoter kill cleans up"
scoter kill > "${IT_SANDBOX}/kill.log" 2>&1 || { cat "${IT_SANDBOX}/kill.log" >&2; it_fail "kill failed"; }
it_expect "${IT_SANDBOX}/kill.log" "Stopped ${host}."
for name in "${host}" "${host}-fr" "${host}-ng"; do
    if is_running "${name}"; then
        it_fail "${name} is still running"
    fi
done
[ -z "$( running_host )" ] || it_fail "the environment is still listed"
[ ! -e "${HOME}/.config/surf-scoter.d/state/environments/${host}" ] || it_fail "its state directory is still there"
it_expect "${IT_SANDBOX}/kill.log" "The workspace is kept"

it_pass
