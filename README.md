# surf-scoter-env

Secure environment for local development.

Developers need a way to run all their tooling, use their IDE of choice, and help prevent them from common security mistakes.  Due to pressure from malware and agents, the development environment increasingly needs to be treated like production from a cybersecurity perspective.  Additionally, with increasingly strict regulations and compliance requirements, we need ways to make sure we aren't putting ourselves in hot water.

Surf Scoter has you run your environment inside a container, using another container like a VPN to control network connectivity, and a small access module to enable limited external access to your host.

Surf Scoter has your back, but it can never prevent social engineering.  In the end, the weak point is you.


## Security Notes

### Review the project configuration file

If you plan on running the Surf Scoter dev-environment from a unknown source, you need to review the project configuration file.  This file defines how the container can communicate outside its sandbox, which means it can do things you're not ready for.  You can read details on do's and don'ts with this file in the [documentation](docs/project-configuration-file.md).

### Don't run git or your IDE on the workspace from your host

Part of running Surf Scoter copies your project directory into a workspace directory.  This allows the dev container to make alterations to it without affecting your local copy.

While this helps keep a strong security setup, you need to bring those changes back into your local copy.  However, you need to take care here.  If an agent or malicious package modified the hooks in that directory, then running commands there could trigger them to run on your host computer.

For strongest safety, treat the workspace as untrusted from the host side:

- Use your IDE through its remote connection to the container, not by opening the workspace folder on the host.
- Bring changes back by fetching from the workspace into your own repository, then review them before merging or cherry-picking:

  ```sh
  cd ~/src/my-project
  git fetch /path/to/workspace/my-project my-branch
  git log -p HEAD..FETCH_HEAD
  ```

  Fetching reads the workspace repository's objects without running its hooks or configured commands.
- Review what you bring back as untrusted code, especially build scripts, before you build or run it on your host.

## Using Surf Scoter

Surf Scoter is still in early development stages.  At the moment, you'll need to download the project locally and make some updates for your local configuration.  Future work will aim to make this more turn-key.  Please add your experiences and customizations to the discussions!

### User Setup

To run Surf Scoter, you need to make some configuration settings to allow for an easier connection between your development container environment and your desktop.

You can perform the changes easily with the [`user-setup.sh`](user-setup.sh) script.  This will:

- Create a new SSH private + public key in your `~/.ssh` directory.
- Alter your `~/.ssh/config` file to add a `Host scoter-*` entry, which covers every environment you run.  It causes the ssh connection to route through `docker` to connect to the underlying sshd server.  This allows the container to receive SSHD connections without opening a port to your host computer.

The script won't do anything unless you pass in the `--force` argument.  Messing with your SSH configuration is nerve-wracking, so the script holds your hand.  Running the script without the `--force` argument shows you what it would do, but doesn't do it.

You only need to do this once.  Each environment you start gets a name (`run-container.sh -n=NAME`, `dev` by default), and you connect to it as `scoter-NAME`.  To run several environments at the same time, give each its own name; each gets its own containers and network.  You can reuse your environments, so you may find yourself coming up with fun names for each one.

### Container Environment Setup

To start up your development environment, you'll need to do several things:

1. Create a `Dockerfile` that describes your environment.  Don't know how to do that?  That's fine.  You can use a blank template and fill it in later.  This just makes the initial setup automated.
2. Create a [`allowed-domains.txt`](containers/network-gateway/allowed-domains.txt) file.  This tells the environment what DNS sites it can make outbound HTTPS connections to.
3. Build the [`dev-env`](containers/dev-env/Dockerfile) parent container.  Your `Dockerfile` should use that in its `FROM` line.  This does the SSH setup and other lock down.
4. Create your [`.scoter-config.yaml`](docs/configuration-file.md) file.

With this, you're ready to run the [`run-container.sh`](run-container.sh) script to get Scotering!

### Connecting through VS Code

VS Code connects to the container over SSH, using the `Host` entry that [`user-setup.sh`](user-setup.sh) adds to your `~/.ssh/config`.  The connection runs through `docker exec`, so the container doesn't open any ports on your computer.

1. Install Microsoft's [Remote - SSH](https://marketplace.visualstudio.com/items?itemName=ms-vscode-remote.remote-ssh) extension.
2. Add these to your VS Code user settings (`settings.json`):

   ```jsonc
   {
       // Don't automatically expose services running in the container on your computer.
       "remote.autoForwardPorts": false,
       // Download the VS Code server on your computer and copy it into the container,
       // since the container can't reach Microsoft's download servers.
       "remote.SSH.localServerDownload": "always",
       // Download extensions on your computer and copy them into the container, for the same reason.
       "remote.downloadExtensionsLocally": true,
       // Skip the prompt asking what operating system the container runs.
       // Add a line for each environment name you use.
       "remote.SSH.remotePlatform": {
           "scoter-dev": "linux"
       }
   }
   ```

3. Start the environment with [`run-container.sh`](run-container.sh).
4. Run **Remote-SSH: Connect to Host...** from the Command Palette, and enter `scoter-NAME` for your environment (`scoter-dev` unless you chose another name).
5. Open the `/workspace` folder.

Some things to keep in mind:

- **Forward ports by hand, and only while you need them.**  To try out a server you're developing, use **Forward a Port** in the Ports view.  A forwarded port lets anything in the container serve content to your computer's `localhost`, so close it when you're done.
- **Be careful with recommended extensions.**  The workspace can list extensions for VS Code to recommend (`.vscode/extensions.json`), and anything in the container can edit that list.  Some extensions run on your computer rather than in the container, so only install ones you'd install anyway.
- **Tasks and terminals run in the container.**  That's what keeps them contained, so don't work around it by opening the workspace folder on your computer (see [Security Notes](#security-notes)).

### Connecting through JetBrains

### Running commands on the host

Some actions need privileges the container deliberately doesn't have, such as cloud credentials or access to the Docker daemon.  For these, the container can ask the host to run a command for it.  The host only runs the exact command forms you list in its configuration file, and refuses everything else.

Because these commands run on your host with your privileges, every file they're given is restricted to the workspace (or a throwaway temporary directory).  A path argument is mapped from the container to the host and resolved, following symlinks, and it must still be inside the workspace.  The command's working directory follows the same rules.

Some suggestions for what to allow running on the host:

- **Only list commands that truly need the host's privileges.**  If a command works inside the container, run it there.  If it's something you can run on your host yourself, do that instead.  Every command you add is a way for whatever runs in the container, such as an agent or malware, to act on your host.
- **Put hard limits on files.** If you allow, say `docker compose up` with a file that the dev container can control, then you've opened yourself up to a root escape.  It's much safer to require the files to be ones the container can't edit.  This goes for commands like `make`, `npm run`, `terraform`, `docker build`, `bash`, and other ones that allow the caller to bypass your controls.


## "Surf Scoter"?

Yes, Surf Scoter.  That's not a typo.  It's a [large sea duck](https://en.wikipedia.org/wiki/Surf_scoter).

## License

[MIT](LICENSE)

