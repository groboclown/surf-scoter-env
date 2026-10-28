#!/bin/sh
#SPDX:MIT
# A full run: set up the user, accept the project, start it with
# `scoter run`, check the gateway lets out only the accepted host, then
# stop the run the hard way (as a crash would) and clean up with `scoter kill`.
# Then FILE-1: with hooks and configuration planted in the project's clone,
# a second run must give the container the project's new commits (through
# the bundle) without running any of them on the host, and the container's
# commits must come back by fetching from the clone.
#
# This builds the real images (the dev-env container compiles the broker),
# pulls their base images, and runs containers: it needs the container tool
# (IT_CONTAINER_TOOL, docker by default, with compose) and the network, and
# can take a while the first time.
. "$( dirname "$0" )/../common/lib.sh"

# How long to wait for the environment to start, in seconds.
IT_RUN_TIMEOUT="${IT_RUN_TIMEOUT:-1800}"

# `setsid` (util-linux) isn't on macOS by default.  The run gets its own
# process group, so the crash below can kill all of it.
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
print(next((e["host"] for e in envs if e.get("suc")), ""))
PY
}

is_running() {
    [ "$( "${tool}" inspect -f '{{.State.Running}}' "$1" 2> /dev/null )" = "true" ]
}

# A field of the one running environment's record.
running_field() {
    python3 -I - "${running}" "$1" <<'PY'
import json, sys
envs = json.load(open(sys.argv[1]))
print(next((e[sys.argv[2]] for e in envs if e.get("suc")), ""))
PY
}

# start_run LOG: start `scoter run` in the background, as its own process
# group, and wait until its environment is up; sets run_pid and host.
start_run() {
    setsid "${IT_DIST}/scoter" --project-dir "${IT_PROJECT}" run > "$1" 2>&1 &
    run_pid=$!
    host=""
    waited=0
    while [ "${waited}" -lt "${IT_RUN_TIMEOUT}" ]; do
        kill -0 "${run_pid}" 2> /dev/null || { cat "$1" >&2; it_fail "the run ended before the environment started"; }
        # (Not from the run's log: the sub-commands' output to a file is buffered.)
        host=$( running_host )
        if [ -n "${host}" ] && is_running "${host}" && is_running "${host}-ng"; then
            return 0
        fi
        sleep 5
        waited=$(( waited + 5 ))
    done
    cat "$1" >&2
    it_fail "the environment didn't start in ${IT_RUN_TIMEOUT} seconds"
}

# in_clone GIT-ARGS...: Git in the project's clone, inside the container, as
# its user (who has the clone as a safe.directory).  The fsmonitor planted
# below is a host path, missing here.
in_clone() {
    "${tool}" exec "${host}" git -c core.fsmonitor=false -C /project "$@"
}

host_git() {
    git -C "${IT_PROJECT}" -c user.name=it -c user.email=it@example.invalid "$@"
}

it_say "user setup"
scoter user-setup --non-interactive > "${IT_SANDBOX}/user-setup.log" 2>&1 \
    || { cat "${IT_SANDBOX}/user-setup.log" >&2; it_fail "user setup failed"; }

it_say "accepting the project"
it_accept

it_say "starting the run (building the images; this can take a while)"
start_run "${IT_SANDBOX}/run.log"
it_say "running as ${host}"
clone=$( running_field project_clone_dir )
setup=$( running_field setup_dir )
case "${host}" in
    scoter-project-*) ;;
    *) it_fail "the environment isn't named by its project: ${host}" ;;
esac

it_say "it has its own ssh config entry, and a second run of the project is refused"
it_expect "${HOME}/.ssh/config" "Host ${host}"
it_expect "${HOME}/.ssh/config" "# Surf Scoter environment"
"${IT_DIST}/scoter" --project-dir "${IT_PROJECT}" run > "${IT_SANDBOX}/second.log" 2>&1 \
    && it_fail "a second run of the project started"
it_expect "${IT_SANDBOX}/second.log" "${host} is already running for this project"

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

it_say "the clone's origin is the bundle, and the container fetched the project's branch from it"
[ -f "${setup}/project.bundle" ] || it_fail "there's no bundle in ${setup}"
[ "$( git config --file "${clone}/.git/config" remote.origin.url )" = "/opt/surf-scoter-setup/project.bundle" ] \
    || it_fail "the clone's origin isn't the bundle"
[ "$( in_clone rev-parse origin/main )" = "$( host_git rev-parse main )" ] \
    || it_fail "the container's origin/main isn't the project's main"

it_say "the run crashes: its containers are left behind"
kill -KILL -- "-${run_pid}" 2> /dev/null || kill -KILL "${run_pid}"
wait "${run_pid}" 2> /dev/null || true
is_running "${host}" || it_fail "the containers went with the run; nothing to clean up"
[ "$( running_host )" = "${host}" ] || it_fail "the environment isn't listed any more"

