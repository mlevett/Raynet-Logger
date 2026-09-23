# RAYNET Message Logger

**Version 1.0 — public testing release**

Created by **Mathew Levett (M0NFZ)** for South East Hampshire RAYNET.

This release is ready for user testing, but should be evaluated on exercises before it is relied upon for live operational work. Please report faults and usability feedback through the repository's GitHub Issues page. Never publish a live database with personal or operational information in an issue.

A self-hosted, installable web application for RAYNET message handling and operator welfare checks. It runs in a browser, so the same build works on Windows, macOS, Linux, iPadOS and Android. Multiple operators can use one event at the same time over a LAN, Wi-Fi hotspot or VPN.

## What version 1.0 includes

- Event-based logs instead of destructive workbook resets
- Live multi-user synchronisation using WebSockets
- Accounts and roles: Admin, Controller, Operator and Viewer
- Sequential message numbers, automatic timestamps and operator attribution
- Received, Sent and Info traffic with Routine, Priority and Immediate classifications
- Fast callsign autocomplete from the event station roster, showing callsign and tactical call together
- Callsign/tactical-call consistency warning, with an explicit override
- Automatic last-heard updates from received traffic
- Operators board with on-duty, off-duty and stood-down states, individual check intervals, overdue status and audible alerts
- Explicit Control status for operators who do not require welfare checks or overdue alerts
- Event setup wizard for permanent users and non-account operators, with individual welfare intervals
- Event notes, locations, contacts and analogue/DMR radio-plan details
- Event-specific user assignments with tactical calls, kept separate from global login accounts
- Message corrections with retained versions and reasons
- Voiding without deleting the original record
- Append-only audit history
- Branded PDF, CSV, plain-text and JSON exports covering selected event records
- Configurable organisation name, header text and logo, plus light and dark themes
- Administrative event deletion with confirmation
- SQLite database in WAL mode, transactional writes and scheduled database backups
- Responsive, installable PWA interface
- Docker and one-click-ish Windows/Linux/macOS launch scripts

## Workbook behaviours carried across

The supplied `LoggingV5v5b.xlsm` was inspected, including its VBA modules. The app retains the useful workflow from the workbook:

| Workbook behaviour | App equivalent |
|---|---|
| Event name requested at startup | Create/select persistent events |
| DTG and time inserted automatically | UTC timestamp stored, Europe/London DTG displayed |
| `s`, `r`, `i` traffic categories | Received, Sent and Info controls, plus Alt+R/S/I |
| Callsign checked against Ops Normal | Operators list with unified callsign and tactical-call validation |
| Message entry updates last heard | Received traffic updates the matching station |
| Completed rows locked | Submitted records are immutable to operators; controllers make versioned corrections |
| Text log written line by line | Every message is committed transactionally to SQLite |
| Periodic workbook save | Immediate commits plus periodic consistent database snapshots |
| Warning sound for overdue checks | Browser alert tone enabled by default, with a mute control |
| Clear-all buttons | Close an event and start another, preserving the evidence trail |

The workbook's hard-coded Windows paths and hard-coded encryption key were deliberately not copied. They would provide a padlock-shaped sticker rather than meaningful protection.

## Quick start with Docker

1. Install Docker Desktop or Docker Engine.
2. Open a terminal in this folder.
3. Run:

```bash
docker compose up -d --build
```

4. Open `http://localhost:8080`.
5. Create the first administrator account.

Other devices on the same network can connect to `http://SERVER-IP:8080`.

## Downloading a test copy

Download the stable [version 1.0 source ZIP](https://github.com/mlevett/Raynet-Logger/archive/refs/tags/v1.0.zip), or use **Code → Download ZIP** on GitHub for the current development version. Extract it before running either startup script.

## Windows without Docker

Double-click `start_windows.bat`. It creates a Python virtual environment, installs the two application dependencies, starts the server and opens the browser.

## Linux or macOS without Docker

```bash
chmod +x start_linux_mac.sh
./start_linux_mac.sh
```

Then open `http://localhost:8080`.

## Network deployment

The server listens on all interfaces when started by the supplied scripts. For a small exercise, a laptop can host it and other operators can connect through the laptop's Wi-Fi hotspot or a portable router.

For operational use:

- Use a private LAN, WireGuard/Tailscale VPN, or an HTTPS reverse proxy.
- Do not expose port 8080 directly to the public internet.
- Set `RAYNET_SECURE_COOKIE=1` only when HTTPS is in use.
- Back up both `data/` and `backups/` to separate media.
- Keep server time synchronised with NTP.

## Data and backups

The live database is `data/raynet_logger.db`. A consistent snapshot is written to `backups/` every five minutes by default. The newest 48 snapshots are retained. These settings can be changed in `docker-compose.yml` or with environment variables.

Manual backup:

```bash
python tools/backup_now.py
```

## Roles

- **Admin:** users, events, stations, messages, corrections, audit and backup
- **Controller:** events, stations, messages, corrections and audit
- **Operator:** enter messages and mark stations heard
- **Viewer:** read-only situational awareness

## Keyboard shortcuts

- In the callsign field, type part of a callsign or tactical call, use `↑`/`↓`, then press `Enter` to insert the station callsign
- `Ctrl+Enter`: log the current message
- `Alt+R`: Received
- `Alt+S`: Sent
- `Alt+I`: Info

## Running the automated test

```bash
python -m pip install -r requirements.txt pytest httpx
pytest -q
```

## License

This project is released under the GNU General Public License v3.0. See `LICENSE` for the full terms.
