#!/bin/bash

########################################
# TeleScoPi Download Script
########################################

set -e

date=$(date '+%Y%m%d%H%M%S')

# logfile
current_file_name=$(basename "$0")
log_file_name=${current_file_name}.${date}.log
exec > >(tee "${log_file_name}") 2>&1

###################################################
################### EXECUTION #####################
###################################################

echo "###################################################"
echo "TeleScoPi Download Script"
echo "###################################################"
echo
echo "Date : ${date}"
echo "Log file : ${log_file_name}"
echo

GITHUB_REPO="shizuka42/telescopi"
INSTALL_DIR="${HOME}/telescopi"

################
# Version selection
################

read -r -p "Which TeleScoPi version do you want to install ? (tag, e.g. v1.0.1, empty = latest) : " A_VERSION
echo

if [[ -z "${A_VERSION}" ]]; then
    echo "Resolving latest release from GitHub..."
    LATEST_JSON=$(curl -sf "https://api.github.com/repos/${GITHUB_REPO}/releases/latest") \
        || { echo "Error: could not reach GitHub API to resolve the latest release." >&2; exit 1; }
    TAG=$(echo "${LATEST_JSON}" | grep -m1 '"tag_name"' | sed -E 's/.*"tag_name": *"([^"]+)".*/\1/')
    if [[ -z "${TAG}" ]]; then
        echo "Error: could not parse the latest release tag from the GitHub API response." >&2
        exit 1
    fi
else
    TAG="${A_VERSION}"
    # release tags are 'v'-prefixed (e.g. v1.0.1); accept a bare version too
    if [[ "${TAG}" != v* ]]; then
        TAG="v${TAG}"
    fi
fi

echo "Selected version : ${TAG}"
echo

echo "Checking that release ${TAG} exists on ${GITHUB_REPO}..."
curl -sf -o /dev/null "https://api.github.com/repos/${GITHUB_REPO}/releases/tags/${TAG}" \
    || { echo "Error: release ${TAG} not found on ${GITHUB_REPO}." >&2; exit 1; }

################
# Download
################

ARCHIVE_URL="https://github.com/${GITHUB_REPO}/archive/refs/tags/${TAG}.tar.gz"
ARCHIVE_PATH="/tmp/telescopi-${TAG}.tar.gz"

echo "Downloading ${ARCHIVE_URL}..."
curl -sfL -o "${ARCHIVE_PATH}" "${ARCHIVE_URL}" \
    || { echo "Error: failed to download the source archive." >&2; exit 1; }

################
# Extraction
################

if [[ -d "${INSTALL_DIR}" ]]; then
    BACKUP_DIR="${INSTALL_DIR}_backup_${date}"
    echo "Existing ${INSTALL_DIR} found, renaming it to ${BACKUP_DIR}..."
    mv "${INSTALL_DIR}" "${BACKUP_DIR}"
fi

mkdir -p "${INSTALL_DIR}"

echo "Extracting ${ARCHIVE_PATH} into ${INSTALL_DIR}..."
tar -xzf "${ARCHIVE_PATH}" --strip-components=1 -C "${INSTALL_DIR}"

echo "Removing hidden files and folders from the extracted sources..."
find "${INSTALL_DIR}" -mindepth 1 -depth -name '.*' -exec rm -rf {} +

echo "Cleaning up downloaded archive..."
rm -f "${ARCHIVE_PATH}"

################
# Setup script
################

SETUP_SCRIPT=$(find "${INSTALL_DIR}" -type f -name 'telescopi_setup.sh' | head -n1)

if [[ -z "${SETUP_SCRIPT}" ]]; then
    echo "Error: telescopi_setup.sh not found in the downloaded sources." >&2
    exit 1
fi

chmod +x "${SETUP_SCRIPT}"

echo "Download completed successfully."
echo
echo "Run the following command to configure and install TeleScoPi :"
echo
echo "  bash \"${SETUP_SCRIPT}\""
echo

exit 0;
