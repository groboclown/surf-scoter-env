# Setting up a project to work with Surf Scoter


### Container Environment Setup

To start up your development environment, you'll need to do several things:

1. Create a `.surf-scoter.d` directory in your project, and a container directory inside it, such as `.surf-scoter.d/dev-env`.  Everything Surf Scoter reads from your project lives in `.surf-scoter.d`.
2. In that container directory, create a `Dockerfile` that describes your environment.  Don't know how to do that?  That's fine.  You can use a blank template and fill it in later.  This just makes the initial setup automated.
3. List the hosts the environment may make outbound HTTPS connections to, as `egress` rules in the project file (see [Egress](project-configuration-file.md#egress)).
4. Build the [`dev-env`](../containers/dev-env/Dockerfile) parent container.  Your `Dockerfile` should use that in its `FROM` line.  This does the SSH setup and other lock down.
5. Create your [project configuration file](project-configuration-file.md), `.surf-scoter.d/project.yaml`.

With this, you're ready to run the [`scoter`](../scoter.py) script to get Scotering!

### Accepting the Settings

The project file is only a suggestion: nothing in it runs until you accept it.  Run `scoter verify` in the project to review its settings (the host commands and egress rules, resolved for your computer, with what the checks found) and use, edit or deny them.  Then `scoter run` starts the environment, built from the copy of `.surf-scoter.d` you accepted.

Every change to `.surf-scoter.d` after that, even one line of a `Dockerfile`, needs another `scoter verify` before the next run.  That's what keeps someone else's change from running unreviewed, but it gets in the way while you're still building the setup.

### Working on the Setup

While you're developing the container, start an exemption, so each run doesn't stop for a review:

```shell
scoter verify          # Review and accept the settings once.
scoter exempt          # For a day; or `scoter exempt --for 8h`.
```

While the exemption holds:

* **Container files are used as they are.**  Edit the `Dockerfile`, or the files it copies, and `scoter run` again: the run copies them off when it starts, and builds from that copy.  The setup's checks on the container files still run, and anything they refuse (a link, say) still stops the run.
* **The project file and the `dev-env.yaml` files stay as you accepted them.**  They decide what runs on your computer (the host commands, the egress rules, the mounts and the user), so the run ignores your changes to them, and says which it's ignoring.  To pick them up, run:

  ```shell
  scoter verify --with-exemption
  ```

  This accepts the settings as they stand, without showing you the review, as long as the checks refuse nothing; if they do, run `scoter verify` and review them.  It doesn't keep edits you made to an earlier review.  The exemption carries on, with the same end.

* **It holds only on the branch you started it on**, if the project's in a repository with branches.  On another branch, changed files need a review again.
* **A change to your own setup** (`setup.yaml`) still needs a review.

An exemption lasts a day by default, and at most a week, and never longer than the acceptance it started from; your setup can change both (`verification: values: exempt-days` and `max-exempt-days`).  Running `scoter exempt` again moves its end.  When you're done:

```shell
scoter exempt --end    # Back to a review for each change.
scoter verify          # Review what you ended up with.
```

`scoter list-accepted` shows your acceptances, and any exemption, with its end and branch.  `scoter revoke` removes an acceptance (which ends an exemption that uses it).  Each branch keeps one acceptance: a new one replaces the branch's last.

For automation, such as a script that rebuilds the environment after each change, start the exemption once, then run `scoter verify --with-exemption` before each `scoter run`.  See [Exemptions](system-config-file.md#exemptions) for the details.
