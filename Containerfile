FROM docker.io/library/debian:bookworm-slim

ARG REX_CHANNEL=preview
RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
       bash ca-certificates curl git libpam0g openssh-client openssh-server tini xz-utils \
    && rm -rf /var/lib/apt/lists/* /etc/ssh/ssh_host_*

# The optional secret is an official, architecture-matched Linux rex executable.
RUN --mount=type=secret,id=rex_binary \
    if [ -f /run/secrets/rex_binary ]; then \
        install -m 0755 /run/secrets/rex_binary /usr/local/bin/rex; \
    else \
        set -eu; \
        curl -fsSL https://pkgs.superlogical.com/rex/install.sh -o /tmp/rex-install.sh; \
        sh /tmp/rex-install.sh --tarball --channel "$REX_CHANNEL"; \
        rm /tmp/rex-install.sh; \
    fi \
    && rex version

RUN useradd --create-home --shell /bin/bash workspace && passwd -d workspace
COPY guest/container-entrypoint.sh /usr/local/bin/rox-container
RUN chmod 0755 /usr/local/bin/rox-container

EXPOSE 22
ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/rox-container"]
