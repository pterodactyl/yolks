#!/bin/bash

#
# Copyright (c) 2021 Matthew Penner
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

# Wait for the container to fully initialize
sleep 1

# Default the TZ environment variable to UTC.
TZ=${TZ:-UTC}
export TZ

# Set environment variable that holds the Internal Docker IP
INTERNAL_IP=$(ip route get 1 | awk '{print $(NF-2);exit}')
export INTERNAL_IP

# Set environment for Steam Proton
if [ -f "/usr/local/bin/proton" ]; then
	if [ -n "${SRCDS_APPID}" ]; then
		mkdir -p "/home/container/.steam/steam/steamapps/compatdata/${SRCDS_APPID}"
		export STEAM_COMPAT_CLIENT_INSTALL_PATH="/home/container/.steam/steam"
		export STEAM_COMPAT_DATA_PATH="/home/container/.steam/steam/steamapps/compatdata/${SRCDS_APPID}"
		# Fix for pipx with protontricks
		export PATH=$PATH:/root/.local/bin
	else
		echo -e "----------------------------------------------------------------------------------"
		echo -e "WARNING!!! SRCDS_APPID is missing and must be set when using Proton"
		echo -e "Server will now terminate"
		echo -e "----------------------------------------------------------------------------------"
		exit 1
	fi
fi

# Switch to the container's working directory
cd /home/container || exit 1

# Set default values for steam if not provided
STEAM_USER=${STEAM_USER:-anonymous}
if [ "${STEAM_USER}" == "anonymous" ]; then
	STEAM_PASS=""
	STEAM_AUTH=""
fi

## If AUTO_UPDATE is not set or is set to 1, run steamcmd to update the server
if [ -z "${AUTO_UPDATE}" ] || [ "${AUTO_UPDATE}" == "1" ]; then
	if [ -n "${SRCDS_APPID}" ]; then
		# shellcheck disable=SC2046,SC2086
		./steamcmd/steamcmd.sh +force_install_dir /home/container +login "${STEAM_USER}" "${STEAM_PASS}" "${STEAM_AUTH}" $([[ "${WINDOWS_INSTALL}" == "1" ]] && printf %s '+@sSteamCmdForcePlatformType windows') $([[ -z ${HLDS_GAME} ]] || printf %s "+app_set_config 90 mod ${HLDS_GAME}") "+app_update ${SRCDS_APPID} $([[ -z ${SRCDS_BETAID} ]] || printf %s "-beta ${SRCDS_BETAID}") $([[ -z ${SRCDS_BETAPASS} ]] || printf %s "-betapassword ${SRCDS_BETAPASS}") ${INSTALL_FLAGS}  $([[ "${VALIDATE}" == "1" ]] && printf %s 'validate')" $([[ "${UPDATE_STEAMWORKS}" == "1" ]] && printf %s '+app_update 1007') +quit
	fi
fi

## If MOD_IDS is set, download/update mods & rebuild modlist
WORKSHOP_APPID=${WORKSHOP_APPID:-440900}
MODS_DIR="/home/container/ConanSandbox/Mods"
MODLIST_FILE="${MODS_DIR}/modlist.txt"
EXTRACTED_DIR="/home/container/ConanSandbox/Saved/ExtractedMods"
MOD_ID_LIST=$(echo "${MOD_IDS}" | tr ',' ' ')

