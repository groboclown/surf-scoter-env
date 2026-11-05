# Verification Attack Vectors

The weak points of [project verification](verification.md): how a project could get settings or files past the pull's review, or into a run without one.  Contributors should treat changes to the code listed here as security-sensitive, and the red team can use each vector as a starting point.  See the [attack vector overview](attack-vectors.md) for how to read the severity and status labels.

Verification is the control that several other vectors depend on: [NET-2](network-attack-vectors.md#net-2-the-project-controls-the-allowlist), [BRIDGE-2](host-bridge-attack-vectors.md#bridge-2-the-project-chooses-the-commands) and [DEV-2](dev-container-attack-vectors.md#dev-2-mount-points-are-written-into-the-compose-env-file) are only as strong as it is.  The design, and the options weighed for each vector below, are in [user-owned project files](../contributors/user-owned-project-files.md).


## Attacker goals

* **Run unreviewed files**: get a changed `Dockerfile`, script or `dev-env.yaml` built without a review.
* **Hide what's approved**: get a setting approved that the reviewer never saw, or saw as something else.
* **Reach the live project**: get a run, or a pull's parse, to read the project's files rather than a copy.
* **Tamper with the store**: change what the run thinks you approved.
* **Write into the project**: get a push to write somewhere it shouldn't, or something it shouldn't.


## Attack surface

| Input | Controlled by | Handled in |
| --- | --- | --- |
| Files in `.surf-scoter.d` (project file, Dockerfiles, `dev-env.yaml`, build files) | The project, including changes pulled from others or brought back from the project clone | `snapshot_source`, `snapshot_bytes` and `select_build` in [`snapshot.py`](../../scripts/surf_scoter_lib/verified/snapshot.py); [`pull/seed.py`](../../scripts/surf_scoter_lib/pull/seed.py) |
| The VCS's reports on committed files | The repository's objects; the handler is setup | [`vcs_files.py`](../../scripts/surf_scoter_lib/vcs_files.py), [`vcs_git_files.py`](../../scripts/vcs_git_files.py) |
| File names and contents shown in the review | The project | [`pull/analyze.py`](../../scripts/surf_scoter_lib/pull/analyze.py), [`pull/reviewer.py`](../../scripts/surf_scoter_lib/pull/reviewer.py), [`review.py`](../../scripts/surf_scoter_lib/review.py); [`checks.py`](../../scripts/surf_scoter_lib/checks.py) |
| The working directory and the unlocked copy | You, your editor, and any process running as you | [`pull/workdir.py`](../../scripts/surf_scoter_lib/pull/workdir.py), [`pull/unlocked.py`](../../scripts/surf_scoter_lib/pull/unlocked.py) |
| The verified store | You, and any process running as you | [`store.py`](../../scripts/surf_scoter_lib/verified/store.py), [`configs.py`](../../scripts/surf_scoter_lib/verified/configs.py); [`host_file.rs`](../../host/src/config/host_file.rs) |
| The setup, and the reviewer it names | You | [`setup.yaml`](../../defaults/setup.yaml); `fingerprint` in [`pull/workdir.py`](../../scripts/surf_scoter_lib/pull/workdir.py) |
| The distribution's scripts and templates | Whoever installed them | [`scoter.py`](../../scoter.py); [`pull/lifecycle.py`](../../scripts/surf_scoter_lib/pull/lifecycle.py) |


## Summary

| ID | Vector | Severity | Status |
| --- | --- | --- | --- |
| [VER-1](#ver-1-the-dockerfile-meaning-match-can-be-fooled) | The Dockerfile meaning match can be fooled | High | Closed |
| [VER-2](#ver-2-any-file-named-dev-envyaml-matches-on-meaning) | Any file named `dev-env.yaml` matches on meaning | High | Closed |
| [VER-3](#ver-3-the-review-doesnt-show-everything-it-accepts) | The review doesn't show everything it accepts | High | Mitigated |
| [VER-4](#ver-4-exemptions-accept-what-nobody-reviewed) | Exemptions accept what nobody reviewed | High | Closed |
| [VER-15](#ver-15-the-push-writes-into-the-project-tree) | The push writes into the project tree | High | Mitigated |
| [VER-23](#ver-23-seeding-reads-hostile-input) | Seeding reads hostile input | High | Mitigated |
| [VER-24](#ver-24-a-lookup-reaches-the-live-project) | A lookup reaches the live project | High | Mitigated |
| [VER-29](#ver-29-skipping-seeding) | Skipping seeding | High | Mitigated |
| [VER-31](#ver-31-resolving-through-a-path-the-project-influences) | Resolving through a `PATH` the project influences | High | Mitigated |
| [VER-32](#ver-32-a-host-file-from-the-project) | A host file from the project | High | Closed |
| [VER-37](#ver-37-the-unlocked-directory-setting) | The unlocked directory setting | High | Mitigated |
| [VER-5](#ver-5-branch-names-dont-agree) | Branch names don't agree | Medium | Closed |
| [VER-6](#ver-6-setup-changes-that-dont-ask-for-a-new-review) | Setup changes that don't ask for a new review | Medium | Mitigated |
| [VER-7](#ver-7-a-distribution-inside-a-project) | A distribution inside a project | Medium | Open |
| [VER-8](#ver-8-the-store-itself) | The store itself | Medium | Mitigated |
| [VER-9](#ver-9-trusted-containers) | Trusted containers | Medium | By design |
| [VER-16](#ver-16-the-push-leaks-host-details) | The push leaks host details | Medium | Mitigated |
| [VER-25](#ver-25-rewriting-the-provenance) | Rewriting the provenance | Medium | Mitigated |
| [VER-26](#ver-26-the-reviewer-is-a-setup-program) | The reviewer is a setup program | Medium | By design |
| [VER-27](#ver-27-approval-cant-prove-a-person-reviewed) | Approval can't prove a person reviewed | Medium | By design |
| [VER-28](#ver-28-approving-something-other-than-what-was-shown) | Approving something other than what was shown | Medium | Mitigated |
| [VER-30](#ver-30-the-vcs-handlers-output) | The VCS handler's output | Medium | Mitigated |
| [VER-33](#ver-33-the-unlocked-state) | The unlocked state | Medium | By design |
| [VER-36](#ver-36-a-stale-unlocked-copy) | A stale unlocked copy | Medium | Mitigated |
| [VER-38](#ver-38-removing-the-unlocked-copy) | Removing the unlocked copy | Medium | Mitigated |
| [VER-39](#ver-39-the-first-push-creates-the-projects-surf-scoterd) | The first push creates the project's `.surf-scoter.d` | Medium | Mitigated |
| [VER-40](#ver-40-removing-a-configuration-in-use) | Removing a configuration in use | Medium | Mitigated |
| [VER-41](#ver-41-reviews-of-nothing-new-train-rubber-stamping) | Reviews of nothing new train rubber-stamping | Medium | By design |
| [VER-42](#ver-42-the-host-file-shares-a-name-with-the-projects-files) | The host file shares a name with the project's files | Medium | Closed |
| [VER-10](#ver-10-project-text-printed-outside-the-review) | Project text printed outside the review | Low | Mitigated |
| [VER-11](#ver-11-the-parse-and-the-snapshot-race) | The parse and the snapshot race | Low | Closed |
| [VER-20](#ver-20-trust-doesnt-expire) | Trust doesn't expire | Low | By design |
| [VER-21](#ver-21-pulls-that-never-happen) | Pulls that never happen | Low | By design |
| [VER-22](#ver-22-checks-on-every-run-can-stop-a-run-that-used-to-work) | Checks on every run can stop a run that used to work | Low | By design |
| [VER-34](#ver-34-a-kept-working-directory) | A kept working directory | Low | Mitigated |
| [VER-35](#ver-35-two-pulls-at-once) | Two pulls at once | Low | Mitigated |

The design's candidate IDs VER-12 to VER-14 and VER-17 to VER-19 were taken into the pull's design before it was built, and aren't used.


## Vectors

### VER-1: The Dockerfile meaning match can be fooled

**Severity:** High.  **Status:** Closed.
**Where:** `canonical_dockerfile` and `is_dockerfile` in [`snapshot.py`](../../scripts/surf_scoter_lib/verified/snapshot.py); `file_lines` and `visible` in [`review.py`](../../scripts/surf_scoter_lib/review.py); `from_findings` in [`checks.py`](../../scripts/surf_scoter_lib/checks.py); `docker: runs: build` in [`setup.yaml`](../../defaults/setup.yaml).

Under the acceptances Surf Scoter used before configurations, a Dockerfile that differed from the accepted one only in comments and blank lines matched on meaning, and the run carried the acceptance forward without asking.  The canonical form used to drop more than Docker does (lines split on `\r` and other breaks Docker doesn't use, heredocs it couldn't follow, files named `Dockerfile.*`), so a hidden command could ride along.  It was made to fail closed: a canonical form only for a Dockerfile plainly within what it understands, split on `\n` only.

**Now:** nothing carries on meaning.  A project's change reaches a run only when it's pulled and reviewed, and a pull's "nothing to pull" compares exact bytes.  The canonical forms remain in the snapshot's manifest only.  The review still shows file contents split as Docker splits them, with control characters shown as `\xNN` rather than removed, and the build still names the one `Dockerfile` the checks read (`-f`).

**Red team:** look for anything Docker or BuildKit reads from a Dockerfile that the review doesn't show: control characters, encodings, a `Containerfile` beside it.

**When changing this code:** keep the review splitting lines as Docker does, and showing what it can't show plainly as escaped text.  Don't reintroduce a match on meaning that lets a change skip the review.

### VER-2: Any file named `dev-env.yaml` matches on meaning

**Severity:** High.  **Status:** Closed.
**Where:** `canonical_hash` and `dev_env_paths` in [`snapshot.py`](../../scripts/surf_scoter_lib/verified/snapshot.py).

Every file named `dev-env.yaml` got the merged dev container's meaning hash, so a copy elsewhere in a container directory could change freely and still match.  It was fixed to give that hash only to the files the merge reads, by path; and with no match on meaning any more (VER-1), it no longer reaches a run at all.

**When changing this code:** give a parsed hash only to a file the parse actually read, named by its path, never by its name.

### VER-3: The review doesn't show everything it accepts

**Severity:** High.  **Status:** Mitigated.
**Where:** `build_report` in [`pull/analyze.py`](../../scripts/surf_scoter_lib/pull/analyze.py); [`pull/reviewer.py`](../../scripts/surf_scoter_lib/pull/reviewer.py).

The old review showed some files only by name and size, and none of the dev container's parsed values.  The pull's report and the default reviewer show every file the build uses, in full, with how it differs from the configuration in use; files that can't be shown (over the size limit, or not UTF-8) are refused unless the pull names each one (`--allow-opaque`).  The dev container's values (`user`, `mounts`, `broker-bin`, the sshd settings, the parents) and the project file's `container` settings have their own node.  At approval, the reviewer lists the new, changed or flagged files you never opened.

The checks still don't look at `RUN` lines, `# syntax=` directives or script contents, and a replaced reviewer (VER-26) may show less.

**Red team:** put an unusual (but valid) mount point or `user` in a project, and look for it in the reviewer.  Look for a file the build reads that the tree can't reach.

**When changing this code:** everything the build reads must be reachable in the report.  Validate values written to host files on the host as strictly as ever ([DEV-1](dev-container-attack-vectors.md#dev-1-user-is-written-into-the-ssh-configuration), [DEV-2](dev-container-attack-vectors.md#dev-2-mount-points-are-written-into-the-compose-env-file)), whoever wrote them.

### VER-4: Exemptions accept what nobody reviewed

**Severity:** High.  **Status:** Closed.

Exemptions (`scoter exempt`, `verify --with-exemption`) let runs build from the live container files, and accepted a project's whole suggestion without showing it.  They're gone, with acceptances.  A developer working on a configuration unlocks their own copy, which runs as it is, with a warning, until `scoter lock` reviews it: see [VER-33](#ver-33-the-unlocked-state).

### VER-5: Branch names don't agree

**Severity:** Medium.  **Status:** Closed.

Acceptances were kept per branch, under two different forms of the branch name.  Configurations aren't kept per branch: the branch is a note on a pull, not part of a match.  Switching branches changes the project's files, which the next run reports ([VER-21](#ver-21-pulls-that-never-happen)), but not what runs.

### VER-6: Setup changes that don't ask for a new review

**Severity:** Medium.  **Status:** Mitigated.
**Where:** `prepare` in [`run_config.py`](../../scripts/surf_scoter_lib/run_config.py); `fingerprint` in [`pull/workdir.py`](../../scripts/surf_scoter_lib/pull/workdir.py).

The checks run again on every run, against the setup as it is: a new or stronger `host-commands`, `project-files` or `egress-hosts` check, or a refreshed public suffix list, applies at once ([VER-22](#ver-22-checks-on-every-run-can-stop-a-run-that-used-to-work)).  A pull records a fingerprint of the setup it was made under (the state directory, the store and the checks), so a pull whose files haven't changed still asks for a review when the checks have.

The build, compose and other `docker` commands, and the compose file, aren't part of the fingerprint: a change to them changes how every configuration runs, without a review.  They're your own setup, and the guarded ones are reported by `scoter user-setup`.

**When changing this code:** keep the checks running at every run, on the staged configuration.

### VER-7: A distribution inside a project

**Severity:** Medium.  **Status:** Open.
**Where:** `require_global_dist_dir` and the sub-command dispatch in [`scoter.py`](../../scoter.py); the distribution check in [`system.rs`](../../host/src/config/system.rs); `template_dir` in [`pull/lifecycle.py`](../../scripts/surf_scoter_lib/pull/lifecycle.py).

`scoter` runs `main_<command>.py` from the distribution's `bin` directory.  The generator refuses a `distribution-dir` inside the project when a setup file sets it, but not when it comes from `--distribution-dir` or from where `scoter` itself was found.  Running a `scoter` that lives inside a cloned repository trusts that repository's scripts, including the ones that run the pull.

Templates have the fix this vector proposes: `scoter init` and `scoter unlock --from-template` refuse a templates directory inside the project, its clone or its repository, however it was found.  A template is reviewed by `scoter lock` before it's stored, in any case.

**Red team:** build or copy a distribution inside a project, change a `main_*.py`, and run that `scoter` on the project.

**When changing this code:** apply the same outside-the-project check to the distribution directory however it's found.

### VER-8: The store itself

**Severity:** Medium.  **Status:** Mitigated on POSIX; Unverified on Windows.
**Where:** [`store.py`](../../scripts/surf_scoter_lib/verified/store.py), [`configs.py`](../../scripts/surf_scoter_lib/verified/configs.py); `exclusive_lock` in [`fileutil.py`](../../scripts/surf_scoter_lib/fileutil.py); `check_for_run` in [`host_file.rs`](../../host/src/config/host_file.rs).

The store holds what the run trusts.  Its directories are `0700` and its files `0600`; every read checks the owner and mode, opens without following symlinks, and limits the size.  Writes are atomic, under an operating system lock.  A configuration's blobs are checked against their hashes as they're restored; a link can't name a configuration that isn't stored.  The generator refuses a store or state directory inside the project.  `scoter-host` checks the universal configuration file's owner and mode, and each host file program (see [BRIDGE-14](host-bridge-attack-vectors.md#bridge-14-the-bridge-trusts-the-universal-configuration-files-command-list) for what it doesn't check).  Nothing in the container can reach the store: it isn't mounted, and it isn't under the project clone.

On Windows there are no mode checks; the profile's access control lists are relied on.  `flock` on a network home directory may lock only against this computer.  Any program running as you can still change the store, as it can change anything else of yours.

**Red team:** replace a store file with a symlink, loosen a file's mode, or change a blob's contents, and check that the run refuses.  On Windows, check what another account can reach.  Look for a host command whose arguments let it write into `~/.config/surf-scoter.d`.

**When changing this code:** keep every store read going through the checks, and never mount the store, or put it under the project clone.

### VER-9: Trusted containers

**Severity:** Medium.  **Status:** By design.
**Where:** `scoter: path-lists: dev-containers-search` in [`setup.yaml`](../../defaults/setup.yaml); `containers_search` in [`config.rs`](../../host/src/config.rs).

Containers found in your `dev-containers-search` directories, by default the distribution's, aren't copied into a configuration or reviewed, and are built from where they are.  A project can build on them by name.  Anyone who can change those directories changes every project that uses them.  (The `verified-containers` setting, which reviewed some of them with each acceptance, went with acceptances.)  Templates aren't trusted this way: they're reviewed by `scoter lock`.

**Red team:** with write access to the distribution's `containers` directory, change a container a project builds on, and check what notices.

**When changing this code:** never let `dev-containers-search` name a directory the project controls ([VER-24](#ver-24-a-lookup-reaches-the-live-project)).

### VER-10: Project text printed outside the review

**Severity:** Low.  **Status:** Mitigated.
**Where:** `check_name` in [`snapshot.py`](../../scripts/surf_scoter_lib/verified/snapshot.py); `clean` in [`review.py`](../../scripts/surf_scoter_lib/review.py); the messages of [`main_run.py`](../../scripts/main_run.py), [`run_config.py`](../../scripts/surf_scoter_lib/run_config.py) and [`pull/`](../../scripts/surf_scoter_lib/pull/).

A project file name with terminal escape sequences could reach a message printed as it is.  A snapshot now refuses any name with a control or non-ASCII character, before it opens the file, and the error shows the name escaped; the pull's messages and the run's pass project text through `review.clean()`.

**Red team:** add a file whose name contains escape sequences to a container directory, and look at every message a pull or a run prints about it.

**When changing this code:** pass every project-derived string through `review.clean()` before printing it, and check names before using them in a message.

### VER-11: The parse and the snapshot race

**Severity:** Low.  **Status:** Closed.
**Where:** `seed` and `select_build` in [`pull/seed.py`](../../scripts/surf_scoter_lib/pull/seed.py) and [`snapshot.py`](../../scripts/surf_scoter_lib/verified/snapshot.py); `inspect` in [`pull/analyze.py`](../../scripts/surf_scoter_lib/pull/analyze.py).

The generator used to parse the live files while the snapshot read their bytes later.  Seeding, analysis and approval now snapshot first and run the generator on a staged copy of that snapshot, so what's parsed is what's kept; a run reads only a staged copy.

**When changing this code:** parse only the bytes that are kept.

### VER-15: The push writes into the project tree

**Severity:** High.  **Status:** Mitigated.
**Where:** [`pull/push.py`](../../scripts/surf_scoter_lib/pull/push.py).

A directory under `.surf-scoter.d` may be a symlink an earlier fetch from the clone brought in, so a push that follows it writes outside the project ([FILE-10](files-attack-vectors.md#file-10-the-clones-path-through-container-symlinks)'s pattern on your own repository).  `scoter push` reads the project's files with the snapshot rules (a link or special file stops it), shows the change as a patch, and, once approved, writes relative to a directory descriptor with `O_NOFOLLOW` on each part of the path, replacing only regular files, each only if it still has the hash the patch showed.  It never deletes, and it pushes only a locked configuration.  `--yes` skips the patch, not the checks.  It refuses a project directory that is, or goes through, a symlink, or lies in the tools' own places.  Windows, without directory descriptors, is refused.

**Red team:** plant a symlinked directory in `.surf-scoter.d`, or change a file between the patch and the write, and push.

**When changing this code:** keep every write relative to a descriptor opened without following links, and every replacement checked against the hash shown.

### VER-16: The push leaks host details

**Severity:** Medium.  **Status:** Mitigated.
**Where:** [`pull/push.py`](../../scripts/surf_scoter_lib/pull/push.py); [`host_file.py`](../../scripts/surf_scoter_lib/host_file.py).

A configuration's resolved values (absolute program paths) live only in its host file, which push never writes, so the project file stays portable.  As a backstop, the patch marks lines naming your home directory or host name, and files the `project-files` checks flag, and the warnings print even with `--yes`.  Neither can tell an internal mirror in an egress rule from one the project means to share; that's what the patch's review is for.

### VER-20: Trust doesn't expire

**Severity:** Low.  **Status:** By design.
**Where:** `_timers` in [`run_config.py`](../../scripts/surf_scoter_lib/run_config.py); `verification: values: review-*` in [`setup.yaml`](../../defaults/setup.yaml).

The permissions behind a configuration's cloud services can change while it stays the same.  Your setup chooses from three timers, counted from your last review (`scoter pull` or `scoter review`; not `scoter lock`): a notice (on by default, after 30 days, then every 7), a warning on every run, and an expiry that stops runs.  The warning and the expiry are off by default, as forced reviews of an unchanged configuration train rubber-stamping ([VER-41](#ver-41-reviews-of-nothing-new-train-rubber-stamping)).  Turning the notice off, or lengthening it, is a guarded change.

### VER-21: Pulls that never happen

**Severity:** Low.  **Status:** By design.
**Where:** `_project_changed` in [`run_config.py`](../../scripts/surf_scoter_lib/run_config.py).

A developer who never pulls misses the project's security fixes too.  Each run hashes the project's files, never parsing them, and prints one line when they differ from the last pull (or push).  It never blocks.  Saying more loudly when the project removes something your copy still allows is a later improvement.

### VER-22: Checks on every run can stop a run that used to work

**Severity:** Low.  **Status:** By design.
**Where:** `_refusal` in [`run_config.py`](../../scripts/surf_scoter_lib/run_config.py).

A stronger setup check refuses a run that worked yesterday, which tempts developers to remove the check.  The refusal names the check, the setup file it's in, and how to relax it with an `overrides` check instead.

### VER-23: Seeding reads hostile input

**Severity:** High.  **Status:** Mitigated.
**Where:** `snapshot_source`, `snapshot_bytes`, `check_name` and the reader in [`snapshot.py`](../../scripts/surf_scoter_lib/verified/snapshot.py); `snapshot_commit` in [`vcs_files.py`](../../scripts/surf_scoter_lib/vcs_files.py); [`pull/seed.py`](../../scripts/surf_scoter_lib/pull/seed.py).

Seeding is the only phase that reads the project.  It reads the whole `.surf-scoter.d` within the limits before anything parses it, refusing links, special files, hard links (but for the project file), setuid, setgid and sticky bits, and names with control or non-ASCII characters; from a commit, symlinks and submodules, sizes over the limits from the listing, and contents that don't hash to the listed id.  The generator parses only a staged copy, with its own limits.

**Red team:** a project file with deep YAML, a huge file, odd names, a symlink, a setuid file, a committed submodule.

**When changing this code:** treat seeding as the sensitive code for pulls: nothing parses a byte before the limits and refusals have run.

### VER-24: A lookup reaches the live project

**Severity:** High.  **Status:** Mitigated.
**Where:** `load_with` in [`system.rs`](../../host/src/config/system.rs); `containers_search` and `live_files_problems` in [`config.rs`](../../host/src/config.rs).

A container found by name, or a setting, could lead back into the project's live files.  The generator looks for a container or parent first in the copy it was given (`${scoter:config-dir}`), then only in your `dev-containers-search` directories, and refuses a search entry inside the project directory, its files directory, the repository or the clone, once symlinks are resolved.  With a copy, it refuses an environment value that names the live files directory.

**Red team:** a parent `dir` or `file` reaching back into the project; a search entry through a symlink into it; a `${scoter:project-dir}/.surf-scoter.d/...` value.

### VER-25: Rewriting the provenance

**Severity:** Medium.  **Status:** Mitigated.
**Where:** [`pull/workdir.py`](../../scripts/surf_scoter_lib/pull/workdir.py).

If the VCS report or the comparison with the base were kept in the working directory, an edit could turn "modified" into "committed".  The provenance and each analysis's record are kept beside the working directory, in the state directory, written only by `seed` and `analyze`.

### VER-26: The reviewer is a setup program

**Severity:** Medium.  **Status:** By design.
**Where:** `pull: runs: reviewer` in [`setup.yaml`](../../defaults/setup.yaml); the report's project text in [`pull/analyze.py`](../../scripts/surf_scoter_lib/pull/analyze.py).

A graphical or IDE reviewer that renders project text as HTML or Markdown could run the project's script.  The report gives every project text field with its `clean` form, for a reviewer to show; the setting is guarded, and a reviewer is trusted setup.  The reviewer only decides whether to approve: `approve` checks everything again.

### VER-27: Approval can't prove a person reviewed

**Severity:** Medium.  **Status:** By design.

The default reviewer needs a terminal and has no `--yes`, but a reviewer of your own, or a program that provides a terminal, can approve without anyone looking.  That can't be prevented; the barriers are a warning against automating the review, not a boundary.  `approve` takes only working directories seeding registered.

### VER-28: Approving something other than what was shown

**Severity:** Medium.  **Status:** Mitigated.
**Where:** `approve` in [`pull/approve.py`](../../scripts/surf_scoter_lib/pull/approve.py); `changed` in [`pull/analyze.py`](../../scripts/surf_scoter_lib/pull/analyze.py); [`pull/reviewer.py`](../../scripts/surf_scoter_lib/pull/reviewer.py).

The working directory can change between the analysis on screen and approval (an editor saving a file, a reviewer bug).  Approval takes the analysis id, snapshots the working directory and its host file again, and refuses unless they're exactly what that analysis named.  The default reviewer checks `scoter pull changed` before every prompt, analyzes again when anything changed, and compares findings by id, marking what's new.

### VER-29: Skipping seeding

**Severity:** High.  **Status:** Mitigated.
**Where:** `open_workdir` and `fingerprint` in [`pull/workdir.py`](../../scripts/surf_scoter_lib/pull/workdir.py).

If `analyze` or `approve` took a path, the project's own `.surf-scoter.d` could be approved directly.  They take only ids `seed` registered, found through the SUC, and refuse one whose setup fingerprint (state directory, store, checks) isn't the SUC's, so a reviewer run from a different environment can't read another state directory.

### VER-30: The VCS handler's output

**Severity:** Medium.  **Status:** Mitigated.
**Where:** [`vcs_files.py`](../../scripts/surf_scoter_lib/vcs_files.py), [`vcs_git_files.py`](../../scripts/vcs_git_files.py).

File names with newlines, very large output, or a handler that reads the working tree through the VCS's filters.  The Git handler runs only `rev-parse`, `ls-tree -z` and `cat-file`, with `core.fsmonitor` off and no hooks, and works out blob ids itself.  Every report is cut off at a limit, checked against its schema, and its paths checked by name and shape; a hung handler is killed.

### VER-31: Resolving through a `PATH` the project influences

**Severity:** High.  **Status:** Mitigated.
**Where:** `resolve` in [`host_file.py`](../../scripts/surf_scoter_lib/host_file.py); [`checks.py`](../../scripts/surf_scoter_lib/checks.py).

Tools such as direnv add project directories to the `PATH`; resolving `program: eslint` could find the project's own script.  Every resolution (seeding, approval, unlocked runs) runs the path checks: a program in the project, one others can write, or one a `paths` check matches is found.  A locked configuration's run never resolves: it uses the host file's paths ([BRIDGE-11](host-bridge-attack-vectors.md)).

### VER-32: A host file from the project

**Severity:** High.  **Status:** Closed, by VER-42.

A host file read from the project's tree would let the project choose the programs a run executes.  The host file is never among a configuration's files ([VER-42](#ver-42-the-host-file-shares-a-name-with-the-projects-files)), so a `host.json` the project ships is an ordinary file.

### VER-33: The unlocked state

**Severity:** Medium.  **Status:** By design.
**Where:** [`run_config.py`](../../scripts/surf_scoter_lib/run_config.py); [`pull/lifecycle.py`](../../scripts/surf_scoter_lib/pull/lifecycle.py).

An unlocked copy runs as it is, unreviewed, with its commands resolved as each run starts: an exemption by another name, possibly left unlocked for good.  Each run warns; after `pull: values: unlock-reminder-days` the warning lists what it allows, and a setup can stop runs after `unlock-days`.  `scoter push` refuses an unlocked configuration, and the checks still run.

### VER-34: A kept working directory

**Severity:** Low.  **Status:** Mitigated.

With `--keep-working-dir`, unreviewed project files stay on disk.  Approval checks a kept directory against an analysis like any other, the pull says where it is, and `scoter cleanup` removes one older than a day.

### VER-35: Two pulls at once

**Severity:** Low.  **Status:** Mitigated.

Each pull has its own working directory, and approval, and every change to the link, happens under the store's lock.

### VER-36: A stale unlocked copy

**Severity:** Medium.  **Status:** Mitigated.

Files can reappear in the unlocked directory after a lock (an editor left open, a crash).  `scoter unlock` refuses while it holds anything (`--replace` moves them to a backup first), and a run of a locked configuration warns that those files aren't used.

### VER-37: The unlocked directory setting

**Severity:** High.  **Status:** Mitigated.
**Where:** `check_location` in [`pull/unlocked.py`](../../scripts/surf_scoter_lib/pull/unlocked.py).

Inside a project clone or a state directory, the container could write its own settings for the next run; inside the project, its files would run unreviewed.  `pull: paths: unlocked-dir` is guarded, and refused inside the project, its repository, the clone, the state directory and the store.  The unlocked copy must be yours, with nothing others can write, checked on every use.

### VER-38: Removing the unlocked copy

**Severity:** Medium.  **Status:** Mitigated.

`scoter lock` and `lock --revert` delete a directory you were editing.  It's removed only after the configuration and link are written, without following links, or renamed with `--keep DIR` (refused inside the project and the tools' own places).  A failed removal or rename doesn't fail the lock; what's left is a stale copy (VER-36).

### VER-39: The first push creates the project's `.surf-scoter.d`

**Severity:** Medium.  **Status:** Mitigated.
**Where:** `_first_push` in [`pull/push.py`](../../scripts/surf_scoter_lib/pull/push.py).

Only when nothing at all is there (`lstat`): the files are written into a new directory beside it, and renamed into place after checking, just before, that nothing appeared.

### VER-40: Removing a configuration in use

**Severity:** Medium.  **Status:** Mitigated.
**Where:** `retire` and `remove_retired` in [`configs.py`](../../scripts/surf_scoter_lib/verified/configs.py); `running_config_ids` in [`run_config.py`](../../scripts/surf_scoter_lib/run_config.py).

A configuration replaced, or removed, while an environment runs it is marked, not removed; the next run, `scoter kill` or `scoter cleanup` removes it once nothing runs it.  A running environment keeps the configuration it started with.

### VER-41: Reviews of nothing new train rubber-stamping

**Severity:** Medium.  **Status:** By design.

A developer who sees the same page every month learns to approve it unread.  The review leads with what's new (findings new since the last review, programs resolved again, changed files) and folds what isn't.  Confirming each egress rule and host command, for teams that turn on the expiry, is a later option.

### VER-42: The host file shares a name with the project's files

**Severity:** Medium.  **Status:** Closed.

A project could put a container directory at `.surf-scoter.d/host.json/`.  Every configuration is a `files/` tree and a host file beside it, built by the tools from those two parts; staging restores only `files/`, and push writes only `files/`.


## Sensitive code

* [`scripts/surf_scoter_lib/pull/`](../../scripts/surf_scoter_lib/pull/): seeding, analysis, approval, the reviewer, locking and unlocking, pushing.
* [`scripts/surf_scoter_lib/verified/`](../../scripts/surf_scoter_lib/verified/): the store, snapshots, configurations and links.
* [`scripts/surf_scoter_lib/run_config.py`](../../scripts/surf_scoter_lib/run_config.py) and [`scripts/main_run.py`](../../scripts/main_run.py): what a run runs.
* [`scripts/surf_scoter_lib/host_file.py`](../../scripts/surf_scoter_lib/host_file.py), [`vcs_files.py`](../../scripts/surf_scoter_lib/vcs_files.py), [`review.py`](../../scripts/surf_scoter_lib/review.py) and [`checks.py`](../../scripts/surf_scoter_lib/checks.py).
* [`scoter.py`](../../scoter.py) and [`subcmd-no-project.txt`](../../scripts/subcmd-no-project.txt): how the project is found, without parsing it.
* [`host/src/config/host_file.rs`](../../host/src/config/host_file.rs), [`host/src/config.rs`](../../host/src/config.rs) and [`host/src/universal.rs`](../../host/src/universal.rs): the host file and the copy in the universal configuration file.
* The `verification` and `pull` sections of [`defaults/setup.yaml`](../../defaults/setup.yaml).
