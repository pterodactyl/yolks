#!/bin/bash
cd /home/container

# Make internal Docker IP address available to processes.
export INTERNAL_IP=`ip route get 1 | awk '{print $(NF-2);exit}'`

if [ -z ${AUTO_UPDATE} ] || [ "${AUTO_UPDATE}" == "1" ]; then
	if [ -f "./DepotDownloader/DepotDownloader" ]; then
		echo -e "Using DepotDownloader"
    	[[ -n "${SRCDS_BETAID:-}" ]] && args+=(-branch "${SRCDS_BETAID}")
    	[[ -n "${SRCDS_BETAPW:-}" ]] && args+=(-branchpassword "${SRCDS_BETAPW}")
    	./DepotDownloader/DepotDownloader -app 258550 "${args[@]}" -dir /home/container -os linux -validate
	else
		echo -e "Using SteamCMD; consider upgrading the egg?"
		./steamcmd/steamcmd.sh +force_install_dir /home/container +login anonymous +app_update 258550 +quit
	fi
else
    echo -e "Not updating game server as auto update was set to 0. Starting Server"
fi

# Replace startup variables
MODIFIED_STARTUP=`eval echo $(echo ${STARTUP} | sed -e 's/{{/${/g' -e 's/}}/}/g')`
echo ":/home/container$ ${MODIFIED_STARTUP}"

# Install selected framework
if [[ "${FRAMEWORK}" == "carbon" ]]; then
    echo "Updating Carbon..."
    curl -fsSL "https://github.com/CarbonCommunity/Carbon.Core/releases/download/production_build/Carbon.Linux.Release.tar.gz" | tar -xz
    export DOORSTOP_ENABLED=1
    export DOORSTOP_TARGET_ASSEMBLY="$(pwd)/carbon/managed/Carbon.Preloader.dll"
    MODIFIED_STARTUP="LD_PRELOAD=$(pwd)/libdoorstop.so ${MODIFIED_STARTUP}"
    echo "Done updating Carbon!"
fi
if [[ "${FRAMEWORK}" == "oxide" ]]; then
    echo "Updating uMod..."
    curl -fsSL "https://github.com/OxideMod/Oxide.Rust/releases/latest/download/Oxide.Rust-linux.zip" -o umod.zip
    unzip -oq umod.zip
    rm umod.zip
    echo "Done updating uMod!"
fi

# Rust library paths
export LD_LIBRARY_PATH="$(pwd)/RustDedicated_Data/Plugins/x86_64:$(pwd)"

# Run the Server
node /wrapper.js "${MODIFIED_STARTUP}"
