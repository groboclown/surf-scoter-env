#!/bin/sh

# Run on your host ONE TIME to set up your environment for the container.

docker_cmd="docker"
dry_run=1
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
    --force)
      dry_run=0
      ;;
    *)
      >&2 echo "Unknown argument ('$n')."
      help=1
      err=1
      ;;
  esac
done

if [ "$help" = 1 ] ; then
  echo "Usage: $0 [--force]"
  echo "Sets up your local environment for running the surf-scoter developer environment."
  echo "Do this once; it covers every environment you run (see run-container.sh's -n option)."
  echo
  echo "If you don't run with the '--force' argument, then this shows you what it will do without doing it."
  echo "That's because this will touch sensitive files that you need to be aware of what it's doing."
  echo
  echo "Once you run this, you won't need to run it again unless you think some of the files might be compromised."
  echo "You should be safe to rerun it, but doing so will change things like SSH keys, which means active development containers will need restarting."
  exit "$err"
fi
if [ "$err" != 0 ] ; then
  exit "$err"
fi

if [ $( id -u ) = "0" ] ; then
  >&2 echo "ERROR: run this as your local user, not as root."
  exit 1
fi

# Create the SSH key.
if [ -f "$HOME/.ssh/dev-container-scoter" ] || [ -f "$HOME/.ssh/dev-container-scoter.pub" ] ; then
  >&2 echo "NOTE: dev-container-scoter key already exists.  Will overwrite."
fi
if [ "$dry_run" = 1 ] ; then
  echo "Testing the setup of the ssh key (putting it in a temporary place for the moment)"
  tdir=$( mktemp -d )
  ssh-keygen -t ed25519 -f "$tdir/dev-container-scoter"
  res="$?"
  rm -rf "$tdir" 2>/dev/null
  if [ "$res" != 0 ] ; then
    exit "$res"
  fi
else
  rm "$HOME/.ssh/dev-container-scoter" "$HOME/.ssh/dev-container-scoter.pub" 2>/dev/null
  ssh-keygen -t ed25519 -f "$HOME/.ssh/dev-container-scoter" || exit 1
fi

# One entry covers every environment: 'ssh scoter-NAME' connects to the
# container named scoter-NAME (see run-container.sh), through 'docker exec', so
# the container doesn't open a port on this computer.  With no HostName, ssh
# looks up the host key under that same name, so each environment has its own
# known_hosts entry.
has_entry=0
if [ -f "$HOME/.ssh/config" ] && grep -Eiq '^[[:space:]]*Host[[:space:]]+scoter-\*[[:space:]]*$' "$HOME/.ssh/config" ; then
  has_entry=1
fi
if [ "$has_entry" = 0 ] ; then
  tf=$( mktemp )
  if [ -f "$HOME/.ssh/config" ] ; then
    cp "$HOME/.ssh/config" "$tf"
  fi
  cat >>$tf <<'EOF'
Host scoter-*
    User ubuntu
    IdentityFile ~/.ssh/dev-container-scoter
    IdentitiesOnly yes
    StrictHostKeyChecking yes
    ProxyCommand docker exec -i %h socat STDIO TCP:127.0.0.1:22022
EOF
  if [ "$dry_run" = 1 ] ; then
    echo "... Would add a 'Host scoter-*' section to your $HOME/.ssh/config file if --force was set."
    echo "... It would look like:"
    cat "$tf"
    echo "..."
    rm "$tf"
  else
    mv "$tf" "$HOME/.ssh/config"
  fi
else
  >&2 echo "Your $HOME/.ssh/config already has a 'Host scoter-*' section.  Not changing."
fi

if [ -f "$HOME/.ssh/config" ] && grep -Eiq '^[[:space:]]*Host[[:space:]]+dev-container-scoter[[:space:]]*$' "$HOME/.ssh/config" ; then
  >&2 echo "NOTE: the 'Host dev-container-scoter' section in $HOME/.ssh/config is from an earlier version and is no longer used.  You can remove it."
fi
