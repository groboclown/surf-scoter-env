#!/bin/sh
#SPDX:MIT
# Start a configuration for a project with none: `scoter init` refuses a
# project that has files, then, for one that doesn't, unlocks the
# distribution's template without writing into the project.  `scoter lock`
# reviews and stores it (the project found by its configuration now), and
# the first `scoter push` creates the project's files directory.  No
# containers: the container tool is a stand-in that fails every build.
. "$( dirname "$0" )/../common/lib.sh"

it_init
cat > "${IT_SANDBOX}/tools/container-tool" <<'STUB'
#!/bin/sh
[ "$1" = build ] && exit 1
exit 0
STUB

it_say "init refuses a project that has its own files"
it_project "${IT_ROOT}/02-re-pull/project" git
scoter init > "${IT_SANDBOX}/refused.log" 2>&1 && it_fail "init replaced the project's files"
it_expect "${IT_SANDBOX}/refused.log" "This project already has"
it_expect "${IT_SANDBOX}/refused.log" "scoter pull"

it_say "init starts one from the template, outside the project"
it_project "${IT_TEST_DIR}/project" git
scoter user-setup --non-interactive > "${IT_SANDBOX}/user-setup.log" 2>&1 \
    || { cat "${IT_SANDBOX}/user-setup.log" >&2; it_fail "user setup failed"; }
scoter init > "${IT_SANDBOX}/init.log" 2>&1 || { cat "${IT_SANDBOX}/init.log" >&2; it_fail "init failed"; }
it_expect "${IT_SANDBOX}/init.log" "Started your configuration from the \`default\` template"
it_expect "${IT_SANDBOX}/init.log" "scoter lock"
[ ! -e "${IT_PROJECT}/.surf-scoter.d" ] || it_fail "init wrote into the project"

it_say "a run uses the unlocked copy, with a warning"
scoter run > "${IT_SANDBOX}/run.log" 2>&1 && it_fail "the stand-in build succeeded"
it_expect "${IT_SANDBOX}/run.log" "You're running your unlocked configuration"

it_say "locking it"
it_approve lock
[ "$( scoter list -q | wc -l )" -eq 1 ] || it_fail "expected one configuration"

it_say "the first push creates the project's files"
scoter push --yes > "${IT_SANDBOX}/push.log" 2>&1 || { cat "${IT_SANDBOX}/push.log" >&2; it_fail "the push failed"; }
it_expect "${IT_PROJECT}/.surf-scoter.d/project.yaml" "dev-container-path: dev-container"
[ -f "${IT_PROJECT}/.surf-scoter.d/dev-container/Dockerfile" ] || it_fail "the container wasn't pushed"
[ ! -e "${IT_PROJECT}/.surf-scoter.d/host.json" ] || it_fail "the host file was pushed"
git -C "${IT_PROJECT}" status --porcelain > "${IT_SANDBOX}/status"
it_expect "${IT_SANDBOX}/status" "?? .surf-scoter.d/"

it_pass
