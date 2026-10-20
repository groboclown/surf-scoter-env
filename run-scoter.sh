#!/bin/sh

here=$( cd $( dirname "$0" ) && pwd )
$(pwd)/scoter.py \
    --distribution-dir="${here}" \
    "$@"
