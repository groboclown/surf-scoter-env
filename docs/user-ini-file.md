# Scoter User INI File

Scoter allows you to set specific configurations for your particular desktop environment in the `~/.config/surf-scoter.ini` file.


## Where to find the ini file

By default, Scoter will look for your user configuration file at `~/.config/surf-scoter.ini`.  You can override this with the `SURF_SCOTER_HOME` environment variable, in which case it will look for the file at `$SURF_SCOTER_HOME/surf-scoter.ini`.

The tool will look in all these places, in order, for *settings*.  That means if any of the files exist, they will be loaded.  Due to the kind of data the file includes, this is assumed to be okay.

* The `--user-config` argument.
* `$SURF_SCOTER_HOME/surf-scoter.ini`
* `$SURF_SCOTER_HOME/defaults/surf-scoter.ini`
* `~/.config/surf-scoter.ini`
* `~/.config/surf-scoter.d/surf-scoter.ini`

Once these are loaded, they can also define the lookup paths:

```ini
[scoter]
install-dir = /my/install/dir
user-dir    = ~/my/scoter.d
```

The script will expand `~` to your home directory.

From these paths, the tool will then also try to find:

* `{scoter:user-dir}/surf-scoter.ini`
* `{scoter:install-dir}/surf-scoter.ini`
* `{scoter:install-dir}/defaults/surf-scoter.ini`


## Format

The ini files follow the [ini file standard](https://docs.python.org/3/library/configparser.html#supported-ini-file-structure).

Inside the values, you can reference other key values with the `{[section:]key}` format, for example:

```ini
[ssh]
home        = ~/.ssh
config-file = {ssh:home}/config
key-file    = {home}/scoter-dev-container
```

This will replace the `{ssh:home}` value with the `[ssh]` section `home` key, forming a final value of `~/.ssh/config` (remember that the tool will also expand `~` to your home directory).

In the same way, the `key-file` key will assume the `{home}` reference refers to the same section as the `key-file`, which will expand this into `~/.ssh/scoter-dev-container`.

## Defining Executables

The tool makes heavy use of running other commands.  In order to remain flexible enough to work with your particular environment, it sets up executables based on the tool name and the uses for it:

```ini
[docker]
exec       = /usr/sbin/podman
run-build  = {exec} -t {x:tag} -f {x:dockerfile} {x:dirname}
```

In this case, when the tool tries to build a container image, it will use the `[docker]` `run-build` key.  The execution will include external values `{x:tag}`, `{x:dockerfile}`, and `{x:dirname}`, and allow the `{exec}` to come from the `[docker]` `exec` key.

If you find yourself using very obscure versions or alternatives of these tools, you may find yourself needing to use a translator script to convert between what Surf Scoter expects and what your tool does.

## Defining Lookup Paths

The tool relies upon the search path to find files it uses for running parts of its task.  This allows you to override parts of the configuration to better suit your project.
