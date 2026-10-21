#!/bin/sh
# Construct a install similar to self-hosted-install.sh but uses
# links between files, so you can use it to directly test outside
# a container.

set -e

if [ -z "$1" ] || [ "$1" = "-h" ] || [ "$1" = "--help" ] ; then
    echo "Usage: $0 INSTALL_DIR [-f]"
    echo "You'll need to rerun this when you add or remove files."
    exit 1
fi

here=$( cd $( dirname "$0" )/.. && pwd )
target="$1"

if [ -e "${target}" ] ; then
    if [ "$2" = "-f" ] ; then
        rm -r "${target}"
    else
        >&2 echo "${target} already exists.  Use '-f' to replace."
        exit 1
    fi
fi

mkdir -p "${target}"
mkdir -p "${target}/bin"

echo "#!/bin/sh" > "${target}/bin/gen-scoter-uc"
echo "exec cargo run --manifest-path \"${here}/host/Cargo.toml\" --bin gen-scoter-uc -- \"\$@\"" >> "${target}/bin/gen-scoter-uc"
chmod +x "${target}/bin/gen-scoter-uc"

echo "#!/bin/sh" > "${target}/bin/scoter-host"
echo "exec cargo run --manifest-path \"${here}/host/Cargo.toml\" --bin scoter-host -- \"\$@\"" >> "${target}/bin/scoter-host"
chmod +x "${target}/bin/scoter-host"

cd "${target}/bin"
ln -s "${here}/scripts/"*.py .
ln -s "${here}/scripts/surf_scoter_lib" .

cd "${target}"
ln -s "${here}/containers" .
ln -s "${here}/defaults" .
ln "${here}/defaults/setup.yaml" .

# This needs a hard-link, not a symbolic link, because of how the
# file finds itself.
ln "${here}/scoter.py" scoter
