# Running Surf Scoter on Windows through WSL2

So you have a WSL2 environment on your Windows computer.  Your WSL2 environment has all the prerequisites installed, and you can run `scoter` to bring up a project environment.  Fantastic!

But how do I get a Windows program to connect?

`scoter` will do several things to configure your WSL2 environment to allow you to connect to it... from within WSL2.  Unfortunately, at this time, there's no simple configuration adjustment to have it update your Windows environment, too.

So, here's the steps to get it working.  First off, we need some of your Windows configuration settings:

* Let's say your Windows login name is `jane`.  Any time you see `jane` in the configuration below, replace that with your Windows login.
* This means you have a Windows "home" directory at `C:\Users\jane`.
* Your WSL2 distribution is named `wsl-ubuntu`.  That means you can launch a new session in that environment with `C:\Windows\System32\wsl.exe -d wsl-ubuntu`.
* You run containers in your WSL2 session using docker, found under `/usr/sbin/docker`.

If you look at your WSL2 session, at your `~/.ssh/config` file, it tells you the port number for the `scoter` setup, as well as the container's username and the SSH key location:

```shell
$ grep -A 8 "Host scoter-" ~/.ssh/config
Host scoter-ubuntu-20000-*
  # For Surf Scoter dev container user ubuntu on port 20000
  User ubuntu
  IdentityFile ~/.ssh/scoter-dev-container
  IdentitiesOnly yes
  StrictHostKeyChecking yes
  ProxyCommand docker exec -i %h socat STDIO 'TCP:127.0.0.1:20000'
```

This tells us it's running user `ubuntu` on port `20000`, and the private SSH key is at `~/.ssh/scoter-dev-container`.

You can also find the host fingerprint for that session by looking in your `~/.ssh/known_hosts` file:

```shell
grep -E "^scoter-ubuntu-20000-" ~/.ssh/known_hosts
scoter-ubuntu-20000-a ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFxnu2CtdL0PdsqsgQSfQuxO0drqIhP0ZM73PQUxP0Xc
```

This tells us that it's running as host `scoter-ubuntu-20000-a`.

We now have all the pieces necessary to get started.

## Configure your Windows ssh `config`

Modify your `C:\Users\jane\.ssh\config` file:

```
Host scoter-ubuntu-20000-a
  User ubuntu
  IdentityFile ~/.ssh/scoter-dev-container
  IdentitiesOnly yes
  StrictHostKeyChecking yes
  ProxyCommand C:\Windows\System32\wsl.exe -d wsl-ubuntu -e /usr/sbin/docker exec -i %h socat STDIO TCP:127.0.0.1:20000
```

Let's take that line by line.

- `Host scoter-ubuntu-20000-a`
    - Declares the host block.
    - Taken from the hostname pulled out of `known_hosts`.
    - Normally, `scoter` will use a `-*` at the end.  Programs like VS Code will use this file to pre-load host names, so replacing the `-*` with the real suffix makes those tools work easier.  You can keep the `-*`, though, if you prefer.
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
grep -E "^scoter-ubuntu-20000-a" ~/.ssh/known_hosts > /mnt/c/Users/jane/.ssh/known_hosts
```

You might not have your Windows drive mounted in your WSL2 session, in which case you need to hand-copy that file (`/mnt/c` is where WSL2 mounts your Windows `C:` drive if you have it enabled).

## Copy the scoter dev container keys

The SSHD server only allows logins through a key pair.  The container setup already populated the scoter side with the public key, and now we need to tell the Windows side the private key.

From your WSL2 session:

```shell
cp ~/.ssh/scoter-dev-container /mnt/c/Users/jane/.ssh/.
```
