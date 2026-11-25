#!/bin/sh
set -eu

: "${ROX_SSH_PUBLIC_KEY:?Start this image with rox up container}"
# A cloud guest mounts a volume here, which starts out owned by root.
chown workspace:workspace /home/workspace
install -d -m 700 -o workspace -g workspace /home/workspace/.ssh
umask 077
printf '%s\n' "$ROX_SSH_PUBLIC_KEY" > /home/workspace/.ssh/authorized_keys
chown workspace:workspace /home/workspace/.ssh/authorized_keys
unset ROX_SSH_PUBLIC_KEY

# Rox sets ROX_SUDO only for engines that give each container its own VM, such as Apple's container.
# Docker and Podman containers share the host kernel, so they get no sudo.
if [ "${ROX_SUDO:-}" = 1 ]; then
    printf 'workspace ALL=(ALL) NOPASSWD:ALL\n' > /etc/sudoers.d/workspace
    chmod 0440 /etc/sudoers.d/workspace
    # The same VM boundary makes Docker safe to run here. Its daemon turns on IP forwarding,
    # which needs /proc/sys writable; that only affects this container's own kernel.
    if command -v dockerd >/dev/null 2>&1 && mount -o remount,rw /proc/sys 2>/dev/null; then
        # As Docker's own dind image does, move this container's processes out of the root cgroup
        # and delegate its controllers. Otherwise Docker's cgroups come out threaded, and the
        # runtime can't kill them through cgroup.kill, so stopping the container fails.
        if [ -f /sys/fs/cgroup/cgroup.controllers ]; then
            mkdir -p /sys/fs/cgroup/init
            xargs -rn1 < /sys/fs/cgroup/cgroup.procs > /sys/fs/cgroup/init/cgroup.procs 2>/dev/null || :
            sed -e 's/ / +/g' -e 's/^/+/' < /sys/fs/cgroup/cgroup.controllers \
                > /sys/fs/cgroup/cgroup.subtree_control 2>/dev/null || :
        fi
        # /run survives a container restart, and its stale pid files can match a new process,
        # which stops Docker's containerd from starting. Nothing of Docker's runs yet at boot.
        rm -rf /var/run/docker /var/run/docker.pid /var/run/docker.sock
        dockerd >>/var/log/dockerd.log 2>&1 &
    fi
else
    rm -f /etc/sudoers.d/workspace
fi
unset ROX_SUDO

mkdir -p /run/sshd
if [ ! -f /etc/ssh/ssh_host_ed25519_key ]; then
    ssh-keygen -q -t ed25519 -N '' -f /etc/ssh/ssh_host_ed25519_key
fi
exec /usr/sbin/sshd -D -e -f /dev/null \
    -h /etc/ssh/ssh_host_ed25519_key \
    -o AuthenticationMethods=publickey -o PermitRootLogin=no \
    -o PasswordAuthentication=no -o KbdInteractiveAuthentication=no \
    -o AllowUsers=workspace -o AllowTcpForwarding=local \
    -o AllowStreamLocalForwarding=local -o X11Forwarding=no
