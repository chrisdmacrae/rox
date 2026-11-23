# rox

Start a disposable project workspace in a Linux container or a Tart VM. `rox` clones the current project's remote branch, runs its `.rox.sh` inside the guest (or reuses a project image that already ran it), and creates a Rex session rooted at the checkout. The guest keeps running after the CLI exits.

A guest doesn't have to belong to one project. Start one with no project, start as many separate guests for the same project as you like, or clone more projects into a guest that's already running with `rox add`.

Rox registers each guest with Rex as soon as its server answers. If cloning or setup fails, you can still reach the guest with `rex -S NAME ls`. Rox prints the host label and private socket address when it registers the guest. Rex.app builds that keep their own host list don't pick up `rex hosts add`. When a Rex app is connected on the same machine and Rox runs in a terminal, `rox up` and `rox reconnect` offer to copy the socket address and open **Add Remote Host** so you can paste it. Pass `--no-app-host` to skip the prompt.

## Install the AI skill

The [rox skill](skills/rox/SKILL.md) teaches a controller agent to launch guests, delegate tasks through Rex, track worker output and exit status, and collect results before teardown.

From this toolkit's directory, install the skill for Codex.

```sh
skill_dir="${CODEX_HOME:-$HOME/.codex}/skills"
mkdir -p "$skill_dir"
cp -R skills/rox "$skill_dir/"
```

Start a new Codex session and invoke `$rox`. Install the toolkit itself on the machine that will run the guests, and make your chosen agent runner available inside those guests.

## Install

Install the latest release into `~/.local/share/rox` and link `rox` into `~/.local/bin`:

```sh
curl -fsSL https://github.com/chrisdmacrae/rox/releases/latest/download/rox.tar.gz -o /tmp/rox.tar.gz \
  && rm -rf ~/.local/share/rox && mkdir -p ~/.local/share/rox ~/.local/bin \
  && tar -xzf /tmp/rox.tar.gz -C ~/.local/share/rox --strip-components=1 \
  && ln -sf ~/.local/share/rox/rox ~/.local/bin/rox && rm /tmp/rox.tar.gz
```

Run the same command to upgrade. Make sure `~/.local/bin` is on your PATH. To install a specific version, replace `latest/download` with `download/vX.Y.Z`. Each release also includes `rox.tar.gz.sha256` to check the download, and the installed version is in `~/.local/share/rox/VERSION`.

Releases are built by `.github/workflows/release.yml` when you push a version tag:

```sh
git tag v0.1.0 && git push origin v0.1.0
```

## Start a container

The host needs Python 3.9+, Git, OpenSSH 8.2+, Rex, and a running local Docker or Podman engine. Make `rox` available on your PATH, or use its absolute path from your project.

Rox builds the default `rox:linux` image on first use and reuses it on later launches. The build uses the Rex Linux preview channel. To prebuild the image with an official Linux `rex` executable for your build architecture, run this from the toolkit's directory.

```sh
docker build -f Containerfile -t rox:linux \
  --secret id=rex_binary,src=/absolute/path/to/linux/rex .
```

