#!/bin/sh

here=$( cd $( dirname "$0" ) && pwd )
$(pwd)/scoter.py --bin-dir="${here}/host/target/debug" --distribution-dir="${here}" "$@"
