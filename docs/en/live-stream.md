#### (Choose your language / Choisissez votre langue)

[![Language-English](https://img.shields.io/badge/Language-English-blue)](../en/live-stream.md)
[![Language-Français](https://img.shields.io/badge/Langue-Fran%C3%A7ais-green)](../fr/live-stream.md)

# Live Stream (Optional, `CAMERA_BACKEND=usb` only)

TeleScoPi can push an on-demand RTSP live view of the camera, started and stopped from Telegram with `/live_start` and `/live_stop` (see [Usage](./utilisation.md)). This feature is **off by default** and only supported with the USB webcam backend.

**Only use this with a VPN.** The stream has no built-in TLS; it must never be exposed directly to the internet (no router port-forward). This guide uses [Tailscale](https://tailscale.com) (a free, WireGuard-based mesh VPN) so the Raspberry Pi and your viewing devices (phone, laptop) join a private network and the RTSP port is reachable only inside that tunnel.

> `scripts/telescopi_setup.sh` automates steps 1-4 below (Tailscale install, MediaMTX install/config, firewall rule, `.env` entries) when you answer "y" to the live stream prompt during a `CAMERA_BACKEND=usb` install. The steps are kept here for reference, to re-run manually, or to set up the viewing devices (step 1, second half).

## Architecture

- [MediaMTX](https://github.com/bluenviron/mediamtx), a lightweight standalone RTSP server, runs as its own systemd service on the Raspberry Pi, bound to `127.0.0.1:8554`/`127.0.0.1:9997` plus whatever interface your firewall allows (the Tailscale interface, `tailscale0`).
- `telescopi.py` encodes the USB webcam feed with `ffmpeg` and pushes it to MediaMTX only while a stream is active (`/live_start`); it auto-stops after `LIVE_STREAM_IDLE_TIMEOUT_SECONDS` without a viewer, or on `/live_stop`.
- Your RTSP client (VLC, Home Assistant, etc.) connects to MediaMTX over the Tailscale network.

## 1. Install Tailscale

On the Raspberry Pi, connected via SSH:

```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up
```

Follow the printed URL to authenticate the Pi to your tailnet. Its Tailscale IP stays stable across reboots (yours and your internet box's) since it does not depend on your LAN/WAN address; it only changes if the node is removed, reset, or reinstalled. Prefer its MagicDNS hostname over the raw IP - more readable, and it keeps working even in the rare case the IP is reassigned:

```bash
tailscale status --self
```

The first column of your own row is the Tailscale IP, the second is the MagicDNS hostname (e.g. `homepi`, usable as `homepi.<your-tailnet>.ts.net` from any device on the tailnet).

Install Tailscale on every device you'll use to watch the stream from (phone, laptop) via the official apps: https://tailscale.com/download, and sign in to the same tailnet.

### Restrict tailnet access to the stream only (ACL)

By default, a new tailnet's policy allows every device to reach every other device on every port - that would expose SSH and anything else running on the Pi to every device on your tailnet. Tags are for **service devices only** (never tag phones/laptops - it replaces their user identity and breaks Tailscale SSH to their own devices, per Tailscale's own guidance). Tag only the Pi, and restrict the ACL to the RTSP port:

1. Open the [Access Controls](https://login.tailscale.com/admin/acl/file) page of the admin console.
2. Define a tag for the Pi and an ACL rule that only allows TCP port `8554` to it, replacing `you@example.com` with your Tailscale login:
   ```json
   {
     "tagOwners": {
       "tag:pi": ["you@example.com"],
     },
     "acls": [
       {
         "action": "accept",
         "src":    ["autogroup:member"],
         "proto":  "tcp",
         "dst":    ["tag:pi:8554"],
       },
     ],
   }
   ```
   With no other `acls` entries, every other port (including SSH, 22) is unreachable from the rest of the tailnet - Tailscale denies by default, only explicit `accept` rules open anything.
3. Apply the tag on the Pi (requires re-authenticating it):
   ```bash
   sudo tailscale up --advertise-tags=tag:pi --force-reauth
   ```
4. Verify: from your phone/laptop, connecting to `<pi-tailscale-ip>:8554` works, but `ssh <pi-tailscale-ip>` or any other port times out.

This only restricts access **over the tailnet**; it does not affect SSH reachability on your local LAN (firewall the Pi separately if that also needs locking down).

## 2. Install MediaMTX

1. On the Raspberry Pi, check the latest release and download the `linux_arm64` archive from the [Releases page](https://github.com/bluenviron/mediamtx/releases) (Raspberry Pi OS Lite 64-bit = `arm64`), for example:
   ```bash
   cd /tmp
   curl -LO https://github.com/bluenviron/mediamtx/releases/latest/download/mediamtx_$(curl -s https://api.github.com/repos/bluenviron/mediamtx/releases/latest | grep -oP '"tag_name": "\K[^"]+')_linux_arm64.tar.gz
   tar xzf mediamtx_*_linux_arm64.tar.gz
   sudo mv mediamtx /usr/local/bin/mediamtx
   sudo chmod +x /usr/local/bin/mediamtx
   ```
2. Generate two strong, distinct random passwords (one for telescopi to publish, one for your RTSP viewers to read), for example:
   ```bash
   openssl rand -base64 24   # run twice, keep both outputs
   ```
3. Copy [config/mediamtx.yml.template](../../config/mediamtx.yml.template) to `/usr/local/etc/mediamtx.yml` and replace `${LIVE_STREAM_PUBLISH_USER}`, `${LIVE_STREAM_PUBLISH_PASSWORD}`, `${LIVE_STREAM_READ_USER}`, `${LIVE_STREAM_READ_PASSWORD}` with your own values (publish/read user names can be anything, e.g. `telescopi`/`viewer`).
4. Copy [config/mediamtx.service.template](../../config/mediamtx.service.template) to `/etc/systemd/system/mediamtx.service`, replacing `${USER}` with your Raspberry Pi username.
5. Enable and start it:
   ```bash
   sudo systemctl daemon-reload
   sudo systemctl enable --now mediamtx
   sudo systemctl status mediamtx
   ```

## 3. Restrict network access (defense in depth)

Even though the stream is meant to be reached over Tailscale, MediaMTX listens on all interfaces by default. If you use `ufw`, restrict the RTSP port to the Tailscale interface and loopback only:

```bash
sudo ufw allow in on tailscale0 to any port 8554 proto tcp
sudo ufw deny 8554/tcp
```

## 4. Configure TeleScoPi

Edit `~/telescopi/config/.env` and add (matching the credentials set in `mediamtx.yml`):

```bash
LIVE_STREAM_ENABLED=1
LIVE_STREAM_QUALITY=reduced   # or "full"; see docs/en/configuration.md
LIVE_STREAM_PUBLISH_USER=telescopi
LIVE_STREAM_PUBLISH_PASSWORD=<publish password generated above>
LIVE_STREAM_READ_USER=viewer
LIVE_STREAM_READ_PASSWORD=<read password generated above>
LIVE_STREAM_VIEWER_URL=rtsp://<pi-hostname>.<your-tailnet>.ts.net:8554/cam
```

Restart the service: `sudo systemctl daemon-reload && sudo systemctl restart telescopi`.

See [Configuration](./configuration.md) for the full list of `LIVE_STREAM_*` variables and their defaults.

## 5. Watch the stream

1. On Telegram, send `/live_start`. The bot replies with the RTSP URL and the read username; it never sends the password over Telegram.
2. Open the URL in VLC (`Media` → `Open Network Stream`) or any RTSP-capable client/NVR, from a device connected to the same tailnet. The client will prompt for the username/password (MediaMTX replies `401` until provided); enter the `LIVE_STREAM_READ_USER`/`LIVE_STREAM_READ_PASSWORD` values from `~/telescopi/config/.env`.
3. Send `/live_stop` when done, or let it auto-stop after `LIVE_STREAM_IDLE_TIMEOUT_SECONDS` without a viewer.

## Resource trade-off

On a Raspberry Pi 3B, a live stream running at the same time as a motion/manual recording means **two** concurrent software H.264 encodes. `LIVE_STREAM_QUALITY=reduced` (default) keeps the live stream at a lower resolution/fps/bitrate than recordings to limit this extra CPU load; switch to `full` only if you have tested the Pi handles both loads at once.
