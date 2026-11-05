# Cleaning Up After Surf Scoter

Surf Scoter keeps a few things on your computer between runs: your project's clone, its dev container's persistent store, your configurations of each project, and the container images it built.  Most of it is meant to be kept, and reused by the next run.  This page says what each one is, where it lives, and how to clear it out when you want the space back, or a fresh start.

`scoter cleanup` sweeps up what's safe to remove in one go (see [The Cleanup Command](#the-cleanup-command)); the sections after it cover each part, and the commands for clearing one thing at a time.  [Uninstalling](#uninstalling) covers removing Surf Scoter itself.

There are two cleanup commands, and they don't overlap:

| | `scoter cleanup` | `scoter user-cleanup` |
| --- | --- | --- |
| Undoes | What runs, pulls and deleted projects left behind | `scoter user-setup` |
| When | Any time; it's routine | When you uninstall |
| Keeps | Your setup, your ssh key, and anything a running environment or a project still there uses | Nothing it set up |
| Status | Available | Not implemented yet: it does nothing, and fails |

## The Cleanup Command

`scoter cleanup` finds what nothing else removes, lists it, and asks before it removes anything.  It works from anywhere: it doesn't need a project.

```shell
scoter cleanup                          # List what's left behind, and ask.
scoter cleanup --dry-run                # Only list it.
scoter cleanup --force                  # Remove it without asking (for scripts).
scoter cleanup --project-state --persistent-stores --images
                                        # Project state, persistent stores and images too.
```

Without a terminal to ask at, and without `--force`, it only lists.  What it removes:

