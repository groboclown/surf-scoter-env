//! The Surf Scoter host-side library.
//!
//! `config` reads the whole configuration: the system setup files, the
//! project configuration file and the dev container's settings.  The
//! `scoter-host` program uses it, as can other tools that need the same view
//! of the configuration.  `universal` turns it into the universal
//! configuration file, for tools that read JSON (`gen-scoter-uc`).

pub mod config;
pub mod universal;
