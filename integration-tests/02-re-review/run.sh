#!/bin/sh
#SPDX:MIT
# Accept a project, change it, and check what the next review shows: what
# changed in full, what didn't on one line, and the Dockerfile's difference.
# No containers are built or run.
. "$( dirname "$0" )/../common/lib.sh"

it_init
it_project "${IT_TEST_DIR}/project"

it_say "accepting the project as it is"
it_accept
id=$( scoter list-accepted -q )

it_say "changing it"
it_overlay "${IT_TEST_DIR}/changed"

it_say "the re-review"
review="${IT_SANDBOX}/review.yaml"
scoter verify --output "${review}" 2> /dev/null
it_expect "${review}" "compared with your acceptance ${id}"
it_expect "${review}" "Since you accepted it: 1 changed since, 1 new, 1 container files changed."
# Unchanged: one line.
it_expect "${review}" "  #scoter: unchanged since you accepted it."
# New.
it_expect "${review}" "  #scoter: new: the project added this since you accepted it."
it_expect "${review}" '  - {host: "pypi.org", protocol: "tls", port: 443}'
# Changed: the project's setting, and what changed.
it_expect "${review}" "  #scoter: changed since you accepted it:"
it_expect "${review}" '  #scoter:   args: [{any: {}}] -> ["-la", {any: {}}]'
it_expect "${review}" '    args: ["-la", {any: {}}]'
# The Dockerfile's difference.
it_expect "${review}" "env/Dockerfile  "
it_expect "${review}" "  #scoter:     | +ENV CHANGED=1"
it_expect "${review}" "  #scoter:   env/dev-env.yaml  unchanged since you accepted it"

it_say "the run won't use the old acceptance for the changed files"
scoter run > "${IT_SANDBOX}/run.log" 2>&1 && it_fail "the run started with the changed files"
it_expect "${IT_SANDBOX}/run.log" "The project's files changed since you last accepted them (${id}): env/Dockerfile, project.yaml."
it_expect "${IT_SANDBOX}/run.log" "Run \`scoter verify\` to review them"

it_say "accepting the re-review as it stands"
it_accept
[ "$( scoter list-accepted -q | wc -l )" -eq 2 ] || it_fail "expected two acceptances"

it_pass
