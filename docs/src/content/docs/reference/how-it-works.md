---
title: How it works
description: What rox does during a launch, how Rex connects, and where state lives.
---

## A launch, step by step

1. **Resolve the project.** Rox finds the checkout root, the remote, and the branch, and checks that the URL is SSH or HTTPS. Outside a checkout, or with `--no-project`, there's no project, and Rox skips steps 6 and 7 and roots the session in the guest's home directory.
2. **Prepare the image.** For containers, Rox builds `rox:linux` if it's missing. If the branch has a `.rox.sh`, it checks or builds the [project image](../../guides/project-images/).
3. **Boot the guest.** For a container, Rox generates a per-workspace login key and starts the container. With Docker or Podman, only its SSH port is published, on loopback. Apple's container runs each container in its own lightweight VM with its own IP address, and Rox connects to port 22 at that address. For a VM, it clones the Tart template and boots it without graphics.
4. **Pin and verify.** Rox pins the guest's SSH host key and checks that the guest OS matches the backend.
5. **Start Rex.** Rox starts a Rex server inside the guest on a private Unix socket, connects to it, and registers the guest as a Rex host.
6. **Check out the branch.** The temporary Git agent is forwarded while the guest clones the branch, or fetches it into the project image's checkout.
7. **Set up.** Rox runs `.rox.sh`, unless the project image already ran it or you passed `--no-hook`.
8. **Forward Git keys.** Rox keeps an SSH agent with the guest's Git keys on the launcher and forwards it into the guest, so Git works there for the guest's whole life. See [Private repositories](../../guides/private-repositories/).
9. **Create the session.** Rox creates a Rex session rooted at the checkout, running your `--` command if you gave one.

`rox add` repeats steps 6 to 9 on a guest that's already running. It reconnects to the guest's Rex server first and clones into a new directory in the guest's home directory, so each project in a guest has its own checkout and session.

## Connecting to Rex

Rex connects to the guest over a private Unix socket forwarded through SSH, at `/tmp/rox-UID/ID/rex.sock` on the launcher. If socket forwarding fails, Rox falls back to a relay that carries each connection over its own authenticated SSH session. Docker and Podman containers publish only their SSH port, and only to `127.0.0.1`. Apple's containers publish no ports. Their SSH server is reachable only on the Mac's private container network.

When the launcher config has `tailscale`, the guest's Rex server also serves over the tailnet as its own device, and the Rex host lists the tailnet name after the socket. Other devices reach the guest that way. See [Reaching guests from other devices](../../guides/tailnet/).

`rox forward` uses the same SSH login to tunnel launcher ports to the guest's `localhost`. The container's SSH server allows only these launcher-to-guest tunnels. It refuses tunnels from the guest back to the launcher, and binds nothing on the guest's network interfaces.

## Where things live

| Path | Contents |
| --- | --- |
| `${XDG_CONFIG_HOME:-~/.config}/rox/config.json` | [Configuration](../configuration/) |
| `${XDG_STATE_HOME:-~/.local/state}/rox/NAME/` | Workspace record (`guest.json`), pinned host key, login key, `tart.log`, `ssh-tunnel.log` |
| `${XDG_STATE_HOME:-~/.local/state}/rox/images/` | Tart project image records |
| `${XDG_STATE_HOME:-~/.local/state}/rox/builds/` | Logs from Tart project image builds that failed |
| `${XDG_CACHE_HOME:-~/.cache}/rox/repos/` | Bare mirrors used to check for `.rox.sh` |
| `${XDG_STATE_HOME:-~/.local/state}/rox/service.log` | What the [rox service](../../guides/keep-running/) restored, and why a guest couldn't be |

A failed launch prints the workspace name, so you can find its records or remove it with `rox down NAME`.