* **Environments whose run is gone**: a record of a running environment whose `scoter run` process isn't running any more (it crashed, or the computer restarted).  It stops their containers and forgets them, as `scoter kill` does, and keeps their project clones and persistent stores.  On Windows it can't tell whether a run's process is still there, so it leaves them to `scoter kill`.
* **A run's files no environment uses**: a `state/environments/HOST` directory with no record, a run's staged configuration (`state/staged-*`) no running environment uses, a pull's working directory more than a day old (left by `--keep-working-dir`, or a crash), and the temporary directories a run that stopped part way left in `state`.
* **The configurations of projects whose directory is gone**, as `scoter list` marks them: a project you deleted, or moved (a configuration is for the directory it was made in).  A project on a drive that isn't mounted looks gone too: check the list before you say yes.
* **Configurations waiting for a run to stop**: one you removed, or replaced, while an environment was running it is kept until the environment stops; then it goes.
* **The verified store's leftovers**: copies of files no configuration uses, directories no project names, what older versions of Surf Scoter kept for acceptances, and temporary files, from a command that stopped part way.
* With **`--project-state`**, the project state directories (each project's clone, setup and resources directories) no running environment uses, in the default place (`state/projects`).  Anything in a clone that you haven't fetched back into your project is lost, so it only removes them when you ask.
* With **`--persistent-stores`**, the dev containers' persistent store volumes (named starting `scoter-`) that no running environment uses.  The next run starts that project's container with an empty one.
* With **`--images`**, the images Surf Scoter built (`local-surf-scoter/...`) that no running environment uses, and then the layers no image uses any more (`image prune`).  The next run builds what it needs again.

* **SSH entries** for environments that aren't running, as `ssh: values: config-cleanup` says (see [SSH Entries](#ssh-entries)): with `auto`, with the rest; with `ask`, it asks about them on their own, even with `--force`; with `no`, it only says how many there are.

It leaves alone: your configurations of projects that are still there (`scoter remove` removes them), your unlocked copies, running environments, and your container tool's build cache and the images it didn't build.

## What's Kept

The locations below are the defaults; your [setup file](system-config-file.md) can move them.  Each project's directories are named by its *project id*: the project directory's name and a short hash of its full path, such as `my-project-c593c7ec2d55`.

| What | Where | Kept until |
|---|---|---|
| A running environment's record | `~/.config/surf-scoter.d/state/running-env.json` | The run ends, `scoter kill`, or `scoter cleanup` once its run is gone |
| A running environment's configuration | `~/.config/surf-scoter.d/state/environments/HOST/` | The run ends, `scoter kill`, or `scoter cleanup` |
| The project's state: its clone (`project-clone`), setup and resources directories | `~/.config/surf-scoter.d/state/projects/PROJECT-ID/` | `scoter run --clean-project-clone`, `scoter kill --clean-project-clone`, or `scoter cleanup --project-state` |
| The dev container's persistent store | A volume in your container tool, `scoter-PROJECT-ID-store` | `scoter run --clean-persistent-store`, `scoter kill --clean-persistent-store`, or `scoter cleanup --persistent-stores` |
| Your configurations, with a copy of their files | `~/.config/surf-scoter.d/verified/` | `scoter remove`, or `scoter cleanup` once the project's directory is gone |
| An unlocked configuration, for you to edit | `~/.config/surf-scoter.d/unlocked/ENV-ID/` | `scoter lock`, or `scoter lock --revert` |
| A run's staged copy of the configuration it runs | `~/.config/surf-scoter.d/state/staged-*/` | The run ends, `scoter kill`, or `scoter cleanup` |
| A pull's working directory | `~/.config/surf-scoter.d/state/pull/` | The pull ends (unless `--keep-working-dir`), or `scoter cleanup` after a day |
| The container images | Your container tool's image store, tagged `local-surf-scoter/...` (and the dev container's parents' tags) | You remove them, or `scoter cleanup --images` |
| Each environment's ssh entries | `~/.ssh/config` and `~/.ssh/known_hosts` | The environment stops, as `ssh: values: config-cleanup` says; or `scoter cleanup` |

## Environments Left Running

`scoter run` stops its containers when it ends.  If it was stopped some other way (it crashed, or you closed the terminal), its containers can keep running, and it stays listed as running.  `scoter kill` stops them and forgets the environment:

```shell
scoter kill                   # In the project: its environment.
scoter kill --host HOST       # Any environment, from anywhere.
```

If more than one is running, or there's no project where you are, it lists the running ones for you to choose from.  `scoter cleanup` stops every environment whose run is gone, without your naming them.

The project clone and the persistent store are kept, so your work isn't lost.  Before you clear the clone, you can bring your changes back into your project with `--fetch BRANCH`, which fetches the clone's branch into your repository for you to review.  `--clean-project-clone` (or `--clean-project`) removes the clone once the containers are stopped; only use it once you've fetched what you need.  `--clean-persistent-store` removes the persistent store.

## The Project Clone

Each project has one clone, reused by each run, and mounted into the dev container at its `mounts: project-clone`.  At the start of a run, your project's latest commits are bundled for the clone (unless you give `--no-project-fetch`), and the dev container fetches them as it starts, into its `origin/*` branches; your changes in the container stay there until you fetch them back (see [Don't run git or your IDE on the project clone from your host](../README.md#dont-run-git-or-your-ide-on-the-project-clone-from-your-host)).

* `scoter run --clean-project-clone` (or `--clean-project`) starts the clone over, from a fresh clone of your project.  Do this if anything in it looks suspicious.  It keeps the persistent store.
* `scoter kill --clean-project-clone` removes it once the environment is stopped.
* `scoter cleanup --project-state` removes every project's state directory no running environment uses (it lists them, and asks, first).
* To remove one without running, delete its `projects/PROJECT-ID` directory, once the project's environment isn't running.  A project you've deleted or moved leaves its state behind: it's safe to delete.

Anything in a clone you haven't fetched back into your project is lost when it's removed.

## The Persistent Store

The dev container keeps its own files between runs, such as its IDE server, caches and shell history, in a *persistent store*: a volume in your container tool, mounted at its `mounts: persistent-store`, and named `scoter-PROJECT-ID-store` (`project-state: values: persistent-store-volume`).  It isn't a directory on your computer, so host commands can't reach it, and neither can tools you run on your computer.  Stopping an environment keeps it.

* `scoter run --clean-persistent-store` starts the container with an empty one.  Do this if anything the container keeps looks suspicious.
* `scoter kill --clean-persistent-store` removes it once the environment is stopped.
* `scoter cleanup --persistent-stores` removes every one no running environment uses.
* By hand: `docker volume ls --filter name=^scoter-` lists them, and `docker volume rm NAME` removes one.

## Upgrading From Workspaces

Before the project clone and the persistent store, each project had a *workspace*, in `state/workspaces`, which held both the clone and the container's home directory.  The new version doesn't read the old running environment records, so:

* **Stop every environment before you upgrade**, with `scoter kill` (or `scoter kill --fetch BRANCH` to bring back what's in its workspace first).
* If you upgraded with one still running, stop its containers with your container tool (`docker ps` lists them, named `scoter-...`), then delete `~/.config/surf-scoter.d/state/running-env.json`.
* Fetch anything you need from the old workspaces, then delete `~/.config/surf-scoter.d/state/workspaces`.  Nothing uses it any more, and `scoter cleanup` doesn't look there.

## Configurations

Each project you pull keeps your configurations of it, with a copy of their files: the one in use, and, after a pull replaces it, the one before it (`default.previous`).  `scoter lock` and `scoter review` replace the one they start from, so they don't pile up.

`scoter list` lists them all, for every project, with how much space their copies take:

```text
/home/me/src/my-project
  last reviewed 2026-10-21T09:15:00+00:00
  20261021T091500Z-1a2b3c4d  default  made 2026-10-21 09:15 UTC  (in use)
  20260915T140000Z-5e6f7a8b  default.previous  made 2026-09-15 14:00 UTC
/home/me/src/gone-project  (the directory is gone)
  20260901T120000Z-9c0d1e2f  default  made 2026-09-01 12:00 UTC  (in use)
3 configurations of 2 projects; their files take 81920 bytes.  Remove one with `scoter remove ID`.
```

`scoter remove` removes them, with the copies nothing else uses:

```shell
scoter remove ID                  # One configuration, from anywhere.
scoter remove default.previous    # In the project: one by name.
scoter remove --all               # In the project: all of them, and forget the project.
scoter remove $( scoter list -q ) # Everything, for every project.
```

Only your copies go: the project's own files are never touched.  Removing the configuration in use means the project's next run asks for `scoter pull`.  One a running environment uses goes once it stops.  The one your unlocked copy came from can't be removed until you `scoter lock` or `scoter lock --revert`.  A project whose directory is gone can only be removed by id; `scoter cleanup` removes every such project's configurations.

## Container Images

Each run builds the project's dev container, and the network containers, into your container tool's image store, tagged under `local-surf-scoter/`; the dev container's parents (`parents` in its `dev-env.yaml`) get the tags they're given there.  The tags are reused from run to run, so they don't pile up, but the layers they replace, and the build cache, do.  `scoter cleanup --images` removes the `local-surf-scoter/` images no running environment uses, then the layers no image uses.  For the rest, such as the build cache or the parents' images, use your container tool, once no environment is running:

```shell
docker image ls 'local-surf-scoter/*'     # What Surf Scoter built.
docker image rm IMAGE                     # One of them.
docker image prune                        # Layers no tag uses any more.
docker builder prune                      # The build cache.
```

With Podman, use `podman` in place of `docker`.  The next run builds what it needs again, which takes longer the first time.

## SSH Entries

Each `scoter run` writes its environment's own `Host` entry to `~/.ssh/config`, named after the project (`scoter-my-project-c593c7ec2d55`), so `ssh` and your IDE find it by name, and puts its host key in `~/.ssh/known_hosts`.  When the environment stops, `ssh: values: config-cleanup` in your setup says what happens to them:

* `ask` (the default): you're asked whether to remove them.  Without a terminal to ask at, they're kept.
* `auto`: they're removed.
* `no`: they're kept, for you to remove.

`scoter cleanup` finds the entries left behind (by `no`, a "no" to the question, or a crash).  The tools only change or remove the entries they wrote, which have a `# Surf Scoter environment` line; your own entries are left alone.  To remove one by hand, delete its `Host` entry from `~/.ssh/config`, and its key (`ssh-keygen -R HOST`).

## Uninstalling

`scoter user-cleanup` is meant to undo `scoter user-setup`, but it isn't implemented yet.  Until it is, uninstall by hand:

1. Stop every environment (`scoter kill`), then run `scoter cleanup --project-state --persistent-stores --images`, so nothing is left in your container tool.
2. Delete `~/.config/surf-scoter.d`: your setup, your configurations of each project, and the state directory.
3. Delete the ssh key pair `scoter user-setup` made, `~/.ssh/scoter-dev-container` and `~/.ssh/scoter-dev-container.pub` (`ssh: paths: key-file` in your setup, if you moved it).  `scoter user-setup` only creates `~/.ssh/config` and `~/.ssh/known_hosts` when they're missing, so leave them, but for any `Host` entries with a `# Surf Scoter environment` line (see [SSH Entries](#ssh-entries)).
4. Delete the distribution.
