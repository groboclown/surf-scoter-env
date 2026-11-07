#!/bin/sh
#SPDX:MIT
# Edit a configuration: pull 02's sample project, unlock it, edit the copy
# and run it (with the unlocked warning), throw the edits away with `scoter
# lock --revert`, then unlock, edit and `scoter lock` for real.  Push the
# result into the project, and review the configuration again.  No
# containers: the container tool is a stand-in that keeps what a build was
# given, and fails it.
. "$( dirname "$0" )/../common/lib.sh"

it_init
it_project "${IT_ROOT}/02-re-pull/project" git
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

# unlock: `scoter unlock`; sets `unlocked` to the copy's files.
unlock() {
    scoter unlock > "${IT_SANDBOX}/unlock.log" 2>&1 || { cat "${IT_SANDBOX}/unlock.log" >&2; it_fail "unlock failed"; }
    unlocked=$( sed -n 's/^Unlocked: edit \(.*\)\.  `scoter lock`.*/\1/p' "${IT_SANDBOX}/unlock.log" )
    [ -f "${unlocked}/env/Dockerfile" ] || { cat "${IT_SANDBOX}/unlock.log" >&2; it_fail "the unlocked copy isn't there"; }
}

edit() {
    printf 'RUN echo %s\n' "$1" >> "${unlocked}/env/Dockerfile"
}

it_say "user setup, and pulling the project"
scoter user-setup --non-interactive > "${IT_SANDBOX}/user-setup.log" 2>&1 \
    || { cat "${IT_SANDBOX}/user-setup.log" >&2; it_fail "user setup failed"; }
it_approve pull
first=$( scoter list -q )

it_say "unlocking it, and running an edit"
unlock
scoter unlock > "${IT_SANDBOX}/again.log" 2>&1 && it_fail "unlocked twice"
it_expect "${IT_SANDBOX}/again.log" "already unlocked"
edit tried
run_builds "${IT_SANDBOX}/run.log"
it_expect "${IT_SANDBOX}/run.log" "You're running your unlocked configuration"
it_expect "${built}/Dockerfile" "RUN echo tried"

it_say "while unlocked, a pull and a push refuse"
scoter pull > "${IT_SANDBOX}/pull.log" 2>&1 && it_fail "a pull ran while unlocked"
it_expect "${IT_SANDBOX}/pull.log" "Your configuration is unlocked"
scoter push --yes > "${IT_SANDBOX}/push.log" 2>&1 && it_fail "a push ran while unlocked"
it_expect "${IT_SANDBOX}/push.log" "unlocked"

it_say "throwing the edit away: asked first, then reverted"
scoter lock --revert < /dev/null > "${IT_SANDBOX}/revert.log" 2>&1 && it_fail "reverted without asking"
it_expect "${IT_SANDBOX}/revert.log" "  env/Dockerfile"
it_expect "${IT_SANDBOX}/revert.log" "Kept: your configuration is still unlocked."
scoter lock --revert --yes > "${IT_SANDBOX}/revert.log" 2>&1 || { cat "${IT_SANDBOX}/revert.log" >&2; it_fail "revert failed"; }
[ ! -e "${unlocked}" ] || it_fail "the unlocked copy is still there"
run_builds "${IT_SANDBOX}/run.log"
it_expect_not "${built}/Dockerfile" "RUN echo tried"
it_expect_not "${IT_SANDBOX}/run.log" "unlocked"

it_say "unlocking, editing and locking"
unlock
edit kept
it_approve lock
[ ! -e "${unlocked}" ] || it_fail "the unlocked copy is still there after the lock"
locked=$( scoter list -q )
[ "${locked}" != "${first}" ] || it_fail "the lock stored nothing new"
[ "$( printf '%s\n' "${locked}" | wc -l )" -eq 1 ] || it_fail "the configuration unlocked from wasn't replaced"
run_builds "${IT_SANDBOX}/run.log"
it_expect "${built}/Dockerfile" "RUN echo kept"

it_say "pushing it into the project"
scoter push --yes > "${IT_SANDBOX}/push.log" 2>&1 || { cat "${IT_SANDBOX}/push.log" >&2; it_fail "the push failed"; }
it_expect "${IT_SANDBOX}/push.log" "Wrote 1 file(s)"
it_expect "${IT_PROJECT}/.surf-scoter.d/env/Dockerfile" "RUN echo kept"
[ ! -e "${IT_PROJECT}/.surf-scoter.d/host.json" ] || it_fail "the host file was pushed"
git -C "${IT_PROJECT}" status --porcelain > "${IT_SANDBOX}/status"
it_expect "${IT_SANDBOX}/status" " M .surf-scoter.d/env/Dockerfile"
# The developer's own push isn't a change in the project.
run_builds "${IT_SANDBOX}/run.log"
it_expect_not "${IT_SANDBOX}/run.log" "changed since your last pull"

it_say "reviewing it again"
it_approve review
[ "$( scoter list -q | wc -l )" -eq 1 ] || it_fail "the reviewed configuration wasn't replaced"

it_pass