if [ -n "${MOD_IDS}" ] || [ -d "${MODS_DIR}" ]; then
	mkdir -p "${MODS_DIR}"

	if [ -n "${MOD_IDS}" ] && { [ -z "${UPDATE_MODS}" ] || [ "${UPDATE_MODS}" == "1" ]; }; then
		WORKSHOP_ARGS=""
		for MOD_ID in ${MOD_ID_LIST}; do
			WORKSHOP_ARGS="${WORKSHOP_ARGS} +workshop_download_item ${WORKSHOP_APPID} ${MOD_ID}"
		done

		./steamcmd/steamcmd.sh +force_install_dir /home/container +login "${STEAM_USER}" "${STEAM_PASS}" "${STEAM_AUTH}" ${WORKSHOP_ARGS} +quit
	fi

	## Remove any managed mods not in MOD_IDS
	for ENTRY in "${MODS_DIR}"/*; do
		[ -L "${ENTRY}" ] || continue
		NAME=$(basename "${ENTRY}")
		KEEP=0
		for MOD_ID in ${MOD_ID_LIST}; do
			if [ "${NAME}" == "${MOD_ID}" ]; then
				KEEP=1
				break
			fi
		done
		if [ "${KEEP}" -eq 0 ]; then
			echo "Removing mod no longer in MOD_IDS: ${NAME}"
			if [ -d "${EXTRACTED_DIR}" ]; then
				while IFS= read -r PAK; do
					BASE="${PAK%.pak}"
					rm -f "${EXTRACTED_DIR}/${BASE}".* "${EXTRACTED_DIR}/${BASE}"-*
				done < <(find -L "${ENTRY}" -maxdepth 1 -name "*.pak" -printf "%f\n" 2>/dev/null)
			fi
			rm -rf "${ENTRY}"
			rm -rf "/home/container/steamapps/workshop/content/${WORKSHOP_APPID}/${NAME}"
			[ -f "${MODLIST_FILE}" ] && sed -i "\|^\*${NAME}/|d" "${MODLIST_FILE}"
		fi
	done

	## Symlink each mods workshop dir into /Mods
	for MOD_ID in ${MOD_ID_LIST}; do
		SRC_DIR="/home/container/steamapps/workshop/content/${WORKSHOP_APPID}/${MOD_ID}"
		DEST_DIR="${MODS_DIR}/${MOD_ID}"

		if [ ! -d "${SRC_DIR}" ]; then
			echo "WARN: workshop mod ${MOD_ID} is not present at ${SRC_DIR}"
			continue
		fi

		if [ -e "${DEST_DIR}" ] && [ ! -L "${DEST_DIR}" ]; then
			echo "WARN: ${DEST_DIR} already exists as a user-managed directory; refusing to overwrite. Rename your custom folder or remove ${MOD_ID} from MOD_IDS."
			continue
		fi

		rm -rf "${DEST_DIR}"
		ln -s "${SRC_DIR}" "${DEST_DIR}"
	done

	## Only sync managed mods, leave the rest alone
	if [ -n "${MOD_IDS}" ]; then
		[ -f "${MODLIST_FILE}" ] || touch "${MODLIST_FILE}"
		for MOD_ID in ${MOD_ID_LIST}; do
			DEST_DIR="${MODS_DIR}/${MOD_ID}"
			DESIRED=$(find -L "${DEST_DIR}" -maxdepth 1 -name "*.pak" -printf "*${MOD_ID}/%f\n" 2>/dev/null | sort)
			if [ -z "${DESIRED}" ]; then
				echo "WARN: no .pak found in ${DEST_DIR} for mod ${MOD_ID}"
				continue
			fi
			EXISTING=$(grep "^\*${MOD_ID}/" "${MODLIST_FILE}" | sort)
			if [ "${EXISTING}" != "${DESIRED}" ]; then
				sed -i "\|^\*${MOD_ID}/|d" "${MODLIST_FILE}"
				echo "${DESIRED}" >> "${MODLIST_FILE}"
			fi
		done

		for ENTRY in "${MODS_DIR}"/*; do
			[ -L "${ENTRY}" ] && continue
			[ -d "${ENTRY}" ] || continue
			NAME=$(basename "${ENTRY}")
			DESIRED=$(find "${ENTRY}" -maxdepth 1 -name "*.pak" -printf "*${NAME}/%f\n" 2>/dev/null | sort)
			[ -z "${DESIRED}" ] && continue
			EXISTING=$(grep "^\*${NAME}/" "${MODLIST_FILE}" | sort)
			if [ "${EXISTING}" != "${DESIRED}" ]; then
				sed -i "\|^\*${NAME}/|d" "${MODLIST_FILE}"
				echo "${DESIRED}" >> "${MODLIST_FILE}"
			fi
		done
	fi
fi

# Replace Startup Variables
MODIFIED_STARTUP=$(echo ${STARTUP} | sed -e 's/{{/${/g' -e 's/}}/}/g')
echo -e ":/home/container$ ${MODIFIED_STARTUP}"

# Run the Server
eval ${MODIFIED_STARTUP}
