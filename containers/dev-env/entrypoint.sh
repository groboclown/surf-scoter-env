#!/bin/sh
set -e

# Set up the sshd configuration file with the SSHD port.
envsubst '${SCOTER_SSHD_PORT}' \
    < /home/ubuntu/.config/sshd/sshd_config.template \
    > /home/ubuntu/.config/sshd/sshd_config

# Assemble the files from the working directory.
cp /opt/surf-scoter-setup/authorized_keys "$HOME/.ssh/authorized_keys"
test -f "$HOME/.config/sshd/ssh_host_ed25519_key" && rm -f "$HOME/.config/sshd/ssh_host_ed25519_key"
cp /opt/surf-scoter-setup/ssh_host_ed25519_key "$HOME/.config/sshd/ssh_host_ed25519_key"
chmod 0400 "$HOME/.config/sshd/ssh_host_ed25519_key"

# Read the 'bridge-cmds.txt' file and hard-link the bridge client to those
# names in the /usr/local/bin directory.
for name in $( cat /opt/surf-scoter-setup/bridge-cmds.txt ) ; do
    ln /usr/local/bin/scoter-client /usr/local/bin/$name
done

# Keep running with the SSHD process.
exec /usr/sbin/sshd -D -e -f "$HOME/.config/sshd/sshd_config"