Use `podman build` with the same arguments if you use Podman, and set `"engine": "podman"` in the config or pass `--engine podman` at launch. On macOS the engine needs a running Docker Desktop or Podman machine. You can extend the supplied image to install system packages required by your project. [Rex for Linux](https://www.superlogical.com/rex/docs/linux/installation)

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

Rex starts a session on the selected host with a **Provision** tab. It registers the guest on that machine, where the private socket exists. Once provisioning succeeds, its **Guest** tab opens an interactive shell at the guest checkout. In Rex.app, find the `rox PROJECT` session under the selected host. Any command after `--` runs in the guest's own Rex session. The Provision tab prints the command for inspecting that session from a terminal on the selected host.

Rox resolves the source repository and branch from your current checkout before dispatching. The selected host uses its own config, runtime images, and SSH keys. Overrides such as `--config`, `--git-key`, and `--git-known-hosts` are paths on that host. Quote paths containing a leading `~` to have the selected host expand it.

To launch from a Rex terminal without a source checkout, supply the repository URL and branch, and optionally a project name. Pass `--no-project` instead for a guest with no checkout. `rox add NAME --host LABEL` adds a project to a guest on that host in the same way.

```sh
rox up container --host buildbox --project my-project \
  --repo git@github.com:organization/my-project.git --branch main
rox add rv-host-3f9a1c2b7d40 --host buildbox
```

Run `rox ls`, `rox shell NAME`, `rox reconnect NAME`, and `rox down NAME` from a terminal on the machine that launched the guest. [Rex CLI host selection](https://www.superlogical.com/rex/docs/reference/cli#rex-hosts)

## Private repositories

Manage the Git SSH key paths on the machine that launches the guest.

```sh
rox key add ~/.ssh/project_readonly
rox key remove ~/.ssh/project_readonly
```

The commands update `git_keys` in `${XDG_CONFIG_HOME:-$HOME/.config}/rox/config.json`. Removing a path leaves the key file on disk. Use `--config PATH` with the key command and with `rox up` to use an alternate file. See [config.example.json](config.example.json) for other settings.

- `git_keys` is a list of host-side SSH private key paths. Prefer read-only deploy keys. Repeat `--git-key PATH` to supply a different list for one launch. Encrypted keys can prompt for a passphrase on the host.
- `git_known_hosts` or `--git-known-hosts PATH` supplies verified host keys for your Git servers. When omitted, Rox looks up the matching host and port in the launch machine's `~/.ssh/known_hosts`. Guest SSH still verifies the server's key.
- The tool uses the current branch's upstream remote and branch when configured. Otherwise it uses `origin` and the current branch name. Use `--remote NAME` or `--repo URL` to override the remote and `--branch NAME` to choose an existing remote branch. The checkout contains the remote branch tip. Push local commits first if you need them in the guest.

For an HTTPS remote that requires authentication, supply its SSH clone URL.

```sh
rox up container \
  --repo git@github.com:organization/project.git \
  --git-key ~/.ssh/project_readonly
```

Only the selected keys are loaded into a temporary SSH agent. The agent is forwarded only while Git clones and is stopped before the project hook or Rex terminal starts. Git private keys stay on the host.

## Tart VMs

On an Apple Silicon Mac, install Tart and prepare one template per guest OS.

```sh
tart clone ghcr.io/cirruslabs/ubuntu:24.04 rox-ubuntu
tart clone ghcr.io/cirruslabs/macos-tahoe-base:latest rox-macos
```

Boot each template once with `tart run TEMPLATE` and prepare it from its console.

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

If the launch machine cannot read the branch, Rox prints a warning and runs the hook at boot. If the hook fails during a build, the launch stops. Remove a project image with `docker rmi rox:REPO`, `podman rmi rox:REPO`, or `tart delete rox-REPO-BACKEND`.

## Workspace commands

| Command | Effect |
| --- | --- |
| `rox ls` | List local workspace records, their provisioning state, and each guest's projects. |
| `rox add NAME` | Clone a project into a running guest and start a Rex session at its checkout. |
| `rox shell NAME [PROJECT]` | Open an interactive shell at a project checkout. Without `PROJECT`, it opens the guest's only checkout, or the guest home when there are none or several. |
| `rex -S NAME ls` | List the guest's Rex sessions after a local launch with host registration. |
| `rox reconnect NAME` | Restore Rex access to a booted guest, including one retained after clone or setup failure. Offers to add it to the local Rex app's host list unless you pass `--no-app-host`. |
| `rox down NAME` | Delete the guest, its filesystem, and its Rex host entry. |
| `rox down` | From a project checkout, remove the guest that has that project. If several do, Rox lists them and asks which one to remove. |

Rex connects over a private Unix socket forwarded through SSH. If socket forwarding fails, Rox relays connections over authenticated SSH sessions. Containers publish only their SSH port to loopback. Workspace state, Tart boot logs, and SSH tunnel diagnostics live under `${XDG_STATE_HOME:-$HOME/.local/state}/rox`. A failed launch prints the workspace name so you can find its records or remove it.
