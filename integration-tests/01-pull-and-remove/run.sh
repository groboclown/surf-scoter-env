#!/bin/sh
#SPDX:MIT
# Pull a project the checks refuse as it stands (it runs a shell), through
# the pull's plumbing: seed a working directory, analyze it, see the approval
# refused, edit the copy, see what changed, analyze again, and approve it.
# Then list the configuration, and remove it.  No containers are built or run.
. "$( dirname "$0" )/../common/lib.sh"

# json FILE KEY...: the value at KEY... in the JSON file FILE.
json() {
    python3 -I - "$@" <<'PY'
import json, sys
value = json.load(open(sys.argv[1]))
for key in sys.argv[2:]:
    value = value[int(key) if isinstance(value, list) else key]
print(value if not isinstance(value, (dict, list)) else json.dumps(value))
PY
}

it_init
it_project "${IT_TEST_DIR}/project"

it_say "without a configuration, the run refuses"
scoter run > "${IT_SANDBOX}/run.log" 2>&1 && it_fail "the run started without a configuration"
it_expect "${IT_SANDBOX}/run.log" "You have no configuration for this project: run \`scoter pull\`"

it_say "without a terminal, the reviewer refuses, and nothing is approved"
scoter pull < /dev/null > "${IT_SANDBOX}/pull.log" 2>&1 && it_fail "a pull was approved without a terminal"
it_expect "${IT_SANDBOX}/pull.log" "The review needs a terminal"
it_expect "${IT_SANDBOX}/pull.log" "Not approved: nothing changed."
[ -z "$( scoter list -q )" ] || it_fail "a configuration was stored"

it_say "seeding a working directory"
scoter pull seed > "${IT_SANDBOX}/seed.json"
id=$( json "${IT_SANDBOX}/seed.json" id )
work=$( json "${IT_SANDBOX}/seed.json" path )
# Not in a repository: the files on disk, with a warning.
it_expect "${IT_SANDBOX}/seed.json" "read from the working tree"
[ -f "${work}/files/project.yaml" ] && [ -f "${work}/host.json" ] || it_fail "the working directory isn't laid out as files/ and host.json"
[ ! -e "${work}/files/host.json" ] || it_fail "the host file is among the project's files"

it_say "analyzing it: the shell is refused"
scoter pull analyze "${id}" > "${IT_SANDBOX}/report.json"
it_expect "${IT_SANDBOX}/report.json" '"$schema": "surf-scoter-pull-analysis-1"'
it_expect "${IT_SANDBOX}/report.json" '"verification:shells"'
it_expect "${IT_SANDBOX}/report.json" '"text": "registry.npmjs.org"'
analysis=$( json "${IT_SANDBOX}/report.json" id )
scoter pull approve "${id}" "${analysis}" > "${IT_SANDBOX}/approve.log" 2>&1 && it_fail "a refused configuration was approved"
it_expect "${IT_SANDBOX}/approve.log" "the checks refuse it"

it_say "editing the copy: the change is noticed, and the old analysis no longer approves"
grep -v 'command: sh' "${work}/files/project.yaml" > "${work}/files/project.yaml.new"
mv "${work}/files/project.yaml.new" "${work}/files/project.yaml"
scoter pull changed "${id}" "${analysis}" > "${IT_SANDBOX}/changed.json"
it_expect "${IT_SANDBOX}/changed.json" '"project.yaml"'
scoter pull approve "${id}" "${analysis}" > "${IT_SANDBOX}/approve.log" 2>&1 && it_fail "a stale analysis was approved"
it_expect "${IT_SANDBOX}/approve.log" "changed since analysis ${analysis}"

it_say "analyzing it again, and approving"
scoter pull analyze "${id}" > "${IT_SANDBOX}/report.json"
it_expect_not "${IT_SANDBOX}/report.json" '"verification:shells"'
analysis=$( json "${IT_SANDBOX}/report.json" id )
scoter pull approve "${id}" "${analysis}" > "${IT_SANDBOX}/approved.json" 2>&1 \
    || { cat "${IT_SANDBOX}/approved.json" >&2; it_fail "the approval failed"; }
config=$( json "${IT_SANDBOX}/approved.json" configuration )
it_expect "${IT_SANDBOX}/approved.json" '"name": "default"'
# The project's own files are untouched.
it_expect "${IT_PROJECT}/.surf-scoter.d/project.yaml" "command: sh"

it_say "listing it"
[ "$( scoter list -q )" = "${config}" ] || it_fail "expected the one configuration"
scoter list > "${IT_SANDBOX}/list.log"
it_expect "${IT_SANDBOX}/list.log" "${IT_PROJECT}"
it_expect "${IT_SANDBOX}/list.log" "${config}  default"
it_expect "${IT_SANDBOX}/list.log" "(in use)"

it_say "removing everything"
scoter remove $( scoter list -q ) > "${IT_SANDBOX}/remove.log"
it_expect "${IT_SANDBOX}/remove.log" "Removed 1:"
scoter list > "${IT_SANDBOX}/list.log"
it_expect "${IT_SANDBOX}/list.log" "You have no configurations."
scoter run > "${IT_SANDBOX}/run.log" 2>&1 && it_fail "the run started after its configuration was removed"
it_expect "${IT_SANDBOX}/run.log" "You have no configuration for this project"

it_pass
