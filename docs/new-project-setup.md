# Setting up a project to work with Surf Scoter


### Container Environment Setup

To start up your development environment, you'll need to do several things:

1. Create a `Dockerfile` that describes your environment.  Don't know how to do that?  That's fine.  You can use a blank template and fill it in later.  This just makes the initial setup automated.
2. Create a [`allowed-domains.txt`](../containers/network-gateway/allowed-domains.txt) file.  This tells the environment what DNS sites it can make outbound HTTPS connections to.
3. Build the [`dev-env`](../containers/dev-env/Dockerfile) parent container.  Your `Dockerfile` should use that in its `FROM` line.  This does the SSH setup and other lock down.
4. Create your [`.scoter-config.yaml`](docs/configuration-file.md) file.

With this, you're ready to run the [`scoter`](scoter.py) script to get Scotering!
