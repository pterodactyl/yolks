#!/bin/bash
set -e

cd /home/container

# Migration from old path to new path
if [[ -f ./config.json || -f ./HytaleServer.jar || -f ./HytaleServer.aot || -f ./whitelist.json || -f ./bans.json || -f ./whitelist.json ]]; then
	if [[ ! -d "/home/container/Server" ]]; then
		mkdir -p /home/container/Server
	fi
	mv ./Licenses ./Server
	mv ./logs ./Server
	mv ./mods ./Server
	mv ./universe ./Server
	mv ./auth.enc ./Server
	mv ./bans.json ./Server
	mv ./config.json ./Server
	mv ./config.json.bak ./Server
	mv ./HytaleServer.jar ./Server
	mv ./HytaleServer.aot ./Server
	mv ./permissions.json ./Server
	mv ./whitelist.json ./Server
fi

# Respect the user's patchline wish, if they so choose to change it from the server console
if [[ -f ./Server/config.json ]]; then
	if [[ ! -z "$(jq -r '.Update.Patchline // ""' ./Server/config.json)" ]]; then
		CONFIG_PATCHLINE=$(jq -r '.Update.Patchline // ""' ./Server/config.json)
		if [[ "$HYTALE_PATCHLINE" != "$CONFIG_PATCHLINE" ]]; then
			HYTALE_PATCHLINE="$CONFIG_PATCHLINE"
		fi
	fi
fi

# If HYTALE_SERVER_SESSION_TOKEN isn't set, assume the user will log in themselves, rather than a host's GSP
if [[ -z "$HYTALE_SERVER_SESSION_TOKEN" ]]; then
	if [[ "$(uname -m)" == "aarch64" ]]; then
		HYTALE_DOWNLOADER="qemu-x86_64-static ./hytale-downloader/hytale-downloader-linux"
	else
		HYTALE_DOWNLOADER="./hytale-downloader/hytale-downloader-linux"
	fi

	# Apply staged update if present
	if [[ -f "./updater/staging/Server/HytaleServer.jar" ]]; then
		curversion=$($HYTALE_DOWNLOADER -patchline "$HYTALE_PATCHLINE" -print-version | tee /dev/tty)
		echo "[Launcher] Applying $curversion update..."
		# Only replace update files, preserve config.json/universe/mods
		cp -f ./updater/staging/Server/HytaleServer.jar ./Server/
		if [[ -f "./updater/staging/Server/HytaleServer.aot" ]]; then
			cp -f ./updater/staging/Server/HytaleServer.aot ./Server/
		fi
		if [[ -d "./updater/staging/Server/Licenses" ]]; then
			rm -rf ./Server/Licenses
			cp -r ./updater/staging/Server/Licenses ./Server
		fi
		if [[ -f "./updater/staging/Assets.zip" ]]; then
			cp -f ./updater/staging/Assets.zip ./
		fi
		#if [[ -f ".updater/staging/start.sh" ]]; then
		#	cp -f ./updater/staging/start.sh ./
		#fi
		#if [[ -f ".updater/staging/start.bat" ]]; then
		#	cp -f ./updater/staging/start.bat ./
		#fi

		rm -rf ./updater/staging
		echo "$curversion" > ./version
	fi
	
	echo -e "Checking for Hytale server update..."

	if [[ -f ./version ]]; then
		curversion=$("$HYTALE_DOWNLOADER" -patchline "$HYTALE_PATCHLINE" -print-version | tee /dev/tty)
	fi

	if ! [[ -e ./version ]] || [ "$curversion" != "$(cat "./version")" ]; then
		if [[ -f ./version ]]; then
			echo -e "New update available, downloading version $curversion..."
		fi

		"$HYTALE_DOWNLOADER" -patchline "$HYTALE_PATCHLINE" -download-path HytaleServer.zip

		# Write the current version if it wasn't set before
		if [[ -z "$curversion" ]]; then
			curversion=$("$HYTALE_DOWNLOADER" -patchline "$HYTALE_PATCHLINE" -print-version | tee /dev/tty)
		fi

		unzip -o ./HytaleServer.zip -d .
		rm -f ./HytaleServer.zip
		echo "$curversion" > ./version
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

# Removing launch scripts, because I don't believe them to be necessary.
# Updating server via /update download will cry about
# "Expected Assets.zip and launcher scripts in parent directory."
# But one can force it with /update download --force.
# If auto-update is the goal, just delete or comment the bottom 2 if-blocks.
if [[ -f start.bat ]]; then
	rm start.bat
fi
if [[ -f start.sh ]]; then
	rm start.sh
fi

# Download the latest hytale-sourcequery plugin if enabled
if [ "${INSTALL_SOURCEQUERY_PLUGIN}" == "1" ]; then
	mkdir -p ./Server/mods
	echo -e "Downloading latest hytale-sourcequery plugin..."
	LATEST_URL=$(curl -sSL https://api.github.com/repos/physgun-com/hytale-sourcequery/releases/latest \
		| grep -oP '"browser_download_url":\s*"\K[^"]+\.jar' || true)
	if [[ -n "$LATEST_URL" ]]; then
		curl -sSL -o ./Server/mods/hytale-sourcequery.jar "$LATEST_URL"
		echo -e "Successfully downloaded hytale-sourcequery plugin to mods folder."
	else
		echo -e "Warning: Could not find hytale-sourcequery plugin download URL."
	fi
fi

if [[ -f ./Server/config.json && -n "$HYTALE_MAX_VIEW_RADIUS" ]]; then
	jq ".MaxViewRadius = $HYTALE_MAX_VIEW_RADIUS" ./Server/config.json > ./Server/config.tmp.json && mv ./Server/config.tmp.json ./Server/config.json
fi

if [[ ! -d "/home/container/Server" ]]; then
	mkdir -p /home/container/Server
fi
cd /home/container/Server

if [[ "${STARTUP:-}" =~ -jar\ Server/HytaleServer\.jar || "${0}" =~ -jar\ Server/HytaleServer\.jar ]]; then
  echo ""
  echo "!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!"
  echo "!!                        OUTDATED STARTUP DETECTED                  !!"
  echo "!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!"
  echo ""
  echo "ERROR: Your startup command still uses 'Server/HytaleServer.jar'"
  echo "       That's an outdated path from early versions of this Hytale egg."
  echo ""
  echo "What would happen on continued use:"
  echo " - Server files (universe/, config.json, logs/, backups/, etc.) are"
  echo "   generated in the wrong directory: /home/container"
  echo "   instead of the intended /home/container/Server directory."
  echo "   Ever since Hytale version 2026.01.24-6e2d4fc36"
  echo "   server files must be located in /home/container/Server"
  echo "   Additionally, the Server will not boot"
  echo "   because we run exit 1 upon detecting Server/HytaleServer.jar used"
  echo ""
  echo "To do:"
  echo " 1. Update to the latest Hytale egg version"
  echo ""
  echo "Server startup aborted to prevent usage on wrong path."
  echo "Update the egg and restart."
  echo ""
  exit 1
fi

/java.sh $@
