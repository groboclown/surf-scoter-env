# Integration Tests

End-to-end runs of the `scoter` tools, from a pull to a running environment, with sample projects.  They're run by hand: nothing in the top-level `Makefile` runs them, as some build images, pull from the network and start containers.

Each test is a directory with a number in front of its name, holding a `run.sh` and the sample files it uses.  Run one from anywhere:

```shell
./integration-tests/01-pull-and-remove/run.sh
```

It prints each step, and ends with `PASS` or `FAIL`.

## What they need

* The debug build of the host tools: `cd host && cargo build`.
* `python3`, `git`, and `script` (from util-linux, to give the pull's reviewer a terminal).
* For the test that runs containers (`03`): the container tool, with `compose`, and the network.

## The sandbox

A test never touches your own setup.  Each one makes a private temporary directory (under `TMPDIR`), and runs the tools with `HOME` set inside it, so their setup file, ssh key and configuration, state, verified store and unlocked copies all go there.  Its distribution is this checkout: the debug binaries, the scripts, the containers and the templates are linked in, and the distribution's `setup.yaml` and `scoter` are copied.  The sample project is copied into the sandbox too (as a Git repository, for the tests that need one), so the tests don't write into this repository.

The container tool is the exception: the sandbox's setup runs it through a small wrapper with your own `HOME`, so it uses your image store and registry logins, rather than pulling every image again.  The containers it starts are named after the sandbox's environments, and the tests stop them.

The sandbox is removed when the test ends, unless it fails (it says where it is, to look at), or `KEEP_SANDBOX=1` is set.

| Variable | Default | What it does |
|---|---|---|
| `KEEP_SANDBOX` | (unset) | Set to keep the sandbox after a passing test. |
| `IT_CONTAINER_TOOL` | `docker` | The container tool to run: `docker`, `podman`, or anything that takes their commands. |
| `IT_RUN_TIMEOUT` | `1800` | How long `03` waits for the environment to start, in seconds.  The first run builds the images. |

## The tests

* **`01-pull-and-remove`**: the run refuses a project with no configuration; `scoter pull` without a terminal approves nothing; then the pull's plumbing: `seed` (a working directory of `files/` and `host.json`), `analyze` (the setup refuses the project's shell), `approve` refused, the copy edited, `changed` naming the file, the old analysis refused, a new one approved; `scoter list`; `scoter remove $(scoter list -q)`.  No containers.
* **`02-re-pull`**: pulls a project (as committed), and changes it.  A run still builds the configuration in use, and says the project's files changed since the last pull; a pull of the committed files finds nothing new; `--working-tree` pulls the change, flagging what isn't committed, and keeps the old configuration as `default.previous`, which `scoter switch` goes back to.  No containers: the container tool is a stand-in that keeps what a build was given, and fails it.
* **`03-run-and-kill`**: `scoter user-setup`, pulling a project that uses the distribution's `dev-env` container, then a real `scoter run`.  Once the environment is up, it checks that the gateway lets out the allowed host (`registry.npmjs.org`) and not another (`example.com`).  Then it kills the run as a crash would, leaving its containers running, and checks that `scoter kill` stops them and forgets the environment.
* **`04-lock-and-unlock`**: pulls 02's sample project, unlocks it, edits the copy, and runs it (with the unlocked warning); a pull and a push refuse while unlocked; `scoter lock --revert` asks, then throws the edit away; an edit is locked, replacing the configuration it came from; `scoter push --yes` writes it into the project (not the host file), and the next run doesn't take it for a change; `scoter review` approves it again.  No containers, as in 02.
* **`05-cleanup`**: pulls 02's sample project, then moves it and pulls it again, and leaves a run's files, a project state directory, a persistent store and an image behind.  `scoter cleanup --dry-run` lists them; without a terminal or `--force`, and answering no, nothing goes; `--force` removes the run's files and the gone directory's configuration, but not the project state, store or image; `--project-state --persistent-stores --images` removes those too.  No containers: the container tool is a stand-in that lists one image and one volume, and records what it's asked.
* **`06-init`**: `scoter init` refuses a project with its own files; for one without, it starts a configuration from the distribution's template without writing into the project; a run uses the unlocked copy; `scoter lock` stores it, finding the project by its configuration; the first `scoter push` creates the project's files directory.  No containers: the container tool's builds fail.

## Adding a test

Make a directory with the next number, and a `run.sh` that sources the shared functions:

```shell
#!/bin/sh
. "$( dirname "$0" )/../common/lib.sh"

it_init                                   # the sandbox
it_project "${IT_TEST_DIR}/project" git   # copy in the sample project (as a Git repository)
it_approve pull                           # pull it, approving the review as it stands
scoter list > "${IT_SANDBOX}/list.log"
it_expect "${IT_SANDBOX}/list.log" "some text"
it_pass
```

[`common/lib.sh`](common/lib.sh) has the rest: `it_overlay` (copy changed files over the project), `it_expect_not`, `it_say`, `it_fail` and `it_require`.  Keep each test's files in its own directory, and the shared scripts in `common`.
