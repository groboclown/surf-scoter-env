# Host Bridge Attack Vectors

The weak points of [the host bridge](host-bridge.md): how code in the development container could misuse the one intended way for it to cause something to happen on the host.  Contributors should treat changes to the code listed here as security-sensitive, and the red team can use each vector as a starting point.  See the [attack vector overview](attack-vectors.md) for how to read the severity and status labels.


## Attacker goals

An allowed command runs on the host, as the developer, with the developer's credentials.  Code in the container that can steer one wants to:

* run arbitrary programs on the host;
* read or change host files outside the workspace;
* use the host command's credentials, or bring them back into the container;
* exhaust host resources.


## Attack surface

| Input | Controlled by | Handled in |
| --- | --- | --- |
| The invocation: command name, arguments, working directory, environment | Code in the container | [`analyze.rs`](../../host/src/host/analyze.rs), [`environment.rs`](../../host/src/config/environment.rs) |
| Wire messages from the broker | Code in the container (the broker is part of the image) | [`host.rs`](../../host/src/host.rs), [`comm/`](../../containers/dev-env/src/comm.rs) |
| Standard input and signals for a running command | Code in the container | [`exec.rs`](../../host/src/host/exec.rs) |
| Workspace files named in path arguments | Code in the container, at any time | `resolve` in [`analyze.rs`](../../host/src/host/analyze.rs); the host program |
| The `commands` configuration | The project, as you accepted it | [`project.rs`](../../host/src/config/project.rs), [`pattern.rs`](../../host/src/config/pattern.rs), [`accepted.rs`](../../host/src/config/accepted.rs), [`checks.py`](../../scripts/surf_scoter_lib/checks.py) |
| The universal configuration file | `scoter`, as you | `load_universal` in [`config.rs`](../../host/src/config.rs); [`accepted.rs`](../../host/src/config/accepted.rs) |
| The accepted programs on the host | Whoever can write them or their directories | `accepted_executable` in [`exec.rs`](../../host/src/host/exec.rs) |
| The host's `PATH` and working directory | The developer | `find_on_host` in [`exec.rs`](../../host/src/host/exec.rs); [`command.rs`](../../host/src/config/command.rs) |


## Summary

