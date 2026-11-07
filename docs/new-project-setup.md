# Setting up a project to work with Surf Scoter


### Starting from a template

For a project with no Surf Scoter files yet, start from the distribution's template:

```shell
scoter init           # Or `scoter init NAME` for another template.
```

This never writes into your project.  It puts the template into your unlocked directory (it says where): a configuration of your own, for you to edit.  `scoter run` runs it as it is, with a warning that it's unreviewed, so you can try each change.  The template assumes Git (`vcs: git`) and the distribution's `dev-env` container as its parent; change what doesn't suit.

### Container Environment Setup

To build the environment by hand, or to change the template's:

1. A container directory, such as `dev-container`, beside the project file.  Everything Surf Scoter reads from your project lives in `.surf-scoter.d`.
2. In that container directory, a `Dockerfile` that describes your environment, starting `FROM` the [`dev-env`](../containers/dev-env/Dockerfile) parent container, which does the SSH setup and other lock down, and a `dev-env.yaml` naming it as the parent (see [the developer environment Dockerfile](project-dockerfile.md)).
3. The hosts the environment may make outbound connections to, as `egress` rules in the project file (see [Egress](project-configuration-file.md#egress)).
4. The [project configuration file](project-configuration-file.md), `project.yaml`.

### Locking and pushing

When it works, review and store it:

```shell
scoter lock           # Review what you made, and store it as your configuration.
scoter push           # Write it into the project's `.surf-scoter.d`, to commit.
```

`scoter lock` shows the same review a pull does; until you approve it, nothing is stored.  `scoter lock --revert` throws your edits away instead.  `scoter push` shows the change as a patch and asks before writing; it never deletes a file and never writes your host file (where each command's program is on your computer).  Commit the result with your VCS: from then on, others `scoter pull` it.

### Working on the Setup

Your configuration is yours: the project's files only suggest one.  To change it, unlock it, edit and run the copy, and lock it again:

```shell
scoter unlock         # A copy of your configuration in use, to edit.
# ... edit, and `scoter run`, as often as you like ...
scoter lock           # Review and store it; or `scoter lock --revert`.
scoter push           # Offer it to the project, if it's yours.
```

While it's unlocked:

* **Runs use the copy as it is**, staged when each run starts, with a warning before each that it's unreviewed.  The setup's checks still run, and anything they refuse still stops the run.  Host commands are found on your computer as the run starts, and listed.
* **`scoter pull` and `scoter push` refuse**, so a pull never drops your edits, and only reviewed files reach the project.
* **After a while the warning gets louder** (`pull: values: unlock-reminder-days`), listing what the configuration allows; a setup can also stop runs after `unlock-days`.  `scoter lock`, then `scoter unlock`, starts the count again.

`scoter list` shows your configurations, `scoter switch NAME` uses another (a pull keeps the one it replaced as `default.previous`), and `scoter remove` removes them.  See [Cleaning up after Surf Scoter](cleaning-up.md) to clear out the images and state your runs leave.
