#!/bin/sh
#SPDX:MIT
# Pull a project, change it, and pull again.  A run builds from the
# configuration in use, never the project's live files, and says when they
# changed since the last pull; a pull reads the committed files unless told
# otherwise; the new configuration keeps the one it replaced as
# `default.previous`, and `scoter switch` goes back to it.  No containers:
# the container tool is a stand-in that keeps what a build was given, and
# fails it.
. "$( dirname "$0" )/../common/lib.sh"

it_init
it_project "${IT_TEST_DIR}/project" git
built="${IT_SANDBOX}/built"
cat > "${IT_SANDBOX}/tools/container-tool" <<STUB
#!/bin/sh
# build ... DIR: keep DIR's files (its last argument), and fail the build.
if [ "\$1" = build ]; then
    for dir; do :; done
    rm -rf '${built}'
    cp -R "\$dir" '${built}'
    exit 1
fi
exit 0
STUB

# run_builds LOG: `scoter run`, which the stand-in's build stops.
run_builds() {
    scoter run > "$1" 2>&1 && it_fail "the stand-in build succeeded"
    it_expect "$1" "Building dev-env image"
}

it_say "user setup"
scoter user-setup --non-interactive > "${IT_SANDBOX}/user-setup.log" 2>&1 \
    || { cat "${IT_SANDBOX}/user-setup.log" >&2; it_fail "user setup failed"; }

it_say "pulling the project as committed"
it_approve pull
first=$( scoter list -q )
run_builds "${IT_SANDBOX}/run.log"
it_expect "${built}/Dockerfile" "FROM scratch"
it_expect_not "${IT_SANDBOX}/run.log" "changed since your last pull"

it_say "changing the project: the run still builds the configuration in use, and says so"
it_overlay "${IT_TEST_DIR}/changed"
run_builds "${IT_SANDBOX}/run.log"
it_expect "${IT_SANDBOX}/run.log" "The project's files changed since your last pull: \`scoter pull\` reviews the changes."
it_expect_not "${built}/Dockerfile" "ENV CHANGED=1"

it_say "a pull reads what's committed: nothing new"
scoter pull > "${IT_SANDBOX}/nothing.log" 2>&1 || { cat "${IT_SANDBOX}/nothing.log" >&2; it_fail "the pull failed"; }
it_expect "${IT_SANDBOX}/nothing.log" "Nothing to pull"

it_say "pulling the working tree: the review flags what isn't committed"
it_approve pull --working-tree
it_expect "${IT_SANDBOX}/pull.log" "Some files aren't as committed"
it_expect "${IT_SANDBOX}/pull.log" "The one it replaced is kept as \`default.previous\`"
run_builds "${IT_SANDBOX}/run.log"
it_expect "${built}/Dockerfile" "ENV CHANGED=1"
it_expect_not "${IT_SANDBOX}/run.log" "changed since your last pull"

it_say "switching back to the previous configuration"
scoter switch default.previous > "${IT_SANDBOX}/switch.log" 2>&1 \
    || { cat "${IT_SANDBOX}/switch.log" >&2; it_fail "the switch failed"; }
it_expect "${IT_SANDBOX}/switch.log" "Now using \`default.previous\` (${first})"
run_builds "${IT_SANDBOX}/run.log"
it_expect_not "${built}/Dockerfile" "ENV CHANGED=1"
scoter list > "${IT_SANDBOX}/list.log"
it_expect "${IT_SANDBOX}/list.log" "${first}  default.previous"
[ "$( scoter list -q | wc -l )" -eq 2 ] || it_fail "expected two configurations"

it_pass
