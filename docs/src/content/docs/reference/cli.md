---
title: CLI
description: Every rox command and option.
---

## `rox up [BACKEND]`

Start and provision a new guest. `BACKEND` is `container`, `linux`, `macos`, or `fly`. Leave it out to use the `backend` from the branch's [`.rox.json`](../../guides/project-settings/). Arguments after `--` run in the guest's first Rex terminal. Each run starts a new guest, even for a project that already has one.

Rox clones the project from the current checkout. Outside a checkout, with no `--repo`, or with `--no-project`, it starts a guest with no project, whose Rex session opens in the guest user's home directory.

```sh
rox up container [options] [-- COMMAND [ARG ...]]
```

| Option | Description |
| --- | --- |
| `-S`, `--host HOST` | Launch on this [Rex remote host](../../guides/remote-hosts/). |
| `--config PATH` | Config file on the machine that runs the guest. |
| `--cwd DIR` | A directory inside the source project. Defaults to the current directory. |
| `--project NAME` | Project name for a launch without a checkout. Requires `--repo` and `--branch`. Outside a checkout, the name defaults to the repository name. |
| `--no-project` | Start a guest without a project, even from inside a checkout. |
| `--image IMAGE` | Container image or prepared Tart template. Overrides the config and `.rox.json`. |
| `--cpus N` | CPUs for this guest. Overrides the config and `.rox.json`. |
| `--memory SIZE` | Memory for this guest, with a unit, such as `8G` or `512M`. Overrides the config and `.rox.json`. |
| `--engine container\|docker\|podman` | Container engine. Defaults to `container` (Apple's container tool) on Apple Silicon Macs and `docker` elsewhere. |
| `--remote NAME` | Git remote to clone from. Can't be combined with `--repo`. |
| `--repo URL` | Repository URL to clone. Must be SSH or HTTPS. |
| `--branch NAME` | Remote branch to clone. |
| `--git-key PATH` | Git SSH identity. Repeat it for several keys. Replaces `git_keys` from the config. |
| `--git-known-hosts PATH` | `known_hosts` file for your Git SSH servers. |
| `--boot-timeout SECONDS` | Timeout for each guest readiness check. Defaults to `120`. |
| `--no-hook` | Skip the project's `.rox.sh` and the project image. |
| `--local` | Copy this checkout into the guest as it is: its HEAD, commits the remote doesn't have, and uncommitted and untracked files. Gitignored files stay behind. The guest still clones from the remote first, so `origin` works for pushing, and the branch tracks the remote branch once it exists there. `.rox.json` is read from the working tree, `.rox.sh` runs at boot on the copied files, and no project image is used. Can't be combined with `--host`, `--project` or `--no-project`. |
| `--no-prebuild` | Clone and run `.rox.sh` at boot instead of using a [project image](../../guides/project-images/). |
| `--rebuild` | Rebuild the project image before launching. |
| `--open-shell` | Open the guest's own Rex session in the Rex app on this machine, like `rox open`. Without a Rex app here, open a Guest tab running `rox shell` in the current Rex session instead. Requires a Rex terminal. |
| `--no-tailscale` | Keep this guest off the tailnet even when `tailscale` is configured. See [Reaching guests from other devices](../../guides/tailnet/). |
| `--no-git-agent` | Give the guest Git keys only while it clones, not for its whole life. See [Private repositories](../../guides/private-repositories/). |
| `--no-agents` | Don't install the configured [agent CLIs](../../guides/agent-clis/) or their credentials in this guest. |

## `rox add NAME`

Clone a project into the running guest `NAME`, run its `.rox.sh`, and create a Rex session rooted at the checkout. Arguments after `--` run in that session's first terminal. The checkout goes to `~/.rox/NAME/projects/ID` in the guest, where `ID` is the project name with a number added if the guest already has it.

```sh
rox add NAME [options] [-- COMMAND [ARG ...]]
```

It accepts `-S`/`--host`, `--config`, `--cwd`, `--project`, `--remote`, `--repo`, `--branch`, `--git-key`, `--git-known-hosts`, `--no-hook`, `--local`, and `--open-shell`, which work as they do for `rox up`. A project is required. Project images don't apply, so `.rox.sh` always runs at boot. If cloning or the hook fails, the project is marked failed and the guest stays ready.

## `rox ls`

List local workspace records with their backend, provisioning state, IP address, and projects. The IP address is where the launcher can reach servers in a Tart VM or an Apple container, such as `http://IP:5173` for a dev server listening on `0.0.0.0`. Docker and Podman containers show `-`, because they're reached only through a loopback SSH port. The address is the one recorded at launch or the last `rox reconnect`. Each project shows as `ID:BRANCH`, followed by its state if it isn't ready. A guest with no project shows `-`.

Each launcher keeps its own workspace records, so plain `rox ls` shows only the guests launched on this machine.

| Option | Description |
| --- | --- |
| `-a`, `--all` | Also list the guests on every Rex host this machine can reach. Rox runs `rox ls` on each host in a short-lived Rex session and adds a `HOST` column. It skips rox guests in your host list, other names for this machine's own server, and hosts it can't reach or that don't have rox, with a note on stderr for each. |
| `-S`, `--host HOST` | List the guests on this Rex remote host instead. |
| `--json` | Print the workspaces as JSON, with `name`, `backend`, `engine`, `phase`, `ip`, `tailnet`, and `projects`, plus `host` with `--all` or `--host`. |

## `rox shell NAME [PROJECT]`

Open an interactive login shell with `REX_SERVER` pointing at the guest's server. `PROJECT` is a project ID or name from `rox ls`. Without it, the shell opens at the guest's only checkout, or in the guest home when it has no projects or several. The guest must be ready, and not stopped with `rox stop`. If the guest stopped or lost its tunnel, for example after a reboot, Rox [starts it again](../../guides/keep-running/) first. If this machine has no record of `NAME`, Rox finds the Rex host that runs it and works there. See [Working with guests on other hosts](../../guides/remote-hosts/#working-with-guests-on-other-hosts).

| Option | Description |
| --- | --- |
| `-S`, `--host HOST` | The Rex host that runs the guest. Without it, Rox looks for the guest on this machine, then on each Rex host. |

## `rox agent [NAME] [PROJECT]`

Start an agent CLI in a new Rex session in a running guest, at a project checkout. Arguments after `--` go to the agent, such as a prompt. The guest's own sessions and projects are left as they are, so you can start several agents in one guest.

```sh
rox agent [NAME] [PROJECT] [--agent claude|codex] [--local] [--open-shell] [-- ARG ...]
```

Without `NAME`, Rox looks at the running guests on this machine and on each Rex host. Inside a checkout it picks among the guests with a checkout of that project and starts there. A guest's checkout counts when its name matches the checkout's folder or a remote's repository name, or its URL matches one of the checkout's remotes, so a second checkout of a repository under another folder name finds the same guest. When none has it, Rox asks at a terminal whether to start one with `rox up` (with `--local` and `--host` carried over) and then starts the agent in it. Outside a checkout, any running guest will do. With one candidate Rox uses it, and with several it asks, or lists them when there's no terminal. `rox agent PROJECT` picks among the guests with that project.

`PROJECT` picks the checkout the way it does for `rox shell`. Without `--agent`, Rox starts the guest's only [agent CLI](../../guides/agent-clis/) and asks you to choose when it has both. The session stays open after the agent exits. Rox prints the new `Session` ID with the `Workspace` and `Checkout`, so a controller can drive it with `rex -S NAME`. If the guest stopped or lost its tunnel, Rox starts it again first. If this machine has no record of `NAME`, Rox finds the Rex host that runs it and starts the agent there.

| Option | Description |
| --- | --- |
| `--agent claude\|codex` | The agent CLI to start, even one the guest got some other way, such as from `.rox.sh`. |
| `--local` | First bring the guest's checkout of this project to this checkout's HEAD and uncommitted files, as `rox add --local` does. Rox refuses when the guest's checkout has working changes, or commits on the branch that this checkout lacks, unless its files already match this checkout's. Only for guests on this machine. |
| `--cwd DIR` | The directory within the current project, instead of the working directory. |
| `--open-shell` | Show the new session in the Rex app on this machine. For a guest on another host, the guest must be on the tailnet. |
| `-S`, `--host HOST` | The Rex host that runs the guest. Without it, Rox looks for the guest on this machine, then on each Rex host. |

## `rox forward NAME PORT...`

Forward ports on the launcher's `localhost` to servers listening on the guest's own `localhost`, over the guest's SSH connection. It works with every backend and engine, including Docker and Podman containers, which have no address of their own. The dev server can keep listening on `localhost`, and you open it at `http://localhost:PORT`.

Give each port as `PORT` to use the same number on both sides, or `LOCAL:GUEST` to choose the launcher's port. For example, `rox forward rv-app-1a2b3c4d5e6f 5173 8080:3000` opens `http://localhost:5173` and `http://localhost:8080`. The forward runs in the foreground until you press Ctrl-C, and it exits if a local port is already taken. Forwarded ports bind only to `127.0.0.1` on the launcher, and the guest can't open tunnels back to it. The guest must be ready, and not stopped with `rox stop`. If the guest stopped or lost its tunnel, for example after a reboot, Rox [starts it again](../../guides/keep-running/) first. For a guest on another host, Rox tunnels through the guest's Rex server on the tailnet. If this machine has no record of `NAME`, Rox finds the Rex host that runs it and works there. See [Working with guests on other hosts](../../guides/remote-hosts/#working-with-guests-on-other-hosts).

| Option | Description |
| --- | --- |
| `-S`, `--host HOST` | The Rex host that runs the guest. Without it, Rox looks for the guest on this machine, then on each Rex host. |

Containers started from a `rox:linux` image built before `rox forward` existed refuse the tunnel. Rox rebuilds `rox:linux` on the next `rox up container`, so start a new guest to use it.

## `rox funnel NAME [PORT]`

Put a server listening on `PORT` in the guest on the public internet with Tailscale Funnel, and print its URL. The launcher's Tailscale serves it at the launcher's tailnet name, through an SSH tunnel to the guest's `localhost`, so the server can listen on `localhost`. The launcher needs Tailscale installed and Funnel allowed by your tailnet policy. Without `PORT`, list the guest's funnels. Funnels come back when the guest restarts, go offline at `rox stop` until `rox start`, and are removed by `rox down`. See [Sharing a guest's server on the internet](../../guides/tailnet/#sharing-a-guests-server-on-the-internet).

| Option | Description |
| --- | --- |
| `--https PORT` | Public HTTPS port: `443` (the default), `8443` or `10000`. |
| `--path PATH` | Public path to serve it at. Defaults to `/`. |
| `--off` | Stop funneling `PORT`, or every funnel of the guest when you leave out `PORT`. |
| `-S`, `--host HOST` | The Rex host that runs the guest. Without it, Rox looks for the guest on this machine, then on each Rex host. |

## `rox reconnect NAME`

Restore Rex access to a guest. This also works for a guest kept after a failed clone or setup. Rox starts the container runtime and the guest if they stopped, restarts the guest's Rex server if needed, reopens the tunnel, rejoins the tailnet, and registers the host again. A guest stopped with `rox stop` is started again. If this machine has no record of `NAME`, Rox finds the Rex host that runs it and works there. See [Working with guests on other hosts](../../guides/remote-hosts/#working-with-guests-on-other-hosts).

| Option | Description |
| --- | --- |
| `-S`, `--host HOST` | The Rex host that runs the guest. Without it, Rox looks for the guest on this machine, then on each Rex host. |

## `rox start NAME` / `rox stop NAME`

`rox stop` closes the guest's tunnel and stops its container or VM without deleting anything. The guest stays stopped until `rox start`: `rox ls` shows it as `stopped`, the [rox service](../../guides/keep-running/) leaves it alone, and `rox shell`, `rox forward` and `rox add` refuse it. `rox start` starts the guest and restores access to it, like `rox reconnect`, without the Rex app prompt. If this machine has no record of `NAME`, Rox finds the Rex host that runs it and works there. See [Working with guests on other hosts](../../guides/remote-hosts/#working-with-guests-on-other-hosts).

| Option | Description |
| --- | --- |
| `-S`, `--host HOST` | The Rex host that runs the guest. Without it, Rox looks for the guest on this machine, then on each Rex host. |

## `rox update-agents NAME` / `rox update-agents --all`

Bring a running guest's [agent CLIs](../../guides/agent-clis/) up to date. Rox checks for the latest releases, installs any version the guest lacks, writes the current credentials from the config again, and copies the launcher's current skills, instructions, and chosen MCP servers and settings. New shells get changed credentials right away, and workers that `rex run` starts get them after `rox stop` and `rox start`. `--all` updates every running guest on this machine. If this machine has no record of `NAME`, Rox finds the Rex host that runs it and works there.

| Option | Description |
| --- | --- |
| `--all` | Update every running guest on this machine, or on `--host`. |
| `-S`, `--host HOST` | The Rex host that runs the guest. Without it, Rox looks for the guest on this machine, then on each Rex host. |
| `--config PATH` | Config file on the machine that runs the guest. |

## `rox open NAME [PROJECT]`

Show the guest's own Rex session in the Rex app on this machine, so new tabs and splits there run in the guest. Without `PROJECT`, Rox opens the session of the guest's only project, or the guest's own session. When the app doesn't have the guest as a host, Rox copies the guest's address and opens **Add Remote Host**, like `rox add-remote-host`, then waits up to three minutes for the app to connect to the guest before selecting the session. A guest on another Rex host must be on the tailnet. See [Working in the guest's session](../../guides/remote-hosts/#working-in-the-guests-session).

| Option | Description |
| --- | --- |
| `-S`, `--host HOST` | The Rex host that runs the guest. Without it, Rox looks for the guest on this machine, then on each Rex host. |

## `rox add-remote-host NAME`

Add a guest to the Rex app on this machine. Rex.app builds that keep their own host list don't pick up `rex hosts add`, and their **Add Remote Host** dialog takes no arguments, so Rox copies the guest's address to the clipboard and opens the dialog for you to paste it. The address is the guest's tailnet name, which works from the app on any device, or its socket, which works only on the machine that runs the guest. For a guest on another Rex host, the guest must be on the tailnet. The Rex app must be open on this machine. `rox up` suggests this command when it is.

| Option | Description |
| --- | --- |
| `-S`, `--host HOST` | The Rex host that runs the guest. Without it, Rox looks for the guest on this machine, then on each Rex host. |

## `rox down [NAME]`

Delete the guest, its filesystem, all of its project checkouts, and its Rex host entry. If you leave out `NAME`, Rox picks the guest that has the current project, on this machine or on any Rex host it can reach. When several do, it lists them, with the host of each one that runs elsewhere, and asks which one to remove. If this machine has no record of `NAME`, Rox finds the Rex host that runs it, removes the guest there, and removes this machine's Rex host entry for it.

| Option | Description |
| --- | --- |
| `--cwd DIR` | Directory inside the project, used when you leave out `NAME`. |
| `-S`, `--host HOST` | Remove a guest that runs on this [Rex remote host](../../guides/remote-hosts/), and this machine's Rex host entry for it. `NAME` is required. |

## `rox service install` / `uninstall` / `status`

Install a background service that [keeps guests running](../../guides/keep-running/) on this machine. Every 15 seconds it starts Apple's container services if they stopped, and restores each ready guest that stopped or lost its tunnel, the same way `rox reconnect` does. It skips guests stopped with `rox stop`. On macOS it's a launchd agent at `~/Library/LaunchAgents/io.rox.service.plist`. On Linux it's a systemd user unit at `${XDG_CONFIG_HOME:-~/.config}/systemd/user/rox.service`. Both start at login. The service records the `PATH` you install it with, so run `rox service install` again if `container`, `tart`, `rex` or `ssh` move. It logs to `${XDG_STATE_HOME:-~/.local/state}/rox/service.log`, and restarts itself when rox is upgraded.

`rox service uninstall` stops and removes the service. `rox service status` shows whether it's running. `rox service run` runs the same loop in the foreground, with `--interval SECONDS` to check more or less often.

## `rox template macos` / `rox template linux`

Prepare a [Tart template](../../guides/tart-vms/) and point the backend's config at it. Rox clones `--base` into the template unless it already exists, creates the VM login key `identity` if it's missing, boots the template, authorizes the key and replaces the base image's password. The new password is saved, readable only by you, next to the config as `TEMPLATE-USER-password`. Run the command again to update Rex in the template. It reuses the saved password and doesn't pull the image again. The template must be shut down, and the base image needs passwordless `sudo` for the user, as Cirrus Labs' images have.

**macOS** templates get this Mac's `/Applications/Rex.app`, with `rex` linked onto SSH's `PATH`. Rox keeps `/etc/kcpassword` and the login keychain in step with the new password, so the template still signs in to its desktop on boot, and reboots it to check. It pins the template's own host key at `host_key` and writes `image`, `user`, `identity` and `host_key` under `macos`.

**Linux** templates get Git, Bash and curl from the distribution's package manager (apt, dnf, pacman, zypper or apk), and Rex from its official installer on the channel this Mac runs. Rox turns on lingering so the guest's Rex server outlives the SSH session, makes sshd take only keys while allowing agent and socket forwarding, and stops cloud-init from replacing host keys. Every Linux template gets the same host key, kept next to the config as `rox-linux-host-key` and pinned at `host_key` (default `~/.ssh/rox_linux_host.pub`), so `rox up linux --image` can pick any of them. Rox then boots a throwaway clone to check that it takes SSH with the pinned key. It sets `linux.image` only when none is set, or with `--default`.

| Option | Description |
| --- | --- |
| `-S`, `--host HOST` | Prepare the template on this Rex remote host. |
| `--config PATH` | A different rox config file, on the machine that runs the guests. |
| `--name NAME` | Template name. Defaults to `macos.image`, then `rox-macos`, for macOS, and to `rox-DISTRO` for Linux. |
| `--distro NAME` | Linux only. `ubuntu` (24.04, the default), `debian` (trixie) or `fedora` (42) pick Cirrus Labs' image. `arch` has none, so pass `--base` with it. |
| `--base IMAGE` | Tart image to clone from. Defaults to `ghcr.io/cirruslabs/macos-tahoe-base:latest` for macOS and the `--distro` image for Linux. With an image rox doesn't know, pass `--name` or `--distro`. |
| `--default` | Linux only. Make this template `linux.image` even when another one is set. |
| `--rex-channel NAME` | Linux only. `stable`, `preview` or `snapshot`. Defaults to the channel of this Mac's Rex. |
| `--base-password PASSWORD` | The base image's login password, which Rox replaces. Defaults to `admin`. |
| `--boot-timeout SECONDS` | Seconds per readiness check. Defaults to 600. |

## `rox key add PATH` / `rox key remove PATH`

Add or remove a Git SSH private key path in `git_keys`. Removing a path doesn't delete the key file.

| Option | Description |
| --- | --- |
| `--config PATH` | A different rox config file. |

## `rox config`

Open the rox config in `$VISUAL`, or `$EDITOR`, or `vi`, creating it as `{}` if it doesn't exist. When the editor exits, Rox checks that the file is still valid JSON.

| Option | Description |
| --- | --- |
| `--config PATH` | A different rox config file. |

## `rox config tailscale`

Ask for a Tailscale auth key, save it to `tailscale-authkey` next to the config with only you able to read it, and set the config's `tailscale` section so new guests join your [tailnet](../../guides/tailnet/). When stdin isn't a terminal, Rox reads the key from stdin. If `auth_key_file` is already set, Rox writes the key to that file. Guests that are already running stay off the tailnet until you recreate them.

| Option | Description |
| --- | --- |
| `--tag TAG` | Tag for guest devices. Repeat it for several. Defaults to `tag:rox-guest`. |
| `--control-url URL` | `https://` URL of a self-hosted control server such as Headscale. Without it, Rox keeps any `control_url` already set. |
| `--config PATH` | A different rox config file. |

## `rox config agents [claude|codex]`

Install [Claude Code or Codex](../../guides/agent-clis/) in new guests, signed in with your credentials. For `claude`, Rox asks for a token from `claude setup-token`, or an Anthropic API key, and saves it to `claude-token` or `anthropic-api-key` next to the config, readable only by you. For `codex`, Rox uses your Codex login in `~/.codex/auth.json` (or `$CODEX_HOME/auth.json`), or, with `--api-key`, asks for an OpenAI API key and saves it to `openai-api-key`. Leave a token blank to install the agent without credentials. Without an agent, Rox asks which ones to set up. When stdin isn't a terminal, Rox reads the token from stdin. Each guest also gets a copy of this machine's skills and instructions for the agent, and, if you choose, its MCP servers and settings. Guests that are already running get the change with `rox update-agents`.

| Option | Description |
| --- | --- |
| `--version VERSION` | Pin a version such as `2.1.330`, or `latest`, the default. |
| `--api-key` | `codex`: sign in with an OpenAI API key instead of the Codex login file. |
| `--auth-file PATH` | `codex`: the login file to copy into guests. Defaults to `~/.codex/auth.json`. |
| `--off` | Stop installing the agent in new guests, and delete the token Rox saved for it. |
| `--sync ITEMS` | What to [copy from this machine](../../guides/agent-clis/#your-skills-instructions-and-mcp-servers): a comma list of `skills`, `instructions`, `mcp` and `settings`, or `none`. The default is `skills,instructions`. Changing only this keeps the agent's credentials. |
| `--mcp NAME` | Copy this MCP server into guests. It must run over the network. Repeat it for more. |
| `--config PATH` | A different rox config file. |

## `rox self-update`

Install the latest rox release in place, then update the rox skill for each agent that has it. Rox checks GitHub for releases, downloads `rox.tar.gz`, checks it against `rox.tar.gz.sha256`, and swaps the whole install folder, so a failed update leaves the old one. It works on an install from a release archive, which has a `VERSION` file, and rox can write to. A Git checkout updates with `git pull`.

Rox also does this on its own before a command, checking at most every six hours, and then runs the command with the new version. A failed check or download never stops the command. Set [`updates`](../configuration/#top-level-settings) to `notify` or `off`, or `ROX_UPDATES` for one command, to change that. `rox --version` prints the installed version.

| Option | Description |
| --- | --- |
| `--check` | Only say whether a newer release is out. |

## `rox skill install`

Copy the [rox skill](../../guides/agents/) bundled with this rox version into an agent's skills folder. Without options, it installs for each agent set up on this machine: Claude Code (`${CLAUDE_CONFIG_DIR:-~/.claude}/skills/rox`) and Codex (`${CODEX_HOME:-~/.codex}/skills/rox`). `rox self-update` and automatic updates reinstall it for each agent that has it.

| Option | Description |
| --- | --- |
| `--agent claude\|codex` | Install for this agent. Repeat it for both. |
| `--dir DIR` | Install into `DIR/rox` instead, for example a project's `.claude/skills`. Can't be combined with `--agent`. |
| `--force` | Replace an existing folder named `rox` that isn't a rox skill. Without it, Rox only replaces its own earlier copy. |
