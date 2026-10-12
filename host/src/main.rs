//! Command that passes the invoked command to the listening host.
//! A "privileged host-execution broker," host side.
//!
//! Usage: scoter-host --config CONFIG.yaml --workspace HOST_DIR:CONTAINER_PATH CONTAINER_NAME
//!
//! Launches the broker inside the container (`host::container`), then runs the
//! commands its clients request, when `host::analyze` allows them.

mod host;

fn main() {
    std::process::exit(host::run(std::env::args_os().skip(1)));
}
