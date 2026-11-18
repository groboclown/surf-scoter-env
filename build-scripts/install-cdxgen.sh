#!/bin/sh
# Download the standalone cdxgen binary (no Node.js needed) from its GitHub
# release, check it against the release's SHA-256 file, and install it as
# OUTDIR/cdxgen.
#
# Usage: install-cdxgen.sh VERSION OUTDIR
#   VERSION  the release tag, such as v13.6.0
#   OUTDIR   the directory to put the `cdxgen` program in

set -e

if [ -z "$1" ] || [ -z "$2" ] || [ "$1" = "-h" ] || [ "$1" = "--help" ] ; then
    echo "Usage: $0 VERSION OUTDIR" >&2
    exit 1
fi

version="$1"
outdir="$2"

case "$( uname -s )" in
    Linux) os=linux ;;
    Darwin) os=darwin ;;
    *)
        echo "No cdxgen binary for $( uname -s ); install cdxgen yourself and pass CDXGEN=..." >&2
        exit 1
        ;;
esac

case "$( uname -m )" in
    x86_64|amd64) arch=amd64 ;;
    aarch64|arm64) arch=arm64 ;;
    *)
        echo "No cdxgen binary for $( uname -m ); install cdxgen yourself and pass CDXGEN=..." >&2
        exit 1
        ;;
esac

asset="cdxgen-${os}-${arch}"
url="https://github.com/cdxgen/cdxgen/releases/download/${version}/${asset}"

mkdir -p "${outdir}"
workdir=$( mktemp -d )
trap 'rm -rf "${workdir}"' EXIT

echo "Downloading ${url}"
curl -fsSL -o "${workdir}/${asset}" "${url}"
curl -fsSL -o "${workdir}/${asset}.sha256" "${url}.sha256"

# The checksum file is "<hash>  <asset>", as sha256sum writes it.
if command -v sha256sum >/dev/null 2>&1 ; then
    ( cd "${workdir}" && sha256sum -c "${asset}.sha256" )
else
    ( cd "${workdir}" && shasum -a 256 -c "${asset}.sha256" )
fi

chmod +x "${workdir}/${asset}"
mv "${workdir}/${asset}" "${outdir}/cdxgen"
echo "Installed cdxgen ${version} as ${outdir}/cdxgen"
