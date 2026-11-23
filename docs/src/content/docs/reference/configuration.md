---
title: Configuration
description: The rox config file and its settings.
---

Rox reads `${XDG_CONFIG_HOME:-$HOME/.config}/rox/config.json` on the machine that runs the guest. Pass `--config PATH` to use a different file. Command-line options take precedence over the config.

```json title="config.json"
{
  "git_keys": ["~/.ssh/project_readonly"],
  "git_known_hosts": "~/.ssh/rox_git_known_hosts",
  "container": {
    "engine": "container",
    "image": "rox:linux",
    "cpus": 4,
    "memory": "4G"
  },
  "linux": {
    "image": "rox-ubuntu",
    "user": "admin",
    "identity": "~/.ssh/rox_vm",
    "host_key": "~/.ssh/rox_ubuntu_host.pub"
  },
  "macos": {
    "image": "rox-macos",
    "user": "admin",
    "identity": "~/.ssh/rox_vm",
    "host_key": "~/.ssh/rox_macos_host.pub",
    "prebuild": false
  }
}
```

## Top-level settings

| Key | Type | Description |
| --- | --- | --- |
| `git_keys` | list of paths | SSH private keys loaded into the temporary Git agent. Manage them with `rox key`. `--git-key` overrides this list. |
| `git_known_hosts` | path | Verified host keys for your Git servers. If you leave it out, Rox copies the matching entries from `~/.ssh/known_hosts`. |

## Backend settings

Put these under `container`, `linux`, or `macos`.

| Key | Backends | Default | Description |
| --- | --- | --- | --- |
| `image` | all | `rox:linux` for containers | Base container image, or the prepared Tart template. Project images are built on top of it. |
| `engine` | `container` | `container` on Apple Silicon Macs, otherwise `docker` | `container` (Apple's [container](https://github.com/apple/container) tool), `docker`, or `podman`. |
| `cpus` | all | engine or template default | CPUs for each guest. Apple's container gives each container 4 by default. For Tart, Rox sets it on the clone before it boots. |
| `memory` | all | engine or template default | Memory for each guest, with a unit: `512M`, `8G`. Apple's container gives each container 1 GB by default, which is tight for most agents. |
| `user` | `linux`, `macos` | `admin` | SSH user in the VM template. |
| `identity` | `linux`, `macos` | — | Private key on the host for logging in to the VM. Required. |
| `host_key` | `linux`, `macos` | — | The template's SSH host public key, used for pinning. Required. |
| `prebuild` | all | `true` | Build and reuse a [project image](../../guides/project-images/) when the branch has a `.rox.sh`. |

## Per-project settings: `.rox.json`

Commit a `.rox.json` at the root of a branch to choose that project's guest. Rox reads it from the launched branch, the same way it reads `.rox.sh`, so what you've pushed is what applies, not your local working tree.

```json title=".rox.json"
{
  "backend": "container",
  "container": { "image": "ghcr.io/acme/agent-base:2", "cpus": 4, "memory": "8G" },
  "linux": { "memory": "16G", "prebuild": false }
}
```

| Key | Description |
| --- | --- |
| `backend` | The backend plain `rox up` uses for this project: `container`, `linux`, or `macos`. |
| `container`, `linux`, `macos` | Per-backend `image`, `cpus`, `memory`, and `prebuild`, with the same meaning as in the launcher config. |

Settings are applied in this order, and later ones win:

1. Built-in defaults
2. The launcher's rox config
3. The branch's `.rox.json`
4. Command-line options such as `--image`, `--cpus`, and `--memory`

`.rox.json` can't set `engine`, `user`, `identity`, `host_key`, `git_keys`, or `git_known_hosts`, because those describe the launch machine and its keys. Rox stops with an error if the file sets one, or any key it doesn't know. A VM `image` in `.rox.json` must be a Tart template on the launcher that works with the launcher's `user`, `identity`, and `host_key` for that backend.

If the launcher can't read the branch, Rox prints a warning and ignores `.rox.json`, so you need to name the backend on the command line.
