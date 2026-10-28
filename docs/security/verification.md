# Project Verification

A project you clone brings its own Surf Scoter settings: the host commands the container may run on your computer, the hosts it may reach, and the dev container it runs in.  Those settings are the biggest weak point in the tool, since you can pull an unknown configuration and run it without meaning to, on first download or on any later pull.  So the tools never act on them directly.  You review them with `scoter verify`, and `scoter run` uses only the copy you accepted.

This document describes what's reviewed, where the acceptance is kept, and how a run uses it.  [Verification attack vectors](verification-attack-vectors.md) analyzes the risks and weak points.  The user's view is in [the project configuration file](../project-configuration-file.md) and [setting up a new project](../new-project-setup.md); the design, and the reasons behind it, are in [projects in v1](../contributors/projects-v1.md).


## What's reviewed

`scoter verify` takes a *snapshot* of the project's files, and shows you a review built from it:

* the project file, `.surf-scoter.d/<project-file-name>`;
* every file in each container directory the build uses (the dev container and its parents) that lies inside the real `.surf-scoter.d`;
* every file in the `dev-containers-search` directories named in `verification: path-lists: verified-containers` (empty by default).

Everything else is **trusted rather than reviewed**: containers found through `dev-containers-search` outside the project (by default, the distribution's `containers` directory), the firewall and gateway containers, the compose file, base images, and your own setup files.  The rest of the repository isn't part of the snapshot; it's what the container works on.

The review holds the project's settings resolved for your computer (each host command's program as an absolute path, each `${section:key}` worked out), with what the [checks](../system-config-file.md#host-command-checks) found as `#scoter:` comments.  You use it as it is, edit it, or deny it; pressing Enter denies.  When you've accepted the project before, the review compares with that acceptance and shows only what changed in full.

Reading the snapshot is defensive.  Each file is opened without following symlinks and checked as it's read.  Symlinks and special files are refused, and so are hard links in container directories.  Each file and the whole snapshot have a size limit (`verification: values: max-file-size` and `max-snapshot-size`).  Only the owner-execute bit of a file's mode is kept.

Everything the project wrote is cleaned before it reaches the review: control characters and terminal escape sequences are removed, and non-ASCII text is written as `\uXXXX`, so bidirectional overrides and look-alike characters show up for what they are.


## Where the acceptance is kept

Acceptances are kept in the *verified store*, `verification: paths: store` (default `~/.config/surf-scoter.d/verified`), never in the project.  [The system configuration file](../system-config-file.md#the-verified-store) describes its layout.  The parts that matter for security:

* **It's private.**  Directories are mode `0700` and files `0600`, owned by you.  Every read checks the owner and mode, and refuses symlinks.  Writes go to a temporary file that's renamed into place.  An operating system lock on the store's `lock` file keeps two `scoter` commands from interleaving.  On Windows, the mode checks are left to the profile's access control lists.
* **It's outside the project.**  The generator refuses a store, or a state directory, inside the project.
* **It's keyed by the environment id**, made from the project directory's name and a hash of its real path.  A second clone, a worktree or a moved directory gets its own record, and a new review.
* **It's kept as data plus blobs.**  Each acceptance has the settings you accepted (`accepted.json`), the project's suggestion you reviewed, the review as you left it, and the snapshot's manifest.  File contents are kept as blobs named by their SHA-256.
* **One acceptance per branch.**  A new acceptance replaces the branch's last one.


## How a run uses it

Before `scoter run` builds anything:

1. The generator parses the live project file and `dev-env.yaml` files.
2. `find-acceptance` takes a fresh snapshot and looks for an acceptance that matches.  First it tries an exact match (the same bytes), then a match on *meaning* (see below), then an [exemption](#exemptions).  A match on meaning stores a *carried* acceptance for the new files, with the same settings and expiry, without asking.
3. Without a usable match, the run stops, says why (never accepted, expired, your setup changed, or which files changed), and tells you to run `scoter verify`.
4. The host commands and egress rules come only from the accepted settings, never from the project file.
5. The run restores the accepted snapshot into a private staging directory, and runs the generator on that copy.  If the copy doesn't parse to the same settings as step 1, the run stops.  The dev container and its parents inside `.surf-scoter.d` are built from the copy, so a change to the live files after the check never reaches the build.

### Matching on meaning

Some changes don't change what a file does, and asking for a new review for each of them would teach people to accept reviews without reading them.  So, alongside each file's SHA-256, the snapshot keeps a hash of its meaning for some kinds of file:

* **The Dockerfile** (named `Dockerfile`, the one file the build reads, as `docker: runs: build` names it with `-f`): comment lines and blank lines are dropped, except inside heredocs; everything else, parser directives and trailing whitespace included, is kept as written, and CRLF line endings count as LF.  It fails closed: a Dockerfile with a control character (a lone carriage return, say), a Unicode line separator, a `<<` that isn't a heredoc it can follow, or a heredoc left open has no meaning hash, so it matches on its bytes only, and any change to it asks for a review.  Files named `Dockerfile.*` or `*.Dockerfile` are files like any other.
* **`.json` files**: parsed, and written back in a canonical form.
* **The project file, and the `dev-env.yaml` files the dev container is merged from** (the one at the top of each container directory it builds from): the generator's parsed data.  A file of that name anywhere else is just a file, hashed by its bytes.

Two snapshots whose files all have the same meaning match.  This is the part of verification that has to agree exactly with how Docker and the generator read the same files; see [VER-1 and VER-2](verification-attack-vectors.md#ver-1-the-dockerfile-meaning-match-can-be-fooled).

### Which commands need an acceptance

Only `scoter run` refuses to start without one.  Some commands load the project without the accepted settings, because their job is to manage them: `verify`, `revoke`, `kill` and `exempt` (listed in [`subcmd-no-acceptance.txt`](../../scripts/subcmd-no-acceptance.txt)).  Others don't load a project at all: `cleanup`, `init` and `list-accepted` ([`subcmd-no-project.txt`](../../scripts/subcmd-no-project.txt)).  The rest load the accepted settings, which can store a carried acceptance.


## Exemptions

While you work on a project's container, each change to a `Dockerfile` would need another review.  `scoter exempt` opens a window, on one project and one branch, in which runs don't stop for that:

* Runs use the **container files as they are**, copied off without links or special files, and checked by the `project-files` checks.
* The **project file and `dev-env.yaml` files stay as accepted**: they decide the host commands, the egress rules, the mounts and the user, so their changes are ignored, and each run says which it's ignoring.
* `scoter verify --with-exemption` accepts the project's current suggestion **without showing it**, as long as the checks refuse nothing.  It's meant for automation.
* The window lasts `verification: values: exempt-days` (1) unless `--for` says otherwise, at most `max-exempt-days` (7), and never past the acceptance's expiry.  `scoter exempt --end`, or revoking the acceptance, ends it.

An exemption hands out trust on purpose; see [VER-4](verification-attack-vectors.md#ver-4-exemptions-accept-what-nobody-reviewed).


## Expiry, revoking and cleaning up

* An acceptance lasts `verification: values: expiry-days` (30).  The permissions a project's cloud services grant can change while an old acceptance stays the same, so an acceptance shouldn't last forever.
* A change to the setup values an acceptance recorded (the gateway it was accepted for, the project's VCS, the verified containers) asks for a new review.
* `scoter revoke` removes acceptances by branch, by fingerprint, by id or all, and `scoter list-accepted` lists them, with any exemption.
* `scoter cleanup` removes acceptances of projects whose directory is gone, and store files nothing uses.  See [cleaning up](../cleaning-up.md).
