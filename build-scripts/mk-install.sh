#!/bin/sh

base=$( cd $( dirname "$0" )/.. && pwd )

if [ -z "$1" ] ; then
    echo "Usage: $0 OUTFILE [rust-target-platform]"
    echo "(leave the '.zip' or '.tar.gz' off of the OUTFILE)"
    exit 1
fi

outfile="$1"
abs_outfile=$( cd $( dirname "${outfile}" ) && pwd )/$( basename "${outfile}" )
target_dir=target/work/release
if [ -z "$2" ] ; then
    target_platform="$2"
    target_dir=target/work/$2/release
else
    # Discover the active toolchain.
    target_platform=$( rustup show active-toolchain | cut -f 2- -d - | cut -f 1 -d ' ' )
fi

ext=""
if echo "${target_platform}" | grep -i 'win' ; then
    ext=".exe"
fi

workdir="${base}/target/installer/${target_platform}"

mkdir -p "${workdir}/bin"

# Set up the bin directory
cp "${base}/host/${target_dir}/scoter-host${ext}" "${workdir}/bin/."
cp "${base}/host/${target_dir}/gen-scoter-uc${ext}" "${workdir}/bin/."
cp "${base}/scripts/"*.py "${workdir}/bin/."
mkdir -p "${workdir}/bin/surf_scoter_lib"
cp "${base}/scripts/surf_scoter_lib"/*.py "${workdir}/bin/surf_scoter_lib/."

cp -R "${base}/containers" "${workdir}/."
rm -r "${workdir}/containers/dev-env/target"
cp -R "${base}/examples" "${workdir}/."

mkdir -p "${workdir}/docs"
cp "${base}/docs"/* "${workdir}/docs/."

cp "${base}/LICENSE" "${workdir}/."

if [ -z "${ext}" ] ; then
    test -f "${outfile}.zip" && rm "${outfile}.zip"
    cp "${workdir}/scoter.py" "${workdir}/scoter.py"
    ( cd "${base}" && zip -9r ${abs_outfile} * )
else
    cp "${base}/scoter.py" "${workdir}/scoter"
    ( cd "${workdir}" && tar zcf ${abs_outfile} * )
fi
