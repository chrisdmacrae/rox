---
title: CLI
description: Every rox command and option.
---

## `rox up BACKEND`

Start and provision a new guest. `BACKEND` is `container`, `linux`, or `macos`. Arguments after `--` run in the guest's first Rex terminal. Each run starts a new guest, even for a project that already has one.

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
| `--image IMAGE` | Container image or prepared Tart template. Overrides the config. |
| `--engine container\|docker\|podman` | Container engine. Defaults to `container` (Apple's container tool) on Apple Silicon Macs and `docker` elsewhere. |
| `--remote NAME` | Git remote to clone from. Can't be combined with `--repo`. |
| `--repo URL` | Repository URL to clone. Must be SSH or HTTPS. |
| `--branch NAME` | Remote branch to clone. |
| `--git-key PATH` | Git SSH identity. Repeat it for several keys. Replaces `git_keys` from the config. |
| `--git-known-hosts PATH` | `known_hosts` file for your Git SSH servers. |
| `--boot-timeout SECONDS` | Timeout for each guest readiness check. Defaults to `120`. |
| `--no-hook` | Skip the project's `.rox.sh` and the project image. |
| `--no-prebuild` | Clone and run `.rox.sh` at boot instead of using a [project image](../../guides/project-images/). |
| `--rebuild` | Rebuild the project image before launching. |
| `--open-shell` | Open a guest shell in the current Rex session. Requires a Rex terminal. |
| `--no-app-host` | Don't offer to add the guest to a Rex app that keeps its own host list. |

## `rox add NAME`

Clone a project into the running guest `NAME`, run its `.rox.sh`, and create a Rex session rooted at the checkout. Arguments after `--` run in that session's first terminal. The checkout goes to `~/.rox/NAME/projects/ID` in the guest, where `ID` is the project name with a number added if the guest already has it.

```sh
rox add NAME [options] [-- COMMAND [ARG ...]]
```

It accepts `-S`/`--host`, `--config`, `--cwd`, `--project`, `--remote`, `--repo`, `--branch`, `--git-key`, `--git-known-hosts`, `--no-hook`, and `--open-shell`, which work as they do for `rox up`. A project is required. Project images don't apply, so `.rox.sh` always runs at boot. If cloning or the hook fails, the project is marked failed and the guest stays ready.

## `rox ls`

List local workspace records with their backend, provisioning state, IP address, and projects. The IP address is where the launcher can reach servers in a Tart VM or an Apple container, such as `http://IP:5173` for a dev server listening on `0.0.0.0`. Docker and Podman containers show `-`, because they're reached only through a loopback SSH port. The address is the one recorded at launch or the last `rox reconnect`. Each project shows as `ID:BRANCH`, followed by its state if it isn't ready. A guest with no project shows `-`.

## `rox shell NAME [PROJECT]`

Open an interactive login shell with `REX_SERVER` pointing at the guest's server. `PROJECT` is a project ID or name from `rox ls`. Without it, the shell opens at the guest's only checkout, or in the guest home when it has no projects or several. The guest must be ready.

## `rox forward NAME PORT...`

Forward ports on the launcher's `localhost` to servers listening on the guest's own `localhost`, over the guest's SSH connection. It works with every backend and engine, including Docker and Podman containers, which have no address of their own. The dev server can keep listening on `localhost`, and you open it at `http://localhost:PORT`.

Give each port as `PORT` to use the same number on both sides, or `LOCAL:GUEST` to choose the launcher's port. For example, `rox forward rv-app-1a2b3c4d5e6f 5173 8080:3000` opens `http://localhost:5173` and `http://localhost:8080`. The forward runs in the foreground until you press Ctrl-C, and it exits if a local port is already taken. Forwarded ports bind only to `127.0.0.1` on the launcher, and the guest can't open tunnels back to it. The guest must be ready.

Containers started from a `rox:linux` image built before `rox forward` existed refuse the tunnel. Rox rebuilds `rox:linux` on the next `rox up container`, so start a new guest to use it.

## `rox reconnect NAME`

Restore Rex access to a guest that's still running. This also works for a guest kept after a failed clone or setup. Rox restarts the guest's Rex server if needed, reopens the tunnel, and registers the host again.

| Option | Description |
| --- | --- |
| `--no-app-host` | Don't offer to add the guest to a Rex app that keeps its own host list. |

## `rox down [NAME]`

Delete the guest, its filesystem, all of its project checkouts, and its Rex host entry. If you leave out `NAME`, Rox picks the guest that has the current project. When several do, it lists them and asks which one to remove.

| Option | Description |
| --- | --- |
| `--cwd DIR` | Directory inside the project, used when you leave out `NAME`. |

## `rox key add PATH` / `rox key remove PATH`

Add or remove a Git SSH private key path in `git_keys`. Removing a path doesn't delete the key file.

| Option | Description |
| --- | --- |
| `--config PATH` | A different rox config file. |
