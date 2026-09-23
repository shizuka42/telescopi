#### (Choose your language / Choisissez votre langue)

[![Language-English](https://img.shields.io/badge/Language-English-blue)](../en/utilisation.md)
[![Language-Français](https://img.shields.io/badge/Langue-Fran%C3%A7ais-green)](../fr/utilisation.md)

# Using TeleScoPi

To use TeleScoPi, make sure that you have correctly installed and configured the service as described in the [installation guide](./installation.md) and the [configuration guide](./configuration.md).

## Telegram Bot Commands

The following commands are available on the Telegram bot to interact with TeleScoPi:

- `/help`: Get the list of available commands and the current status of the TeleScoPi service.
- `/photo`: Take a photo with the camera.
- `/video`: Record a video with the camera (duration defined in the service configuration).
- `/start_motion`: Enable motion detection with the camera.
- `/stop_motion`: Disable motion detection with the camera.
- `/status`: Get the system status (monitoring status, camera status, detected day/night mode, and time since the last startup).

## Automatic Status Message

In addition to responding to the `/status` command, TeleScoPi automatically sends the same message in two cases:

- **Every day** at the time defined by `PING_TIME` (see the ./configuration.md), to confirm that the system is still operating. The absence of this daily message indicates an extended outage.
- **Whenever the service starts** (initial launch, manual restart, Raspberry Pi reboot, or automatic restart following an error), with a note indicating that the system has just started. This is a simple way to be notified of every restart, whether intentional or not.

## Accessing Logs

The TeleScoPi service logs are retained for 7 days and copied daily to the `~/telescopi/logs` directory for easier access.

To browse the stored service logs while connected to the Raspberry Pi via SSH:

```bash
# All logs for the telescopi service
journalctl -u telescopi

# Follow logs in real time (like tail -f)
journalctl -u telescopi -f

# The last 50 lines
journalctl -u telescopi -n 50

# Since the last service startup
journalctl -u telescopi -b
```