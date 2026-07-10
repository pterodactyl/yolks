FROM ubuntu
LABEL maintainer="EsserGaming"
USER root

# Getting the essentials
RUN apt-get update && apt-get install -y ffmpeg

# Container setup for Pterodactyl
RUN adduser --home /home/container container --disabled-password
RUN usermod -a -G container container
RUN chown -R container:container /home/container
RUN mkdir /mnt/server
RUN chown -R container:container /mnt/server
ARG CACHBUST=1
USER container
ENV USER=container HOME=/home/container
WORKDIR /home/container
COPY ./entrypoint.sh /entrypoint.sh

CMD ["/bin/bash", "/entrypoint.sh"]
