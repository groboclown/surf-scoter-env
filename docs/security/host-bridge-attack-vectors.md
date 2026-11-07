# Host Bridge Attack Vectors

The weak points of [the host bridge](host-bridge.md): how code in the development container could misuse the one intended way for it to cause something to happen on the host.  Contributors should treat changes to the code listed here as security-sensitive, and the red team can use each vector as a starting point.  See the [attack vector overview](attack-vectors.md) for how to read the severity and status labels.


## Attacker goals

An allowed command runs on the host, as the developer, with the developer's credentials.  Code in the container that can steer one wants to:

* run arbitrary programs on the host;
* read or change host files outside the project clone;
* use the host command's credentials, or bring them back into the container;
* exhaust host resources.


## Attack surface

| Input | Controlled by | Handled in |
| --- | --- | --- |
| The invocation: command name, arguments, working directory, environment | Code in the container | [`analyze.rs`](../../host/src/host/analyze.rs), [`environment.rs`](../../host/src/config/environment.rs) |
| Wire messages from the broker | Code in the container (the broker is part of the image) | [`host.rs`](../../host/src/host.rs), [`comm/`](../../containers/dev-env/src/comm.rs) |
| Standard input and signals for a running command | Code in the container | [`exec.rs`](../../host/src/host/exec.rs) |
| Project clone files named in path arguments | Code in the container, at any time | `resolve` in [`analyze.rs`](../../host/src/host/analyze.rs); the host program |
| The `commands` configuration | The project, as pulled into your configuration | [`project.rs`](../../host/src/config/project.rs), [`pattern.rs`](../../host/src/config/pattern.rs), [`checks.py`](../../scripts/surf_scoter_lib/checks.py) |
| The universal configuration file | `scoter`, as you | `load_universal` in [`config.rs`](../../host/src/config.rs); `check_for_run` in [`host_file.rs`](../../host/src/config/host_file.rs) |
| The host file's programs on the host | Whoever can write them or their directories | `accepted_executable` in [`exec.rs`](../../host/src/host/exec.rs) |
| The host's `PATH` and working directory | The developer | `find_on_host` in [`exec.rs`](../../host/src/host/exec.rs); [`command.rs`](../../host/src/config/command.rs) |


## Summary

