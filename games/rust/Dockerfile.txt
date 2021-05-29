FROM	ubuntu:18.04

LABEL	author="Isaac A." maintainer="isaac@isaacs.site"

RUN		apt update \
    		&& apt upgrade -y \
    		&& apt install -y lib32gcc1 lib32stdc++6 unzip curl iproute2 tzdata libgdiplus \
    		&& curl -sL https://deb.nodesource.com/setup_6.x | bash - \
    		&& apt install -y nodejs npm \
    		&& mkdir /node_modules \
    		&& npm install --prefix / ws \
   			&& useradd -d /home/container -m container

USER 	container
ENV  	USER=container HOME=/home/container

WORKDIR /home/container

COPY 	./entrypoint.sh /entrypoint.sh
COPY 	./wrapper.js /wrapper.js

CMD 	["/bin/bash", "/entrypoint.sh"]
