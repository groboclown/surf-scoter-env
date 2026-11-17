#!/bin/sh
# Stage a build context for the dev-env image with the bridge programs'
# source copied in from this checkout, for Surf Scoter development.
#
# Usage: stage-dev-env.sh OUTDIR
#
# The distribution's containers/dev-env/Dockerfile fetches `rust/container`
# from the repository at a pinned commit.  This copies the Dockerfile's
# directory and this checkout's `rust/container` (without its build output)
# into OUTDIR, for a build with `--build-arg SCOTER_SOURCE=local`.

set -e

if [ -z "$1" ] || [ "$1" = "-h" ] || [ "$1" = "--help" ] ; then
    echo "Usage: $0 OUTDIR" >&2
    exit 1
fi

base=$( cd "$( dirname "$0" )/.." && pwd )
out="$1"

# Copies the files under SRC_DIR into DEST_DIR, without any `target`
# directory (build output).
copy_tree() {
    ( cd "$1" && find . -name target -prune -o -type f -print ) \
        | while read -r file ; do
            mkdir -p "$2/$( dirname "${file}" )"
            cp -p "$1/${file}" "$2/${file}"
        done
}

rm -rf "${out}"
mkdir -p "${out}"
copy_tree "${base}/containers/dev-env" "${out}"
copy_tree "${base}/rust/container" "${out}/rust-container"
