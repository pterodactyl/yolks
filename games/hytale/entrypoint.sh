#!/bin/bash
set -e

cd /home/container

# If HYTALE_SERVER_SESSION_TOKEN isn't set, assume the user will log in themselves, rather than a host's GSP
if [[ -z "$HYTALE_SERVER_SESSION_TOKEN" ]]; then
	./hytale-downloader/hytale-downloader-linux -patchline "$HYTALE_PATCHLINE"
fi

/java.sh $@
