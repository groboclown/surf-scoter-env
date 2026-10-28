# Attack Vector Analysis

These pages list the places where Surf Scoter could be weak.  They're written for two readers:

* **Contributors**, who should treat changes to the code a vector names as security-sensitive, and get them reviewed with that vector in mind.
* **The red team**, who can use each vector as a starting point.  Findings go in [Red Team Events](red-team.md), citing the vector's ID.

Each area has its own page:

* [Runtime network](network-attack-vectors.md) (`NET`)
* [The development container](dev-container-attack-vectors.md) (`DEV`)
* [File access](files-attack-vectors.md) (`FILE`)
* [The host bridge](host-bridge-attack-vectors.md) (`BRIDGE`)
* [Project verification](verification-attack-vectors.md) (`VER`)

The [user and default settings](settings.md) aren't analyzed here.  They come from the developer rather than a cloned project, so they rank lower, but changes to them can break the network properties; that page has its own checklist.


## Reading a page

Each page has the same parts:

* **Attacker goals:** what an attacker in this area is after.
* **Attack surface:** each input an attacker can influence, who controls it, and the code that handles it.
* **Summary:** every vector, with its severity and status.
* **Vectors:** for each one, where it lives, what the weakness is, what the red team should try, and what to watch when changing that code.
* **Sensitive code:** the files whose changes need a security review.

### IDs

Each vector has an ID made from the area's prefix and a number, such as `FILE-2`.  IDs are stable: when a vector is fixed or no longer applies, change its status rather than renumbering, so issues and red team reports keep pointing at the right thing.

### Severity

The severity is an initial estimate of the worst realistic outcome:

| Severity | Meaning |
| --- | --- |
| **Critical** | Code execution on the host, or host files exposed, triggered by a cloned project or by the container, without any mistake by the developer. |
| **High** | Host compromise or broad exfiltration that needs a plausible condition: a particular runtime, a configuration choice, or a normal developer action. |
| **Medium** | Limited host impact, attacks from other local accounts on shared computers, or a property whose failure would be serious but hasn't been verified. |
| **Low** | Denial of service, hardening gaps, or problems contained to the container. |

### Status

| Status | Meaning |
| --- | --- |
| **Open** | A known weakness that hasn't been fixed. |
| **Unverified** | It may or may not be exploitable; it needs testing, often on several runtimes. |
| **By design** | An accepted risk that the design leaves to the developer or the configuration.  Changes that make it easier to get wrong still matter. |
| **Mitigated** | Controls exist.  It's listed because changes to that code could undo them, and the red team should keep testing them. |


## Highest priority

The Critical vectors, and the High vectors that are open or unverified:

| ID | Vector | Severity | Status |
| --- | --- | --- | --- |
| [DEV-1](dev-container-attack-vectors.md#dev-1-user-is-written-into-the-ssh-configuration) | `user` is written into the SSH configuration | Critical | Mitigated |
| [DEV-2](dev-container-attack-vectors.md#dev-2-mount-points-are-written-into-the-compose-env-file) | Mount points are written into the compose `.env` file | Critical | Mitigated |
| [FILE-1](files-attack-vectors.md#file-1-scoter-run-runs-git-inside-the-project-clone) | `scoter run` runs Git inside the project clone | Critical | Open |
| [BRIDGE-1](host-bridge-attack-vectors.md#bridge-1-allowed-commands-that-read-container-controlled-input) | Allowed commands that read container-controlled input | Critical | By design |
| [VER-1](verification-attack-vectors.md#ver-1-the-dockerfile-meaning-match-can-be-fooled) | The Dockerfile meaning match can be fooled | High | Open |
| [VER-2](verification-attack-vectors.md#ver-2-any-file-named-dev-envyaml-matches-on-meaning) | Any file named `dev-env.yaml` matches on meaning | High | Open |
| [VER-3](verification-attack-vectors.md#ver-3-the-review-doesnt-show-everything-it-accepts) | The review doesn't show everything it accepts | High | Open |
| [VER-4](verification-attack-vectors.md#ver-4-exemptions-accept-what-nobody-reviewed) | Exemptions accept what nobody reviewed | High | By design, partly Open |
| [FILE-3](files-attack-vectors.md#file-3-the-file-vcs-merge) | The `file` VCS merge | High | Open |
| [DEV-3](dev-container-attack-vectors.md#dev-3-image-builds-run-outside-the-isolation) | Image builds run outside the isolation | High | Open |
| [DEV-4](dev-container-attack-vectors.md#dev-4-host-ids-under-rootful-docker) | Host ids under rootful Docker | High | Open |
| [DEV-5](dev-container-attack-vectors.md#dev-5-the-ide-session-as-a-channel-to-the-host) | The IDE session as a channel to the host | High | Unverified |
| [NET-3](network-attack-vectors.md#net-3-dns-from-the-dev-container) | DNS from the dev container | High | Unverified |
| [BRIDGE-3](host-bridge-attack-vectors.md#bridge-3-path-checks-race-the-host-program) | Path checks race the host program | High | Open |


## Recurring themes

A few patterns show up across the areas, and are worth watching for in any new code:

* **Project values written into host files.**  Anything the project supplies that `scoter` writes into a host file or command line needs strict validation and escaping (DEV-1, DEV-2).
* **The host acting inside the project clone.**  Anything that runs on the host with the clone as its working directory, opens a path through it, or shares files with it, gives the container a way out.  The container can replace any directory in the clone with a symlink between sessions, so the host must never build a path below it (FILE-1, FILE-2, FILE-4, FILE-10).
* **Project configuration as permission.**  The allowlist, the bridge commands and the dev container all come from the project.  [Project verification](verification.md) puts a review in front of them (NET-2, BRIDGE-2, DEV-3), so the review's own gaps are now where these risks live (VER-3, VER-4).
* **Matching on meaning must agree with the consumer.**  Wherever a change is let through because it "means the same", the canonical form must drop exactly what the real consumer (Docker, the shell, the generator) ignores, and nothing more (VER-1, VER-2).
* **The review must show everything it accepts.**  A value that reaches the host but isn't on the review page is accepted unseen (VER-3, DEV-2, FILE-10).
* **State that outlives a session.**  Anything the container can leave in the project clone (its `.git`) or its persistent store is input to the next session, and the clone is input to the host (DEV-16, FILE-1).
* **Differences between runtimes.**  Rootful and rootless runtimes, Docker and Podman, and VM-based runtimes on macOS and Windows behave differently, so tests on one don't cover the others (NET-3, NET-8, DEV-4, BRIDGE-12).


## Adding a vector

1. Add it to the area's page, with the next free ID, in the same format as the others.
2. Add it to that page's summary, and to the highest priority table here if it's Critical, or High and not mitigated.
3. Add the code it names to the page's sensitive code list, if it isn't already there.
