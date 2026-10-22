#!/bin/sh
# Install a copy of Surf Scoter from a commit, to run against this checkout.
#
# Usage:
#   self-host-install.sh install REF DEST
#   self-host-install.sh remove DEST
#
# When Surf Scoter is the project, whatever the container writes, once
# merged, becomes code that runs on the host the next time the tools run from
# this checkout: the scripts, the setup file, the containers, and (through
# `bin/gen-scoter-uc`) the Rust source.  Running a copy installed from a
# reviewed commit keeps the tools apart from the code being worked on: a merge
# can't change them until you install again.
#
# The copy comes from `git archive`, so only committed files go in, never
# uncommitted changes, untracked files, or the workspace.  It's built from that
# export, installed outside this repository, and left read-only.

set -e

MARKER=.surf-scoter-self-host

base=$( cd "$( dirname "$0" )/.." && pwd -P )

usage() {
    echo "Usage: $0 install REF DEST" >&2
    echo "       $0 remove DEST" >&2
    exit 2
}

fail() {
    echo "$0: $*" >&2
    exit 1
}

# The absolute, canonical form of DEST, which needn't exist yet (its parent
# directory is created).
resolve_dest() {
    case "$1" in
        "") fail "DEST must not be empty" ;;
    esac
    parent=$( dirname "$1" )
    mkdir -p "${parent}"
    echo "$( cd "${parent}" && pwd -P )/$( basename "$1" )"
}

# DEST must be outside this repository, and must not hold it (it gets
# replaced), nor be the root or home directory.
check_dest() {
    case "$1/" in
        "${base}/"*) fail "$1 is inside this repository; install the copy somewhere else" ;;
    esac
    case "${base}/" in
        "$1/"*) fail "$1 holds this repository; install the copy somewhere else" ;;
    esac
    home=$( cd "${HOME:-/}" && pwd -P )
    if [ "$1" = "/" ] || [ "$1" = "${home}" ] ; then
        fail "$1 can't be the install directory"
    fi
}

# Only a copy this script made may be replaced or removed.
check_replaceable() {
    if [ -e "$1" ] || [ -L "$1" ] ; then
        if [ -L "$1" ] || [ ! -f "$1/${MARKER}" ] ; then
            fail "$1 exists, and isn't a copy this script installed; remove it yourself"
        fi
    fi
}

# Remove an earlier copy, but only one this script made.
remove_copy() {
    check_replaceable "$1"
    if [ -e "$1" ] ; then
        chmod -R u+w "$1"
        rm -rf "$1"
    fi
}

cmd="$1"
case "${cmd}" in
    install)
        [ $# -eq 3 ] || usage
        ref="$2"
        dest=$( resolve_dest "$3" )
        ;;
    remove)
        [ $# -eq 2 ] || usage
        dest=$( resolve_dest "$2" )
        check_dest "${dest}"
        remove_copy "${dest}"
        echo "Removed ${dest}"
        exit 0
        ;;
    *)
        usage
        ;;
esac

check_dest "${dest}"
check_replaceable "${dest}"
commit=$( git -C "${base}" rev-parse --verify --quiet "${ref}^{commit}" ) \
    || fail "${ref} isn't a commit in ${base}"

work=$( mktemp -d )
staging="${dest}.new.$$"
cleanup() {
    rm -rf "${work}"
    if [ -d "${staging}" ] ; then
        chmod -R u+w "${staging}"
        rm -rf "${staging}"
    fi
}
trap cleanup EXIT INT TERM

echo "Exporting ${ref} (${commit})"
mkdir "${work}/src"
git -C "${base}" archive --format=tar "${commit}" | tar -x -C "${work}/src"
src="${work}/src"

echo "Building the host programs"
locked=""
if [ -f "${src}/host/Cargo.lock" ] ; then
    locked="--locked"
fi
cargo build --release ${locked} \
    --manifest-path "${src}/host/Cargo.toml" \
    --target-dir "${work}/target"

echo "Installing into ${dest}"
mkdir "${staging}"
mkdir "${staging}/bin" "${staging}/bin/surf_scoter_lib"
cp "${work}/target/release/scoter-host" "${work}/target/release/gen-scoter-uc" "${staging}/bin/"
cp "${src}/scripts/"*.py "${staging}/bin/"
cp -R "${src}/scripts/surf_scoter_lib/." "${staging}/bin/surf_scoter_lib/"
rm -r \
    "${staging}/bin/surf_scoter_lib/__pycache__" \
    "${staging}/bin/surf_scoter_lib/"*/__pycache__ \
    2>/dev/null || true
cp -R "${src}/containers" "${src}/defaults" "${src}/docs" "${staging}/"
cp "${src}/scoter.py" "${src}/LICENSE" "${staging}/"
# The distribution's own setup file.
cp "${src}/defaults/setup.yaml" "${staging}/setup.yaml"
{
    echo "Surf Scoter, installed by build-scripts/self-host-install.sh"
    echo "from: ${base}"
    echo "ref: ${ref}"
    echo "commit: ${commit}"
    echo "installed: $( date -u '+%Y-%m-%dT%H:%M:%SZ' )"
} > "${staging}/${MARKER}"
chmod -R a-w "${staging}"

remove_copy "${dest}"
mv "${staging}" "${dest}"

echo
echo "Installed ${commit} into ${dest}"
echo "Run it against this checkout with:"
echo "    python3 ${dest}/scoter.py SUB-COMMAND"
