#!/bin/sh

set -e

base=$( cd "$( dirname "$0" )/.." && pwd )

if [ -z "$1" ] ; then
    echo "Usage: $0 OUTFILE [rust-target-platform]"
    echo "(leave the '.zip' or '.tar.gz' off of the OUTFILE)"
    exit 1
fi

outfile="$1"
mkdir -p "$( dirname "${outfile}" )"
abs_outfile=$( cd "$( dirname "${outfile}" )" && pwd )/$( basename "${outfile}" )
target_dir=target/release
if [ -n "$2" ] ; then
    # Built for this platform: rust/host/target/PLATFORM/release.
    target_platform="$2"
    target_dir=target/$2/release
else
    # Discover the active toolchain.
    target_platform=$( rustup show active-toolchain | cut -f 2- -d - | cut -f 1 -d ' ' )
    if [ -z "${target_platform}" ] ; then
        echo "Failed to select active toolchain."
        exit 1
    fi
fi

ext=""
if echo "${target_platform}" | grep -i 'win' ; then
    ext=".exe"
fi

workdir="${base}/target/installer/${target_platform}"

mkdir -p "${workdir}/bin"

# Set up the bin directory.
cp "${base}/rust/host/${target_dir}/scoter-host${ext}" "${workdir}/bin/."
cp "${base}/rust/host/${target_dir}/gen-scoter-uc${ext}" "${workdir}/bin/."
cp -R "${base}/py/src/dist_bin/"* "${workdir}/bin/."

cp -R "${base}/containers" "${workdir}/."
# Not a local build of the Envoy gateway's secrets filter: its image builds its own.
rm -rf "${workdir}/containers/network-gateway-envoy/secrets/target"
# The files installed as they are, laid out as in the distribution: the setup
# file at the top, where the tools look for it
# (`${scoter:distribution-dir}/setup.yaml`), the `defaults` and `templates`.
cp -R "${base}/dist-files/." "${workdir}/."
cp -R "${base}/examples" "${workdir}/."

mkdir -p "${workdir}/docs"
cp "${base}/docs"/* "${workdir}/docs/." || true

cp "${base}/LICENSE" "${workdir}/."

mkdir -p "${workdir}/sbom"
cp \
    "${base}/target/sboms/"*.cdx.json \
    "${workdir}/sbom/."

# Remove potential build left-overs.
rm -r \
    "${workdir}/__pycache__" \
    "${workdir}/"*/__pycache__ \
    "${workdir}/"*/*/__pycache__ \
    "${workdir}/"*/*/*/__pycache__ \
    2>/dev/null || true


if [ -z "${ext}" ] ; then
    cp "${base}/py/src/dist_root/scoter.py" "${workdir}/scoter"
    ( cd "${workdir}" && tar zcf ${abs_outfile}.tar.gz * )
else
    test -f "${outfile}.zip" && rm "${outfile}.zip"
    cp "${base}/py/src/dist_root/scoter.py" "${workdir}/scoter.zip"
    ( cd "${workdir}" && zip -9r ${abs_outfile}.zip * )
fi
