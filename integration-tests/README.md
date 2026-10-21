# Integration Tests

End-to-end runs of the `scoter` tools, from a review to a running environment, with sample projects.  They're run by hand: nothing in the top-level `Makefile` runs them, as some build images, pull from the network and start containers.

Each test is a directory with a number in front of its name, holding a `run.sh` and the sample files it uses.  Run one from anywhere:

```shell
./integration-tests/01-verify-and-revoke/run.sh
```

It prints each step, and ends with `PASS` or `FAIL`.

## What they need

* The debug build of the host tools: `cd host && cargo build`.
* `python3`, `git`, and `script` (from util-linux, to give the review prompt a terminal).
* For the test that runs containers (`03`): the container tool, with `compose`, and the network.

## The sandbox

A test never touches your own setup.  Each one makes a private temporary directory (under `TMPDIR`), and runs the tools with `HOME` set inside it, so their setup file, ssh key and configuration, state and verified store all go there.  Its distribution is this checkout: the debug binaries and the scripts are linked in, and the distribution's `setup.yaml` and `scoter` are copied.  The sample project is copied into the sandbox too (as a Git repository, for the tests that need one), so the tests don't write into this repository.

The container tool is the exception: the sandbox's setup runs it through a small wrapper with your own `HOME`, so it uses your image store and registry logins, rather than pulling every image again.  The containers it starts are named after the sandbox's environments, and the tests stop them.

The sandbox is removed when the test ends, unless it fails (it says where it is, to look at), or `KEEP_SANDBOX=1` is set.

| Variable | Default | What it does |
|---|---|---|
| `KEEP_SANDBOX` | (unset) | Set to keep the sandbox after a passing test. |
| `IT_CONTAINER_TOOL` | `docker` | The container tool to run: `docker`, `podman`, or anything that takes their commands. |
| `IT_RUN_TIMEOUT` | `1800` | How long `03` waits for the environment to start, in seconds.  The first run builds the images. |

## The tests

* **`01-verify-and-revoke`**: the run refuses a project nobody accepted; the review refuses to ask without a terminal; the review file (a refused shell commented out, with its override); accepting it; `list-accepted`; a second review comparing with it; `scoter revoke $(scoter list-accepted -q)`.  No containers.
* **`02-re-review`**: accepts a project, changes it (`changed/` over `project/`), and checks the next review: what's unchanged on one line, what's new and what changed in full, the Dockerfile's difference; the run refusing the changed files; accepting the re-review.  No containers.
* **`03-run-and-kill`**: `scoter user-setup`, accepting a project that uses the distribution's `dev-env` container, then a real `scoter run`.  Once the environment is up, it checks that the gateway lets out the accepted host (`registry.npmjs.org`) and not another (`example.com`).  Then it kills the run as a crash would, leaving its containers running, and checks that `scoter kill` stops them and forgets the environment.
* **`04-exempt`**: accepts 02's sample project, starts `scoter exempt`, and changes the Dockerfile, the project file and `dev-env.yaml`.  A run on the branch builds the changed Dockerfile with the accepted `dev-env.yaml`, and says which changes it ignores; another branch isn't exempt; `scoter verify --with-exemption` refuses the suggestion while the checks refuse its shell, then accepts it without, and the next run uses the new `dev-env.yaml`; `scoter exempt --end`.  No containers: the container tool is a stand-in that keeps what a build was given, and fails it.
* **`05-cleanup`**: accepts 02's sample project, then moves it and accepts it again, and leaves a run's files, a workspace and an image behind.  `scoter cleanup --dry-run` lists them; without a terminal or `--force`, and answering no, nothing goes; `--force` removes the run's files and the gone directory's acceptance, but not the workspace or image; `--workspaces --images` removes those too.  No containers: the container tool is a stand-in that lists one image, and records what it's asked.

## Adding a test

Make a directory with the next number, and a `run.sh` that sources the shared functions:

```shell
#!/bin/sh
. "$( dirname "$0" )/../common/lib.sh"

it_init                                   # the sandbox
it_project "${IT_TEST_DIR}/project" git   # copy in the sample project (as a Git repository)
it_accept                                 # use its review as it stands
scoter verify --output "${IT_SANDBOX}/review.yaml"
it_expect "${IT_SANDBOX}/review.yaml" "some text"
it_pass
```

[`common/lib.sh`](common/lib.sh) has the rest: `it_overlay` (copy changed files over the project), `it_expect_not`, `it_say`, `it_fail` and `it_require`.  Keep each test's files in its own directory, and the shared scripts in `common`.
