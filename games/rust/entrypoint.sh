#!/bin/bash

# Set environment variable that holds the Internal Docker IP
INTERNAL_IP=$(ip route get 1 | awk '{print $NF;exit}')
export INTERNAL_IP

# Switch to the container's working directory
cd /home/container || exit 1

# Update Rust Server
./steamcmd/steamcmd.sh +login anonymous +force_install_dir /home/container +app_update 258550 +quit

# Replace Startup Variables
# Convert all of the "{{VARIABLE}}" parts of the command into the expected shell
# variable format of "${VARIABLE}" before evaluating the string and automatically
# replacing the values.
PARSED=$(echo "${STARTUP}" | sed -e 's/{{/${/g' -e 's/}}/}/g' | eval echo "$(cat -)")
printf "\033[1m\033[33mcontainer@pterodactyl~ \033[0m%s\n" "$PARSED"

# OxideMod has been replaced with uMod
if [ -f OXIDE_FLAG ] || [ "${OXIDE}" = 1 ] || [ "${UMOD}" = 1 ]; then
    echo "Updating uMod..."
    curl -sSL "https://umod.org/games/rust/download/develop" > umod.zip
    unzip -o -q umod.zip
    rm umod.zip
    echo "Done updating uMod!"
fi

# Fix for Rust not starting
export LD_LIBRARY_PATH=$LD_LIBRARY_PATH:$(pwd)

# Run the Server
node /wrapper.js "${PARSED}"
