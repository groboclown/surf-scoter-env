//! Command that passes the invoked command to the listening host.
//! A "privileged host-execution broker," host side.
//!
//! Usage: scoter-host --universal UNIVERSAL.json CONTAINER_NAME
//!
//! Reads the configuration from the universal configuration file that
//! `gen-scoter-uc` made, and no other file (`scoter_host::config`), launches
//! the broker inside
//! the container (`host::container`), then runs the commands its clients
//! request, when `host::analyze` allows them.

mod host;

fn main() {
    std::process::exit(host::run(std::env::args_os().skip(1)));
}
