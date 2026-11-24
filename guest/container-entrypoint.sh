#!/bin/sh
set -eu

: "${ROX_SSH_PUBLIC_KEY:?Start this image with rox up container}"
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
