FROM        eclipse-temurin:23-jdk-noble

LABEL       author="Pterodactyl Team" maintainer="support@pterodactyl.io"

LABEL       org.opencontainers.image.source="https://github.com/pterodactyl/yolks"
LABEL       org.opencontainers.image.licenses=MIT

# hadolint ignore=DL3015
RUN         apt-get update -y \
            && apt-get upgrade -y \
            && apt-get install -y \
                curl \
                lsof \
                ca-certificates \
                openssl \
                git \
                tar \
                sqlite3 \
                fontconfig \
                tzdata \
                iproute2 \
                libfreetype6 \
                tini \
				zip \
				unzip \
			&& rm -rf /var/lib/apt/lists/*

## Setup working directory and environment
ENV         USER=container HOME=/home/container
WORKDIR     /home/container

COPY        --chown=root:root --chmod=755 ./../entrypoint.sh /entrypoint.sh
ENTRYPOINT    ["/usr/bin/tini", "-g", "--", "/entrypoint.sh"]
