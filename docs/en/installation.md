#### (Choose your language / Choisissez votre langue)

[![Language-English](https://img.shields.io/badge/Language-English-blue)](../en/installation.md)
[![Language-Français](https://img.shields.io/badge/Langue-Fran%C3%A7ais-green)](../fr/installation.md)

# Installing TeleScoPi

This guide explains how to install TeleScoPi on a Raspberry Pi. It covers hardware preparation, operating system installation, network configuration, and the installation of the dependencies required to run the project.

## Prerequisites

Before starting the installation, make sure you have the following hardware and software:

- A Raspberry Pi (3B or newer)
- A camera module compatible with the Raspberry Pi (Camera Module v1/v2/v3 or HQ Camera) or a USB webcam (e.g., Logitech C310)
- A microSD card (16 GB minimum, 32 GB recommended, Class 10 / A1)
- A 5V/2.5A micro-USB power supply for the Raspberry Pi
- A computer (Windows/Mac/Linux) with an SD card reader, to prepare the card
- An Internet connection for the Raspberry Pi and the computer
- A Telegram account to configure the bot and receive notifications

## Operating System Installation

For this project, we recommend using Raspberry Pi OS Lite (64-bit). You can flash the microSD card with Raspberry Pi Imager by following the steps below:

1. Download and install Raspberry Pi Imager from the official website: [raspberrypi.com/software](https://www.raspberrypi.com/software)
2. Launch the Raspberry Pi Imager application and insert your microSD card into your computer's card reader.
3. Click "Choose Device" and select "Raspberry Pi 3".
4. Click "Choose OS" and select "Raspberry Pi OS (other)" → "Raspberry Pi OS Lite (64-bit)".
5. Click "Choose Storage" and select your microSD card.
6. Configure the hostname, Wi-Fi network, and user according to your preferences. In the rest of this documentation, the hostname used will be `homepi` and the admin user will be `pi`.
7. Enable SSH access.
8. Click "Write" to flash the operating system onto the microSD card.
9. Once the writing process is complete, safely remove the microSD card from your computer.

## Project Initialization

After flashing and inserting the microSD card into the Raspberry Pi, follow these steps to initialize the project:

1. Insert the microSD card into the Raspberry Pi and connect the power supply.
2. Connect to the Raspberry Pi via SSH on your local network: `ssh pi@homepi.local`.
3. Create a directory for the TeleScoPi project on the Raspberry Pi: `mkdir -p ~/telescopi`
4. Update the system by running the following commands:
   ```bash
   sudo apt update
   sudo apt upgrade -y
   sudo apt full-upgrade -y
   ```
5. Restart the Raspberry Pi to apply the updates: `sudo reboot`

### Wi-Fi Network Configuration

An Internet connection is required so that the project can communicate with Telegram and send notifications. The Raspberry Pi must therefore be configured to connect to the Internet, either via Ethernet or Wi-Fi, and to retain its authentication settings, which must be configured when installing Raspberry Pi OS.

**If your local Wi-Fi network is configured with MAC address allowlist restrictions, the Raspberry Pi must always use the same MAC address.** If your local network does not have MAC address allowlist restrictions, this step can be skipped.

To do this, you must first determine its MAC address. You will therefore need to disable this restriction on your network during the initial setup, so that the Raspberry Pi can connect for the first time, before re-enabling it afterward.

1. Temporarily disable the MAC address allowlist restriction on your Wi-Fi network.
2. Allow the Raspberry Pi to connect to your Wi-Fi network.
3. Connect to the Raspberry Pi via SSH on your local network: `ssh pi@homepi.local`.
4. Check the Raspberry Pi's MAC address by running the following command: `ip -br link show wlan0`.
5. Configure the Raspberry Pi to always use the same MAC address: `sudo nano /etc/NetworkManager/conf.d/100-disable-wifi-mac-randomization.conf`
   
   Add the following lines to the file:
   ```ini
   [connection]
    wifi.cloned-mac-address=preserve

    [device]
    wifi.scan-rand-mac-address=no
   ```
6. Apply these changes: `sudo nmcli general reload conf`, then `sudo systemctl restart NetworkManager`
7. Add the Raspberry Pi's MAC address to your Wi-Fi network's allowlist to ensure that it can always connect.
8. Re-enable the MAC address allowlist restriction on your Wi-Fi network.

### Telegram Configuration

If the Telegram bot creation process changes, please refer to the official documentation for the most up-to-date instructions: https://core.telegram.org/bots#how-do-i-create-a-bot 

1. Create a bot on Telegram by contacting BotFather: `@BotFather`, and follow its instructions to create a new bot (using the `/newbot` command).
2. Make a note of the access token provided by BotFather after the bot is created. You will need it to configure the bot on the Raspberry Pi.
3. Contact the `@userinfobot` bot to obtain your Telegram user ID. You will need it to configure the bot on the Raspberry Pi.

### Camera Verification

You need to verify that the camera you have chosen is correctly identified.

#### Raspberry Pi Camera Module

1. Connect to the Raspberry Pi via SSH on your local network: `ssh pi@homepi.local`.
2. Verify that the camera is correctly detected: `rpicam-hello --list-cameras` (it should display `imx219` for the Camera Module v2). If nothing appears, check the ribbon cable connection while the Pi is powered off.

#### USB Webcam

1. Connect to the Raspberry Pi via SSH on your local network: `ssh pi@homepi.local`.
2. Verify that your USB camera appears on the USB ports using `lsusb`.
3. Identify the V4L2 path of your camera by analyzing the output of `v4l2-ctl --list-devices` (for example, a Logitech webcam appears under the name "UVC Camera") and note the first device path (for example, "/dev/video1").
4. Identify the symlink associated with this path in `/dev/v4l/by-id` using `ls -l /dev/v4l/by-id/` because the ID-based path will not change after a reboot.

## Installing TeleScoPi

1. Transfer the TeleScoPi project files to the Raspberry Pi, for example by using `scp -r /local/path/to/TeleScoPi pi@homepi.local:/home/pi/telescopi/`
2. Connect to the Raspberry Pi via SSH: `ssh pi@homepi.local`.
3. Go to the project directory on the Raspberry Pi: `cd ~/telescopi`
4. Fix the installation script if necessary: `dos2unix scripts/telescopi_setup.sh`
5. Grant execute permissions to the script: `chmod +x scripts/telescopi_setup.sh`
6. Run the TeleScoPi installation script and follow its instructions: `scripts/telescopi_setup.sh`.
7. Monitor the TeleScoPi service logs to verify that everything starts correctly: `journalctl -u telescopi -f`.
8. Open the bot's Telegram interface to interact with TeleScoPi and verify that notifications and commands work correctly.

If you wish, you can customize the Telegram bot to suit your preferences, including its avatar, menu, description, and other settings, by following the instructions provided by BotFather on Telegram.
