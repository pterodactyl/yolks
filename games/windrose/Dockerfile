ARG WINDROSE_UPSTREAM_IMAGE=docker.io/windroseserver/windroseserver:latest
FROM --platform=linux/amd64 ${WINDROSE_UPSTREAM_IMAGE} AS windrose-upstream

USER root
# Remove symbols before COPY so they never enter a runtime image layer.
RUN rm -f /home/ue_user/app/R5/Binaries/Linux/*.debug \
    /home/ue_user/app/R5/Binaries/Linux/*.sym

FROM --platform=linux/amd64 debian:trixie-slim

ARG WINDROSE_UPSTREAM_IMAGE=docker.io/windroseserver/windroseserver:latest
ARG WINDROSE_UPSTREAM_DIGEST=unknown
ARG WINDROSE_RUNTIME_REVISION=unknown

LABEL org.opencontainers.image.title="Windrose Pterodactyl Runtime"
LABEL org.opencontainers.image.description="Pterodactyl runtime containing the native Linux Windrose dedicated-server payload."
LABEL org.opencontainers.image.source="https://github.com/pterodactyl/yolks"
LABEL org.opencontainers.image.base.name="${WINDROSE_UPSTREAM_IMAGE}"
LABEL org.opencontainers.image.base.digest="${WINDROSE_UPSTREAM_DIGEST}"

ENV DEBIAN_FRONTEND=noninteractive \
    WINDROSE_APPLICATION_ROOT=/opt/windrose \
    WINDROSE_SERVER_ROOT=/home/container \
    WINDROSE_CONFIG_PATH=/home/container/R5/ServerDescription.json \
    WINDROSE_UPSTREAM_DIGEST=${WINDROSE_UPSTREAM_DIGEST} \
    WINDROSE_RUNTIME_REVISION=${WINDROSE_RUNTIME_REVISION}

RUN apt-get update \
    && apt-get install --yes --no-install-recommends \
        ca-certificates \
        libcurl4 \
        netcat-traditional \
        procps \
        python3 \
        tini \
        xdg-user-dirs \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --home-dir /home/container --shell /bin/bash container

COPY --from=windrose-upstream --chown=container:container /home/ue_user/app /opt/windrose

RUN rm -rf /opt/windrose/R5/Saved \
    && rm -f /opt/windrose/R5/ServerDescription.json \
    && ln -s /home/container/R5/Saved /opt/windrose/R5/Saved \
    && ln -s /home/container/R5/ServerDescription.json /opt/windrose/R5/ServerDescription.json \
    && rm -rf /var/tmp/R5 \
    && ln -s /home/container/R5/Saved/.windrose-var-tmp /var/tmp/R5 \
    && chmod 0755 /opt/windrose/R5/Binaries/Linux/WindroseServer-Linux-Shipping

COPY NOTICE.md /usr/share/doc/windrose-pterodactyl/NOTICE.md
COPY --chmod=0755 entrypoint.sh /entrypoint.sh
COPY --chmod=0755 windrose-start.sh /usr/local/bin/windrose-start
COPY --chmod=0755 windrose-configure.py /usr/local/bin/windrose-configure
COPY --chmod=0755 windrose-report.py /usr/local/bin/windrose-report

USER container
ENV USER=container HOME=/home/container PYTHONUNBUFFERED=1
WORKDIR /home/container

STOPSIGNAL SIGINT
ENTRYPOINT ["/usr/bin/tini", "-g", "--"]
CMD ["/entrypoint.sh"]
