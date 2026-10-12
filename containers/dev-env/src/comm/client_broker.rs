//! Communication between the 'client' application, invoked within the container
//! by the restricted user, and the broker service.
//! This is just a simple set of read/write actions over a socket.

pub const UNIX_SOCKET: &str = "/tmp/container-uds.sock";