| ID | Vector | Severity | Status |
| --- | --- | --- | --- |
| [BRIDGE-1](#bridge-1-allowed-commands-that-read-container-controlled-input) | Allowed commands that read container-controlled input | Critical | By design |
| [BRIDGE-2](#bridge-2-the-project-chooses-the-commands) | The project chooses the commands | High | Mitigated |
| [BRIDGE-3](#bridge-3-path-checks-race-the-host-program) | Path checks race the host program | High | Open |
| [BRIDGE-4](#bridge-4-host-output-returns-to-the-container) | Host output returns to the container | High | By design |
| [BRIDGE-5](#bridge-5-allowed-environment-variables-steer-the-program) | Allowed environment variables steer the program | High | By design |
| [BRIDGE-6](#bridge-6-path-mapping-and-resolution) | Path mapping and resolution | High | Mitigated |
| [BRIDGE-7](#bridge-7-argument-matching) | Argument matching | High | Mitigated |
| [BRIDGE-8](#bridge-8-a-hostile-broker) | A hostile broker | Medium | Mitigated |
| [BRIDGE-9](#bridge-9-no-limit-on-host-processes) | No limit on host processes | Medium | Open |
| [BRIDGE-10](#bridge-10-signals-delivered-to-host-processes) | Signals delivered to host processes | Low | By design |
| [BRIDGE-11](#bridge-11-the-hosts-path-lookup) | The host's `PATH` lookup | Low | Closed |
| [BRIDGE-12](#bridge-12-windows-hosts) | Windows hosts | Medium | Unverified |
| [BRIDGE-13](#bridge-13-an-accepted-program-can-change-after-the-review) | An accepted program can change after the review | Medium | Open |
| [BRIDGE-14](#bridge-14-the-bridge-trusts-the-universal-configuration-files-command-list) | The bridge trusts the universal configuration file's command list | Low | Open |


## Vectors

### BRIDGE-1: Allowed commands that read container-controlled input

**Severity:** Critical, when configured.  **Status:** By design; the configuration is the control.
**Where:** the project's `commands`; [`analyze.rs`](../../host/src/host/analyze.rs).

The bridge checks the command line, not what the program does with it.  A program that reads configuration or code from its working directory, from a file argument or from its environment (`docker compose`, `make`, `npm run`, `terraform`, `docker build`, `git`, any shell or interpreter) runs whatever the container wrote there.  Allowing `docker compose up` in a workspace directory gives the container root on the host.

**Red team:** for each allowed command in real project configurations, list every file and variable it reads, and check which of them the container can write.

**When changing this code:** documentation and examples should never show such commands without a fixed, container-unwritable input.

### BRIDGE-2: The project chooses the commands

**Severity:** High.  **Status:** Mitigated by [project verification](verification.md).
**Where:** [`project.rs`](../../host/src/config/project.rs); [`host_commands.rs`](../../host/src/config/host_commands.rs) and [`checks.py`](../../scripts/surf_scoter_lib/checks.py) (the checks); [`accepted.rs`](../../host/src/config/accepted.rs) and [`universal.rs`](../../host/src/universal.rs) (the accepted copy).

The project only suggests `commands`.  `scoter-host` runs only the commands you accepted with `scoter verify`, each with the absolute program the review resolved, and a change to the project's suggestion needs a new review.  The setup's `verification: host-commands` checks refuse shells, interpreters, command runners and the like, and warn about programs that read configuration from their working directory (BRIDGE-1); programs inside the project are refused, and programs other users can write to are refused or flagged.

What's left:

* **A command no check covers gets only a note.**  The checks are a deny-list; a dangerous program that isn't on it, or a dangerous use of one that's allowed, depends on the reviewer.
* **Overrides.**  A reviewer can set a policy refusal aside for a command with `$override`.  It's recorded and shown at the next review, but it's still one keystroke from a refused command to an accepted one.
* **Accepting without looking.**  `scoter verify --with-exemption` accepts new commands that no check refuses, unseen ([VER-4](verification-attack-vectors.md#ver-4-exemptions-accept-what-nobody-reviewed)).
* **Checks added later don't apply** to projects already accepted, except for the host command checks, which are recorded with each acceptance ([VER-6](verification-attack-vectors.md#ver-6-setup-changes-that-dont-ask-for-a-new-review)).

**Red team:** get a widened `commands` entry accepted: through a program the checks don't name, through an `any` placeholder after an option that runs code, or through `--with-exemption`.

**When changing this code:** `scoter-host` must keep reading commands only from the accepted settings.  New checks belong in the setup's `host-commands` lists, so they're recorded with each acceptance.

### BRIDGE-3: Path checks race the host program

**Severity:** High.  **Status:** Open.
**Where:** `resolve` in [`analyze.rs`](../../host/src/host/analyze.rs); process start in [`exec.rs`](../../host/src/host/exec.rs).

A path argument is resolved and checked when the request arrives, then passed to the host program as a path.  The container can change the workspace in between, for example by replacing a checked directory with a symlink, and the host program then follows it outside the workspace.

**Red team:** with an allowed command that takes a workspace path, change the path's parent directories while requests are in flight, and check where the host program actually reads or writes.

**When changing this code:** consider passing open file descriptors, opening with `O_NOFOLLOW` along the path, or copying inputs to the request's temporary directory.  Until then, `within: temp` is the safer choice for files a host command writes.

### BRIDGE-4: Host output returns to the container

**Severity:** High.  **Status:** By design.
**Where:** [`exec.rs`](../../host/src/host/exec.rs).

Everything a host command prints goes back to the container, and the container supplies its standard input.  A command that prints a token, a session credential or a file's contents delivers it to the code the bridge was keeping it from.

**Red team:** for each allowed command, find arguments that make it print credentials, configuration or file contents, including in error messages.

**When changing this code:** any future output filtering would need to be opt-in per command, since the bridge can't recognize secrets in general.

### BRIDGE-5: Allowed environment variables steer the program

**Severity:** High.  **Status:** By design; the configuration is the control.
**Where:** [`environment.rs`](../../host/src/config/environment.rs); `env_plan` use in [`analyze.rs`](../../host/src/host/analyze.rs).

Commands start with an empty environment, and only variables the configuration allows come through.  But many variables change what a program loads or where it connects (`LD_PRELOAD`, `PATH`, `PYTHONPATH`, `NODE_OPTIONS`, `GIT_SSH_COMMAND`, `AWS_CONFIG_FILE`, `DOCKER_HOST`), and broad `allow` patterns pass them.  Combined with knowledge of the workspace's host path, a library or configuration file in the workspace can be loaded by the host program.

**Red team:** in configurations with broad `allow` patterns, find variables that redirect the allowed program's loading or configuration.

**When changing this code:** keep the default empty, and keep `error` patterns refusing the whole request.

### BRIDGE-6: Path mapping and resolution

**Severity:** High.  **Status:** Mitigated.
**Where:** `resolve`, `resolve_dir` and the `path` matching in [`analyze.rs`](../../host/src/host/analyze.rs); [`workspace.rs`](../../host/src/host/workspace.rs).

Container paths must be inside the workspace mount.  They're mapped to the host's workspace directory and resolved following symlinks, and the result must still be inside the workspace and match the entry's `within` rule.  Missing paths are allowed only when their directory exists and they aren't dangling symlinks.

**Red team:** try relative paths, `..`, repeated separators, symlink chains, dangling symlinks, hard links to files outside the workspace, very long paths and non-UTF-8 names.

With a `workspace-subdir` ([FILE-10](files-attack-vectors.md#file-10-the-workspace-sub-path-through-container-symlinks)), the workspace mount holds the container's home directory as well as the project's clone, and `within: workspace`, `allowed` patterns and `cwd: allowed-workspace-dirs` are all relative to the mount, not the clone.  A configuration written when the clone was the whole workspace now also lets host commands work on the container's home directory.

**When changing this code:** every path a host process receives must come from this resolution.  Add tests for any new `within` mode.

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

**Severity:** Low.  **Status:** Closed.
**Where:** `accepted_executable` in [`exec.rs`](../../host/src/host/exec.rs); `find_executable` in [`command.rs`](../../host/src/config/command.rs).

Bare command names used to be found on `scoter-host`'s own `PATH`, where relative entries such as `.` were resolved against its working directory, usually the project directory.

Now `scoter-host` runs exactly the absolute program the universal configuration file gives (the review resolved it, or, until reviews exist, the generator found it), refuses one that isn't absolute (when it loads the file, and again before running it), and never looks anything up on its `PATH`.  A program that's gone since is refused, not looked for elsewhere.  Where programs are found on a `PATH` (by the generator, for the setup's programs and the host command checks), relative and empty entries are skipped.

**Red team:** with a relative `PATH` entry on the host, check that a file in the project directory can't shadow an allowed command; remove an accepted program, and check the request is refused.

**When changing this code:** keep programs absolute from the configuration to the `exec`; don't add a fallback lookup.

### BRIDGE-12: Windows hosts

**Severity:** Medium.  **Status:** Unverified.
**Where:** path mapping in [`analyze.rs`](../../host/src/host/analyze.rs) and [`workspace.rs`](../../host/src/host/workspace.rs); temporary file names; signal handling in [`exec.rs`](../../host/src/host/exec.rs).

The bridge supports Windows hosts, where path rules differ: case-insensitive names, short (8.3) names, junctions, alternate data streams, device names and `\\?\` prefixes.  The temporary file name checks cover separators, `.`, `..` and device names, but the path resolution hasn't been tested against the rest.

**Red team:** repeat BRIDGE-3 and BRIDGE-6 on a Windows host, including through WSL.

**When changing this code:** run the path tests on Windows as well as Linux.

### BRIDGE-13: An accepted program can change after the review

**Severity:** Medium.  **Status:** Open.
**Where:** `accepted_executable` in [`exec.rs`](../../host/src/host/exec.rs); `AcceptedSettings::check` in [`accepted.rs`](../../host/src/config/accepted.rs); the program checks in [`checks.py`](../../scripts/surf_scoter_lib/checks.py).

The review checks each program's location, owner and permissions, and the acceptance records its absolute path.  At each run, the generator checks only that the path is still an executable file outside the project, and `scoter-host` checks only that it's absolute and still an executable file, following symlinks.  Nothing records the program's contents.  So anything that can replace the program, or change a symlink along its path, between the review and a request (up to the acceptance's 30 days) gets its replacement run: a package upgrade, a version manager switching what a link points to, or a host command whose accepted arguments let it write there.

The workspace also isn't counted as part of the project when the review and the generator check where a program lives, so a program found through a `PATH` entry inside the workspace isn't refused.  The project can't name such a path itself (`program` is a bare name), so this needs your own `PATH` to reach into the workspace.

**Red team:** accept a command, then change the program or a link on its path, and check whether the request is refused.  Put a workspace directory on the host's `PATH` and accept a command found there.

**When changing this code:** record each program's hash, owner and mode at the review, and check them again before running it, opening the file once and running what was checked where the platform allows.  Count the workspace as part of the project in every location check.

### BRIDGE-14: The bridge trusts the universal configuration file's command list

**Severity:** Low (defense in depth).  **Status:** Open.
**Where:** `load_universal` in [`config.rs`](../../host/src/config.rs); `check_for_run` in [`accepted.rs`](../../host/src/config/accepted.rs).

`scoter-host` reads its commands from the universal configuration file's `host-brokered` list.  It checks the file's owner and mode, and that its copy of the accepted settings matches the store, but not that `host-brokered` is the accepted settings' commands.  When the file has no accepted settings at all, it skips the store check and still installs `host-brokered`.  Only something that can already write your private files could use either, but either one turns a change to one file into commands run on the host without any acceptance.

**Red team:** edit a running environment's `suc.json` (as you) to add a command to `host-brokered`, or to remove `accepted-settings`, restart `scoter-host`, and see whether it runs the command.

**When changing this code:** build the command list from the accepted settings in `scoter-host` itself, or refuse a file whose `host-brokered` differs from them, and refuse a file without accepted settings.


## Sensitive code

* [`host/src/host/analyze.rs`](../../host/src/host/analyze.rs): request matching and path resolution.
* [`host/src/host/exec.rs`](../../host/src/host/exec.rs): process start, environment, input, output and signals.
* [`host/src/host.rs`](../../host/src/host.rs) and [`host/src/host/container.rs`](../../host/src/host/container.rs): the broker connection.
* [`host/src/config/`](../../host/src/config/): the `commands`, `env` and pattern parsing, and the accepted settings checks (`accepted.rs`).
* [`containers/dev-env/src/comm/`](../../containers/dev-env/src/comm.rs): the wire protocol.