it_say "scoter kill cleans up"
scoter kill < /dev/null > "${IT_SANDBOX}/kill.log" 2>&1 || { cat "${IT_SANDBOX}/kill.log" >&2; it_fail "kill failed"; }
it_expect "${IT_SANDBOX}/kill.log" "Stopped ${host}."
for name in "${host}" "${host}-fr" "${host}-ng"; do
    if is_running "${name}"; then
        it_fail "${name} is still running"
    fi
done
[ -z "$( running_host )" ] || it_fail "the environment is still listed"
[ ! -e "${HOME}/.config/surf-scoter.d/state/environments/${host}" ] || it_fail "its state directory is still there"
it_expect "${IT_SANDBOX}/kill.log" "The project clone is kept"
"${tool}" volume inspect "${host}-store" > /dev/null 2>&1 || it_fail "the persistent store went with the kill"

it_say "without a terminal, its ssh entry is kept (config-cleanup: ask), and cleanup lists it"
it_expect "${IT_SANDBOX}/kill.log" "Kept the ssh entries for ${host}"
it_expect "${HOME}/.ssh/config" "Host ${host}"
scoter cleanup --force < /dev/null > "${IT_SANDBOX}/cleanup.log" 2>&1 \
    || { cat "${IT_SANDBOX}/cleanup.log" >&2; it_fail "cleanup failed"; }
it_expect "${IT_SANDBOX}/cleanup.log" "SSH config entries for environments that aren't running:"
it_expect "${IT_SANDBOX}/cleanup.log" "The ssh config entries were kept: there's no terminal to ask at."
it_expect "${HOME}/.ssh/config" "Host ${host}"

it_say "FILE-1: plant hooks and configuration in the clone, as the container could"
# The configuration names the traps by their host paths, so Git run on the
# host would run them; inside the container those paths don't exist.  Each
# trap writes to a file on the host, and succeeds.
pwned="${IT_SANDBOX}/pwned"
traps="${clone}/.git/traps"
mkdir -p "${traps}" "${clone}/.git/hooks"
for name in reference-transaction pre-receive update post-receive post-update post-checkout post-merge; do
    for dir in "${traps}" "${clone}/.git/hooks"; do
        printf '#!/bin/sh\necho "%s $0" >> "%s" 2> /dev/null\nexit 0\n' "${name}" "${pwned}" > "${dir}/${name}"
        chmod 755 "${dir}/${name}"
    done
done
# An fsmonitor that fails makes Git scan for itself; a smudge filter that
# passes its input through changes nothing.
printf '#!/bin/sh\necho "fsmonitor" >> "%s" 2> /dev/null\nexit 1\n' "${pwned}" > "${traps}/fsmonitor"
printf '#!/bin/sh\necho "smudge" >> "%s" 2> /dev/null\ncat\n' "${pwned}" > "${traps}/smudge"
chmod 755 "${traps}/fsmonitor" "${traps}/smudge"
cat >> "${clone}/.git/config" <<TRAPS
[core]
	hooksPath = ${traps}
	fsmonitor = ${traps}/fsmonitor
[filter "trap"]
	smudge = ${traps}/smudge
TRAPS
echo '* filter=trap' > "${clone}/.git/info/attributes"

it_say "a new commit in the project, and a second run"
host_git commit -q --allow-empty -m "After the first run"
start_run "${IT_SANDBOX}/run2.log"
[ ! -e "${pwned}" ] || { cat "${pwned}" >&2; it_fail "something planted in the clone ran on the host"; }
[ "$( in_clone rev-parse origin/main )" = "$( host_git rev-parse main )" ] \
    || it_fail "the container didn't get the project's new commit from the bundle"

it_say "the container's commits come back by fetching from the clone"
in_clone -c user.name=it -c user.email=it@example.invalid switch -q -c from-container
in_clone -c user.name=it -c user.email=it@example.invalid commit -q --allow-empty -m "From the container"
host_git remote add scoter "${clone}"
host_git config remote.scoter.fetch '+refs/heads/*:refs/remotes/scoter/*'
host_git fetch -q --no-recurse-submodules scoter
[ "$( host_git rev-parse scoter/from-container )" = "$( in_clone rev-parse from-container )" ] \
    || it_fail "the container's branch didn't come back"
[ ! -e "${pwned}" ] || { cat "${pwned}" >&2; it_fail "fetching from the clone ran something planted in it"; }

it_say "stopping the second run"
# As the first: a background command of a non-interactive shell starts with
# SIGINT ignored, so the run can't be stopped as Ctrl-C would.
kill -KILL -- "-${run_pid}" 2> /dev/null || kill -KILL "${run_pid}"
wait "${run_pid}" 2> /dev/null || true
scoter kill < /dev/null > "${IT_SANDBOX}/kill2.log" 2>&1 || { cat "${IT_SANDBOX}/kill2.log" >&2; it_fail "kill failed"; }
it_expect "${IT_SANDBOX}/kill2.log" "Stopped ${host}."
[ ! -e "${pwned}" ] || { cat "${pwned}" >&2; it_fail "stopping the run ran something planted in the clone"; }

it_pass
