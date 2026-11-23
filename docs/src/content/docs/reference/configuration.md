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
    "engine": "docker",
    "image": "rox:linux"
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
| `engine` | `container` | `docker` | `docker` or `podman`. |
| `user` | `linux`, `macos` | `admin` | SSH user in the VM template. |
| `identity` | `linux`, `macos` | — | Private key on the host for logging in to the VM. Required. |
| `host_key` | `linux`, `macos` | — | The template's SSH host public key, used for pinning. Required. |
| `prebuild` | all | `true` | Build and reuse a [project image](../../guides/project-images/) when the branch has a `.rox.sh`. |
