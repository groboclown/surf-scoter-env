#!/bin/sh
set -e

# This expects the workspace directory to be a parent (at some level) to the
# cloned project directory.  On first startup, this will ensure that the user's
# home path is a symlink to the workspace directory, and will copy the original
# files to the new one.
export WORKSPACE_DIR=/workspace
if [ ! -L "${HOME}" ] ; then
    # 1. Change to a non-working directory.
    cd /tmp
    # 2. Copy (without replace) the original home directory files into the workspace directory.
    for src in "${HOME}"/* "${HOME}"/.* ; do
        # One of the globs may not exist, which means it appears as a '*'.
        if [ -e "${src}" ] ; then
            name="$( basename "${src}" )"
            tgt="${WORKSPACE_DIR}/${name}"
            if [ ! -e "${tgt}" ] ; then
                cp -R "${src}" "${tgt}"
            fi
        fi
    done
    # 3. Backup the original.  Note that this requires write access to the /home directory.
    parent="$( dirname "${HOME}" )"
    mv "${HOME}" "${HOME}.backup"
    # 4. Link home.
    ln -s "${WORKSPACE_DIR}" "${HOME}"
fi

# Set up the sshd configuration file with the SSHD port.
envsubst '${SCOTER_SSHD_PORT}' \
    < /home/ubuntu/.config/sshd/sshd_config.template \
    > /home/ubuntu/.config/sshd/sshd_config

# Assemble the files from the working directory.
cp /opt/surf-scoter-setup/authorized_keys "${HOME}/.ssh/authorized_keys"
test -f "${HOME}/.config/sshd/ssh_host_ed25519_key" && rm -f "${HOME}/.config/sshd/ssh_host_ed25519_key"
cp /opt/surf-scoter-setup/ssh_host_ed25519_key "${HOME}/.config/sshd/ssh_host_ed25519_key"
chmod 0400 "${HOME}/.config/sshd/ssh_host_ed25519_key"

# Read the 'bridge-cmds.txt' file and link the bridge client to those names
# in /usr/local/scoter-bridge, which comes first on the PATH.  The container
# runs as its user, who owns that directory, but not /usr/local/bin.  The
# links are symbolic: the kernel refuses a user a hard link to a file it
# doesn't own (protected_hardlinks), and the client takes the command's name
# from how it was called (argv[0]), which a symbolic link keeps.
for name in $( cat /opt/surf-scoter-setup/bridge-cmds.txt ) ; do
    case "${name}" in
        ''|*/*|.|..) echo "Skipping the host command name '${name}'" >&2 ; continue ;;
    esac
    ln -sf /usr/local/bin/scoter-client "/usr/local/scoter-bridge/${name}"
done

# Keep running with the SSHD process.
exec /usr/sbin/sshd -D -e -f "${HOME}/.config/sshd/sshd_config"
