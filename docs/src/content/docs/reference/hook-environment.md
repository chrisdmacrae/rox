---
title: Hook environment
description: Variables and paths available to .rox.sh.
---

Rox runs `.rox.sh` with Bash from the root of the checkout, as the guest's workspace user, with standard input closed. `$HOME/.local/bin`, `/usr/local/bin`, and `/opt/homebrew/bin` are added to the front of `PATH`.

| Variable | Build | Boot | Description |
| --- | --- | --- | --- |
| `ROX_PHASE` | `build` | `boot` | Whether the hook is running for a [project image](../../guides/project-images/) build or at guest boot. |
| `ROX_BACKEND` | ✓ | ✓ | `container`, `linux`, or `macos`. |
| `ROX_NAME` | image name | workspace name | For example `rox:my-project` during a build, or `rv-my-project-3f9a1c2b7d40` at boot. |
| `ROX_PROJECT_DIR` | ✓ | ✓ | Absolute path of the checkout. Also the working directory. |
| `REX_SERVER` | — | ✓ | The guest's running Rex server, for starting terminals. |
| `SSH_AUTH_SOCK` | — | — | Always unset. Git credentials aren't available to the hook. |

## Checkout paths

Every checkout is at `~/ID` in the guest's home directory. `ID` is the project's ID in the guest, as `rox ls` shows it. A project image builds its checkout at that same path, and absolute paths the hook writes (virtualenvs, for example) keep working.
