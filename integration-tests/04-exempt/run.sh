#!/bin/sh
#SPDX:MIT
# An exemption: accept a project, start `scoter exempt`, change its files,
# and check that a run on the branch uses the container files as they are,
# with the project file and `dev-env.yaml` as accepted; that another branch
# isn't exempt; and that `scoter verify --with-exemption` picks up the
# settings' changes (refusing a suggestion the checks refuse).  Uses 02's
# sample project.  No containers: the container tool is a stand-in that
# keeps what a build was given, and fails it.
. "$( dirname "$0" )/../common/lib.sh"

it_init
it_project "${IT_ROOT}/02-re-review/project" git
built="${IT_SANDBOX}/built"
cat > "${IT_SANDBOX}/tools/container-tool" <<STUB
#!/bin/sh
# build -t TAG DIR: keep DIR's files, and fail the build.
if [ "\$1" = build ]; then
    rm -rf '${built}'
    cp -R "\$4" '${built}'
    exit 1
fi
exit 0
STUB

it_say "user setup"
scoter user-setup --non-interactive > "${IT_SANDBOX}/user-setup.log" 2>&1 \
    || { cat "${IT_SANDBOX}/user-setup.log" >&2; it_fail "user setup failed"; }

it_say "accepting the project, and starting an exemption"
it_accept
id=$( scoter list-accepted -q )
scoter exempt > "${IT_SANDBOX}/exempt.log" 2>&1 || { cat "${IT_SANDBOX}/exempt.log" >&2; it_fail "exempt failed"; }
it_expect "${IT_SANDBOX}/exempt.log" "Exempt on main until "
it_expect "${IT_SANDBOX}/exempt.log" "from your acceptance ${id}"
scoter list-accepted > "${IT_SANDBOX}/list.log"
it_expect "${IT_SANDBOX}/list.log" "(holds)  branch main  from ${id}"
scoter exempt --for 30d > "${IT_SANDBOX}/long.log" 2>&1 && it_fail "an exemption longer than the setup allows started"
it_expect "${IT_SANDBOX}/long.log" "at most 7 days"

it_say "changing the files: the Dockerfile, the project file and dev-env.yaml"
it_overlay "${IT_ROOT}/02-re-review/changed"
# Not `sed -i`: it takes different arguments on macOS.
dev_env="${IT_PROJECT}/.surf-scoter.d/env/dev-env.yaml"
sed 's/^user: ubuntu$/user: root/' "${dev_env}" > "${dev_env}.new"
mv "${dev_env}.new" "${dev_env}"

it_say "a run on the branch: the container files as they are, the settings as accepted"
scoter run > "${IT_SANDBOX}/run.log" 2>&1 && it_fail "the stand-in build succeeded"
it_expect "${IT_SANDBOX}/run.log" "Exempt on main until "
it_expect "${IT_SANDBOX}/run.log" "it ignores your changes to env/dev-env.yaml, project.yaml until you run \`scoter verify --with-exemption\`"
it_expect "${IT_SANDBOX}/run.log" "Building dev-env image"
it_expect "${built}/Dockerfile" "ENV CHANGED=1"
it_expect "${built}/dev-env.yaml" "user: ubuntu"

it_say "another branch isn't exempt"
git -C "${IT_PROJECT}" checkout -q -b other
scoter run > "${IT_SANDBOX}/other.log" 2>&1 && it_fail "the run started on another branch"
it_expect "${IT_SANDBOX}/other.log" "The project's files changed since you last accepted them"
git -C "${IT_PROJECT}" checkout -q main

it_say "verify --with-exemption refuses what the checks refuse"
scoter verify --with-exemption > "${IT_SANDBOX}/refused.log" 2>&1 && it_fail "a refused shell was accepted"
it_expect "${IT_SANDBOX}/refused.log" "can't be accepted without a review"

it_say "verify --with-exemption picks up the settings' changes"
it_overlay "${IT_TEST_DIR}/fixed"
scoter verify --with-exemption > "${IT_SANDBOX}/accept.log" 2>&1 \
    || { cat "${IT_SANDBOX}/accept.log" >&2; it_fail "verify --with-exemption failed"; }
it_expect "${IT_SANDBOX}/accept.log" "Accepted the project's settings as they stand"
new=$( scoter list-accepted -q )
[ "${new}" != "${id}" ] || it_fail "no new acceptance"
[ "$( printf '%s\n' "${new}" | wc -l )" -eq 1 ] || it_fail "expected one acceptance: it replaces the branch's"
scoter list-accepted > "${IT_SANDBOX}/list.log"
it_expect "${IT_SANDBOX}/list.log" "${new}  exempted"
it_expect "${IT_SANDBOX}/list.log" "(holds)  branch main  from ${new}"
scoter run > "${IT_SANDBOX}/run2.log" 2>&1 && it_fail "the stand-in build succeeded"
it_expect_not "${IT_SANDBOX}/run2.log" "it ignores your changes"
it_expect "${built}/dev-env.yaml" "user: root"

it_say "ending it"
scoter exempt --end > "${IT_SANDBOX}/end.log"
it_expect "${IT_SANDBOX}/end.log" "The exemption ended."
scoter list-accepted > "${IT_SANDBOX}/list.log"
it_expect_not "${IT_SANDBOX}/list.log" "exempt until"

it_pass
