#!/bin/sh

host_dir=$( cd "$( dirname "$0" )/../host" && pwd )
exec cargo run --manifest-path "${host_dir}/Cargo.toml" --bin scoter-host -- "$@"
