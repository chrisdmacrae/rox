# rox

Start a disposable project workspace in a Linux container or a Tart VM. `rox` clones the current project's remote branch, runs its `.rox.sh` inside the guest (or reuses a project image that already ran it), and creates a Rex session rooted at the checkout. The guest keeps running after the CLI exits.

A guest doesn't have to belong to one project. Start one with no project, start as many separate guests for the same project as you like, or clone more projects into a guest that's already running with `rox add`.

Rox registers each guest with Rex as soon as its server answers. If cloning or setup fails, you can still reach the guest with `rex -S NAME ls`. Rox prints the host label and private socket address when it registers the guest. Rex.app builds that keep their own host list don't pick up `rex hosts add`. To add a guest to the Rex app on this machine, run `rox add-remote-host NAME`. It copies the guest's address, its tailnet name when it has one, and opens **Add Remote Host** so you can paste it. It works for guests on other Rex hosts too, when they're on the tailnet.

## Install the AI skill

The [rox skill](skills/rox/SKILL.md) teaches a controller agent to launch guests, delegate tasks through Rex, track worker output and exit status, and collect results before teardown.

The skill ships with each release. After you [install rox](#install), install it for each agent set up on this machine, Claude Code and Codex, or pick one with `--agent claude` or `--agent codex`:

```sh
rox skill install
```

Start a new session and invoke `/rox` in Claude Code or `$rox` in Codex. `rox self-update` and automatic updates keep the skill up to date. Install the toolkit itself on the machine that will run the guests. To have Claude Code or Codex ready in every guest, see [Agent CLIs in guests](#agent-clis-in-guests).

## Install

Install the latest release into `~/.local/share/rox` and link `rox` into `~/.local/bin`:

```sh
curl -fsSL https://github.com/chrisdmacrae/rox/releases/latest/download/rox.tar.gz -o /tmp/rox.tar.gz \
  && rm -rf ~/.local/share/rox && mkdir -p ~/.local/share/rox ~/.local/bin \
  && tar -xzf /tmp/rox.tar.gz -C ~/.local/share/rox --strip-components=1 \
  && ln -sf ~/.local/share/rox/rox ~/.local/bin/rox && rm /tmp/rox.tar.gz
```

A release install keeps itself up to date: before a command, at most every six hours, rox asks GitHub for the latest release. When one is out, it downloads it, checks it against the release's `rox.tar.gz.sha256`, swaps it in, updates the rox skill for each agent that has it, and runs your command with the new version. If it can't write to its install folder, or it's a Git checkout, it says the release is out instead. Run `rox self-update` to update now, or `rox self-update --check` to only check. Set `"updates": "notify"` in the rox config to be told without installing, or `"off"` to stop checking. `ROX_UPDATES` overrides it for one command. You can also run the same command to upgrade. Make sure `~/.local/bin` is on your PATH. To install a specific version, replace `latest/download` with `download/vX.Y.Z`. Each release also includes `rox.tar.gz.sha256` to check the download, and the installed version is in `~/.local/share/rox/VERSION`.

Releases are built by `.github/workflows/release.yml` when you push a version tag:

```sh
git tag v0.1.0 && git push origin v0.1.0
```

## Start a container

The host needs Python 3.9+, Git, OpenSSH 8.2+, Rex, and a container engine. On an Apple Silicon Mac, Rox uses Apple's [container](https://github.com/apple/container) tool by default, which runs each container in its own lightweight VM. Start its services with `container system start`. On Linux and Windows hosts it uses Docker or Podman. There's nothing to build by hand. The first `rox up container` builds the `rox:linux` base image with your container engine, and later launches reuse it. When a new rox release changes the image, Rox rebuilds it on the next launch. To choose an engine, set `"engine"` to `container`, `docker`, or `podman` under `container` in the config, or pass `--engine` at launch. Apple's container gives each container 1 GB of memory by default, so set `"memory": "4G"` (and optionally `"cpus"`) under `container` in the config for heavier agents. For project dependencies, use a [project startup hook](#project-startup-hook).

With Apple's container, guests can run Docker, so `docker compose up` works for dependencies such as Postgres. The `rox:linux` image ships Docker Engine and the Compose plugin, and the workspace user is in the `docker` group. Each Apple container is its own VM, so Rox gives it every Linux capability and starts Docker inside it. Neither reaches your Mac. Published ports open on the guest's `localhost`. Docker and Podman containers share the host's kernel, so Rox doesn't start Docker in them. Docker isn't running while a project image builds, so start Compose services from `.rox.sh` only when `ROX_PHASE` is `boot`, or from a Rex terminal.

Run either command from a Git checkout or any subdirectory within it. Each run starts a new, separate guest, so run it again for another isolated copy of the same project.

```sh
rox up container
rox up container --branch feature/agent -- my-agent --model example
```

Replace `my-agent --model example` with a command installed in your guest. Arguments after `--` go to the first Rex terminal. Without a command, the terminal opens a shell.

## Guests without a project, and several projects per guest

Run `rox up` outside a Git checkout, or pass `--no-project`, to start a guest with no checkout. Its Rex session opens in the guest user's home directory.

```sh
rox up container --no-project
```

`rox add NAME` clones a project into a guest that's already running. It resolves the project the same way `rox up` does: from the current checkout, or from `--repo` and `--branch` (the project name defaults to the repository name). It runs the project's `.rox.sh` unless you pass `--no-hook`, then creates a Rex session rooted at the new checkout. Arguments after `--` go to that session's first terminal.

```sh
rox add rv-host-3f9a1c2b7d40
rox add rv-host-3f9a1c2b7d40 --repo git@github.com:organization/api.git --branch main -- my-agent
```

Each project gets its own checkout under `~/.rox/NAME/projects/ID` in the guest. `ID` is the project name, with a number added when the guest already has a checkout by that name, so adding the same project twice gives two separate checkouts. A project added with `rox add` always runs `.rox.sh` at boot, because the guest's image is already fixed. If cloning or the hook fails, that project is marked failed and the guest stays usable.

## Launch on a Rex remote host

Pick a host label from `rex hosts list`. Install `rox` on that host's Rex terminal PATH and prepare the runtime you want to use there. The machine issuing the command needs Rex connectivity to that host.

```sh
rox up container --host buildbox
rox up macos -S studio -- my-agent --model example
```

Rex starts a session on the selected host with a **Provision** tab. It registers the guest as a Rex host, but its private socket exists only on that machine. To reach remote guests directly, configure `tailscale` in the selected host's rox config (see [Reaching guests from other devices](#reaching-guests-from-other-devices)). Your machine then also adds the guest to its own Rex hosts by its tailnet name, and `rox down NAME --host HOST` removes both. Once provisioning succeeds, the Provision session closes. When the guest is on the tailnet and the Rex app is open on your machine, Rox opens the guest's own Rex session in the app, so new tabs and splits there run in the guest. Otherwise it prints `rox shell NAME`. Any command after `--` runs in the guest's own Rex session. If provisioning fails, the Provision tab stays open to inspect.

Rox resolves the source repository and branch from your current checkout before dispatching. The selected host uses its own config, runtime images, and SSH keys. Overrides such as `--config`, `--git-key`, and `--git-known-hosts` are paths on that host. Quote paths containing a leading `~` to have the selected host expand it.

To launch from a Rex terminal without a source checkout, supply the repository URL and branch, and optionally a project name. Pass `--no-project` instead for a guest with no checkout. `rox add NAME --host LABEL` adds a project to a guest on that host in the same way.

```sh
rox up container --host buildbox --project my-project \
  --repo git@github.com:organization/my-project.git --branch main
rox add rv-host-3f9a1c2b7d40 --host buildbox
```

`rox shell`, `rox forward`, `rox reconnect`, `rox start`, `rox stop`, `rox down` and `rox add` work on a guest on any of your Rex hosts. When this machine has no record of the guest, Rox finds the host that runs it, the way `rox ls --all` does, or you can name it with `-S HOST`. `rox shell` opens in your terminal when the guest is on the tailnet, and in the Rex app otherwise. `rox forward` needs the guest on the tailnet. See [Rex CLI host selection](https://www.superlogical.com/rex/docs/reference/cli#rex-hosts) for host labels.

## Private repositories

Manage the Git SSH key paths on the machine that launches the guest.

```sh
rox key add ~/.ssh/project_readonly
rox key remove ~/.ssh/project_readonly
```

The commands update `git_keys` in `${XDG_CONFIG_HOME:-$HOME/.config}/rox/config.json`. Removing a path leaves the key file on disk. Use `--config PATH` with the key command and with `rox up` to use an alternate file. Run `rox config` to open the file in `$VISUAL` or `$EDITOR`. See [config.example.json](config.example.json) for other settings.

- `git_keys` is a list of host-side SSH private key paths. Prefer read-only deploy keys. Without `git_keys`, Rox uses your default SSH keys in `~/.ssh`, the ones `git clone` would use. Set `"git_keys": []` to clone with no key. Repeat `--git-key PATH` to supply a different list for one launch. Encrypted keys can prompt for a passphrase on the host.
- `git_known_hosts` or `--git-known-hosts PATH` supplies verified host keys for your Git servers. When omitted, Rox looks up the matching host and port in the launch machine's `~/.ssh/known_hosts`. Guest SSH still verifies the server's key.
- The tool uses the current branch's upstream remote and branch when configured. Otherwise it uses `origin` and the current branch name. Use `--remote NAME` or `--repo URL` to override the remote and `--branch NAME` to choose an existing remote branch. The checkout contains the remote branch tip. Push local commits first if you need them in the guest, or pass `--local` to copy the checkout as it is: its unpushed commits and its uncommitted and untracked files, but not gitignored ones. With `--local`, the branch doesn't need to be on the remote yet, `.rox.json` is read from the working tree, and `.rox.sh` runs at boot instead of from a project image.

For an HTTPS remote that requires authentication, supply its SSH clone URL.

```sh
rox up container \
  --repo git@github.com:organization/project.git \
  --git-key ~/.ssh/project_readonly
```

Only the selected keys are loaded into an SSH agent on the host, one per guest. Rox forwards it into the guest after the project hook, so `git fetch`, `pull` and `push` work in every guest terminal for the guest's whole life. The guest can use the keys but never reads them: Git private keys stay on the host. Anything in the guest can use them while the forward runs, so prefer deploy keys. Pass `--no-git-agent`, or set `"git_agent": false`, to forward them only while cloning. After a host restart, an encrypted key needs `rox reconnect NAME` in a terminal to load its passphrase again.

## Tart VMs

On an Apple Silicon Mac, install Tart and prepare one template per guest OS. With the Rex app installed, `rox template macos` (or `rox template macos --host HOST`) builds the macOS one: it pulls `ghcr.io/cirruslabs/macos-tahoe-base:latest` into `rox-macos`, authorizes a VM login key, pins the host key, replaces the default password while keeping auto-login working, installs Rex, checks that it boots to SSH and its desktop, and fills in the `macos` config. Run it again after upgrading Rex.

`rox template linux` does the same for Linux: Ubuntu 24.04 by default, `--distro debian` or `--distro fedora`, or any Tart image with `--base`, such as an Arch one. Each gets Git and Rex, lingering, key-only SSH and one host key shared by all Linux templates, so `rox up linux --image rox-debian` can pick any of them.

To prepare a template by hand instead:

```sh
tart clone ghcr.io/cirruslabs/ubuntu:24.04 rox-ubuntu
tart clone ghcr.io/cirruslabs/macos-tahoe-base:latest rox-macos
```

Boot each template once with `tart run TEMPLATE` and prepare it from its console. If you change a macOS template's password, also update `/etc/kcpassword`, which auto-login reads, or the template boots to the login window.

1. Enable SSH. On macOS, use **System Settings → General → Sharing → Remote Login**. On Ubuntu, install `openssh-server`, then run `sudo systemctl enable --now ssh`. Use key-based login and change the default image password before using the template. SSH must allow local Unix-socket forwarding and, for private Git clones, SSH agent forwarding.
2. Generate a VM login key on the host with `ssh-keygen -t ed25519 -f ~/.ssh/rox_vm`. Add the public key to the guest user's `~/.ssh/authorized_keys` and set permissions to `700` for `~/.ssh` and `600` for the file. Keep the private key on the host and set `identity` to its path. If it has a passphrase, load it into your regular host SSH agent before launch.
3. Install Git, Bash, and Rex in the guest and verify `rex version` as the SSH user. For Ubuntu, use the official Rex Linux installer. For macOS, install the official Rex app. If the CLI is missing from SSH's PATH, link its bundled executable into the guest user's `~/.local/bin`.

   ```sh
   mkdir -p ~/.local/bin
   ln -s '/Applications/Rex.app/Contents/Helpers/Rex Server.rex/Contents/MacOS/rex' ~/.local/bin/rex
   ~/.local/bin/rex version
   ```

   On Ubuntu, enable lingering for the guest account with `sudo loginctl enable-linger "$USER"`. [Rex installation](https://www.superlogical.com/rex/docs/installation) · [Rex over SSH](https://www.superlogical.com/rex/docs/linux/remote-access)
4. From the guest console, read its SSH host public key, typically `/etc/ssh/ssh_host_ed25519_key.pub`. Save that public key to a file on the host and set `host_key` to its path. Use your own prepared template and keep its host key stable across clones. Configure `image` and `user` for that template and verify that it boots to SSH unattended. Shut the template down before launching clones. [Tart template and SSH setup](https://tart.run/quick-start/)

Launch from a project checkout.

```sh
rox up linux
rox up macos
```

## Project startup hook

Commit `.rox.sh` at the root of the remote branch. Rox runs it with Bash from the guest checkout and waits for it to finish. A nonzero exit leaves the workspace in a failed state and prevents the initial Rex session from starting. Use `--no-hook` to skip it for a launch.

```bash
#!/usr/bin/env bash
set -euo pipefail

case "$ROX_BACKEND" in
  container|linux) ./scripts/setup-linux.sh ;;
  macos) ./scripts/setup-macos.sh ;;
esac
```

The hook receives `ROX_BACKEND`, `ROX_NAME`, `ROX_PROJECT_DIR`, and `ROX_PHASE`. The hook runs as the guest's workspace user. It can install project dependencies or write config. Changes made with `export` inside the hook affect only that process and its children.

### Project images

When the branch has a `.rox.sh`, Rox runs it once while building a project image and reuses that image for later cold starts. A launch from the image skips the clone and the hook. It fetches the branch into the prepared checkout and moves it to the branch tip. Untracked and ignored files the hook created, such as `node_modules`, stay in place.

| Backend | Image | Built by |
| --- | --- | --- |
| `container` | `rox:REPO` | A generated Containerfile `FROM` the configured image, labelled with the hook, base image, and repository |
| `linux`, `macos` | `rox-REPO-BACKEND` | Booting a clone of the configured Tart template, cloning and running the hook over SSH, then stopping and renaming it |

Tart names cannot contain `:`, so VM images use a dash and include the backend. To decide whether the image is current, Rox fetches the branch into a cached mirror under `${XDG_CACHE_HOME:-$HOME/.cache}/rox/repos` on the launch machine. It uses the same keys and pinned host keys as the guest. Rox rebuilds the image when `.rox.sh` changes, when the base image changes, or when the repository URL differs. A new commit that leaves `.rox.sh` unchanged reuses the image.

While the image builds, `ROX_PHASE` is `build`, `ROX_NAME` is the image name, the checkout is at `~/.rox/image/project`, and neither `REX_SERVER` nor a Git agent is available. At boot without an image, `ROX_PHASE` is `boot` and `REX_SERVER` points to the guest's running server. Because the image does not keep running processes, a hook that starts Rex terminals for long-running services should launch with `--no-prebuild`.

| Option | Effect |
| --- | --- |
| `--rebuild` | Rebuild the project image before launching, for example after dependency manifests change. |
| `--no-prebuild` | Clone and run `.rox.sh` at boot, as before. Set `"prebuild": false` under a backend in the config to make this the default. |
| `--no-hook` | Skip the hook and project image and start from the base image. |

If the launch machine cannot read the branch, Rox prints a warning and runs the hook at boot. If the hook fails during a build, the launch stops. Remove a project image with `container image delete rox:REPO`, `docker rmi rox:REPO`, `podman rmi rox:REPO`, or `tart delete rox-REPO-BACKEND`.

## Per-project and per-launch guest settings

Commit a `.rox.json` at the root of the branch to set the project's backend and guest settings. Rox reads it from the launched branch, like `.rox.sh`.

```json
{
  "backend": "container",
  "container": { "image": "ghcr.io/acme/agent-base:2", "cpus": 4, "memory": "8G" }
}
```

With a `backend` there, plain `rox up` picks it. The [Project settings guide](docs/src/content/docs/guides/project-settings.mdx) covers the full format. Each backend section accepts `image`, `cpus`, `memory`, and `prebuild`. Machine-specific settings, such as `engine`, the VM `user`, `identity` and `host_key`, and Git keys, stay in the launcher's config, and Rox rejects them in `.rox.json`.

To change these for your own launches without committing anything, put the same settings in a gitignored `.rox.local.json` at the root of your checkout. It overrides `.rox.json`, and with `--host` it's sent along from the machine you run `rox up` on.

For a one-off guest, override any of these at launch. Command-line options win over `.rox.local.json`, which wins over `.rox.json`, which wins over the launcher config.

```sh
rox up linux --image rox-ubuntu-cuda --cpus 8 --memory 16G
rox up container --image ghcr.io/acme/agent-base:3 --memory 8G
```

`--cpus` and `--memory` work with every backend. Containers get them as run limits, and Tart VMs get them through `tart set` on the clone before it boots.

## Reaching guests from other devices

By default a guest's Rex server is reachable only on its launcher, through a Unix socket. To reach guests from any device, including the Rex app, run `rox config tailscale` on the launcher and paste a Tailscale auth key. It saves the key where only you can read it and adds a `tailscale` section to the launcher's config. Rox then puts each guest's Rex server on your tailnet as its own device, named after the workspace, and registers that name as the host's second address. After `rox up --host`, the machine you launched from registers the guest by that name too. Other devices can reach it at the tailnet name.

```json
{ "tailscale": { "auth_key_file": "~/.config/rox/tailscale-authkey", "tags": ["tag:rox-guest"] } }
```

Use a reusable, ephemeral auth key tagged `tag:rox-guest`, stored in a file only you can read, and tailnet ACLs that let your devices reach `tag:rox-guest` without letting guests reach anything. An agent in the guest can use the guest's tailnet identity, so the tag's ACLs are what keep guests isolated. Pass `--no-tailscale` to keep one guest off the tailnet. The [tailnet guide](docs/src/content/docs/guides/tailnet.mdx) has the policy and key setup.

## Agent CLIs in guests

Rox can install Claude Code and Codex in every guest, signed in with your credentials. On the machine that runs the guests:

```sh
claude setup-token && rox config agents claude   # paste the token, or an Anthropic API key
codex login && rox config agents codex           # copies ~/.codex/auth.json, or pass --api-key
```

The credentials go to each guest over SSH on stdin and are never written into an image. The guest's Rex server and shells read them, so `rex run -- claude ...` works with no sign-in. Each agent follows its latest release: Rox checks at `rox up`, at most every six hours, and rebuilds only a last `-agents` layer of the container image when a new one comes out. Pin a version with `rox config agents codex --version 0.171.0`. Update guests that are already running with `rox update-agents NAME` or `rox update-agents --all`. Guests also get a copy of your skills and your `CLAUDE.md` or `AGENTS.md` from the launcher. Add network MCP servers and settings with `rox config agents claude --sync skills,instructions,mcp,settings`. The [agent CLIs guide](docs/src/content/docs/guides/agent-clis.mdx) has the details.

## Keeping guests running

When the Mac reboots, or Apple's container services restart, guests stop, and Apple's `container` doesn't start them again. `rox shell`, `rox forward`, `rox add` and `rox reconnect` start the runtime and the guest if they stopped, then reopen the tunnel and the guest's Rex server. To bring guests back without running anything, install the rox service on the machine that runs them:

```sh
rox service install
```

It checks every 15 seconds, logs to `~/.local/state/rox/service.log`, and leaves alone guests you stopped with `rox stop`. The [guide](docs/src/content/docs/guides/keep-running.mdx) has the details.

## Workspace commands

| Command | Effect |
| --- | --- |
| `rox ls` | List local workspace records, their provisioning state, IP address (Tart VMs and Apple containers), and each guest's projects. In a checkout, it lists only the guests with that project; add `--all-projects` for every guest. Add `--all` to include the guests on every Rex host you can reach, `--host HOST` for one host, or `--json`. |
| `rox add NAME` | Clone a project into a running guest and start a Rex session at its checkout. |
| `rox shell NAME [PROJECT]` | Open an interactive shell at a project checkout. Without `PROJECT`, it opens the guest's only checkout, or the guest home when there are none or several. |
| `rox agent [NAME] [PROJECT]` | Start Claude Code or Codex in a new Rex session at a checkout in a running guest. Without `NAME`, it picks the running guest with the current project, here or on a Rex host, matching by folder, repository name or remote URL, and offers to start one with `rox up` when there's none. `--local` first copies this checkout's changes into the guest's checkout, refusing when the guest has working changes. Pick one with `--agent` when the guest has both, pass the agent's arguments after `--`, and add `--open-shell` to show it in the Rex app. |
| `rox forward NAME PORT...` | Forward `localhost` ports on the launcher to servers on the guest's `localhost`, for example dev servers. Use `LOCAL:GUEST` to pick a different local port. Runs until Ctrl-C. |
| `rox funnel add NAME PORT` | Put a server in the guest on the internet with Tailscale Funnel, through the launcher's Tailscale. `--https 8443` or `10000` and `--path` pick the public address. `rox funnel remove NAME PORT` (or `--all`) stops it, and `rox funnel ls [NAME]` lists funnels. See the [tailnet guide](docs/src/content/docs/guides/tailnet.mdx#sharing-a-guests-server-on-the-internet). |
| `rex -S NAME ls` | List the guest's Rex sessions after a local launch with host registration. |
| `rox reconnect NAME` | Restore Rex access to a guest, starting the container runtime and the guest first if they stopped. Also works for one retained after clone or setup failure. |
| `rox open NAME [PROJECT]` | Show the guest's own Rex session in the Rex app on this machine, so splits and new tabs run in the guest. If the app doesn't have the guest as a host yet, it copies the guest's address and opens Add Remote Host for you to paste it. |
| `rox add-remote-host NAME` | Add a guest to the Rex app on this machine, for app builds that keep their own host list. |
| `rox stop NAME` / `rox start NAME` | Stop a guest without deleting it, and keep it stopped until `rox start`. |
| `rox self-update` | Install the latest rox release and update the rox skill. `--check` only checks. Rox also does this on its own; see [Install](#install). |
| `rox service install` | Keep guests running across reboots and container runtime restarts. See [Keeping guests running](#keeping-guests-running). |
| `rox down NAME` | Delete the guest, its filesystem, and its Rex host entry. |
| `rox down` | From a project checkout, remove the guest that has that project. If several do, Rox lists them and asks which one to remove. |

Rex connects over a private Unix socket forwarded through SSH. If socket forwarding fails, Rox relays connections over authenticated SSH sessions. Docker and Podman containers publish only their SSH port to loopback. Apple's containers publish no ports, and Rox reaches them at their own address on the Mac's container network. Workspace state, Tart boot logs, and SSH tunnel diagnostics live under `${XDG_STATE_HOME:-$HOME/.local/state}/rox`. A failed launch prints the workspace name so you can find its records or remove it.
