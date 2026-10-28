#!/bin/sh
#SPDX:MIT
# Shared functions for the integration tests.  Source this from a test's
# `run.sh`; see ../README.md.
#
# Each test runs in a sandbox: a private temporary directory with its own
# HOME, so the tools' setup, ssh files, state and verified store go there,
# never into your own.  The sandbox's distribution is built from this
# checkout: the debug binaries (`cd host && cargo build`) and the scripts.

set -eu

IT_COMMON=$( cd "$( dirname "$0" )/../common" && pwd )
IT_ROOT=$( cd "${IT_COMMON}/.." && pwd )
IT_REPO=$( cd "${IT_ROOT}/.." && pwd )
IT_TEST_DIR=$( cd "$( dirname "$0" )" && pwd )
IT_NAME=$( basename "${IT_TEST_DIR}" )

# The container tool the tests drive (docker, or anything that takes its
# commands, such as podman).  It runs with your own HOME, so it uses your
# image store and logins, not the sandbox's.
IT_CONTAINER_TOOL="${IT_CONTAINER_TOOL:-docker}"

it_say() {
    printf '\n== %s: %s\n' "${IT_NAME}" "$*"
}

it_fail() {
    printf '\nFAIL %s: %s\n' "${IT_NAME}" "$*" >&2
    if [ -n "${IT_SANDBOX:-}" ]; then
        printf 'The sandbox is kept for a look: %s\n' "${IT_SANDBOX}" >&2
        IT_KEEP=1
    fi
    exit 1
}

it_pass() {
    printf '\nPASS %s\n' "${IT_NAME}"
}

it_require() {
    for cmd in "$@"; do
        command -v "${cmd}" > /dev/null 2>&1 || it_fail "needs \`${cmd}\` on the PATH"
    done
}

# it_init: make the sandbox, its distribution and its user setup.
it_init() {
    it_require python3 script git
    for exe in gen-scoter-uc scoter-host; do
        [ -x "${IT_REPO}/host/target/debug/${exe}" ] \
            || it_fail "needs the debug build: cd host && cargo build"
    done

    IT_SANDBOX=$( mktemp -d "${TMPDIR:-/tmp}/surf-scoter-it-${IT_NAME}-XXXXXX" )
    chmod 700 "${IT_SANDBOX}"
    IT_KEEP="${KEEP_SANDBOX:-}"
    trap it_cleanup EXIT

    IT_REAL_HOME="${HOME}"
    export HOME="${IT_SANDBOX}/home"
    mkdir -m 700 "${HOME}"
    export SURF_SCOTER_HOME="${HOME}/.config/surf-scoter.d"
    mkdir -p -m 700 "${SURF_SCOTER_HOME}"
    # Nothing from your shell steers the tools or Git in here.
    unset GIT_DIR GIT_WORK_TREE GIT_CONFIG_GLOBAL GIT_CONFIG_SYSTEM XDG_CONFIG_HOME || true

    # The distribution: like an installed one, but from this checkout.
    IT_DIST="${IT_SANDBOX}/dist"
    mkdir -p "${IT_DIST}/bin"
    ln -s "${IT_REPO}/host/target/debug/gen-scoter-uc" "${IT_DIST}/bin/gen-scoter-uc"
    ln -s "${IT_REPO}/host/target/debug/scoter-host" "${IT_DIST}/bin/scoter-host"
    for script in "${IT_REPO}/scripts/"*.py "${IT_REPO}/scripts/"*.txt; do
        ln -s "${script}" "${IT_DIST}/bin/"
    done
    ln -s "${IT_REPO}/scripts/surf_scoter_lib" "${IT_DIST}/bin/surf_scoter_lib"
    ln -s "${IT_REPO}/defaults" "${IT_DIST}/defaults"
    ln -s "${IT_REPO}/containers" "${IT_DIST}/containers"
    cp "${IT_REPO}/defaults/setup.yaml" "${IT_DIST}/setup.yaml"
    # A copy, not a link: it finds the distribution from where it is.
    cp "${IT_REPO}/scoter.py" "${IT_DIST}/scoter"
    chmod 755 "${IT_DIST}/scoter"

    # The container tool, with your own HOME.
    tool=$( command -v "${IT_CONTAINER_TOOL}" || true )
    mkdir -m 700 "${IT_SANDBOX}/tools"
    cat > "${IT_SANDBOX}/tools/container-tool" <<EOF
#!/bin/sh
HOME='${IT_REAL_HOME}' exec '${tool:-${IT_CONTAINER_TOOL}}' "\$@"
EOF
    chmod 700 "${IT_SANDBOX}/tools/container-tool"

    cat > "${SURF_SCOTER_HOME}/setup.yaml" <<EOF
docker:
    programs:
        exec: ${IT_SANDBOX}/tools/container-tool
EOF
    chmod 600 "${SURF_SCOTER_HOME}/setup.yaml"
    it_say "sandbox ${IT_SANDBOX}"
}

it_cleanup() {
    status=$?
    if [ -n "${IT_SANDBOX:-}" ] && [ -d "${IT_SANDBOX}" ]; then
        if [ -n "${IT_KEEP:-}" ]; then
            printf 'Sandbox kept: %s\n' "${IT_SANDBOX}"
        else
            chmod -R u+w "${IT_SANDBOX}" 2> /dev/null || true
            rm -rf "${IT_SANDBOX}"
        fi
    fi
    exit "${status}"
}

# it_project DIR [git]: copy a sample project into the sandbox, as the
# project to work on (IT_PROJECT); with `git`, as a commit in a repository.
it_project() {
    IT_PROJECT="${IT_SANDBOX}/project"
    rm -rf "${IT_PROJECT}"
    cp -R "$1" "${IT_PROJECT}"
    if [ "${2:-}" = "git" ]; then
        git -C "${IT_PROJECT}" init -q -b main
        git -C "${IT_PROJECT}" add -A
        git -C "${IT_PROJECT}" -c user.name=it -c user.email=it@example.invalid \
            commit -q -m "Sample project"
    fi
}

# it_overlay DIR: copy DIR's files over the project's.
it_overlay() {
    cp -R "$1/." "${IT_PROJECT}/"
}

# scoter ARGS...: the sandbox's scoter, in the project.
scoter() {
    ( cd "${IT_PROJECT}" && "${IT_DIST}/scoter" "$@" )
}

# it_tty COMMAND: run the shell COMMAND through a terminal, with `script`.
# The util-linux `script` takes the command with `-c`; the BSD one (macOS)
# takes it after the output file.
it_tty() {
    case "$( uname -s )" in
        Linux) script -qec "$1" /dev/null ;;
        *) script -q /dev/null sh -c "$1" ;;
    esac
}

# it_accept: use the project's review as it stands, through a terminal (the
# prompt refuses to ask without one).
it_accept() {
    out="${IT_SANDBOX}/accept.log"
    printf 'u\n' | it_tty "cd '${IT_PROJECT}' && '${IT_DIST}/scoter' verify" \
        > "${out}" 2>&1 || true
    grep -q "Accepted (" "${out}" || { cat "${out}" >&2; it_fail "the review wasn't accepted"; }
}

# it_expect FILE TEXT: FILE has a line holding TEXT.
it_expect() {
    grep -qF -- "$2" "$1" || { printf -- '--- %s\n' "$1" >&2; cat "$1" >&2; it_fail "expected: $2"; }
}

# it_expect_not FILE TEXT: no line of FILE holds TEXT.
it_expect_not() {
    if grep -qF -- "$2" "$1"; then
        printf -- '--- %s\n' "$1" >&2
        cat "$1" >&2
        it_fail "didn't expect: $2"
    fi
}
