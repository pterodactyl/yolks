#
# Copyright (c) 2026 Pterodactyl
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.
#

FROM --platform=$TARGETOS/$TARGETARCH debian:trixie-slim

LABEL author="Isaac A." maintainer="isaac@isaacs.site"

LABEL org.opencontainers.image.source="https://github.com/pterodactyl/yolks"
LABEL org.opencontainers.image.licenses=MIT

ENV DEBIAN_FRONTEND=noninteractive 

RUN dpkg --add-architecture i386 \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    gnupg \
    unzip \
    iproute2 \
    tzdata \
    libgdiplus \
    lib32gcc-s1 \
    lib32stdc++6 \
    libsdl2-2.0-0:i386 \
    && mkdir -p /etc/apt/keyrings \
    && curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key \
    | gpg --dearmor -o /etc/apt/keyrings/nodesource.gpg \
    && printf '%s\n' \
    'Types: deb' \
    'URIs: https://deb.nodesource.com/node_24.x' \
    'Suites: nodistro' \
    'Components: main' \
    'Architectures: amd64' \
    'Signed-By: /etc/apt/keyrings/nodesource.gpg' \
    > /etc/apt/sources.list.d/nodesource.sources \
    && apt-get update \
    && apt-get install -y --no-install-recommends nodejs \
    && mkdir -p /srv/node \
    && cd /srv/node \
    && npm install ws \
    && mv node_modules /node_modules \
    && cd / \
    && rm -rf /srv/node /var/lib/apt/lists/* \
    && useradd -d /home/container -m container

USER 		container
ENV  		USER=container HOME=/home/container

WORKDIR 	/home/container

COPY 		./entrypoint.sh /entrypoint.sh
COPY 		./wrapper.js /wrapper.js

CMD ["/bin/bash", "/entrypoint.sh"]
