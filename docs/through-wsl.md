# Running Surf Scoter on Windows through WSL2

So you have a WSL2 environment on your Windows computer.  Your WSL2 environment has all the prerequisites installed, and you can run `scoter` to bring up a project environment.  Fantastic!

But how do I get a Windows program to connect?

`scoter` will do several things to configure your WSL2 environment to allow you to connect to it... from within WSL2.  Unfortunately, at this time, there's no simple configuration adjustment to have it update your Windows environment, too.

So, here's the steps to get it working.  First off, we need some of your Windows configuration settings:

* Let's say your Windows login name is `jane`.  Any time you see `jane` in the configuration below, replace that with your Windows login.
* This means you have a Windows "home" directory at `C:\Users\jane`.
* Your WSL2 distribution is named `wsl-ubuntu`.  That means you can launch a new session in that environment with `C:\Windows\System32\wsl.exe -d wsl-ubuntu`.
* You run containers in your WSL2 session using docker, found under `/usr/sbin/docker`.

When `scoter run` starts, it tells you the environment's host name: `scoter-` and the project's id, such as `scoter-my-project-c593c7ec2d55`.  If you look at your WSL2 session, at your `~/.ssh/config` file, the environment's entry tells you its port number, the container's username and the SSH key location:

```shell
$ grep -A 8 "Host scoter-my-project-c593c7ec2d55" ~/.ssh/config
Host scoter-my-project-c593c7ec2d55
  # Surf Scoter environment; see `ssh: values: config-cleanup`.
  User ubuntu
  IdentityFile /home/jane/.ssh/scoter-dev-container
  IdentitiesOnly yes
  StrictHostKeyChecking yes
  ProxyCommand docker exec -i %h socat STDIO TCP:127.0.0.1:20000
```

This tells us it's running user `ubuntu` on port `20000`, and the private SSH key is at `~/.ssh/scoter-dev-container`.  The port can change from run to run, if another environment is running.

You can also find the host fingerprint for that session by looking in your `~/.ssh/known_hosts` file:

```shell
grep -E "^scoter-my-project-c593c7ec2d55 " ~/.ssh/known_hosts
scoter-my-project-c593c7ec2d55 ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFxnu2CtdL0PdsqsgQSfQuxO0drqIhP0ZM73PQUxP0Xc
```

The host key stays the same from run to run of the project: it's kept with the project's workspace.

We now have all the pieces necessary to get started.

## Configure your Windows ssh `config`

Modify your `C:\Users\jane\.ssh\config` file:

```
Host scoter-my-project-c593c7ec2d55
  User ubuntu
  IdentityFile ~/.ssh/scoter-dev-container
  IdentitiesOnly yes
  StrictHostKeyChecking yes
  ProxyCommand C:\Windows\System32\wsl.exe -d wsl-ubuntu -e /usr/sbin/docker exec -i %h socat STDIO TCP:127.0.0.1:20000
```

Let's take that line by line.

- `Host scoter-my-project-c593c7ec2d55`
    - Declares the host block.
    - The environment's host name, the same as in WSL2.  Programs like VS Code use this file to offer host names.
- `User ubuntu`
    - Declares the username SSH will use to connect to this host.
    - Copied directly from the WSL2 `config` file.
- `IdentitiesOnly yes` and `StrictHostKeyChecking yes`
    - These go unchanged from the WSL2 `config` file.  They provide good security practice.
- `ProxyCommand C:\Windows\System32\wsl.exe -d wsl-ubuntu -e /usr/sbin/docker exec -i %h socat STDIO TCP:127.0.0.1:20000`
    - That's the big one.  Let's break this one line into pieces.
    - `C:\Windows\System32\wsl.exe` - your Windows WSL executable, that starts a WSL2 session.
    - `-d wsl-ubuntu` - tell WSL to run your `wsl-ubuntu` distribution.
    - `/usr/sbin/docker` - the Docker executable within that WSL2 distribution.
    - `socat STDIO TCP:127.0.0.1` - this is some magic to run a `docker exec` into the running container, and, within that, connect to the loopback, directing all console input and output through that port.  It's how we can connect to the container without opening a host port.
    - `:20000` - connect to port `20000`, the listening SSHD server.

Whew!

## Set up Windows `known_hosts`

The Windows side needs the exact same line in the `C:\Users\jane\.ssh\known_hosts` that we extracted above.

If you want a short-cut, then, from your WSL2 session:

```shell
grep -E "^scoter-my-project-c593c7ec2d55 " ~/.ssh/known_hosts >> /mnt/c/Users/jane/.ssh/known_hosts
```

You might not have your Windows drive mounted in your WSL2 session, in which case you need to hand-copy that file (`/mnt/c` is where WSL2 mounts your Windows `C:` drive if you have it enabled).

## Copy the scoter dev container keys

The SSHD server only allows logins through a key pair.  The container setup already populated the scoter side with the public key, and now we need to tell the Windows side the private key.

From your WSL2 session:

```shell
cp ~/.ssh/scoter-dev-container /mnt/c/Users/jane/.ssh/.
```
