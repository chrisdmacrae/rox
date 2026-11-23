---
title: Configuration
description: The rox config file and its settings.
---

Rox reads `${XDG_CONFIG_HOME:-$HOME/.config}/rox/config.json` on the machine that runs the guest. Pass `--config PATH` to use a different file. Run `rox config` to open it in your editor. Command-line options take precedence over the config.

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
| `git_keys` | list of paths | SSH private keys loaded into the temporary Git agent. Manage them with `rox key`. `--git-key` overrides this list. Without it, Rox uses your default SSH keys in `~/.ssh`. |
| `git_known_hosts` | path | Verified host keys for your Git servers. If you leave it out, Rox copies the matching entries from `~/.ssh/known_hosts`. |
| `tailscale` | object | Put each guest's Rex server on your tailnet so other devices can reach it. See [Reaching guests from other devices](../../guides/tailnet/). |
| `agents` | object | Install Claude Code (`claude`) and Codex (`codex`) in new guests, with their credentials. Set it up with `rox config agents`. See [Agent CLIs in guests](../../guides/agent-clis/). |

### `tailscale`

| Key | Type | Description |
| --- | --- | --- |
| `auth_key_file` | path | File holding a reusable, ephemeral Tailscale auth key. It must be readable only by you. Required. |
| `tags` | list of `tag:NAME` | ACL tags for guest devices, such as `["tag:rox-guest"]`. With tags, the tailnet ACLs decide who may connect. |
| `control_url` | `https://` URL | A self-hosted control server, such as Headscale. |

### `agents`

Each key is an agent, `claude` or `codex`, and each value is an object. An empty object installs the agent without credentials. Each credential is a file that only you may read, and an agent takes at most one.

| Key | Agent | Description |
| --- | --- | --- |
| `version` | both | `latest`, the default, or a version such as `2.1.330`. Rox checks for the latest release at most every six hours. |
| `token_file` | `claude` | A token from `claude setup-token`. Guests get it as `CLAUDE_CODE_OAUTH_TOKEN`. |
| `api_key_file` | `claude` | An Anthropic API key. Guests get it as `ANTHROPIC_API_KEY`. |
| `auth_file` | `codex` | A Codex login file, usually `~/.codex/auth.json`, copied to `~/.codex/auth.json` in each guest. |
| `api_key_file` | `codex` | An OpenAI API key, which each guest signs in with through `codex login --with-api-key`. |

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

A branch can commit a `.rox.json` to set its default `backend` and, for each backend, `image`, `cpus`, `memory`, and `prebuild`. Its values override this config, and command-line options override both. It can't set `engine`, `user`, `identity`, `host_key`, `git_keys`, `git_known_hosts`, `tailscale`, or `agents`. See [Project settings](../../guides/project-settings/) for the format and how settings combine.

```json title=".rox.json"
{
  "backend": "container",
  "container": { "image": "ghcr.io/acme/agent-base:2", "cpus": 4, "memory": "8G" }
}
```
