#!/bin/bash
set -e

cd /home/container

# If HYTALE_SERVER_SESSION_TOKEN isn't set, assume the user will log in themselves, rather than a host's GSP
if [[ -z "$HYTALE_SERVER_SESSION_TOKEN" ]]; then

	echo -e "Checking for Hytale server update..."

	if [[ -f version ]]; then
		curversion=$(./hytale-downloader/hytale-downloader-linux -patchline "$HYTALE_PATCHLINE" -print-version | tee /dev/tty)
	fi

	if ! [[ -e version ]] || [ "$curversion" != "$(cat "version")" ]; then
		if [[ -f version ]]; then
			echo -e "New update available, downloading version $curversion..."
		fi

		./hytale-downloader/hytale-downloader-linux -patchline "$HYTALE_PATCHLINE" -download-path HytaleServer.zip

		# Write the current version if it wasn't set before
		if [[ -z "$curversion" ]]; then
			curversion=$(./hytale-downloader/hytale-downloader-linux -patchline "$HYTALE_PATCHLINE" -print-version | tee /dev/tty)
		fi

		unzip -o HytaleServer.zip -d .
		rm -f HytaleServer.zip
		echo "$curversion" > version
	fi

elif [[ -f "HytaleMount/HytaleServer.zip" ]]; then
	unzip -o HytaleMount/HytaleServer.zip -d .
elif [[ -f "HytaleMount/Assets.zip" ]]; then
	ln -s -f HytaleMount/Assets.zip Assets.zip
elif [[ -f "Server/Assets.zip" ]]; then
	ln -s -f Server/Assets.zip Assets.zip
elif [[ -f "HytaleServer.zip" ]]; then
	unzip -o HytaleServer.zip -d .
fi

# Download the latest hytale-sourcequery plugin if enabled
if [ "${INSTALL_SOURCEQUERY_PLUGIN}" == "1" ]; then
	mkdir -p mods
	echo -e "Downloading latest hytale-sourcequery plugin..."
	LATEST_URL=$(curl -sSL https://api.github.com/repos/physgun-com/hytale-sourcequery/releases/latest \
		| grep -oP '"browser_download_url":\s*"\K[^"]+\.jar' || true)
	if [[ -n "$LATEST_URL" ]]; then
		curl -sSL -o mods/hytale-sourcequery.jar "$LATEST_URL"
		echo -e "Successfully downloaded hytale-sourcequery plugin to mods folder."
	else
		echo -e "Warning: Could not find hytale-sourcequery plugin download URL."
	fi
fi

if [[ -f config.json && -n "$HYTALE_MAX_VIEW_RADIUS" ]]; then
	jq ".MaxViewRadius = $HYTALE_MAX_VIEW_RADIUS" config.json > config.tmp.json && mv config.tmp.json config.json
fi

/java.sh $@
