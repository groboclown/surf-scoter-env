#!/bin/sh
#SPDX:MIT
# `scoter cleanup`: accept two projects, delete one, and leave a run's files,
# a project state directory, a persistent store and an image behind; check
# that the cleanup lists them, removes nothing until it's told to, and then
# removes only what nothing uses.  Uses 02's sample project.  No containers:
# the container tool is a stand-in that lists one leftover image and volume,
# and records what it's asked.
. "$( dirname "$0" )/../common/lib.sh"

it_init
state="${HOME}/.config/surf-scoter.d/state"
log="${IT_SANDBOX}/container-tool.log"
cat > "${IT_SANDBOX}/tools/container-tool" <<STUB
#!/bin/sh
echo "\$*" >> '${log}'
if [ "\$1 \$2" = "image ls" ]; then
    echo 'local-surf-scoter/scoter-ubuntu-20001-z:latest'
fi
if [ "\$1 \$2" = "volume ls" ]; then
    echo 'scoter-old-project-0123456789ab-store'
fi
exit 0
STUB

it_say "accepting a project, then moving it and accepting it again"
it_project "${IT_ROOT}/02-re-review/project"
it_accept
gone_id=$( scoter list-accepted -q )
# The first acceptance is for the old directory, which is gone now.
mv "${IT_PROJECT}" "${IT_SANDBOX}/moved"
IT_PROJECT="${IT_SANDBOX}/moved"
it_accept
kept_id=$( scoter list-accepted -q | grep -v "${gone_id}" )

it_say "leaving a run's files and a project's state behind"
mkdir -p -m 700 "${state}/environments/scoter-ubuntu-20001-q" "${state}/projects/old-project-0123456789ab/project-clone"

it_say "a dry run lists them, and removes nothing"
scoter cleanup --dry-run --project-state --persistent-stores --images > "${IT_SANDBOX}/dry.log" 2>&1 \
    || { cat "${IT_SANDBOX}/dry.log" >&2; it_fail "the dry run failed"; }
it_expect "${IT_SANDBOX}/dry.log" "${state}/environments/scoter-ubuntu-20001-q"
it_expect "${IT_SANDBOX}/dry.log" "Accepted settings of projects whose directory is gone:"
it_expect "${IT_SANDBOX}/dry.log" "${state}/projects/old-project-0123456789ab"
it_expect "${IT_SANDBOX}/dry.log" "scoter-old-project-0123456789ab-store"
it_expect "${IT_SANDBOX}/dry.log" "local-surf-scoter/scoter-ubuntu-20001-z:latest"
it_expect "${IT_SANDBOX}/dry.log" "Nothing was removed (--dry-run)."
[ -d "${state}/environments/scoter-ubuntu-20001-q" ] || it_fail "the dry run removed something"

it_say "without a terminal or --force, nothing is removed"
scoter cleanup < /dev/null > "${IT_SANDBOX}/ask.log" 2>&1
it_expect "${IT_SANDBOX}/ask.log" "give --force"
[ "$( scoter list-accepted -q | wc -l )" -eq 2 ] || it_fail "an acceptance went without --force"

it_say "asked at a terminal, no removes nothing"
printf 'n\n' | it_tty "'${IT_DIST}/scoter' cleanup" > "${IT_SANDBOX}/no.log" 2>&1 || true
it_expect "${IT_SANDBOX}/no.log" "Remove these?"
it_expect "${IT_SANDBOX}/no.log" "Nothing was removed."

it_say "--force removes it, but not the project state, store or image unless asked"
scoter cleanup --force > "${IT_SANDBOX}/force.log" 2>&1 \
    || { cat "${IT_SANDBOX}/force.log" >&2; it_fail "the cleanup failed"; }
[ ! -e "${state}/environments/scoter-ubuntu-20001-q" ] || it_fail "the run's files are still there"
[ "$( scoter list-accepted -q )" = "${kept_id}" ] || it_fail "expected only the kept project's acceptance"
[ -d "${state}/projects/old-project-0123456789ab" ] || it_fail "the project state went without --project-state"
it_expect_not "${log}" "image rm"
it_expect_not "${log}" "volume rm"

it_say "--project-state, --persistent-stores and --images"
scoter cleanup --force --project-state --persistent-stores --images > "${IT_SANDBOX}/all.log" 2>&1 \
    || { cat "${IT_SANDBOX}/all.log" >&2; it_fail "the cleanup failed"; }
[ ! -e "${state}/projects/old-project-0123456789ab" ] || it_fail "the project state is still there"
it_expect "${log}" "volume rm scoter-old-project-0123456789ab-store"
it_expect "${log}" "image rm local-surf-scoter/scoter-ubuntu-20001-z:latest"
it_expect "${log}" "image prune -f"

it_pass
