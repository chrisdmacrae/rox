---
title: CLI
description: Every rox command and option.
---

## `rox up BACKEND`

Start and provision a new guest. `BACKEND` is `container`, `linux`, or `macos`. Arguments after `--` run in the guest's first Rex terminal.

```sh
rox up container [options] [-- COMMAND [ARG ...]]
```

| Option | Description |
| --- | --- |
| `-S`, `--host HOST` | Launch on this [Rex remote host](../../guides/remote-hosts/). |
| `--config PATH` | Config file on the machine that runs the guest. |
| `--cwd DIR` | A directory inside the source project. Defaults to the current directory. |
| `--project NAME` | Project name for a launch without a checkout. Requires `--repo` and `--branch`. |
| `--image IMAGE` | Container image or prepared Tart template. Overrides the config. |
| `--engine docker\|podman` | Container engine. Defaults to `docker`. |
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

## `rox ls`

List local workspace records with their backend, provisioning state, project, and branch.

## `rox shell NAME`

Open an interactive login shell at the guest checkout, with `REX_SERVER` pointing at the guest's server. The workspace must be ready.

## `rox reconnect NAME`

Restore Rex access to a guest that's still running. This also works for a guest kept after a failed clone or setup. Rox restarts the guest's Rex server if needed, reopens the tunnel, and registers the host again.

| Option | Description |
| --- | --- |
| `--no-app-host` | Don't offer to add the guest to a Rex app that keeps its own host list. |

## `rox down [NAME]`

Delete the guest, its filesystem, and its Rex host entry. If you leave out `NAME`, Rox picks the current project's workspace. When the project has several, it lists them and asks which one to remove.

| Option | Description |
| --- | --- |
| `--cwd DIR` | Directory inside the project, used when you leave out `NAME`. |

## `rox key add PATH` / `rox key remove PATH`

Add or remove a Git SSH private key path in `git_keys`. Removing a path doesn't delete the key file.

| Option | Description |
| --- | --- |
| `--config PATH` | A different rox config file. |
