#!/bin/bash

########################################
# TeleScoPi Setup Script
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
echo "TeleScoPi Setup Script"
echo "###################################################"
echo
echo "Date : ${date}"
echo "Log file : ${log_file_name}"
echo

################
# .env file configuration
################

echo "Starting TeleScoPi configuration..."

mkdir -p "${HOME}"/telescopi
rm -f "${HOME}"/telescopi/config/.env
touch "${HOME}"/telescopi/config/.env

read -r -s -p"Telegram settings : what is your Telegram bot token ? " A_BOT_TOKEN
echo
read -r -s -p"Telegram settings : what is your Telegram personal chat ID ? " A_CHAT_IDS
echo
while true; do
    read -r -p "Add another chat ID? (y/N) " answer
    echo

    case "$answer" in
        [Yy]|[Yy][Ee][Ss])
            read -r -s -p"Enter the additional chat ID : " chat_id
            echo
            A_CHAT_IDS="${A_CHAT_IDS},${chat_id}"
            ;;
        *)
            break
            ;;
    esac
done

read -r -s -p "Camera settings : picam or usb ? " A_CAMERA_TYPE
echo
while [[ "${A_CAMERA_TYPE}" != "picam" && "${A_CAMERA_TYPE}" != "usb" ]]; do
    echo "Invalid camera type. Please enter 'picam' or 'usb'."
    read -r -s -p "Camera settings : picam or usb ? " A_CAMERA_TYPE
    echo
done

cat > "${HOME}"/telescopi/config/.env <<EOF
BOT_TOKEN=${A_BOT_TOKEN}
ALLOWED_USER_IDS=${A_CHAT_IDS}
CAMERA_BACKEND=${A_CAMERA_TYPE}
EOF

chmod 600 "${HOME}"/telescopi/config/.env
chown "${USER}:${USER}" "${HOME}"/telescopi/config/.env

echo "Configuration completed successfully."

################
# Required packages
################

echo "Installing required packages..."
sudo apt-get update
sudo apt-get install -y python3 python3-pip
sudo apt install -y python3-picamera2 --no-install-recommends
sudo apt install -y python3-opencv python3-venv ffmpeg
echo "Required packages installed successfully."

################
# Init python venv
################

echo "Initializing Python virtual environment..."

python3 -m venv --system-site-packages "${HOME}"/telescopi/venv
source "${HOME}"/telescopi/venv/bin/activate
pip install -r "${HOME}"/telescopi/requirements.txt
python3 -c "from picamera2 import Picamera2; import cv2, numpy; print('OK')"
echo "Python virtual environment initialized successfully."

################
# Live stream (optional, usb backend only)
################

A_ENABLE_LIVE_STREAM="n"
if [[ "${A_CAMERA_TYPE}" == "usb" ]]; then
    read -r -p "Enable the optional on-demand RTSP live stream, reachable over a Tailscale VPN (see docs/en/live-stream.md)? (y/N) " A_ENABLE_LIVE_STREAM
    echo
fi

