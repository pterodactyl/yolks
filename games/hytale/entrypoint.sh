#!/bin/bash
set -e

cd /home/container

# If HYTALE_SERVER_SESSION_TOKEN isn't set, assume the user will log in themselves, rather than a host's GSP
if [[ -z "$HYTALE_SERVER_SESSION_TOKEN" ]]; then
	./hytale-downloader/hytale-downloader-linux -patchline "$HYTALE_PATCHLINE" -download-path HytaleServer.zip

	unzip -o HytaleServer.zip -d .

	rm -f HytaleServer.zip
elif [[ -f "HytaleServer.zip" ]]; then
	unzip -o HytaleServer.zip -d .
elif [[ -f "HytaleMount/HytaleServer.zip" ]]; then
	unzip -o HytaleMount/HytaleServer.zip -d .
fi

/java.sh $@
