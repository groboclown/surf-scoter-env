#!/bin/sh

# Runs the environment.
here=$( dirname "$0" )

docker_cmd="docker"
parent_tag="local-surf-scoter-env/dev-parent"
name="dev"
repodir=""
workdir=""
dev_docker="Dockerfile"
help=0
err=0
for n in "$@" ; do
  case "$n" in
    -h)
      help=1
      ;;
    --help)
      help=1
      ;;
    -p=*)
      parent_tag=$( echo "$n" | cut -f 2 -d = )
      ;;
    -r=*)
      repodir=$( echo "$n" | cut -f 2 -d = )
      repodir=$( cd "$repodir" && pwd ) || exit 1 
      ;;
    -w=*)
      workdir=$( echo "$n" | cut -f 2 -d = )
      ;;
    -d=*)
      dev_docker=$( echo "$n" | cut -f 2 -d = )
      ;;
    -n=*)
      name=$( echo "$n" | cut -f 2 -d = )
      ;;
    *)
      >&2 echo "Unknown argument ($n)"
      err=1
      help=1
      ;;
  esac
done


if [ "$help" = 1 ] ; then
  echo "Usage: $0 -r=REPOSITORY_DIR -w=WORKING_DIR [-n=NAME] [-d=Dockerfile] [-p=PARENT_IMAGE_TAG]"
  echo "This will build the Dockerfile (-d argument) and the parent image."
  echo "NAME (default 'dev') names the environment; connect to it with 'ssh scoter-NAME'."
  echo "Give each environment you run at the same time its own name."
  echo "Your 'Dockerfile' should have, at the top:"
  echo
  echo "ARG PARENT_TAG=local-parent"
  echo "FROM \${PARENT_TAG}"
  echo
  exit "$err"
fi
if [ "$err" != 0 ] ; then
  exit "$err"
fi

# The name is used for the compose project, which allows only lowercase
# letters, digits, '-' and '_'.
case "$name" in
  ""|[!a-z0-9]*|*[!a-z0-9_-]*)
    >&2 echo "Invalid name ($name): use lowercase letters, digits, '-' and '_', starting with a letter or digit."
    exit 1
    ;;
esac
# Matches the 'Host scoter-*' entry that user-setup.sh adds to ~/.ssh/config.
container_name="scoter-$name"

mkdir -p "$workdir" || exit 1
workdir=$( cd "$workdir" && pwd ) || exit 1

# Setup SSH access.
cp "$HOME/.ssh/dev-container-scoter.pub" "$workdir/authorized_keys" || exit 1
if [ ! -f "$workdir/ssh_host_ed25519_key" ] ; then
    ssh-keygen -t ed25519 -N "" \
      -f "$workdir/ssh_host_ed25519_key" || exit 1
    chmod 444 "$workdir/ssh_host_ed25519_key"
fi

# Trust the container's host key under its name, which is the name ssh connects
# to (see user-setup.sh), replacing a key from an earlier run.
known_hosts="$HOME/.ssh/known_hosts"
if [ -f "$known_hosts" ] ; then
    ssh-keygen -R "$container_name" -f "$known_hosts" >/dev/null 2>&1
fi
# The key type and key, without the comment.
host_key=$( cut -d ' ' -f 1,2 "$workdir/ssh_host_ed25519_key.pub" ) || exit 1
echo "$container_name $host_key" >> "$known_hosts" || exit 1

# For now, this is Git specific.
repo_name=$( basename "$repodir" )
if [ ! -d "$workdir/$repo_name" ] ; then
    git clone "$repodir" "$workdir/$repo_name" || exit 1
fi

"$docker_cmd" build -t "$parent_tag" "$here/containers/dev-env" || exit 1

env_file=$( mktemp )
echo "WORKSPACE=$workdir" >> "$env_file"
echo "CONTAINER_NAME=$container_name" >> "$env_file"
# A project per environment keeps their containers and networks apart.
"$docker_cmd" compose --project-name "$container_name" --env-file "$env_file" up -d || exit 1
universal=$( mktemp ) || exit 1
cargo run --manifest-path "$here/host/Cargo.toml" --bin gen-scoter-uc -- \
  --config .scoter-config.yaml --dev-container-workspace-dir "$workdir" --output "$universal" || exit 1
cargo run --manifest-path "$here/host/Cargo.toml" --bin scoter-host -- \
  --universal "$universal" "$container_name"

# FIXME the user should use `git fetch $workdir/$repo_name` to get the files + review them.
#       Running 'git push' from the workdir allows potential malware to run .git/config hooks in the host.