case "$A_ENABLE_LIVE_STREAM" in
    [Yy]|[Yy][Ee][Ss])
        echo "Setting up the live stream (Tailscale + MediaMTX)..."

        # VPN: the RTSP stream has no built-in TLS and must never be exposed directly to the internet.
        if ! command -v tailscale >/dev/null 2>&1; then
            curl -fsSL https://tailscale.com/install.sh | sh
        fi
        echo "Follow the printed URL to authorize this Raspberry Pi on your tailnet..."
        sudo tailscale up
        # MagicDNS hostname (e.g. homepi.tailnet-name.ts.net): more stable/readable than the raw
        # Tailscale IP, and stays correct even if the IP is ever reassigned by a tailnet admin.
        LIVE_STREAM_TAILSCALE_HOST=$(tailscale status --peers=false --json | grep -oP '"DNSName":\s*"\K[^"]+' | head -1)
        LIVE_STREAM_TAILSCALE_HOST=${LIVE_STREAM_TAILSCALE_HOST%.}

        # MediaMTX: lightweight standalone RTSP server, installed as its own systemd service.
        MEDIAMTX_TAG=$(curl -s https://api.github.com/repos/bluenviron/mediamtx/releases/latest | grep -oP '"tag_name": "\K[^"]+')
        curl -LO "https://github.com/bluenviron/mediamtx/releases/latest/download/mediamtx_${MEDIAMTX_TAG}_linux_arm64.tar.gz"
        tar xzf "mediamtx_${MEDIAMTX_TAG}_linux_arm64.tar.gz" mediamtx
        sudo mv mediamtx /usr/local/bin/mediamtx
        sudo chmod +x /usr/local/bin/mediamtx
        rm -f "mediamtx_${MEDIAMTX_TAG}_linux_arm64.tar.gz"

        export LIVE_STREAM_PUBLISH_USER="telescopi"
        export LIVE_STREAM_PUBLISH_PASSWORD
        LIVE_STREAM_PUBLISH_PASSWORD=$(openssl rand -base64 24)

        read -r -p "Live stream settings: username for RTSP viewers (read access)? " LIVE_STREAM_READ_USER
        echo
        while [[ -z "${LIVE_STREAM_READ_USER}" ]]; do
            echo "Username cannot be empty."
            read -r -p "Live stream settings: username for RTSP viewers (read access)? " LIVE_STREAM_READ_USER
            echo
        done
        export LIVE_STREAM_READ_USER
        export LIVE_STREAM_READ_PASSWORD
        LIVE_STREAM_READ_PASSWORD=$(openssl rand -base64 24)

        dos2unix "${HOME}"/telescopi/config/mediamtx.yml.template
        envsubst < "${HOME}"/telescopi/config/mediamtx.yml.template > "${HOME}"/telescopi/config/mediamtx.yml
        sudo mkdir -p /usr/local/etc
        sudo cp "${HOME}"/telescopi/config/mediamtx.yml /usr/local/etc/mediamtx.yml

        dos2unix "${HOME}"/telescopi/config/mediamtx.service.template
        envsubst < "${HOME}"/telescopi/config/mediamtx.service.template > "${HOME}"/telescopi/config/mediamtx.service
        sudo cp "${HOME}"/telescopi/config/mediamtx.service /etc/systemd/system/mediamtx.service

        sudo systemctl daemon-reload
        sudo systemctl enable --now mediamtx.service

        # Defense in depth: restrict the RTSP port to the Tailscale interface when ufw is present.
        if command -v ufw >/dev/null 2>&1; then
            sudo ufw allow in on tailscale0 to any port 8554 proto tcp
            sudo ufw deny 8554/tcp
        else
            echo "ufw not found: firewall the RTSP port (8554/tcp) to the tailscale0 interface manually, see docs/en/live-stream.md."
        fi

        cat >> "${HOME}"/telescopi/config/.env <<EOF
LIVE_STREAM_ENABLED=1
LIVE_STREAM_QUALITY=reduced
LIVE_STREAM_PUBLISH_USER=${LIVE_STREAM_PUBLISH_USER}
LIVE_STREAM_PUBLISH_PASSWORD=${LIVE_STREAM_PUBLISH_PASSWORD}
LIVE_STREAM_READ_USER=${LIVE_STREAM_READ_USER}
LIVE_STREAM_READ_PASSWORD=${LIVE_STREAM_READ_PASSWORD}
LIVE_STREAM_VIEWER_URL=rtsp://${LIVE_STREAM_TAILSCALE_HOST}:8554/cam
EOF

        echo "Live stream configured. Install Tailscale on your viewing devices too (https://tailscale.com/download) before using /live_start."
        ;;
    *)
        echo "Skipping the live stream setup (can be configured later, see docs/en/live-stream.md)."
        ;;
esac

################
# Service setup
################

echo "Setting up TeleScoPi service and its logs retention..."

dos2unix "${HOME}"/telescopi/config/telescopi.service.template
envsubst < "${HOME}"/telescopi/config/telescopi.service.template > "${HOME}"/telescopi/config/telescopi.service
sudo cp "${HOME}"/telescopi/config/telescopi.service /etc/systemd/system/telescopi.service

sudo mkdir -p /var/log/journal
sudo systemd-tmpfiles --create --prefix /var/log/journal
sudo mkdir -p /etc/systemd/journald.conf.d
rm -f "${HOME}"/telescopi/config/telescopi-retention.conf
touch "${HOME}"/telescopi/config/telescopi-retention.conf
cat > "${HOME}"/telescopi/config/telescopi-retention.conf <<EOF
[Journal]
Storage=persistent
MaxRetentionSec=7day
SystemMaxUse=200M
EOF
sudo cp "${HOME}"/telescopi/config/telescopi-retention.conf /etc/systemd/journald.conf.d/telescopi-retention.conf
sudo systemctl restart systemd-journald
sudo journalctl --flush
sudo journalctl --rotate

mkdir -p /home/pi/telescopi/logs
CRON_JOB=$(cat <<EOF
55 23 * * * journalctl -u telescopi --since "00:00" --until "23:59" > "$HOME/telescopi/logs/telescopi-\$(date +\%F).log" 2>&1 && find "$HOME/telescopi/logs" -name "telescopi-*.log" -mtime +7 -delete
EOF
)
(crontab -l 2>/dev/null | grep -F -q "$CRON_JOB") || \
(
    crontab -l 2>/dev/null
    echo "$CRON_JOB"
) | crontab -


sudo systemctl daemon-reload
sudo systemctl enable telescopi.service
sudo systemctl start telescopi.service

echo "TeleScoPi service setup completed successfully."

echo "Setup script completed."

exit 0;