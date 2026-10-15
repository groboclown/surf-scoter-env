#!/bin/sh

here=$( cd $( dirname "$0" ) && pwd )
extra_arg=
if [ -f "$here/user-setup.yaml" ] ; then
    extra_arg="-U $here/user-setup.yaml"
fi
$(pwd)/scoter.py --bin-dir="${here}/host/target/debug" $extra_arg --distribution-dir="${here}" "$@"
