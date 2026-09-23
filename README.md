#### (Choose your language / Choisissez votre langue)

[![Language-English](https://img.shields.io/badge/Language-English-blue)](README.md)
[![Language-Français](https://img.shields.io/badge/Langue-Fran%C3%A7ais-green)](README.fr.md)

# TeleScopi - Raspberry project

![TeleScopi](docs/logo.png)

## General Description

TeleScopi is a Python **home surveillance** project that uses a camera connected to a Raspberry Pi and can be controlled remotely via Telegram. It detects motion, notifies authorized users, and allows them to request photos or videos on demand.

The project can be used with a Raspberry Pi camera module or a USB webcam.

## Features

### Main Features

- Automatic motion detection, with detection adapted to ambient light levels (separate settings for day and night)
- Sending a Telegram alert when motion is detected, including a photo (with the detected area highlighted), followed by an automatically recorded video
- Enabling/disabling motion detection via Telegram
- Taking photos on demand via Telegram
- Recording videos on demand via Telegram
- Sending a daily Telegram message to monitor whether the system is operating correctly
- Telegram communications restricted to explicitly authorized users
- Configuration customization (motion detection sensitivity, video duration, etc.)

### Error Handling

- Temporary storage of photos/videos in the event of a transfer error
- Preservation of pending photos/videos after a restart
- Preservation of logs after a restart
- Automatic retry of unsent photos/videos
- Automatic service restart in the event of a problem
- Automatic cleanup of old files if disk space becomes insufficient or if the files are too old

## Required Hardware

This project has been tested with:

- Raspberry Pi 3 model B [link](https://www.raspberrypi.com/products/raspberry-pi-3-model-b/)
- Raspberry Pi Camera Module v2 NoIR [link](https://www.raspberrypi.com/products/pi-noir-camera-v2/)
- a USB webcam (e.g., Logitech C310)
- Raspberry Pi OS Lite (64-bit)
- Carte microSD 32 Go, classe 10

## Installation

To install the project, follow the steps in the [installation documentation](./docs/en/installation.md).

## Configuration

To configure the project, follow the steps in the [configuration documentation](./docs/en/configuration.md).

## Usage

For help using TeleScoPi, read the [usage documentation](./docs/en/utilisation.md).

## Implementation

To implement the project, review the information in the [implementation documentation](./docs/en/implementation.md).

> [!NOTE]
> The migrations and implementations of this project were assisted by AI (mainly Claude Sonnet 5.0).
