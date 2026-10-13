#!/bin/sh
set -e

# Assemble the files from the working directory.
# TODO move the authorized_keys and ssh_host_ed25519_key files into a different (read-only) mount
cp /workspace/authorized_keys "$HOME/.ssh/authorized_keys"
test -f "$HOME/.config/sshd/ssh_host_ed25519_key" && rm -f "$HOME/.config/sshd/ssh_host_ed25519_key"
cp /workspace/ssh_host_ed25519_key "$HOME/.config/sshd/ssh_host_ed25519_key"
chmod 0400 "$HOME/.config/sshd/ssh_host_ed25519_key"

# Keep running with the SSHD process.
exec /usr/sbin/sshd -D -e -f "$HOME/.config/sshd/sshd_config"