| ID | Vector | Severity | Status |
| --- | --- | --- | --- |
| [BRIDGE-1](#bridge-1-allowed-commands-that-read-container-controlled-input) | Allowed commands that read container-controlled input | Critical | By design |
| [BRIDGE-2](#bridge-2-the-project-chooses-the-commands) | The project chooses the commands | High | Mitigated |
| [BRIDGE-3](#bridge-3-path-checks-race-the-host-program) | Path checks race the host program | High | Mitigated |
| [BRIDGE-4](#bridge-4-host-output-returns-to-the-container) | Host output returns to the container | High | By design |
| [BRIDGE-5](#bridge-5-allowed-environment-variables-steer-the-program) | Allowed environment variables steer the program | High | By design |
| [BRIDGE-6](#bridge-6-path-mapping-and-resolution) | Path mapping and resolution | High | Mitigated |
| [BRIDGE-7](#bridge-7-argument-matching) | Argument matching | High | Mitigated |
| [BRIDGE-8](#bridge-8-a-hostile-broker) | A hostile broker | Medium | Mitigated |
| [BRIDGE-9](#bridge-9-no-limit-on-host-processes) | No limit on host processes | Medium | Open |
| [BRIDGE-10](#bridge-10-signals-delivered-to-host-processes) | Signals delivered to host processes | Low | By design |
| [BRIDGE-11](#bridge-11-the-hosts-path-lookup) | The host's `PATH` lookup | Low | Closed; Open while unlocked |
| [BRIDGE-12](#bridge-12-windows-hosts) | Windows hosts | Medium | Unverified |
| [BRIDGE-13](#bridge-13-an-accepted-program-can-change-after-the-review) | An accepted program can change after the review | Medium | Partly Mitigated |
| [BRIDGE-14](#bridge-14-the-bridge-trusts-the-universal-configuration-files-command-list) | The bridge trusts the universal configuration file's command list | Low | Open |


## Vectors

### BRIDGE-1: Allowed commands that read container-controlled input

**Severity:** Critical, when configured.  **Status:** By design; the configuration is the control.
**Where:** the project's `commands`; [`analyze.rs`](../../host/src/host/analyze.rs).

The bridge checks the command line, not what the program does with it.  A program that reads configuration or code from its working directory, from a file argument or from its environment (`docker compose`, `make`, `npm run`, `terraform`, `docker build`, `git`, any shell or interpreter) runs whatever the container wrote there.  Allowing `docker compose up` in a directory of the project clone gives the container root on the host.

**Red team:** for each allowed command in real project configurations, list every file and variable it reads, and check which of them the container can write.

**When changing this code:** documentation and examples should never show such commands without a fixed, container-unwritable input.

### BRIDGE-2: The project chooses the commands

**Severity:** High.  **Status:** Mitigated by [project verification](verification.md).
**Where:** [`project.rs`](../../host/src/config/project.rs); [`host_commands.rs`](../../host/src/config/host_commands.rs) and [`checks.py`](../../scripts/surf_scoter_lib/checks.py) (the checks); [`host_file.rs`](../../host/src/config/host_file.rs) and [`universal.rs`](../../host/src/universal.rs) (the configuration's commands and their programs).

The project only suggests `commands`.  `scoter-host` runs only your configuration's commands, which you approved with `scoter pull` (or `lock`, or `review`), each with the absolute program its host file names, and a change to the project's suggestion reaches a run only through another pull.  The checks run again on every run, so a check added since applies at once.  The setup's `verification: host-commands` checks refuse shells, interpreters, command runners and the like, and warn about programs that read configuration from their working directory (BRIDGE-1); programs inside the project are refused, and programs other users can write to are refused or flagged.

What's left:

* **A command no check covers gets only a note.**  The checks are a deny-list; a dangerous program that isn't on it, or a dangerous use of one that's allowed, depends on the reviewer.
* **Overrides.**  A refusal can only be set aside in your setup, with a check that `overrides` it; there's no per-review override any more.
* **Approving without looking.**  A reviewer of your own can approve anything the checks don't refuse ([VER-27](verification-attack-vectors.md#ver-27-approval-cant-prove-a-person-reviewed)).
* **The unlocked state** runs your edited commands unreviewed, with a warning ([VER-33](verification-attack-vectors.md#ver-33-the-unlocked-state)).

**Red team:** get a widened `commands` entry approved: through a program the checks don't name, or through an `any` placeholder after an option that runs code.

**When changing this code:** `scoter-host` must keep reading commands only from the configuration and its host file.  New checks belong in the setup's `host-commands` lists, so they apply at every run.

### BRIDGE-3: Path checks race the host program

**Severity:** High.  **Status:** Mitigated: closed for `path` arguments and the working directory itself; a program run in a directory of the clone still reads what's there by path.
**Where:** `resolve` and `match_path` in [`analyze.rs`](../../host/src/host/analyze.rs); [`clone_path.rs`](../../host/src/host/clone_path.rs); `copy_in`, `copy_out` and `enter_clone_dir` in [`exec.rs`](../../host/src/host/exec.rs).

A path argument used to be resolved and checked when the request arrived, then passed to the host program as a path.  The container could change the clone at any time after the check, including while the program ran, for example by replacing a checked directory with a symlink, and the host program then followed it outside the clone.  So could the working directory.

**What closed it:**

* **Paths are never handed over.**  A `path` argument in the clone goes through the request's private temporary directory, under its own name: a file the program reads (`access: read`) is copied in before it starts; one it writes (`access: write`) is put back when it exits; `read-write` does both.  The program never gets a path into the clone, so nothing the container does to the clone while it runs can redirect it.
* **No symlink is ever followed.**  Each copy reaches the file from the clone's root one name at a time (`openat` with `O_NOFOLLOW`), and refuses a symlink anywhere, even one that stays in the clone.  The clone's root is a host directory the container can't reach (FILE-10), so it's opened by path.  Names are taken as written, with `.` and `..` worked out by name.
* **Writing back can't be redirected.**  A file is put back as a new file (`O_CREAT | O_EXCL | O_NOFOLLOW`) in the directory opened the same way, renamed over the old name; a rename replaces a symlink at that name rather than following it.  A write-back that fails ends the command with 125, and says why.
* **The working directory is entered by handle.**  It's opened the same way, and the child `fchdir`s into the open directory before it runs the program.
* **Only files.**  A directory can't be a `path` argument: it can't be copied through.

**What's left:**

* A program run in a directory of the clone (a `cwd` rule) reads the files there by their paths, so it follows whatever symlinks the container puts there, then or later.  That's [BRIDGE-1](#bridge-1-allowed-commands-that-read-container-controlled-input): a `cwd` rule is for programs that treat the files there as data.
* On Windows, which has no `openat`, each name is checked not to be a symlink or junction before the copy, which can race; the program itself still only sees the copies ([BRIDGE-12](#bridge-12-windows-hosts)).
* The program sees only the file, not its neighbours, and its output appears when it exits.  Tools that need a directory, or the files beside one, can't be given one; `within: temp` and a command that needs no clone files are the choices.

**Red team:** with an allowed command taking a `read` and a `write` path, swap the path's directories for symlinks, before and during the run, and check that nothing outside the clone is read or written.  With a `cwd` rule, try to make the program start anywhere else.  On Windows, race the copy.

**When changing this code:** never give a host program a path into the clone, and never open one there by path: go through `clone_path`.  A new kind of argument that needs a directory needs its own design.

### BRIDGE-4: Host output returns to the container

**Severity:** High.  **Status:** By design.
**Where:** [`exec.rs`](../../host/src/host/exec.rs).

Everything a host command prints goes back to the container, and the container supplies its standard input.  A command that prints a token, a session credential or a file's contents delivers it to the code the bridge was keeping it from.

**Red team:** for each allowed command, find arguments that make it print credentials, configuration or file contents, including in error messages.

**When changing this code:** any future output filtering would need to be opt-in per command, since the bridge can't recognize secrets in general.

### BRIDGE-5: Allowed environment variables steer the program

**Severity:** High.  **Status:** By design; the configuration is the control.
**Where:** [`environment.rs`](../../host/src/config/environment.rs); `env_plan` use in [`analyze.rs`](../../host/src/host/analyze.rs).

Commands start with an empty environment, and only variables the configuration allows come through.  But many variables change what a program loads or where it connects (`LD_PRELOAD`, `PATH`, `PYTHONPATH`, `NODE_OPTIONS`, `GIT_SSH_COMMAND`, `AWS_CONFIG_FILE`, `DOCKER_HOST`), and broad `allow` patterns pass them.  Combined with knowledge of the clone's host path, a library or configuration file in the clone can be loaded by the host program.

**Red team:** in configurations with broad `allow` patterns, find variables that redirect the allowed program's loading or configuration.

**When changing this code:** keep the default empty, and keep `error` patterns refusing the whole request.

### BRIDGE-6: Path mapping and resolution

**Severity:** High.  **Status:** Mitigated.
**Where:** `resolve`, `resolve_dir` and the `path` matching in [`analyze.rs`](../../host/src/host/analyze.rs); [`clone_path.rs`](../../host/src/host/clone_path.rs); [`project_clone.rs`](../../host/src/host/project_clone.rs).

Container paths must be inside the project clone's mount (the effective `mounts: project-clone`).  They're mapped to names below the host's clone directory, with `.` and `..` worked out by name (`..` can't leave the clone), and must match the entry's `within` rule.  No name on the way may be a symlink, and the last must be a regular file; a `write` path may be missing, when its directory exists.  The host program never gets the path itself: it gets a copy ([BRIDGE-3](#bridge-3-path-checks-race-the-host-program)).

**Red team:** try relative paths, `..`, repeated separators, symlink chains, dangling symlinks, hard links to files outside the clone, very long paths and non-UTF-8 names.

The container's persistent store is a container volume, not a host directory, so it has no host path to map to: a path there is refused like any other outside the clone.  The mount points can't be inside one another, so no part of the clone's mount is really another mount ([DEV-2](dev-container-attack-vectors.md#dev-2-mount-points-are-written-into-the-compose-env-file)).  `within: project-clone`, `allowed` patterns and `cwd: allowed-project-clone-dirs` are relative to the clone's root.

**When changing this code:** every file of the clone a host process uses must be reached through `clone_path`, never by path.  Add tests for any new `within` mode.

### BRIDGE-7: Argument matching

**Severity:** High.  **Status:** Mitigated.
**Where:** `match_args` and the `any` matching in [`analyze.rs`](../../host/src/host/analyze.rs); [`pattern.rs`](../../host/src/config/pattern.rs).

Arguments must match an entry one-to-one.  `any` placeholders refuse a leading `-` unless allowed, so they can't introduce options.  But a value without a leading dash can still mean something special to the program: an alternate URL scheme, a remote location, a response-file syntax such as `@file`, or a glob.

**Red team:** for each `any` placeholder in real configurations, find values the program interprets as more than plain data.

**When changing this code:** keep refusing leading dashes by default, and keep the argument count exact.

### BRIDGE-8: A hostile broker

**Severity:** Medium.  **Status:** Mitigated.
**Where:** [`host.rs`](../../host/src/host.rs); [`low.rs`](../../containers/dev-env/src/comm/low.rs), [`broker_host.rs`](../../containers/dev-env/src/comm/broker_host.rs), [`client_host.rs`](../../containers/dev-env/src/comm/client_host.rs).

The broker runs from the image, which the project controls, so the host must treat every message from it as hostile.  Messages are length-prefixed and capped at 32 MiB, and malformed input ends the connection.  The cap is per message, not per connection or in total.

**Red team:** fuzz the host side of the broker protocol: unexpected process ids, messages for closed processes, maximum-size messages across many processes, invalid UTF-8 in invocations.  Measure host memory under load.

**When changing this code:** any change to the wire format needs fuzzing, and new message types need size limits.

### BRIDGE-9: No limit on host processes

**Severity:** Medium.  **Status:** Open.
**Where:** [`host.rs`](../../host/src/host.rs) (a thread per process); [`exec.rs`](../../host/src/host/exec.rs).

The container's process and memory limits don't apply on the host.  Each connection can start an allowed host process, with buffered input, and nothing limits how many run at once.

**Red team:** open many connections requesting a long-running allowed command, and measure host processes, threads and memory.

**When changing this code:** add a limit on concurrent processes, and on buffered data per process.

### BRIDGE-10: Signals delivered to host processes

**Severity:** Low.  **Status:** By design.
**Where:** the signal handling in [`exec.rs`](../../host/src/host/exec.rs); the forwarded signals in [`client_host.rs`](../../containers/dev-env/src/comm/client_host.rs).

The container can send a fixed set of signals to the host processes it started.  Some programs change behavior on signals such as `SIGUSR1`, for example by opening a debugging interface on the host.

**Red team:** for each allowed command, check what the forwarded signals do to it.

**When changing this code:** keep the forwarded set small, and consider making it configurable per command.

### BRIDGE-11: The host's `PATH` lookup

**Severity:** Low.  **Status:** Closed for a locked configuration; knowingly Open while it's unlocked.
**Where:** `accepted_executable` in [`exec.rs`](../../host/src/host/exec.rs); `find_executable` in [`command.rs`](../../host/src/config/command.rs); [`host_file.rs`](../../host/src/config/host_file.rs); `resolve` in [`host_file.py`](../../scripts/surf_scoter_lib/host_file.py).

Bare command names used to be found on `scoter-host`'s own `PATH`, where relative entries such as `.` were resolved against its working directory, usually the project directory.

Now `scoter-host` runs exactly the absolute program the configuration's host file names, which the pull resolved through the path checks and you approved, refuses one that isn't absolute (when it loads the file, and again before running it), and never looks anything up on its `PATH`.  A program that's gone, or is now a different file, stops the run (and `scoter-host` checks again when it starts); it's never looked for elsewhere.  While a configuration is unlocked, its commands are resolved as each run starts, through the same path checks, and listed: a `PATH` change can change what runs until `scoter lock` reviews it.  Where programs are found on a `PATH` (by the generator, for the setup's programs and the host command checks), relative and empty entries are skipped.

**Red team:** with a relative `PATH` entry on the host, check that a file in the project directory can't shadow an allowed command; remove an accepted program, and check the request is refused.

**When changing this code:** keep programs absolute from the configuration to the `exec`; don't add a fallback lookup.

### BRIDGE-12: Windows hosts

**Severity:** Medium.  **Status:** Unverified.
**Where:** path mapping in [`analyze.rs`](../../host/src/host/analyze.rs) and [`project_clone.rs`](../../host/src/host/project_clone.rs); temporary file names; signal handling in [`exec.rs`](../../host/src/host/exec.rs).

The bridge supports Windows hosts, where path rules differ: case-insensitive names, short (8.3) names, junctions, alternate data streams, device names and `\\?\` prefixes.  The temporary file name checks cover separators, `.`, `..` and device names, but the path resolution hasn't been tested against the rest.

**Red team:** repeat BRIDGE-3 and BRIDGE-6 on a Windows host, including through WSL.

**When changing this code:** run the path tests on Windows as well as Linux.

### BRIDGE-13: An accepted program can change after the review

**Severity:** Medium.  **Status:** Partly Mitigated; Open for a program's contents.
**Where:** `accepted_executable` in [`exec.rs`](../../host/src/host/exec.rs); `check_program` and `check_for_run` in [`host_file.rs`](../../host/src/config/host_file.rs); `resolve` in [`host_file.py`](../../scripts/surf_scoter_lib/host_file.py); the program checks in [`checks.py`](../../scripts/surf_scoter_lib/checks.py).

A configuration's host file records each program's absolute path and the real file it resolved to, with its symlinks followed.  At each run, the generator refuses a program that isn't an executable any more, that's now a different real file (a version manager switching a link, say), or that's in the project, its clone or its repository; `scoter-host` checks the same again as it starts.  So a changed link stops the run rather than running its new target.

Contents aren't checked unless the host file has a `sha256`: the tools don't record one when they resolve a program (an upgrade would stop every run), so anything that replaces the program's file in place (a package upgrade, or a host command whose arguments let it write there) gets its replacement run, until the next `scoter review` shows the program again.  A developer can add a `sha256` to an entry while the configuration is unlocked.

**Red team:** approve a command, then replace the program's file in place, and change a link on its path; check which is refused.

**When changing this code:** keep the real-file check at every run; consider recording owner and mode, and opening the file once and running what was checked where the platform allows.

### BRIDGE-14: The bridge trusts the universal configuration file's command list

**Severity:** Low (defense in depth).  **Status:** Open.
**Where:** `load_universal` in [`config.rs`](../../host/src/config.rs); `check_for_run` in [`host_file.rs`](../../host/src/config/host_file.rs).

`scoter-host` reads its commands from the universal configuration file's `host-brokered` list.  It checks the file's owner and mode, and that each program its `host-file` names is still the file it was approved as, but not that `host-brokered` is the configuration's commands with those programs, nor that the configuration is still the one in use in the store.  Only something that can already write your private files could use that, but it turns a change to one file into commands run on the host without a review.

**Red team:** edit a running environment's `suc.json` (as you) to add a command to `host-brokered`, restart `scoter-host`, and see whether it runs the command.

**When changing this code:** build the command list from the configuration and its host file in `scoter-host` itself, or refuse a file whose `host-brokered` differs from them.


## Sensitive code

* [`host/src/host/analyze.rs`](../../host/src/host/analyze.rs): request matching and path resolution.
* [`host/src/host/exec.rs`](../../host/src/host/exec.rs): process start, environment, input, output and signals.
* [`host/src/host.rs`](../../host/src/host.rs) and [`host/src/host/container.rs`](../../host/src/host/container.rs): the broker connection.
* [`host/src/config/`](../../host/src/config/): the `commands`, `env` and pattern parsing, and the host file and its checks (`host_file.rs`).
* [`containers/dev-env/src/comm/`](../../containers/dev-env/src/comm.rs): the wire protocol.
