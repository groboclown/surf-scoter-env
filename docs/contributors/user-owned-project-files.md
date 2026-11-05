# User-Owned Project Files

How Surf Scoter handles a project's `.surf-scoter.d`: the developer owns their own copy of the files, and the project's are only a suggestion, brought in by a reviewed pull.  This page is the design, with the reasons and the options weighed for each risk; [project verification](../security/verification.md) is the shorter account, and [verification attack vectors](../security/verification-attack-vectors.md) lists the risks with their current status.

It replaced the acceptance flow of [projects in v1](projects-v1.md), where the developer accepted, denied or edited the project's suggestion, `scoter run` used a snapshot of the project's files that matched the acceptance by bytes or by meaning, and project owners working on their own container used exemptions.  Where this page says "the acceptance flow", it means that design.


## The design

* The developer keeps their own Surf Scoter files for each environment: a project file and container directories, in the same layout as `.surf-scoter.d`.
* While locked, the copy lives in space `scoter` controls (the verified store), where it can't be changed by accident.  `scoter unlock` moves it to a directory the setup names, for editing, and `scoter lock` moves it back after a review, or `scoter lock --revert` throws the edits away (see [locking and unlocking](#locking-and-unlocking)).  `scoter` links each project directory to its copy.
* `scoter unlock --from-template NAME` starts a configuration from a template in the distribution, instead of the project's files.  The developer works on it unlocked, `scoter lock` reviews and stores it, and `scoter push` can offer it to the project.  `scoter init` is its friendly form for new users, for a project with no `.surf-scoter.d` ([starting from a template](#starting-from-a-template)).
* `scoter pull` brings in the project's suggested `.surf-scoter.d`, and later changes to it.
* `scoter push` offers the developer's copy back to the project, for project owners.
* `scoter run` builds and runs only from the developer's copy.  It trusts the copy the way it trusts the developer's setup files, but runs the refusal checks again on every run, and runs host commands only from the resolved paths in its [host file](#the-host-file).

**Why:** a project's files never decide what runs.  Only the developer's own files do, and a project's suggestion reaches them only through a pull the developer asks for.


## What moves where

The acceptance flow crossed the boundary on every run: each run read the live project files and asked whether an acceptance still covered them.  With user-owned files, it's crossed on every *pull*.  `scoter run` never reads the project's `.surf-scoter.d`, except to say that it has changed since the last pull (below).

So the review's weak points become the pull's weak points, and the per-run matching that guarded against project changes goes away.


## Vectors this closes or shrinks

* [VER-1](../security/verification-attack-vectors.md#ver-1-the-dockerfile-meaning-match-can-be-fooled) and [VER-2](../security/verification-attack-vectors.md#ver-2-any-file-named-dev-envyaml-matches-on-meaning): no acceptance is carried forward on meaning, because a project change doesn't reach a run until it's pulled.  The canonical forms are needed, if at all, only to make pull diffs quieter.
* [VER-4](../security/verification-attack-vectors.md#ver-4-exemptions-accept-what-nobody-reviewed): exemptions go away.  A project owner edits their own copy, which runs as it is, and pushes it when it's ready.
* [VER-5](../security/verification-attack-vectors.md#ver-5-branch-names-dont-agree): switching branches no longer changes what runs.  The branch is a note on a pull, not part of a match.
* [VER-6](../security/verification-attack-vectors.md#ver-6-setup-changes-that-dont-ask-for-a-new-review): improves.  The checks run on every run, so a new or stronger check applies at once.
* [VER-11](../security/verification-attack-vectors.md#ver-11-the-parse-and-the-snapshot-race): the race against the project's files is gone.  A milder one remains against the developer's own directory, which the staged copy covers.
* [FILE-5](../security/files-attack-vectors.md#file-5-changes-brought-back-to-the-project): a change to `.surf-scoter.d` brought back from the container no longer affects the next run unless the developer pulls it.
* [NET-2](../security/network-attack-vectors.md#net-2-the-project-controls-the-allowlist), [BRIDGE-2](../security/host-bridge-attack-vectors.md#bridge-2-the-project-chooses-the-commands) and [DEV-3](../security/dev-container-attack-vectors.md#dev-3-image-builds-run-outside-the-isolation): these now depend on the pull's review, not the run's.
* **New:** a developer can start from a template with `scoter init` or `scoter unlock --from-template`, which `scoter lock` reviews like any edit, and which the developer can push to the project.


## How to read the risks

The rest of the page lists the risks this approach keeps or adds.  Each has:

* **Risk:** what goes wrong, with a severity estimate on the [attack vector scale](../security/attack-vectors.md#severity).
* **Options:** the ways to deal with it, each with what it protects, what it costs and what it leaves open.  "In every case" marks rules that apply whichever option is chosen.
* **Decided:** the option taken, and why; the options stay for the record.  A risk another decision deals with entirely is marked **Mitigated**, with a link to that decision.

The new risks are numbered `VER-12` onwards, and [verification attack vectors](../security/verification-attack-vectors.md) lists them with their status.  The [pull design](#designing-the-pull) took in several of them while it was designed, as well as the existing VER-3 and VER-10; their IDs are left unused, so references to them in earlier discussions stay unambiguous.


## Vectors that carry over

VER-3 (showing everything) and VER-10 (cleaning project text) carry over to the pull, and the [pull design](#designing-the-pull) deals with them in its [analysis](#analysis) and [review](#review).  A pull that approves without a person is covered by [review](#review) and [VER-27](#ver-27-approval-cant-prove-a-person-reviewed).  Templates are no longer trusted the way [VER-9](../security/verification-attack-vectors.md#ver-9-trusted-containers)'s containers are: `scoter init` and `scoter unlock --from-template` write one into the unlocked copy, and `scoter lock` reviews it like any edit ([starting from a template](#starting-from-a-template)).  A template upgrade reaches a configuration only when the developer starts from the template again, or edits the files.  The rest are below.

### VER-7: templates from a distribution inside the project

**Risk** (Low): [VER-7](../security/verification-attack-vectors.md#ver-7-a-distribution-inside-a-project) now covers templates too.  `scoter init` or `scoter unlock --from-template` run with a distribution inside a project could take that project's templates.  As a template reaches a run only through `scoter lock`'s review, a project-chosen template is no worse than files the project ships itself.  What's left is a reviewer trusting "it came from the template" more than it deserves.

**Options:**

* **A.** Refuse a template directory that resolves inside the project, its clone or its repository, however the distribution was found.
* **B.** Rely on `scoter lock`'s review alone.

**Decided:** A.  It's VER-7's own proposed fix, applied to one more directory, and it costs little.  The review in `scoter lock` doesn't treat template files differently from other files either way.

### DEV-1, DEV-2 and DEV-15

**Risk** (Critical, High, Medium): [DEV-1](../security/dev-container-attack-vectors.md#dev-1-user-is-written-into-the-ssh-configuration) and [DEV-2](../security/dev-container-attack-vectors.md#dev-2-mount-points-are-written-into-the-compose-env-file) values still reach host files, and [DEV-15](../security/dev-container-attack-vectors.md#dev-15-the-project-chooses-shared-image-tags) tags still name shared images.  A pulled value is the project's, whatever directory it sits in.

**Decided:** no change.  The generator keeps validating these values strictly, whatever their source.  Don't relax a host-file injection check because a file is "the developer's".


## New risks

The merge (VER-12), the pull writing files (VER-13), the pull's source (VER-14), resolving host paths (VER-17), the mapping (VER-18) and the developer's directory (VER-19) are now part of the [pull design](#designing-the-pull): its [seeding](#seeding), [VCS reports](#vcs-reports), [approval](#approval), [host file](#the-host-file) and [locking and unlocking](#locking-and-unlocking), and the new vectors that come with them ([VER-23](#ver-23-seeding-reads-hostile-input) onwards).  The rest are below.

### VER-15: the push writes into the project tree

**Risk** (High): a directory under `.surf-scoter.d` may be a symlink that an earlier fetch from the clone brought in, so a push that follows it writes, or deletes, outside the project.  This is the [FILE-10](../security/files-attack-vectors.md#file-10-the-clones-path-through-container-symlinks) pattern on the developer's own repository.

**Options:**

* **A.** Write with `openat` relative to a directory descriptor, with `O_NOFOLLOW` on each part of the path.  Refuse to replace anything but a regular file or a directory.  Never delete; list the files the owner should remove.
* **B.** Don't write into the project.  The push makes a patch (in `git diff` format) for the owner to apply with their VCS, which shows it to them before applying.
* **C.** Write a new sibling directory and swap it in with a rename.  Deletes come for free, but it replaces a directory the VCS tracks, and any untracked files in it.

**In every case:** refuse a target `.surf-scoter.d` that's a symlink, or that resolves into the clone, the state directories or the verified store.

**Decided:** B's format for the approval, and A for the write.  `scoter push` shows the change as a patch, in `git diff` format, and asks the developer to approve it.  Once approved, it writes the files itself, as A describes.  `--yes` approves without asking.

**Rules:**

* Push reads from a locked configuration in the store, never from the unlocked copy, so what it writes has already been through a review.
* The patch compares the configuration with the project's files as they are on disk, read with the snapshot rules (`O_NOFOLLOW`, no links or special files, the size limits).  Those files are project text, so the patch is shown through `review.visible`, as the reviewer shows files.
* The patch includes the project's own uncommitted changes to `.surf-scoter.d` that the push would overwrite, and says so; the developer may not have meant to lose them.
* **The write checks what was shown.**  Just before replacing each file, push checks that it still has the hash the patch was made from, and refuses that file otherwise.  A change between approving and writing, by an editor or a fetch, isn't overwritten unseen.
* It never deletes.  Files in the project that the configuration doesn't have are listed in the patch, and left for the developer to remove with their VCS.
* The host file is never written ([VER-16](#ver-16-the-push-leaks-host-details)).
* After writing, push records the configuration it pushed as the link's base, so [VER-21](#ver-21-pulls-that-never-happen)'s notice doesn't report the developer's own push as a change in the project.  Files the push didn't write (refused on a changed hash, or extra files it left in place) still differ from the base, so the notice still reports them.

**Why `--yes` is acceptable here, when a pull has none:** push only writes content the developer already reviewed and locked, into their own project, where their VCS's review and commit is the next control (FILE-5).  The patch protects against surprises in the project, not against the configuration.  What `--yes` skips is a look at what the push overwrites; the hash checks and the write rules still apply.

When the project has no `.surf-scoter.d` yet, as after [starting from a template](#starting-from-a-template), the push creates it; see [VER-39](#ver-39-the-first-push-creates-the-projects-surf-scoterd).

**In the code:** [`pull/push.py`](../../scripts/surf_scoter_lib/pull/push.py) and [`main_push.py`](../../scripts/main_push.py).  Windows, which has no directory descriptors, is refused.

### VER-16: the push leaks host details

**Risk** (Medium): the developer's copy may hold absolute program paths, home directory paths, internal mirrors and hosts, or values frozen from `${section:key}` references.  A push commits them to the project.

**Options:**

* **A.** Keep only the portable form in the project file: `program` is a name, and references stay unresolved.  The resolved values live only in the [host file](#the-host-file), which push never sends, so a push has nothing resolved to leak.
* **B.** Have the push look for the home directory, the host name and the `secret-files` names, and warn.
* **C.** A `push-exclude` list (default `*.local.*`) for files the developer keeps private.

**Decided:** A, which fixes it structurally, with B as a backstop.  B's warnings appear beside the lines they're about in the patch [VER-15](#ver-15-the-push-writes-into-the-project-tree) shows for approval, and are printed even with `--yes`.  Neither can tell an internal mirror in an egress rule from one the project means to share; that's what the patch's review is for.  C waits until developers ask for it.

### VER-39: the first push creates the project's `.surf-scoter.d`

**Risk** (Medium): after `scoter init` ([starting from a template](#starting-from-a-template)), the project has no `.surf-scoter.d`, and the first `scoter push` creates it.  (This was `scoter init`'s own risk while it wrote into the project; `init` now writes only the unlocked copy.)  A path through a symlink, such as a `.surf-scoter.d` link brought back from the clone ([FILE-10](../security/files-attack-vectors.md#file-10-the-clones-path-through-container-symlinks)'s pattern), could send the writes outside the project.  A `.surf-scoter.d` that appears between the push's check and its write could be overwritten, or mixed with the configuration into something nobody wrote.

**Options:**

* **A.** Create only when `.surf-scoter.d` doesn't exist at all, not even empty; otherwise it's [VER-15](#ver-15-the-push-writes-into-the-project-tree)'s write into an existing directory.  Build the configuration's files in a new directory (`mkdtemp`) beside it, inside the project directory, then rename it to `.surf-scoter.d`.  A new directory replaces nothing the VCS tracks.
* **B.** Write file by file, with `openat` and `O_NOFOLLOW` on each part of the path, merging into an existing directory where names don't clash.
* **C.** Make a patch for the developer to apply with their VCS.

**In every case:**

* Refuse a project directory that is, or resolves through, a symlink, or that lies inside the clone, a state directory or the store.
* The files are written without links or special files, keeping only the owner-execute bit.
* The patch shows every file as new, and needs approval like any push.
* Only the configuration's `files/` tree is written, never its host file ([VER-42](#ver-42-the-host-file-shares-a-name-with-the-projects-files)).

**Decided:** A.  It's simple and it never touches existing files.  On POSIX, a rename onto an existing *empty* directory succeeds, so check that `.surf-scoter.d` doesn't exist just before the rename; the project directory isn't writable by the container, so only the developer's own programs could race it.  C is awkward for a directory that doesn't exist yet.

### VER-20: trust doesn't expire

**Risk** (Low): an acceptance expires because the permissions behind a project's cloud services can change while it stays the same.  That still happens, but a developer's own file has no expiry.

**Options considered:**

* **A.** A reminder after a number of days, on every run.
* **B.** A refusal after a number of days, until the developer reviews.
* **C.** No expiry, like setup files.

**Decided:** the developer's setup chooses, to suit what their team demands, from three timers that can each be on or off.  All three count from `last-reviewed` in the environment's link.

1. **Expiry.**  `verification: values: review-expire-days`.  Once that many days pass, `scoter run` refuses until the developer reviews.  An approved review moves the expiry ahead by the same number of days.
2. **Warning.**  `verification: values: review-warn-days`.  Once that many days pass, every run warns that the configuration is due for review.  A review moves the warning ahead by the same number of days.
3. **Notice.**  `verification: values: review-notice-first-days` and `review-notice-every-days`.  Once the first number of days pass, the next run prints a notice that the settings haven't been reviewed, and so on once in each later interval: at the first run on or after each due day, not on every run.  A review starts the first timer again.

**Defaults:** the notice is on (first after 30 days, then every 7).  The expiry and the warning are off, as both lead to [rubber-stamping](#ver-41-reviews-of-nothing-new-train-rubber-stamping): a review forced, or nagged for on every run, of a configuration that hasn't changed teaches people to approve it unread.  A team that needs a hard limit turns on the expiry, knowing that.

**Rules:**

* **What counts as a review.**  An approval from [`scoter review`](#reviewing-again-scoter-review), which shows the whole configuration, or from `scoter pull`, which shows everything the project supplies, resets the timers.  `scoter lock` doesn't: it shows only what changed since the configuration was unlocked.  A denied review resets nothing.
* **Expiry** never stops an environment that's already running; it stops the next `scoter run`.  The refusal says how long ago the configuration was reviewed, and to run `scoter review`.
* The notice and the warning list the egress rules and host commands, so even a developer who never reviews sees what they've allowed.
* Turning off the notice, or lengthening any timer beyond the distribution's value, is a [guarded](../system-config-file.md#guarded-settings) weakening that `scoter user-setup` reports.  Turning on the expiry or the warning isn't.

### VER-21: pulls that never happen

**Risk** (Low): a developer who never pulls also misses the project's security fixes: an egress rule removed, a base image pinned, a command narrowed.

**Options:**

* **A.** On each run, snapshot the project's `.surf-scoter.d` (hashing only, never parsing it), compare it with the link's base, and print one cleaned line when they differ.  It never blocks, and it's the only time a run reads the project's files.
* **B.** A, plus run the checks on the project's suggestion, and say more loudly when it *removes* something the developer's copy still has.

**Decided:** A first.  B is a later improvement.

**Branches.**  An environment is keyed by the project directory, not the branch, so all branches share its configurations.  Switching to a branch whose `.surf-scoter.d` differs from the base changes the snapshot, and the next run prints A's notice, like any other change.  A more nuanced approach is future work.  One idea, not a good one but a start: the developer locks a configuration under a name for each branch, and a run compares the project's files with the named configurations' bases to find the one that matches.

### VER-22: checks on every run can stop a run that used to work

**Risk** (Low): running the checks on every run means a stronger setup check refuses a run that worked yesterday, which tempts developers to remove the check.

**Options:**

* **A.** Refuse, and say which check, from which setup file, refused it, and how to relax it with [`overrides`](projects-v1.md#relaxing-a-check-without-copying-it).
* **B.** Warn on a run, and refuse only at the next pull or `scoter lock`.

**Decided:** A.  B lets a command known to be refused keep running.


## Designing the pull

This section designs `scoter pull` from a project.  There's no pull from a template: a template starts an unlocked copy, which `scoter lock` reviews ([starting from a template](#starting-from-a-template)).  It deals with VER-3 and VER-10, the risks VER-12, VER-13, VER-14, VER-17, VER-18 and VER-19 from earlier versions of this page, and [BRIDGE-11](../security/host-bridge-attack-vectors.md#bridge-11-the-hosts-path-lookup).

A pull has four phases, in this order:

1. **Seeding** copies the files that matter out of the source into a private working directory, and records where they came from.
2. **Analysis** annotates the working directory's files, and writes a JSON report.
3. **Review** shows the report to the developer, who can edit the working directory.
4. **Approval** stores the working directory as a locked configuration, and points the project's link at it.

Seeding is the only phase that reads the project.  Everything after it works on the copy, so nothing has to check the project again, and a change to the project during a pull can't slip in.

### The project's `.surf-scoter.d`: found, then copied

The project's `.surf-scoter.d` has exactly two uses:

1. **Discovery.**  `scoter` finds where it is, to know where the project is, and so seeding knows what to copy into the developer's space.
2. **Nothing else reads it as configuration.**  Every reference to a project file or container directory points within a copy: the developer's configuration, locked or unlocked, as staged for the command, or, while seeding, the staged copy of the snapshot that's about to become one.  A parent container found by name, a `dir` or `file` parent, the dev container and the build context are looked for only in that copy or in the developer's own `dev-containers-search` list.  The rule holds inside every copy, so seeding's generator run, the analysis, a run and `scoter lock` all resolve the same way, and none of them can step out of the copy into the live project.

The live directory is otherwise touched only by commands that move files across the boundary on purpose, each with its own rules: seeding copies out of it, `scoter push` writes into it ([VER-15](#ver-15-the-push-writes-into-the-project-tree), and [VER-39](#ver-39-the-first-push-creates-the-projects-surf-scoterd) when it creates it), and `scoter run` hashes it, never parsing it, for [VER-21](#ver-21-pulls-that-never-happen)'s one-line notice.

The acceptance flow assumed the opposite in several places, which changed:

* **`${scoter:project-config}`** named `project-file-name` inside the live `project-dir/project-files-dir`.  It's the project file in the copy now.  A tool-set location, `${scoter:config-dir}`, names the copy's `files/` root as staged for the command ([VER-42](#ver-42-the-host-file-shares-a-name-with-the-projects-files)), and `gen-scoter-uc` is given it (`--config-dir`).  `project-files-dir` stays, as the name discovery looks for.  No tool-set location names the live directory, so no setting can point a lookup at it by reference.
* **`dev-containers-search`'s default** started with `${scoter:project-dir}/${scoter:project-files-dir}`.  It's dropped: the copy is always searched first, by the generator itself rather than through the list, and the list holds only the developer's own directories (the distribution's `containers` by default).  An entry that still reaches into the project is refused ([VER-24](#ver-24-a-lookup-reaches-the-live-project)).
* **The confinement rule** (a container and its parents must resolve inside `.surf-scoter.d` or a search directory, in [the project configuration file](../project-configuration-file.md#the-development-container) and [step 5](projects-v1.md)) is: inside the copy, or a search directory.  `dev-container-path` in the project file is relative to the copy.
* **Staging** used to make the staged copy *stand in* for the project files directory, and check that it parsed to the same settings as the live files.  With nothing live to compare against, there's no substitution and no comparison: the copy simply is the files directory.
* **Paths in the files themselves**: an environment value naming the live `.surf-scoter.d` is refused when the generator reads a copy.
* **Finding the project** needs `project-files-dir` holding `project-file-name` in the project directory, for a project that supplies its files.  A project [started from a template](#starting-from-a-template) has a link in the store but nothing on disk until the first push, so discovery has a second signal: a link for the directory's environment id.
* **The review and its messages** show paths relative to the copy, and say "your configuration", not "the project's", for what's in it.

**In the code:** [`scoter.py`](../../scoter.py) finds the project for every command but those in [`subcmd-no-project.txt`](../../scripts/subcmd-no-project.txt), and records it in the SUC's `found-project`, with how it was found (`found-by`: its `files`, its `link` in the store, or only the `start` of the search, which `init` and `unlock --from-template` take), without running the generator on the project.  The sub-commands run the generator themselves, on a copy: `--project-dir` the found project, `--config-dir` the copy.  `containers_search` in [`config.rs`](../../host/src/config.rs) puts the config directory ahead of the search list; `system::load_with` in [`system.rs`](../../host/src/config/system.rs) refuses a search entry inside the project directory, its files directory, the VCS root or the clone, once symlinks are resolved; `live_files_problems` in `config.rs` refuses an `env` value naming the live files directory.  `checks.check_project_files` reads the files from `config-dir`.

### Commands

Each phase is a plumbing command, and `scoter pull` is the porcelain that runs them:

* `scoter pull seed SOURCE` makes a working directory, and prints its id.
* `scoter pull analyze WORKDIR-ID` writes the analysis report.  It changes nothing.
* The **reviewer** is a program the setup names, `pull: runs: reviewer`.  The default is the built-in terminal explorer; a developer can name a graphical or IDE reviewer instead, built on the report.  The reviewer runs `analyze` again as the developer edits.
* `scoter pull changed WORKDIR-ID ANALYSIS-ID` lists the working directory's files that were added, removed or changed since an analysis, without analyzing them ([VER-28](#ver-28-approving-something-other-than-what-was-shown)).  It changes nothing.
* `scoter pull approve WORKDIR-ID ANALYSIS-ID` stores the working directory as a locked configuration.

`scoter pull` seeds, stops with "nothing to pull" when the source is unchanged since the last pull, analyzes, runs the reviewer, approves when the reviewer reports that the developer approved, and removes the working directory.

Three porcelain commands run the same phases, and differ only in where seeding reads from:

| Command | Seeds from | Approval |
| --- | --- | --- |
| `scoter pull` | the project's `.surf-scoter.d` | a new locked configuration, which the link then points at |
| `scoter lock` | the unlocked copy | a new locked configuration; the unlocked copy is removed |
| `scoter review` | the locked configuration in use | a new locked configuration; the one reviewed is removed |

Only `scoter pull` skips the review when nothing changed; the other two are asked for because the developer wants to look.

The reviewer could be the orchestrating command instead.  Keeping orchestration in `scoter pull` means a replaced reviewer only decides *whether* to approve: it can't skip seeding, and it can't approve without approval's checks.

**Rules:**

* `pull: runs: reviewer` is a [guarded setting](../system-config-file.md#guarded-settings).  The reviewer is part of the developer's setup, trusted like any other setup program.
* `analyze` and `approve` take only ids of working directories that `seed` registered.  They never take a path, so nothing can be analyzed or approved without being seeded first.

**Finding the project without reading it.**  `scoter` normally runs the generator on the project it finds, which parses the live project file.  Every command but `cleanup` and `list` is in the found scope ([`subcmd-no-project.txt`](../../scripts/subcmd-no-project.txt) lists those two): `scoter` finds the project, its repository and its branch, records them in the SUC's `found-project`, and stops there.  The phases run the generator only on copies, with `--project-dir` the found project and `--config-dir` the copy.

**How the commands call each other.**  `scoter.py` builds the system universal configuration file (the SUC) for a sub-command, saves it to a private temporary file, and runs `main_NAME.py` with it through the setup's `python: runs: sub-command`.  The porcelains (`pull`, `lock`, `review`) then have to run the phases, and the reviewer has to call `analyze` and `changed` back.  Two ways to do that:

* **A. Sub-sub-commands.**  The phases are `scoter pull seed`, `analyze`, `changed` and `approve`, all in `main_pull.py`, with a fixed command line that no setting can change.  A new library function lets one sub-command run another the way `scoter.py` does: the same interpreter command, the SUC passed through to a fresh private temporary file and removed afterwards, the same exit codes.  It hands on the SUC the parent was given rather than building a new one, so a porcelain and its phases always agree on where the state directory and the store are.
* **B. Setup commands.**  Each phase is a `pull: runs:` command in the setup, like the reviewer, which the porcelain runs with the SUC.  It's simpler to wire, and anyone could swap a phase.

**Decided:** A.  The phases are where the boundary is enforced: seeding's defensive reads, the binding of approval to an analysis, and the refusal checks.  Under B a setup could replace any of them, guarded or not, and the "a replaced reviewer can't skip a phase" argument above would no longer hold.  Only the reviewer, which decides *whether* to approve and never *what*, is a setup command.  `pull`, `lock` and `review` are in the found scope, with every phase of a command in the same one.

**In the code:** [`main_pull.py`](../../scripts/main_pull.py) is the porcelain and the four phases (`seed [--from project|unlocked|locked] [--ref REF | --working-tree]`, `analyze`, `changed`, `approve [--name NAME]`), each printing JSON.  [`subcommand.py`](../../scripts/surf_scoter_lib/subcommand.py) runs one sub-command from another, handing on the parent's SUC through a private temporary file; the porcelains ([`pull/porcelain.py`](../../scripts/surf_scoter_lib/pull/porcelain.py), and `lock` and `review` in [`pull/lifecycle.py`](../../scripts/surf_scoter_lib/pull/lifecycle.py)) run the phases with it.  `scoter pull` refuses up front while the configuration is unlocked.

### Seeding

Seeding reads the source, which is the project, or the unlocked copy for `scoter lock` (below).

**Which version of the project.**  The project's working tree may hold changes to `.surf-scoter.d` fetched from the project clone and not yet reviewed ([FILE-5](../security/files-attack-vectors.md#file-5-changes-brought-back-to-the-project)).  So by default a pull reads the files as committed at `HEAD` (or `--ref`), through the VCS's `show-file` action, run in the developer's repository and never the clone ([FILE-1](../security/files-attack-vectors.md#file-1-scoter-run-runs-git-inside-the-project-clone)).  `--working-tree` reads the files on disk instead, for project owners trying out uncommitted changes, and the review flags every file that differs from the commit.  The steps below are the same either way; from a commit, the bytes come from `show-file` rather than the file system.

1. **Snapshot the source's `.surf-scoter.d`** with `snapshot_source`, or `snapshot_bytes` for files from a commit ([`snapshot.py`](../../scripts/surf_scoter_lib/verified/snapshot.py)): every file opened with `O_NOFOLLOW` and checked with `fstat`, links and special files refused, the size limits applied, and only the owner-execute bit kept (setuid, setgid and sticky bits are refused).  Every path passes the store's manifest path check (`check_relative`), and file names with control characters or non-ASCII characters are refused, so they never reach a terminal, a report or a directory the developer opens (VER-10).  The bytes go into the store's blobs.
2. **Find what the build uses from the snapshot, not the project.**  Restore the snapshot into a private staging directory, run the generator on that copy, and take the project file and the container directories it references (`select_build`).  The selection then comes from the same bytes that are copied, which avoids the [VER-11](../security/verification-attack-vectors.md#ver-11-the-parse-and-the-snapshot-race) race.  The staged copy is the generator's `${scoter:config-dir}`, so a parent found by name is looked for in the copy first and then in the developer's `dev-containers-search` list, never in the live project ([found, then copied](#the-projects-surf-scoterd-found-then-copied)).  Containers found in the developer's list are trusted, and not copied ([VER-9](../security/verification-attack-vectors.md#ver-9-trusted-containers)).
3. **Restore only those files** into `files/` in a new directory made with `mkdtemp` under `${scoter:state-dir}/pull/`, mode `0700`.  It's never inside the project, its clone, the workspace or the verified store.
4. **Tool configuration** (`.vscode/`, `.idea/`, `.envrc`, `.exrc`, `.tool-versions` and the like, from the `pull-files` checks): refused outside container directories, and copied but flagged inside them, where they're build input.  Seeding says to open the working directory in the IDE's restricted mode ([FILE-4](../security/files-attack-vectors.md#file-4-host-tools-acting-on-the-project-clone)).
5. **Ask the VCS about the copied files**, at the same moment (see [VCS reports](#vcs-reports)).
6. **Resolve the host commands**, and write the [host file](#the-host-file) at the top of the working directory, beside `files/`.
7. **Record the provenance** in the state directory, beside the working directory but outside it: the source, the snapshot id, the VCS report, how the snapshot compares with the last pull, the seeding findings and the time.  Nothing in the working directory says where it came from, because the developer and the reviewer can change anything there.

**No change, no review.**  When the snapshot's id equals the base (the last pulled snapshot) exactly, byte for byte, and the setup's checks are unchanged, there's nothing to pull and the pull stops.  This never matches on meaning ([VER-1](../security/verification-attack-vectors.md#ver-1-the-dockerfile-meaning-match-can-be-fooled)).

**Lifetime.**  The working directory lasts as long as `scoter pull`, and is removed however it ends, including errors and Ctrl-C.  `--keep-working-dir` keeps it, for debugging, and says where it is.  `scoter cleanup` removes any left by a crash.

**In the code:** [`pull/seed.py`](../../scripts/surf_scoter_lib/pull/seed.py).  The source is read by `snapshot_source` (the working tree), `vcs_files.snapshot_commit` (a commit, through `snapshot_bytes`), [`pull/unlocked.py`](../../scripts/surf_scoter_lib/pull/unlocked.py) (the unlocked copy, for `lock`) or `snapshot.from_store` (the configuration in use, for `review`); `select_build` keeps the build's files.  [`pull/workdir.py`](../../scripts/surf_scoter_lib/pull/workdir.py) keeps the working directories and their records, and the SUC fingerprint ([VER-29](#ver-29-skipping-seeding)).  Seeding's tool configuration rule (step 4) has no `pull-files` checks to read yet: only the build's files are copied, so tool configuration outside the container directories never reaches the working directory, and the setup's `project-files` checks flag what's inside them.

### VCS reports

Knowing whether a file is committed needs the VCS, and each VCS answers differently.  So each VCS gets a handler that answers in one format.

* A new guarded VCS action, `NAME-vcs: runs: file-status`, given `{x:root}`, `{x:ref}` (default `HEAD`) and the paths.  It prints a fixed JSON report:
    * the commit it compared with;
    * for each path, `committed`, `modified`, `untracked`, `ignored` or `unknown`, with the committed version's id.
* Pulling from a commit, the default (see [seeding](#seeding)), needs two more actions: `list-files`, which resolves the ref to one commit and lists the files under a directory at it, and `show-file`, which prints a committed file's contents at that commit.
* Both join the [VCS checklist](../security/files.md#adding-a-vcs-implementation).

**Rules for handlers:**

* Run in the developer's repository (`${scoter:vcs-root}`), never in the clone ([FILE-1](../security/files-attack-vectors.md#file-1-scoter-run-runs-git-inside-the-project-clone)).
* Never read the working tree through the VCS.  `git status` and `git diff` run `core.fsmonitor` and the clean filters `.gitattributes` names.  Git's handler works out each copied file's blob id itself (`blob <size>\0` and the bytes, hashed) and compares it with `git ls-tree -z -r HEAD`, which reads only the repository's objects.
* NUL-separated output where the VCS offers it, as file names may contain newlines.
* `scoter` checks the report against its schema and a size limit, and checks every path with `check_relative`.
* A VCS without a handler, such as `file`, reports `unknown` for every file.

The analysis turns the report into recommendations: with `--working-tree`, commit or review a modified file before pulling it, or pull from a commit instead.

**No report of where a change came from.**  The report says only whether a file is committed, not who wrote it.  A hint such as "this change was fetched from the project clone" would make an agent's edit to its own `.surf-scoter.d` stand out ([FILE-5](../security/files-attack-vectors.md#file-5-changes-brought-back-to-the-project)), but nothing depends on it: the review shows every change since the base in full, and treats it as the project's, whoever wrote it.  Asking each VCS for its history adds a handler action per VCS for a best-effort signal, so the report leaves it out.  Two ways to get the signal back later, neither through the VCS report:

* compare file contents at the boundary: record the `.surf-scoter.d` hashes a run gives the clone, snapshot the clone's at `scoter kill --fetch` (defensively, never running a VCS in it), and flag a seeded file whose content matches one the container changed;
* flag it at fetch time, when `scoter fetch` is designed: report whether the fetched change touches `.surf-scoter.d`, as FILE-5 already suggests.

**In the code:** three actions, not two: `list-files` resolves the ref once and lists the files under a directory at that commit, with each one's kind, id and size; `show-file` takes that commit id, never a ref, so every file comes from one commit; `file-status` is as above.  [`vcs_files.py`](../../scripts/surf_scoter_lib/vcs_files.py) runs them, with a size limit, a timer, a schema check and the name and path checks; [`vcs_git_files.py`](../../scripts/vcs_git_files.py) is Git's handler.  It never reports `ignored`, which would need the working tree's ignore files.

### Analysis

`analyze` reads a registered working directory and its provenance, and changes nothing.

* Paths are confined to the working directory: a parent that reaches outside it, such as back into the project, is refused, as are links.
* It snapshots the working directory.  The analysis id is tied to that snapshot's id, so it names exactly what was analyzed.
* It runs the [checks](projects-v1.md#4-add-programs-and-files-to-prohibit-or-warn-to-the-default-settings) on the project file, the dev container values and the files.

**The report** (`$schema: surf-scoter-pull-analysis-1`) is for the reviewer, and for anyone building a graphical or IDE reviewer:

* `root`: the overall summary, and counts of each kind of finding;
* `containers[]`: each container directory, its parents and its summary;
* `files[]`: each file's path, SHA-256, size and execute bit; how it compares with the developer's current copy and with the base; the VCS report's entry; its annotations; and its summary;
* `host-commands[]`: each command's resolved path, where the path came from (below), and its findings.  A command the developer added while editing shows as unresolved, with the path it would resolve to;
* `recommendations[]`.

The report has a size limit, and a limit on its number of entries.

**Findings have ids.**  Every finding, wherever it appears in the report, carries an `id` that stays the same while the issue does, so a reviewer can tell a new issue from one the developer has already seen:

* The id is a hash of what the finding is about, not where: the check that raised it, its severity, the kind of subject (a file, a host command, an egress rule, a dev container value), the subject's key (a path or a name), and the value that triggered it (the program name, the host, the base image).  Line numbers and message text are left out, so an edit elsewhere in a file doesn't change it.  Identical findings on the same subject are numbered in order.
* A change that matters gives a new id: the same check on a different host, or a warning that a setup change made a refusal.

**Each analysis stands alone.**  `analyze` knows nothing of earlier analyses: it reads the working directory and its provenance, and writes one report.  It records only its id and the snapshot it names in the state directory (outside the working directory, like the provenance), so `changed` and `approve` can check against it.  Comparing one analysis with another is left to the reviewer ([VER-28](#ver-28-approving-something-other-than-what-was-shown)), so `analyze` stays a plain function of the files.

**Rules:**

* Text from the project appears only in fields marked as project text, each with a `clean` form made by `review.clean()`.  Annotations and summaries are generated text only, so a project can't write an annotation such as "reviewed by security: safe".  This is the `#scoter:` rule of the [review file](projects-v1.md#design-data-is-whats-accepted-everything-else-is-a-comment), in JSON.
* The report is written with `ensure_ascii`, so no raw control characters or look-alike characters reach a reviewer that forgets to clean them.
* **The report is output, not authority.**  `approve` never reads a report.  It takes ids, and reads the store and the state directory.

**In the code:** [`pull/analyze.py`](../../scripts/surf_scoter_lib/pull/analyze.py).  `inspect` snapshots the working directory and generates on a staged copy of that snapshot; a command the host file doesn't name is resolved and marked `unresolved`.  The report also has a `dev-container` node (the dev container's values, and the project file's `container` settings) and `egress`.  A finding no setup check raised is told apart by its generated message, in its id.

### Review

The default reviewer is an explorer in the terminal.

* Both standard input and standard output must be terminals (`argutil`), or it fails.  There's no `--yes`, and pressing Enter denies.  This keeps automation from reviewing; it isn't a boundary, as a program such as `script` can provide a terminal.  Automation provides its own files, which a person vets with `scoter pull` outside the automation.
* It's a tree: the root summary, then a container, then a file, then the file's contents.  The developer moves down or back up.  It's a numbered menu to start with, as `curses` isn't available on Windows; a fuller interface can follow on the same report.
* File contents are shown through `review.file_lines` and `review.visible`: lines split on `\n` only, and control and non-ASCII characters shown escaped.  Never raw bytes, and never through `less -R`.
* Everything the build reads can be reached in the tree ([VER-3](../security/verification-attack-vectors.md#ver-3-the-review-doesnt-show-everything-it-accepts)).  New and changed files are shown in full.  Opaque files (over the size limit, or not UTF-8) are refused unless `--allow-opaque PATH` names each one.  The dev container's parsed values (`user`, `mounts`, `broker-bin`, the sshd settings, and the project file's container settings) have their own node.
* **No merge.**  The working directory starts from the source's version, and the developer's current configuration is shown as a difference to apply again by hand.  A merge that succeeds can join a project change and a developer's edit into a result neither of them wrote or reviewed, so the tools never write a combined result.  A structured merge of the project file alone, item by item as in [step 17](projects-v1.md#17-add-diff-verify-to-main_verifypy-script), is a later improvement if this proves tedious; it would be sensitive code that has to agree with the generator's reading (the [VER-1](../security/verification-attack-vectors.md#ver-1-the-dockerfile-meaning-match-can-be-fooled) lesson).
* The developer can edit the working directory, in their editor or IDE.  The reviewer re-analyzes as [VER-28](#ver-28-approving-something-other-than-what-was-shown) describes: it checks for changes with `scoter pull changed`, runs `analyze` again, compares the new report with the last by finding id, and carries its marks over.
* At approval, it lists the new and flagged files the developer never opened.  This is a nudge against approving without reading, not a block.
* **The reviewer's contract.**  `scoter pull` runs `pull: runs: reviewer` with its own SUC (`{x:universal}`), the working directory's id (`{x:workdir}`) and a session file (`{x:session}`, `surf-scoter-pull-session-1`).  The session file holds the pull's options (`allow-opaque`, and `known-findings`, the ids an earlier review showed); the reviewer replaces it with its decision: `approve` with the analysis id on screen, or `deny`.  The decision is a request: `approve` checks the working directory against that analysis, and every check, again.

**In the code:** [`pull_reviewer.py`](../../scripts/pull_reviewer.py) over [`pull/reviewer.py`](../../scripts/surf_scoter_lib/pull/reviewer.py): a numbered menu over the report, with `compare` by finding id.  `pull: runs: reviewer` is in the `pull` section of [`setup.yaml`](../../defaults/setup.yaml), guarded.

### Approval

* `approve` takes a working directory's id and the id of the analysis the reviewer last showed.  It snapshots the working directory again, and refuses if the snapshot isn't the one that analysis names: the developer approves exactly what they last saw, and an edit since then (say, an IDE saving a file) needs another look.
* It refuses when a check refuses.
* **The host file:** entries the developer wrote as absolute paths (`origin: user`) are kept.  The rest (`origin: resolved`), and commands the developer added, are resolved again, checked, and any difference is shown.
* **What it makes** is a new *locked configuration* in the store: the snapshot of the working directory (already in the store's blobs), with its host file, under a name (`default` unless `--name` says otherwise).  Nothing is copied into a directory the developer edits.  The configuration it replaces is kept, renamed `NAME.previous`, so going back is a switch (`scoter switch default.previous`); the one that had that name is retired ([VER-40](#ver-40-removing-a-configuration-in-use)), so each name keeps one step of history.
* **The link** is recorded in the store's index, by environment id, never in the project.  Only `scoter`'s own commands (approval, `switch`, `lock` and `unlock`) change it, never a setting, an environment variable or the project, so a project can't point a run at files it controls, even through `SURF_SCOTER_HOME` and direnv ([settings](../security/settings.md#where-settings-come-from)).  It points at the configuration in use, and holds the base snapshot id, the provenance and when it was last reviewed.
* `scoter switch NAME` switches a project to another of its locked configurations, to try a different one.  It runs the checks.  It never takes a path, so the link can only point at the store or the unlocked copy.

**In the code:** [`pull/approve.py`](../../scripts/surf_scoter_lib/pull/approve.py).  What happens to the configuration it replaces depends on where the working directory was seeded from: from the project, it's kept as `NAME.previous` (the one that had that name is retired), and the base, the provenance and the review timers are reset; from the unlocked copy (`scoter lock`), the configuration unlocked from is retired, and the timers carry on; from the configuration in use (`scoter review`), it's retired, and the timers reset.  The new one takes the name given, or the replaced one's, or `default`.  Pull and review approvals record the analysis's finding ids in the link, for the next review.

### The host file

The host file (`host.json`) is kept beside each configuration's files, never among them.  The working directory and the unlocked copy each hold a `files/` tree, a copy of the source's `.surf-scoter.d` with nothing added, with the host file next to it; the store keeps a configuration's snapshot and its host file side by side in its record ([VER-42](#ver-42-the-host-file-shares-a-name-with-the-projects-files)).  It holds each host command's handshake name, absolute path, the identity of its real target, an optional hash, the frozen `${section:key}` values, and where each path came from: `resolved` by the tools, or written by the developer (`user`).  The project file keeps `program` as a name, so it stays portable, and `scoter push` never sends the host file (VER-16).

`scoter run` uses only the host file's paths.  A missing or changed executable stops the run; it never falls back to the `PATH`.  That's what closes BRIDGE-11.  [BRIDGE-13](../security/host-bridge-attack-vectors.md#bridge-13-an-accepted-program-can-change-after-the-review) is unchanged.

The host file is part of the configuration, so it moves with it when the developer switches, and they can read and edit it once unlocked.  Two other answers were considered:

* Absolute paths written into the project file: no host file, but nothing is portable, and a push leaks them (VER-16).
* Resolving on every run, but only in system directories the setup lists: simpler, but a `PATH` change still quietly changes what runs.

**In the code:** [`host_file.rs`](../../host/src/config/host_file.rs) and [`host_file.py`](../../scripts/surf_scoter_lib/host_file.py), with the same rules.  The file (`surf-scoter-host-file-1`) is keyed by the program as the project file names it (a command's `program`, or its `command`), not by the handshake name: commands can share a name and differ in arguments, or share a program, and each program resolves once.  Each entry has `path`, `real` (the file it resolved to), `origin` and an optional `sha256`.  `gen-scoter-uc --host-file` checks it against the configuration's commands and this computer, and puts it in the SUC's `host-file`; `host-brokered` is then the configuration's commands with the host file's programs, and `egress` the configuration's rules with the setup's gateway.  `scoter-host` checks each program again as it starts.  The frozen `${section:key}` values aren't recorded: the setup fingerprint (VER-29) and the checks at every run cover a setup change.

### Locking and unlocking

Strict checks on the developer's own files get in the way of the people who write Surf Scoter projects.  If refining the files means a review for every edit, they give up: they either open everything up, or stop using the tool.  So a configuration has two states, and each has its own place.

**Locked**, the normal state, after any approval:

* The configuration lives in the store, as a snapshot plus its host file.  It isn't a directory anyone edits, so a developer can't change it by accident, say by forgetting that they locked it and opening it in an editor.  This replaces `scoter edit`.
* `scoter run` restores the snapshot into a private staging directory ([`run_config.py`](../../scripts/surf_scoter_lib/run_config.py)), checking each blob's hash, and runs host commands only from the host file's paths.  The snapshot's hashes are the integrity check; nothing else needs to compare against a lock.

**`scoter unlock [NAME]`** moves a configuration out for editing:

* It restores the configuration into the unlocked directory, `${pull:paths:unlocked-dir}/ENV-ID`, and points the link at it.  `pull: paths: unlocked-dir` is a guarded setting, outside every project, clone, state directory and the store (VER-37).
* The configuration it came from stays in the store, unchanged, and the link records which one it was, so the edits can be thrown away with `scoter lock --revert` (below).  The store's pruning never removes a configuration while it's unlocked.
* The unlocked copy must be owned by the developer, with nothing in it writable by group or other, checked on every run with `O_NOFOLLOW` and `fstat`.  Not the store's exact `0600` and `0700`, as editors create `0644` files.  It's never mounted, and runs build from a staged copy of it, never from the directory itself.  The container directory rules apply: links and special files are refused, and secret-looking names warn.  On Windows, [VER-8](../security/verification-attack-vectors.md#ver-8-the-store-itself)'s note about the profile's access control lists applies.
* Keeping the files in a dotfiles repository means keeping the unlocked copy there, and taking changes from it with `scoter lock`.  Pulling a dotfiles repository others write to brings their changes, which the next `scoter lock` reviews.
* `scoter run` then runs from the unlocked copy, warning before each run.  It still runs the refusal checks on the files as they are.  Commands that are new or changed are resolved as the run starts, through the [path checks](projects-v1.md#checks), and listed.  BRIDGE-11 is knowingly open while unlocked.
* `scoter push` refuses: only reviewed content goes to the project.
* `scoter pull` asks the developer to lock first, so a pull never drops their edits.
* `scoter unlock` refuses when the unlocked directory already holds files, rather than overwriting someone's edits (VER-36).

**`scoter lock`** moves it back:

* It's a pull from the unlocked copy: seeding from it, the analysis, a review of what changed since the configuration was unlocked, and approval, which writes the host file again and stores a new locked configuration.
* Once the configuration is safely in the store, it points the link back at it, and removes the unlocked copy (VER-38).

**`scoter lock --revert`** throws the edits away, and goes back to the configuration that was unlocked:

* There's no seeding, analysis or review: nothing new comes into use, only the configuration the developer had before, which was reviewed when it was stored.
* It lists the files in the unlocked copy that differ from that configuration, and asks before throwing them away; pressing Enter keeps them.  `--yes` skips the question, for scripts.  This guards the developer's work, not the host, so a `--yes` is safe here.
* It points the link back at that configuration, then removes the unlocked copy the same way `scoter lock` does: only after the link is written, never following links, and renamed with `--keep DIR` instead (VER-38).  As with `lock`, a removal or rename that fails doesn't undo the revert.
* The [review timers](#ver-20-trust-doesnt-expire) carry on from where they were; an unlock and a revert are neither a review nor a change.
* An environment started from the unlocked copy keeps running, as it was built from a staged copy; the next `scoter run` uses the locked configuration.
* If that configuration is gone from the store anyway (removed by hand, say), it refuses, and says to `scoter lock` the edits or `scoter pull` again.

Locking and unlocking repoint the link, as `scoter switch` does.  A project author's loop is then: unlock, edit and run, then lock (or `lock --revert` to drop the experiment), push.

**The trade-off.**  Unlocked is an exemption by another name ([VER-4](../security/verification-attack-vectors.md#ver-4-exemptions-accept-what-nobody-reviewed)), and a developer can leave a configuration unlocked for good.

* **A.** No limit; the warnings on each run are the only reminder.
* **B.** An unlock lasts `pull: values: unlock-days` (guarded), after which runs stop until `scoter lock`.
* **C.** After `unlock-reminder-days`, each run adds a louder reminder, listing the egress rules and host commands.

**Decided:** C by default, with B for those who want it.  The balance will need tuning once people use it.  The reminder counts from the unlock, so a developer who finds it tiresome can run `scoter lock` then `scoter unlock`: the lock reviews what changed, which is the point of the reminder, and the new unlock starts the count again.  Whether a configuration is unlocked is kept in the store's link record, and only the developer's own commands change it.


**In the code:** [`pull/lifecycle.py`](../../scripts/surf_scoter_lib/pull/lifecycle.py) (`unlock`, `lock`, `revert`, `switch`, `review_in_use`, `init`), [`pull/unlocked.py`](../../scripts/surf_scoter_lib/pull/unlocked.py) (the unlocked directory's location and ownership checks, and reading it), and [`run_config.py`](../../scripts/surf_scoter_lib/run_config.py) (what a run runs).  `scoter unlock --replace` moves files left in the unlocked directory to a backup beside it.  `scoter switch NAME` runs the checks a run would (`run_config.check_config`) before pointing the link at it.

### Reviewing again: `scoter review`

An acceptance used to expire, because what a project's settings allow can change while the settings stay the same: the permissions behind a cloud service, the hosts behind an egress rule, the program at a host path.  A locked configuration doesn't expire ([VER-20](#ver-20-trust-doesnt-expire)), so `scoter review` is how the developer looks at it again.

* **Seeding** reads the locked configuration in use from the store, restoring its blobs and checking each hash, rather than reading a directory.  Everything after that is the pull's: the same analysis, the reviewer, the editing loop and approval.  It runs the setup's checks as they are now, so a check added since the last review shows up here.
* **What changed around it** leads the review, as the files themselves usually haven't changed: findings that are new since the last review, the host file's paths resolved again (a different program at the same name, or a changed hash, [BRIDGE-13](../security/host-bridge-attack-vectors.md#bridge-13-an-accepted-program-can-change-after-the-review)), and the full list of egress rules and host commands.
* **Approval** stores a new locked configuration (the same blobs, unless the developer edited something), points the link at it, sets `last-reviewed`, and removes the configuration it reviewed ([VER-40](#ver-40-removing-a-configuration-in-use)).
* **Denying** changes nothing: the old configuration stays in use, and its [review timers](#ver-20-trust-doesnt-expire) carry on.
* An unlocked configuration can't be reviewed; `scoter lock` is its review.

**In the code:** `review_in_use` in [`pull/lifecycle.py`](../../scripts/surf_scoter_lib/pull/lifecycle.py) runs the pull's porcelain from the configuration in use, with the last review's finding ids as the session's `known-findings`, so only what's new is marked.

## Attack vectors in the pull design

A review of the design above.  The IDs continue from the new risks.

### VER-23: seeding reads hostile input

**Risk** (High): seeding is the only phase that reads the project, so every defensive rule has to be there, and it runs the generator on the project's YAML.

**Options:**

* **A.** Seeding uses the snapshot reader for every read, and parses only the staged copy, with the generator's existing limits (no anchors, aliases, tags or duplicate keys).
* **B.** As A, and seeding also refuses a source above the snapshot limits before parsing anything.

**Decided:** B.  Contributors should treat seeding as the sensitive code for pulls.

### VER-24: a lookup reaches the live project

**Risk** (High): with [found, then copied](#the-projects-surf-scoterd-found-then-copied), no tool-set location names the live `.surf-scoter.d`, and the default search list no longer includes it.  It can still be reached by a setting written by hand: a `dev-containers-search` entry such as `${scoter:project-dir}/.surf-scoter.d`, or `${scoter:project-dir}` itself.  A parent found by name there is read from the live project, at seeding or on any run, so what's built isn't what was reviewed, and the selection at seeding no longer matches what was copied.  The acceptance flow's setup only warned about such an entry.

**Options:**

* **A.** Refuse any `dev-containers-search` entry, and any resolved container or parent directory, that lies inside the project directory, its VCS root or the project clone, after symlinks are resolved.  Then check that every selected container directory lies inside the staged copy or a search directory.
* **B.** Keep the warning, and rely on the list being a [guarded setting](../system-config-file.md#guarded-settings) already, which `scoter user-setup` reports; check the selection as in A.
* **C.** Check the selection only.

**In every case:** the generator searches the copy first, by itself, and never through the list.

**Decided:** A.  Under the new rule, a container the project supplies reaches a run only through the copy; a search entry inside the project is a second path to the same files, one no review covers, and there's no case for allowing it.  A developer who wants a container they keep in their own repository puts it outside the project directory, or in their configuration.

### VER-25: rewriting the provenance

**Risk** (Medium): if the VCS report or the comparison with the base were kept in the working directory, an edit or a reviewer could turn "modified" into "committed".

**Decided:** keep the provenance in the state directory, keyed by the working directory's id, and written only by `seed`.

### VER-26: the reviewer is a setup program

**Risk** (Medium): a graphical or IDE reviewer shows project text.  A reviewer that renders it as HTML or Markdown, in a web view for example, can run the project's script on the host.

**Options:**

* **A.** Document that a reviewer is trusted setup, and must show project fields as plain text.
* **B.** As A, plus a reference contract: the report's project fields come with their `clean` form, and the reviewer shows only that.

**Decided:** B, with `pull: runs: reviewer` guarded.

### VER-27: approval can't prove a person reviewed

**Risk** (Medium): `approve` is a plumbing command, and a script can call it.  Requiring a terminal in the default reviewer doesn't change that.

**Decided:** accepted; it can't be avoided.  Under user-owned files, approving is no stronger than the developer writing their own directory, which they can always do, and no barrier stops a determined developer from scripting around the review.  The boundary is that nothing from the project can start an approval: `approve` takes only ids that `seed` registered, and the analysis id the developer was shown.

What's left is a warning, not a control.  The user documentation for `scoter pull`, `scoter lock` and `scoter review`, and the `approve` command's own help, say plainly what skipping the review gives up: whatever the project's files allow (host commands, egress rules, the dev container and its build) runs with nobody having looked at it, and every other protection on this page assumes someone did.

### VER-28: approving something other than what was shown

**Risk** (Medium): the working directory changes between the last analysis and approval, through an IDE saving a file, or a reviewer bug.

**In every case:** the binding above: approval snapshots again, and refuses unless it matches the analysis id the reviewer showed.  That stops a stale approval, but on its own it would make the developer approve again from scratch after every edit, so every reviewer, however it's built, also needs the loop below.

**The re-analysis loop:**

1. **Notice the change.**  A plumbing command, `scoter pull changed WORKDIR-ID ANALYSIS-ID`, snapshots the working directory and lists the paths added, removed or changed since that analysis's snapshot, without analyzing.  An empty list means the analysis still holds.  It's cheap enough to call often, and it's the only place file changes are worked out.
2. **Analyze again** with a plain `analyze`, which knows nothing of the last analysis.
3. **Compare the findings**, in the reviewer.  Both reports carry finding ids, so the comparison is a matter of sets: a finding of the new report is `new` or `same`, and an id missing from it is `resolved`.  It compares findings only, never file contents: the files are expected to differ, as that's why the analysis ran again, and step 1 already said which ones.  Each reviewer does this itself, for now.  If that makes reviewers too big, or replacements start disagreeing, a separate plumbing command (`scoter pull compare`) can take it over; it doesn't belong in `analyze`, which stays stateless, or in `changed`, which only checks files.
4. **Carry the state over by finding id.**  The reviewer keeps which findings the developer has seen, by id, and which files they opened, by path.  After the comparison:
    * a `new` finding is marked for the developer's attention;
    * a `same` finding keeps whatever the developer did with it;
    * a resolved finding loses its mark, and goes into a folded "no longer found" list; it never asks for attention;
    * a file in step 1's list with no new finding isn't marked, but the summary says how many files changed since the last analysis, and the file shows the difference when opened.  The developer usually made the change themselves; a change they didn't make (a tool, an IDE) still shows in that count.
5. **Show it**, so the analysis id the reviewer holds is always the one on screen, and approval binds to that.

**Options for noticing the change:**

* **A.** Check before each prompt and before approval, with `scoter pull changed`.
* **B.** Watch the working directory for changes (inotify and its kin), and re-analyze as they happen.
* **C.** Re-analyze only when the developer asks.

**Decided:** the binding, the loop and finding ids, as above.  For noticing changes: A for the default reviewer, which has a prompt to check at; B is open to a graphical or IDE reviewer, which already watches files.  C leaves the developer looking at an analysis that's out of date until approval refuses it.  The reviewer's state is its own convenience, not authority: approval still checks the snapshot and the refusal findings itself, whatever the reviewer marked.

### VER-29: skipping seeding

**Risk** (High): if `analyze` or `approve` took a path, the project's `.surf-scoter.d` could be approved directly, without seeding's tool configuration rules, and with the project able to change it while approval reads it.

**Decided:** both take only registered working directory ids, read through a SUC that matches the one at seeding (below).

An id means nothing on its own: `scoter` finds the working directory, its provenance and its analyses through the SUC, from `${scoter:state-dir}` and the store's location, which come from the developer's setup and never from an argument, an environment variable or the project.  So the id is only as good as the SUC it's read through:

* A porcelain passes its own SUC on to its phases ([how the commands call each other](#commands)), so they read the same state directory.
* The reviewer calls `analyze` and `changed` back through `scoter`, which builds a new SUC from the reviewer's environment.  A reviewer started from an IDE may see a different `SURF_SCOTER_HOME` or a direnv-changed environment ([settings](../security/settings.md#where-settings-come-from)), so its SUC could name a different state directory, or a different setup.  `seed` records, with the working directory, a fingerprint of the SUC's setup: the state directory, the store and the checks.  `analyze`, `changed` and `approve` refuse an id when the SUC they were given doesn't match it.

### VER-30: the VCS handler's output

**Risk** (Medium): file names with newlines, very large output, or a handler that reads the working tree through the VCS's filters.

**Decided:** the handler rules above: NUL separation, the schema and size checks, `check_relative`, and no command that reads the working tree.

### VER-31: resolving through a `PATH` the project influences

**Risk** (High): tools such as direnv add project directories (`node_modules/.bin`) to the `PATH` when the developer enters the project.  Resolving `program: eslint` at seeding finds the project's own script.

**Decided:** run the path checks of [step 4](projects-v1.md#resolving-the-commands) at every resolution: seeding, approval, `lock` and unlocked runs.  It's the place BRIDGE-11 could come back.

### VER-32: a host file from the project

**Risk** (High): a project that ships its own `host.json` could set `origin: user` on a path, which approval keeps without resolving it again.

**Mitigated** by [VER-42](#ver-42-the-host-file-shares-a-name-with-the-projects-files)'s A, with nothing more to do here.  The host file is never read from a project's tree: seeding takes a host file only from the top of the unlocked copy, during `scoter lock`, and a `host.json` the project ships is an ordinary file under `files/`, which nothing reads as a host file, so its `origin: user` entries mean nothing.

### VER-33: the unlocked state

**Risk** (Medium): edits run without review, and commands are resolved as runs start.  It's [VER-4](../security/verification-attack-vectors.md#ver-4-exemptions-accept-what-nobody-reviewed) again, and the risk of a directory left unlocked for good.

**Decided:** the reminder (C) from [locking and unlocking](#locking-and-unlocking), with an expiring unlock (B) for setups that want it, push refusing, a warning on each run, and the refusal checks still running.

### VER-34: a kept working directory

**Risk** (Low): with `--keep-working-dir`, unreviewed project files stay on disk, where a later command might take them for reviewed ones.

**Decided:** approval checks a kept directory against an analysis id like any other, `--keep-working-dir` prints the path and a warning, and `scoter cleanup` removes it.

### VER-35: two pulls at once

**Risk** (Low): two pulls of one environment approve over each other, or update the link out of order.

**Decided:** a separate working directory for each pull, and the store's lock around approval and every change to the link.

### VER-36: a stale unlocked copy

**Risk** (Medium): files can reappear in the unlocked directory after `scoter lock`: an IDE left open saves a file, or a crash leaves a copy behind.  A developer who edits them thinks they're changing their configuration, and a later `scoter unlock` could overwrite them or mix them with the store's version.

**Options:**

* **A.** `scoter unlock` refuses while the unlocked directory holds anything, and says what's there.  `--replace` moves the old files to a backup first.
* **B.** As A, and `scoter run`, on a locked configuration, warns when its unlocked directory isn't empty, saying those files aren't used.

**Decided:** B.

### VER-37: the unlocked directory setting

**Risk** (High): `pull: paths: unlocked-dir` decides where an unlocked configuration is run from.  Inside a project clone, a state directory or anything else the container can write, the container would write its own settings for the next run.  Inside the project, the project's own files would run without review.

**Options:**

* **A.** Refuse it inside the clone, the state directories, the store and any project or repository, the same check the setup already applies to `verified-dir`.
* **B.** As A, but allow it inside a repository as a guarded setting, for an author who wants to edit inside the project.

**Decided:** A.  An author working in the project uses `scoter lock` from the unlocked copy and `scoter push`, and it keeps the project's files from ever being run as they are.

### VER-38: removing the unlocked copy

**Risk** (Medium): `scoter lock`, and `scoter lock --revert`, delete a directory the developer was editing.  A symlink in it could send the deletion elsewhere, and a removal before the configuration is safely stored loses the edits.

**Options:**

* **A.** Remove it only after the new configuration and link are written (for `--revert`, only the link), with the store's existing removal that never follows links (`fileutil.rm`).
* **B.** Rename it to a backup instead of deleting it.
* **C.** Leave it in place, and rely on VER-36's warnings.

**Decided:** A, with B behind `--keep DIR`.  The unlocked directory and the store can be on different file systems, so storing the configuration is always a copy and a check of every hash; only then is the unlocked copy removed or renamed.

* **`--keep DIR`** names where the developer wants the old copy, and `lock` tries to rename the unlocked copy there.  `DIR` mustn't exist yet.  It's refused inside the project directory, its VCS root, the project clone, the developer's Surf Scoter directory (`${scoter:user-dir}`), the state directory, the store and the unlocked directory itself, with a message saying why: files there could be picked up as configuration, or reach the container.  The developer can still move it there by hand; the tool just won't do it for them.  The kept copy is the developer's from then on: nothing tracks it, and `scoter cleanup` leaves it alone.
* **A failed removal or rename doesn't fail the lock.**  An editor or IDE holding a file open (on Windows, especially) can stop either, as can a rename across file systems.  The lock's work is done once the configuration is stored and the link points at it, so `lock` finishes, and says what it couldn't remove or rename, and where it is.  A partly removed copy is harmless: the configuration is in the store.  Whatever's left is a stale unlocked copy, which [VER-36](#ver-36-a-stale-unlocked-copy)'s warnings cover until the developer removes it, and the next `scoter unlock` refuses until they do.

### VER-40: removing a configuration in use

**Risk** (Medium): `scoter review` and `scoter lock` remove the configuration they replace.  An environment started from it may still be running: `scoter-host`'s check that the accepted settings match the store ([VER-8](../security/verification-attack-vectors.md#ver-8-the-store-itself)) could then fail mid-session, or, worse, the bridge could pick up the new configuration's host commands without a restart.

**Options:**

* **A.** Remove a replaced configuration only when no running environment uses it, by the running environment records; otherwise mark it for removal, and let the next `scoter kill`, `scoter run` or `scoter cleanup` remove it.
* **B.** Refuse to approve while the environment is running.
* **C.** Keep replaced configurations, and leave them to the store's pruning (`verified-keep`).

**In every case:** a running environment keeps the configuration it started with; nothing switches it mid-session.

**Decided:** A.  C keeps a rollback the developer may want, but a configuration they reviewed and replaced is one they meant to stop using.

### VER-41: reviews of nothing new train rubber-stamping

**Risk** (Medium): `scoter review` shows a configuration that usually hasn't changed.  A developer who sees the same page every month learns to approve it unread, which is the habit the review is meant to prevent, and a review-required setting makes it worse.

**Options:**

* **A.** Lead with what changed around the configuration (new findings, re-resolved paths, setup changes), and show the unchanged files folded, one level down in the tree.
* **B.** As A, and ask the developer to confirm each egress rule and host command, rather than approving the whole configuration at once.
* **C.** Show everything in full every time.

**Decided:** A, with B as a later option for teams that turn on the [expiry](#ver-20-trust-doesnt-expire).  The review timers are a notice by default, not a warning or an expiry, for the same reason.

### VER-42: the host file shares a name with the project's files

**Risk** (Medium): a container directory can be any single directory name, so a project can put its dev container in `.surf-scoter.d/host.json/` (with a `Dockerfile` in it).  If the host file sits beside the project file, inside the copied tree, the generated `host.json` and the project's directory want the same path.  At best seeding, approval or `scoter lock` fails on the clash.  At worst one side quietly wins: the host file isn't written, or a later read finds the project's content where the host file should be.  The same goes for any name the tools add to the tree later.

**Options:**

* **A.** Keep the two apart.  A configuration has two parts: its *files*, a copy of the source's `.surf-scoter.d` with nothing added, and its *host file*, kept beside the files and never among them.  The tools build each layout themselves, from these two parts, rather than copying a tree that happens to hold both:
    * the working directory is `WORKDIR/files/` and `WORKDIR/host.json`;
    * the unlocked copy is `${pull:paths:unlocked-dir}/ENV-ID/files/` and `.../ENV-ID/host.json`;
    * the store keeps a configuration as a snapshot id and a host file blob, side by side in its record.

    Staging restores only `files/` as the project files directory, so the generator never sees the host file, and a parent's `dir` can't reach it (it's outside `.surf-scoter.d`).  Seeding and `scoter lock` take files only from `files/`, and the host file only from the top.  Push sends only `files/`.
* **B.** Reserve the name: refuse a top-level `host.json`, file or directory, at seeding, in the distribution's templates and in the generator's container name check.  It's a smaller change, but the project's namespace and the tools' share one tree: each name the tools add later is another reserved name, push has to filter them out, and a project that already uses one can't be pulled.
* **C.** As B, but with a name a project can't produce, one seeding already refuses (say, with a character outside the allowed set).  It avoids clashing with real projects, but the host file is then a file name nobody can type, and the tree is still shared.

**Decided:** A.  It takes away the clash rather than guarding against it, and in doing so makes [VER-16](#ver-16-the-push-leaks-host-details) and [VER-32](#ver-32-a-host-file-from-the-project) hold by construction: push has no host file to leave out, and a `host.json` the project ships is an ordinary file.

### Against the recurring themes

The [recurring themes](../security/attack-vectors.md#recurring-themes), checked against the four phases:

* **Project values written into host files:** the host file is written from project command names.  They're names only ([`program` is a name](projects-v1.md#program-is-a-name)), checked before they're resolved, and the paths come from the host (VER-31).
* **The host acting inside the project clone:** the VCS handlers run only in the developer's repository, and nothing reads the clone (VER-30).
* **Matching on meaning:** the no-change check is by bytes only.
* **Showing everything it accepts:** the reviewer reaches every file the build reads, and the dev container values (VER-3).
* **State that outlives a session:** the working directory lasts one command unless kept (VER-34); the provenance and the locked configurations are in the state directory and the store, which the container never sees; an unlocked copy that outlives its lock is VER-36.
* **Differences between runtimes:** on Windows, the working directory's mode and the store's lock rely on the profile's access control lists, as [VER-8](../security/verification-attack-vectors.md#ver-8-the-store-itself) says.


## Starting from a template

Some developers won't use the project's files: the project ships none (it isn't their project, or the team doesn't use Surf Scoter yet), or its suggestion doesn't suit them.  They start a configuration from a distribution template instead:

1. `scoter unlock --from-template NAME` writes the template into the unlocked directory.
2. The developer edits it and tries it with `scoter run`, with the usual warnings for an unlocked configuration.
3. `scoter lock` reviews it and stores it, like any unlocked copy.
4. `scoter push`, for a developer who owns the project, offers it to the project: creating its `.surf-scoter.d` when there's none ([VER-39](#ver-39-the-first-push-creates-the-projects-surf-scoterd)), or as any push otherwise ([VER-15](#ver-15-the-push-writes-into-the-project-tree)).  A developer who doesn't own it stops at step 3.

**`scoter unlock --from-template NAME`:**

* Works whether or not the project has a `.surf-scoter.d`; the developer is choosing the template over whatever the project supplies.
* The project directory is found as usual, or, when the project has no `.surf-scoter.d` to find it by, is the current directory or `--project-dir`, with the same limits as discovery: inside a repository of the setup's VCS, where the VCS has a `find-root`.  The link in the store, by environment id, is then how later commands find the project ([found, then copied](#the-projects-surf-scoterd-found-then-copied)).
* The template comes only from the distribution's templates directory ([VER-7](#ver-7-templates-from-a-distribution-inside-the-project)), copied with the snapshot rules into the unlocked copy's `files/`.  No host file is written; `scoter lock` resolves the commands.
* The unlocked directory's rules apply as for any unlock ([VER-36](#ver-36-a-stale-unlocked-copy), [VER-37](#ver-37-the-unlocked-directory-setting)).  A configuration already in use stays in the store, so `scoter lock --revert` goes back to it; with none, `--revert` removes the unlocked copy and the link, and leaves the project unconfigured.
* The provenance records the template's name and snapshot id.  The base is the project's `.surf-scoter.d` as it was at the unlock, by hash, or empty when there was none.  So [VER-21](#ver-21-pulls-that-never-happen)'s notice reports a later change to the project's files (a teammate adding or changing them), not the files the developer chose not to use.  After the developer's own push, the base is the configuration they pushed ([VER-15](#ver-15-the-push-writes-into-the-project-tree)).

**`scoter init [NAME]`** is the same action made friendly for new users, close to an alias of `scoter unlock --from-template NAME`, with two differences:

* **It refuses, before doing any work, when the project directory holds a `.surf-scoter.d` of any kind:** a directory, even empty, a file, or a symlink, even a broken one (checked with `lstat`).  A new user in a project that already supplies files should pull them, not replace them without knowing they're there; `scoter unlock --from-template` is there for a developer who means to.
* **It finishes with instructions**: where the files are, and the next steps: edit and `scoter run`, `scoter lock` to review and store, `scoter push` to add the configuration to the project, or `scoter lock --revert` to give up.

`NAME` defaults to `pull: values: default-template` (`default`).  Unlike the acceptance flow's `init`, [`main_init.py`](../../scripts/main_init.py) never writes into the project; only `scoter push` does.

**Why it matters for security:** the easy answer is to start the developer's configuration straight from a distribution template, without a review.  That brings [VER-9](../security/verification-attack-vectors.md#ver-9-trusted-containers) back: the template is trusted rather than reviewed, and whoever can change the distribution's templates changes every configuration made from them.  Here the template reaches a run only through `scoter lock`'s seeding, analysis, review and approval, so VER-9 stays closed, and [VER-7](#ver-7-templates-from-a-distribution-inside-the-project) stays Low.

**Options considered:**

* **A. Unlock from a template**, as above.
* **B. Seed from the template.**  Let `scoter pull seed` take a template as its source, alongside the project and the unlocked copy.  The review is the same as A's, without an unlocked step, but `seed` gains a third kind of source.
* **C. Seed from any directory the developer names.**  As B, for a directory outside the project (a dotfiles repository, a team's shared configurations).  The most flexible, and the most to check: the directory gets [VER-37](#ver-37-the-unlocked-directory-setting)'s location rules, and the reviewer can't tell how trustworthy it is.
* **D. Trust the template without a review.**  The simplest, and VER-9 again.

**Decided:** A, with `scoter init` as its friendly form.  It reuses `unlock`, `lock` and `push`, and opens nothing new.  C may follow if teams want to share configurations outside a project.  Telling the developer when the distribution's template changes, as VER-21 does for projects, is a later improvement.


**In the code:** the distribution's [`templates/`](../../templates/) (`default`: a project file, and a `dev-container` whose parent is the distribution's `dev-env`), named by `pull: paths: templates-dir`, guarded.  The template assumes `vcs: git` and the default image prefix, which the developer edits while unlocked.

## The verified store's new role

The store keeps:

* the snapshots a pull takes (the existing manifests and blobs): the seeded source, each analysis of the working directory, and the last pulled base for each environment;
* each environment's locked configurations: a snapshot and its host file, under a name.  Each environment keeps its own; none is shared between environments, so a review, a lock or a removal in one never changes what another runs;
* the index, and for each environment its link: the configuration in use (or the unlocked copy), the base, the provenance and when it was last reviewed.

The pull's working directories and their provenance live in the state directory, not the store, as they last only one command.  Only the unlocked copy, in `pull: paths: unlocked-dir`, is a directory the developer edits.

Acceptances, carried acceptances, exemptions and expiry are gone, with `scoter verify` and `scoter exempt`.

**In the code:** [`configs.py`](../../scripts/surf_scoter_lib/verified/configs.py): `ENV-ID/configs/ID.json`, each a configuration (an id, a name, the snapshot's manifest and the host file), never overwritten, with at most one live configuration per name; and `ENV-ID/link.json`, the environment's link (`in-use` or `unlocked`, the base, the provenance, `last-reviewed`, `last-notice`, `removing`, `reviewed-findings`).  A configuration replaced or removed while an environment runs it is marked in `removing`, and goes once nothing runs it (`retire`, `remove_retired`; the run's SUC records the configuration it runs).  Blob collection keeps what configurations and bases use.  [`verified/remove.py`](../../scripts/surf_scoter_lib/verified/remove.py) is `scoter list` and `scoter remove`; [`cleanup.py`](../../scripts/surf_scoter_lib/cleanup.py) removes gone projects' configurations, retired ones, and what acceptances left in the store.


## Where the project's files go

For contributor analysis: each copy of a project's configuration files, where it's made, and where it goes, command by command.  `STATE` is `${scoter:state-dir}`, `STORE` is `${verification:paths:store}`, `UNLOCKED` is `${pull:paths:unlocked-dir}`, and `PROJECT` the project directory.  Everything under `STATE`, `STORE` and `UNLOCKED` is private to the developer, and none of it is ever mounted into a container.

* **`scoter pull`** (and `scoter pull seed`):
    1. `PROJECT/.surf-scoter.d` is read, never parsed: through the VCS's committed objects (the default), or from the working tree (`--working-tree`), into a whole snapshot in memory, within the limits.
    2. That snapshot is written to `STATE/pull/TMP/staged/`, where the generator reads it (`--config-dir`) to find what the build uses; the directory is removed.
    3. The build's files go to the working directory, `STATE/pull/work/ID/files/`, with the host file beside them, `STATE/pull/work/ID/host.json`; their blobs to `STORE/blobs/`.  The provenance goes to `STATE/pull/records/ID/provenance.json`.
    4. `analyze` snapshots `files/` and stages it again (`STATE/pull/TMP/staged/`) for the generator and the checks; its record goes to `STATE/pull/records/ID/analyses/AID.json`, and its report to standard output.
    5. `approve` snapshots `files/` again, stages it once more for the checks, writes the build's blobs to `STORE/blobs/`, the configuration to `STORE/ENV-ID/configs/CID.json`, and the link to `STORE/ENV-ID/link.json`.
    6. The working directory and its records are removed (unless `--keep-working-dir`; `scoter cleanup` removes them after a day).
* **`scoter run`:** the configuration in use is restored from `STORE` (each blob checked) to `STATE/staged-XXXX/files/`, with its host file at `STATE/staged-XXXX/host.json`; while unlocked, `UNLOCKED/ENV-ID/files/` is read as a pull reads a source, and staged there instead, with a host file resolved as the run starts.  The generator reads only the staged copy, and the run's SUC goes to `STATE/environments/HOST/suc.json` for `scoter-host`.  The containers build from the staged copy, and the user's own containers from where they are.  `PROJECT/.surf-scoter.d` is only hashed, never parsed.  The staged copy is removed when the run ends, or by `scoter kill` or `scoter cleanup`.
* **`scoter unlock`:** the configuration in use is restored from `STORE` to `UNLOCKED/ENV-ID/files/`, with its host file at `UNLOCKED/ENV-ID/host.json`.  With `--from-template NAME` (and `scoter init`), `${pull:paths:templates-dir}/NAME` is read as a source and written to `UNLOCKED/ENV-ID/files/`, with no host file.
* **`scoter lock`:** a pull seeded from `UNLOCKED/ENV-ID` (`--from unlocked`) instead of the project, as above, ending in `STORE`; then `UNLOCKED/ENV-ID` is removed, or renamed to `--keep DIR`.  `--revert` stores nothing, and removes or renames it the same way.
* **`scoter review`:** a pull seeded from `STORE` (`--from locked`), ending in `STORE`.
* **`scoter switch`:** the named configuration is restored to `STATE/pull/TMP/` for the checks, then the link is changed.
* **`scoter push`:** the configuration in use is read from `STORE`'s blobs, compared with `PROJECT/.surf-scoter.d` read as a source, and written into it file by file; or, on the first push, into `PROJECT/.surf-scoter-push-XXXX/`, renamed to `PROJECT/.surf-scoter.d`.  The host file is never written.


## Summary

VER-3, VER-10, VER-12, VER-13, VER-14, VER-17, VER-18 and VER-19 are dealt with in the [pull design](#designing-the-pull), and VER-9 no longer applies to templates, so they aren't listed here.  [Verification attack vectors](../security/verification-attack-vectors.md) has each one's status.

| ID | Risk | Severity | Decision | Other options | Where |
| --- | --- | --- | --- | --- | --- |
| VER-7 | Templates from a distribution in the project | Low | Refuse that template directory | Rely on the pull's review | `pull/lifecycle.py` |
| VER-15 | The push writes into the project tree | High | Approve a patch (or `--yes`), then write with `openat`, checking each file's hash | A patch only; sibling and rename | `pull/push.py` |
| VER-16 | The push leaks host details | Medium | Portable files only; scan as a backstop | `push-exclude` | `pull/push.py` |
| VER-20 | Trust doesn't expire | Low | Expiry, warning and notice timers, chosen by the setup; the notice only by default | A single reminder or refusal; no expiry | `run_config.py`, `setup.yaml` |
| VER-21 | Pulls that never happen | Low | One line on each run | Louder on removals | `run_config.py` |
| VER-22 | Checks on every run stop a run | Low | Refuse, with how to override | Warn until the next pull | `run_config.py` |
| VER-23 | Seeding reads hostile input | High | Snapshot reads only; refuse oversize sources before parsing | Snapshot reads only | `snapshot.py`, `pull/seed.py`, `vcs_files.py` |
| VER-24 | A lookup reaches the live project | High | Refuse search entries inside the project; search the copy first | Guarded warning | `system.rs`, `config.rs` |
| VER-25 | Rewriting the provenance | Medium | Kept in the state directory, written only by `seed` | | `pull/workdir.py` |
| VER-26 | The reviewer is a setup program | Medium | Plain text only, from the report's cleaned fields; guarded | Documentation only | `pull/analyze.py`, `setup.yaml` |
| VER-27 | Approval can't prove a person reviewed | Medium | Accepted; registered ids only | | `pull/workdir.py` |
| VER-28 | Approving something other than what was shown | Medium | Approval bound to the analysis id; re-analysis by finding id; check before each prompt | Watch the files; re-analyze on request | `pull/approve.py`, `pull/reviewer.py` |
| VER-29 | Skipping seeding | High | Registered ids only, read through a SUC matching the one at seeding | | `pull/workdir.py` |
| VER-30 | The VCS handler's output | Medium | NUL separation, schema and size checks; no working tree reads | | `vcs_files.py`, `vcs_git_files.py` |
| VER-31 | Resolving through a `PATH` the project influences | High | Path checks at every resolution | | `host_file.py`, `checks.py` |
| VER-32 | A host file from the project | High | Mitigated by VER-42: never read from the project's tree | | |
| VER-33 | The unlocked state | Medium | A reminder; push refuses; checks still run | An unlock that expires; no limit | `run_config.py`, `pull/lifecycle.py` |
| VER-34 | A kept working directory | Low | Approval re-checks; cleanup removes it | | `pull/approve.py`, `cleanup.py` |
| VER-35 | Two pulls at once | Low | A working directory per pull; the store's lock | | `pull/workdir.py`, `store.py` |
| VER-36 | A stale unlocked copy | Medium | `unlock` refuses a non-empty directory; runs warn | Refuse only | `pull/lifecycle.py`, `run_config.py` |
| VER-37 | The unlocked directory setting | High | Refused in container-writable places and repositories | Guarded inside a repository | `pull/unlocked.py`, `setup.yaml` |
| VER-38 | Removing the unlocked copy | Medium | Remove after storing, without following links; `--keep DIR` renames; a failure doesn't fail the lock | Always back up; leave it | `pull/lifecycle.py` |
| VER-39 | The first push creates the project's `.surf-scoter.d` | Medium | Create only when nothing's there; build beside it and rename | Write with `openat`; a patch | `pull/push.py` |
| VER-40 | Removing a configuration in use | Medium | Remove only when no running environment uses it | Refuse while running; keep until pruned | `configs.py`, `run_config.py` |
| VER-41 | Reviews of nothing new train rubber-stamping | Medium | Lead with what changed around it; fold the rest | Confirm each rule; show everything | `pull/reviewer.py` |
| VER-42 | The host file shares a name with the project's files | Medium | Keep the host file beside a `files/` tree, never in it | Reserve the name; a name a project can't produce | `pull/workdir.py`, `pull/unlocked.py`, `run_config.py` |


## Open questions

* Does the review mark files as "from the template", and should it?  A label that reads as an endorsement makes rubber-stamping more likely, which is how VER-9 creeps back even with a review.  For now, the template's name is in the provenance only.
* Does the review's extra "type 'approve'" step add security over, say, requiring typing 'approve' rather than selecting option 'a'?  Can the analysis ID be useful to the user to make any meaningful decision?
