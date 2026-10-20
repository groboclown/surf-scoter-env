# Setting up a project to work with Surf Scoter


### Container Environment Setup

To start up your development environment, you'll need to do several things:

1. Create a `.surf-scoter.d` directory in your project, and a container directory inside it, such as `.surf-scoter.d/dev-env`.  Everything Surf Scoter reads from your project lives in `.surf-scoter.d`.
2. In that container directory, create a `Dockerfile` that describes your environment.  Don't know how to do that?  That's fine.  You can use a blank template and fill it in later.  This just makes the initial setup automated.
3. Create a [`allowed-domains.txt`](../containers/network-gateway/allowed-domains.txt) file.  This tells the environment what DNS sites it can make outbound HTTPS connections to.
4. Build the [`dev-env`](../containers/dev-env/Dockerfile) parent container.  Your `Dockerfile` should use that in its `FROM` line.  This does the SSH setup and other lock down.
5. Create your [project configuration file](project-configuration-file.md), `.surf-scoter.d/project.yaml`.

With this, you're ready to run the [`scoter`](scoter.py) script to get Scotering!
