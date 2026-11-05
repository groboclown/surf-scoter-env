# Project Verification

A project you clone brings its own Surf Scoter settings: the host commands the container may run on your computer, the hosts it may reach, and the dev container it runs in.  Those settings are the biggest weak point in the tool, since you can pull an unknown configuration and run it without meaning to, on first download or on any later pull.  So the tools never act on them.  The project's `.surf-scoter.d` is only a suggestion: you review it with `scoter pull`, which stores your own copy, a *configuration*, and `scoter run` runs only that.

This document describes what's reviewed, where your configurations are kept, and how a run uses one.  [Verification attack vectors](verification-attack-vectors.md) analyzes the risks and weak points.  The user's view is in [the project configuration file](../project-configuration-file.md) and [setting up a new project](../new-project-setup.md); the design, and the reasons behind it, are in [user-owned project files](../contributors/user-owned-project-files.md).


## Found, then copied

The project's `.surf-scoter.d` has two uses only: finding the project, and seeding a pull.  Nothing parses it as configuration.  `scoter` finds the project without reading its files, and every command that needs them reads a copy: the pull's working directory, your unlocked copy, or a configuration staged from the store.  The generator is given the copy (`--config-dir`), looks for a container or parent found by name in it first, then in your own `dev-containers-search` directories, never in the project, and refuses a setting that names the live directory.  The only other commands that touch it are `scoter push`, which writes into it on purpose, and `scoter run`, which hashes it, never parsing it, to tell you when it changed.


## What's reviewed

`scoter pull` seeds a working directory, in your state directory, from the project's files as committed (`--working-tree` for the files on disk):

* the whole `.surf-scoter.d` is read first, within the limits, before anything parses it;
* the generator runs on a staged copy of those bytes, and only the project file and the container directories the build uses (the dev container and its parents inside the copy) are kept;
* the host commands are resolved for your computer, through the path checks, into a *host file* beside the files, never among them.

Everything else is **trusted rather than reviewed**: containers found in your own `dev-containers-search` directories (by default, the distribution's `containers`), the firewall and gateway containers, the compose file, base images, and your setup files.  The rest of the repository isn't copied; it's what the container works on.

`scoter pull analyze` checks the working directory with the [checks](../system-config-file.md#host-command-checks), and reports each finding with an id that stays the same while the issue does.  The reviewer (by default, a tree explorer in the terminal, `pull: runs: reviewer`) shows the report: what changed since the last pull and since the configuration in use, the host commands with the programs they'd run, the egress rules, the dev container's values, and each file's contents.  You can edit the working directory while you review; the reviewer notices, analyzes again, and marks what's new.  Approval is of the analysis on screen: `scoter pull approve` takes its id, snapshots the working directory again, and refuses if anything changed since, or if any check refuses.  Typing `approve` approves; anything else denies.

Reading the project is defensive.  Each file is opened without following symlinks and checked as it's read.  Symlinks, special files, hard links (but for the project file, which only warns), setuid, setgid and sticky bits, and names with control or non-ASCII characters are refused.  Each file and the whole source have a size limit (`verification: values: max-file-size` and `max-snapshot-size`).  Only the owner-execute bit is kept.  From a commit, the VCS's handler reads only the repository's objects, never the working tree, and its output is checked against a schema and a size limit.

Everything the project wrote is cleaned before it's shown: control characters and terminal escape sequences are removed or escaped, and non-ASCII text is written as `\uXXXX`, so bidirectional overrides and look-alike characters show up for what they are.  The report marks the project's text, with its clean form, for any reviewer to show.


## Where your configurations are kept

Configurations are kept in the *verified store*, `verification: paths: store` (default `~/.config/surf-scoter.d/verified`), never in the project.  [The system configuration file](../system-config-file.md#the-verified-store) describes it.  The parts that matter for security:

* **It's private.**  Directories are mode `0700` and files `0600`, owned by you.  Every read checks the owner and mode, and refuses symlinks.  Writes go to a temporary file that's renamed into place.  An operating system lock keeps two `scoter` commands from interleaving.  On Windows, the mode checks are left to the profile's access control lists.
* **It's outside the project.**  The generator refuses a store, or a state directory, inside the project.
* **It's keyed by the environment id**, made from the project directory's name and a hash of its real path.  A second clone, a worktree or a moved directory gets its own.
* **Each configuration** is a snapshot's manifest (the contents are blobs named by their SHA-256) and its host file, under a name; it's never changed once stored.
* **Each environment's link** says which configuration is in use (or that it's unlocked), the snapshot last pulled (the base), and when you last reviewed it.  Only `scoter`'s own commands change it.


## How a run uses it

Before `scoter run` builds anything:

1. It finds the environment's link.  Without a configuration in use, or an unlocked copy, it stops, and says to `scoter pull`.
2. It restores the configuration from the store into a private staging directory, checking each blob, or reads the unlocked copy as a pull reads a source, and stages that.
3. The generator reads only the staged copy, and the host file: each host command runs the program the host file names, never one looked up on the `PATH`, and a program that's gone or changed stops the run.  The egress rules are the configuration's.
4. The checks run again; a refusal stops the run, naming the check and the setup file it's in.
5. It reminds you to review a configuration you haven't for a while, and, if your setup asks, stops until you do; it warns before each run of an unlocked copy.
6. It hashes the project's live files, and says, in one line, if they changed since your last pull.

The build uses only the staged copy, so a change to the project's files never reaches it.


## Changing your configuration

* `scoter unlock` copies the configuration in use (or, with `--from-template`, a distribution template) into `pull: paths: unlocked-dir`, for you to edit; runs use it as it is, staged, with a warning.  `scoter lock` reviews it like a pull and stores it; `scoter lock --revert` throws the edits away.
* `scoter review` looks at the configuration in use again, leading with what's new since the last review.
* `scoter switch NAME` uses another configuration, after the checks.  A pull keeps the one it replaced as `default.previous`.
* `scoter push` writes the configuration in use into the project, after showing it as a patch; it never deletes, and never writes the host file.
* `scoter list` lists your configurations, and `scoter remove` removes them.  `scoter cleanup` removes those of projects whose directory is gone.  See [cleaning up](../cleaning-up.md).
