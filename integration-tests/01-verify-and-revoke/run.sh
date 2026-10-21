#!/bin/sh
#SPDX:MIT
# Review a project, accept it, list the acceptance, and revoke it.
# No containers are built or run.
. "$( dirname "$0" )/../common/lib.sh"

it_init
it_project "${IT_TEST_DIR}/project"

it_say "without an acceptance, the run refuses"
scoter run > "${IT_SANDBOX}/run.log" 2>&1 && it_fail "the run started without an acceptance"
it_expect "${IT_SANDBOX}/run.log" "You haven't accepted this project's settings"

it_say "without a terminal, the review refuses to ask"
scoter verify < /dev/null > "${IT_SANDBOX}/verify.log" 2>&1 && it_fail "the review asked without a terminal"
it_expect "${IT_SANDBOX}/verify.log" "needs a terminal"

it_say "the review file"
scoter verify --output "${IT_SANDBOX}/review.yaml" 2> /dev/null
it_expect "${IT_SANDBOX}/review.yaml" 'review: "surf-scoter-review-1"'
it_expect "${IT_SANDBOX}/review.yaml" '  - {host: "registry.npmjs.org", protocol: "tls", port: 443}'
it_expect "${IT_SANDBOX}/review.yaml" '  - command: "ls"'
# The shell is refused, so it's commented out, with its override offered.
it_expect "${IT_SANDBOX}/review.yaml" '  # - command: "sh"'
it_expect "${IT_SANDBOX}/review.yaml" '    # $override: ["verification:shells"]'
# What the setup changes about the host command checks (nothing, here), unless left out.
it_expect "${IT_SANDBOX}/review.yaml" "#scoter: Your setup's host command checks: none removed, replaced or overridden."
scoter verify --skip-setup-review --output "${IT_SANDBOX}/skipped.yaml" 2> /dev/null
it_expect_not "${IT_SANDBOX}/skipped.yaml" "host command checks"

it_say "accepting it"
it_accept
scoter list-accepted -q > "${IT_SANDBOX}/ids"
[ "$( wc -l < "${IT_SANDBOX}/ids" )" -eq 1 ] || it_fail "expected one acceptance"
scoter list-accepted > "${IT_SANDBOX}/list.log"
it_expect "${IT_SANDBOX}/list.log" "${IT_PROJECT}"
it_expect "${IT_SANDBOX}/list.log" "$( cat "${IT_SANDBOX}/ids" )  reviewed"

it_say "a second review compares with it"
scoter verify --output "${IT_SANDBOX}/again.yaml" 2> /dev/null
it_expect "${IT_SANDBOX}/again.yaml" "compared with your acceptance $( cat "${IT_SANDBOX}/ids" )"
it_expect "${IT_SANDBOX}/again.yaml" "Since you accepted it: nothing changed."

it_say "revoking everything"
scoter revoke $( scoter list-accepted -q ) > "${IT_SANDBOX}/revoke.log"
it_expect "${IT_SANDBOX}/revoke.log" "Revoked 1:"
scoter list-accepted > "${IT_SANDBOX}/list.log"
it_expect "${IT_SANDBOX}/list.log" "You haven't accepted any project's settings."

it_pass
