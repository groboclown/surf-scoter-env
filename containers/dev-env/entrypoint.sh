#!/bin/sh
set -e

# `scoter run` says where it mounted the project's clone and the persistent
# store (the dev container's `mounts`), through the compose file.
if [ -z "${SCOTER_PROJECT_CLONE}" ] || [ -z "${SCOTER_PERSISTENT_STORE}" ] ; then
    echo "SCOTER_PROJECT_CLONE and SCOTER_PERSISTENT_STORE must be set; start this with scoter run" >&2
    exit 1
fi
if [ ! -w "${SCOTER_PERSISTENT_STORE}" ] ; then
    echo "The persistent store, ${SCOTER_PERSISTENT_STORE}, isn't writable by $( id -un )" >&2
    exit 1
fi

# Keep what /etc/scoter/persistent-store.txt lists in the persistent store:
# each one is linked from the home directory into the store, copied there
# from the image the first time.  Everything else in the home directory comes
# from the image, as it is on every start.
cd /tmp
while IFS= read -r entry ; do
    case "${entry}" in
        ''|'#'*) continue ;;
        /*|*..*) echo "Skipping the persistent store entry '${entry}'" >&2 ; continue ;;
    esac
    rel="${entry%/}"
    home_path="${HOME}/${rel}"
    store_path="${SCOTER_PERSISTENT_STORE}/${rel}"
    # The store is the container's own: an earlier session may have put a
    # link there.  Use only what's really there.
    if [ -L "${store_path}" ] ; then
        echo "Skipping the persistent store entry '${entry}': it's a link in the store" >&2
        continue
    fi
    if [ ! -e "${store_path}" ] ; then
        mkdir -p "$( dirname "${store_path}" )"
        if [ -e "${home_path}" ] && [ ! -L "${home_path}" ] ; then
            cp -a "${home_path}" "${store_path}"
        elif [ "${entry}" != "${rel}" ] ; then
            mkdir -p "${store_path}"
        else
            : > "${store_path}"
        fi
    fi
    rm -rf "${home_path}"
    mkdir -p "$( dirname "${home_path}" )"
    ln -s "${store_path}" "${home_path}"
done < /etc/scoter/persistent-store.txt

# Git refuses a repository another user owns, as the clone may be.
git config --global --replace-all safe.directory "${SCOTER_PROJECT_CLONE}"

# The SSH server's configuration, host key and authorized keys, fresh from
# the image and the read-only setup directory on every start.
sshd_dir=/run/scoter-sshd
envsubst '${SCOTER_SSHD_PORT}' \
    < /etc/scoter/sshd_config.template \
    > "${sshd_dir}/sshd_config"
cp /opt/surf-scoter-setup/authorized_keys "${sshd_dir}/authorized_keys"
chmod 0600 "${sshd_dir}/authorized_keys"
rm -f "${sshd_dir}/ssh_host_ed25519_key"
cp /opt/surf-scoter-setup/ssh_host_ed25519_key "${sshd_dir}/ssh_host_ed25519_key"
chmod 0400 "${sshd_dir}/ssh_host_ed25519_key"

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
cd "${SCOTER_PROJECT_CLONE}"
exec /usr/sbin/sshd -D -e -f "${sshd_dir}/sshd_config"
