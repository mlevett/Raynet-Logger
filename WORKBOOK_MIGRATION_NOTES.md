# Workbook migration notes

## Source reviewed

`LoggingV5v5b.xlsm`, containing the `OpsNorm` and `MsgLog` worksheets and VBA modules for workbook startup, message entry, timed saves, sound alerts, tactical-call handling and exports.

## Observed operational workflow

1. The workbook requests an event name and creates an event-specific directory, workbook and text log.
2. The operator enters a callsign, which is converted to uppercase and checked against both station and tactical callsigns on `OpsNorm`.
3. The workbook inserts the current time and updates the matched station's last-heard cell.
4. The operator enters message text and selects Sent, Received or Info. Single-letter entries `s`, `r` and `i` are expanded.
5. The row is coloured and locked, then appended to a text file.
6. `OpsNorm` calculates the next check time from the last-heard time and station-specific interval.
7. Check Due and Overdue states are derived, with a sound that can be muted.
8. A timer recalculates the workbook and periodically saves it.
9. Closing attempts to create an encrypted copy using an external Windows executable and a fixed key.

## Changes made deliberately

- **Database transactions replace row locking.** A message either commits completely or not at all.
- **Operator identity is recorded.** Every entry, correction, station update and administrative action is attributable to an account.
- **Corrections are versioned.** Controllers cannot silently overwrite history.
- **Voiding is non-destructive.** Incorrect traffic remains visible and auditable.
- **Events replace clear-all operations.** Old logs cannot disappear through an enthusiastic double-click.
- **UTC is stored internally.** The interface renders DTG in the configured operational timezone.
- **Received traffic updates last heard by default.** Sent and Info traffic can update it when the operator explicitly selects that option.
- **No hard-coded encryption key.** The workbook's external encryption mechanism was not reproduced because a fixed embedded key does not provide credible confidentiality.
- **No Windows-only paths or APIs.** Sound, storage and networking use browser/server facilities.

## Compatibility direction

The application is designed as a browser-based PWA because this gives one operational interface across Windows, macOS, Linux, iPadOS and Android. A future Tauri wrapper can package the same interface as native desktop installers without replacing the server or data model.
