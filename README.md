# AriYouLive — Edge Monitor

A local-first desktop application. Electron provides a standalone macOS window and menu bar tray; the bundled Python service owns polling, alert state, and the SQLite database. Closing the window hides the app to the menu bar, so it does not stop monitoring. The service binds to `127.0.0.1` by default and does not need an internet connection. There is no user-facing browser workflow.

## Run on macOS

### Run from source

Install Node.js, Python 3, and the Python package PyInstaller. From this directory run `npm install`, then `npm run dev`. Electron opens a native desktop window and adds a menu bar icon. Closing the window keeps the application and monitor in the menu bar; choose **Quit and stop monitoring** from that menu to stop it. For backend-only development, `start.command` still launches the service at `http://127.0.0.1:8765`.

### Build a macOS app

Run `npm install`, install PyInstaller for the Python interpreter on the build machine, then run `npm run package:mac`. Electron Builder creates a `.dmg` and `.zip` under `release/`. Builds are for the Mac architecture on which they are produced. Distribution outside your own Mac may require Apple Developer signing and notarization.

The service creates `ariyou_live.db` beside the application. Set `ARIYOULIVE_DB` to move it, `ARIYOULIVE_HOST` to change its bind address, and `ARIYOULIVE_PORT` to change its port. ICMP probing uses the system `ping` command, which is present on macOS and Windows. The sample configuration includes a private gateway address; replace it with your reachable targets.

Custom MP3/WAV notification sounds are stored locally in the desktop app's user data directory under `audio/` and remain separate from the packaged application files.

## Included

- Background ICMP polling process with per-device interval, timeout, and failure threshold.
- SQLite configuration, IP state, alerts, recent samples, and 15-minute per-IP aggregates. Raw ping samples are pruned according to history retention; aggregates are retained by the same policy.
- Device groups, multi-IP status tiers, JSON configuration export/import, device history, alert acknowledgement, mute duration, and browser notification tones/toasts.
- Toggle **Arrange** to enter the home-screen style edit mode. Drop a device onto a neighbor to swap positions, or onto a main-group/subgroup column to move it. Create a subgroup from a main group's **＋ Subgroup** action. Resize cards with the lower-right handle; dimensions snap to fixed increments and stay within the column bounds. Right-click resize presets remain available. Placement and dimensions are stored locally and included in JSON backups.
- Desktop-style dark interface with responsive layout.

## Current framework limits

The Electron desktop shell, menu bar lifecycle, native macOS notifications, app packaging configuration, and Python service bundling are included. The starter UI currently exposes device history and per-device muting; group-wide and sub-group maintenance and global defaults editing remain follow-on work. The repeating audio cue is rendered by the hidden app window, so it continues while the app is hidden but requires the desktop app to be running. The database and monitor remain active while the app is in the menu bar; quitting the app stops its bundled service.

## Security and closed-network use

The internal service binds only to loopback by default and has no authentication. Keep it on loopback unless you add an access-control layer before exposing it to other machines. JSON backups contain device and network configuration; store them accordingly.
