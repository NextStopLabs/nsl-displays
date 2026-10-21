# Operations - sync, updates, backups

## Sync order (hotspot)
1. program data + config first, 2. audio by hash (resume + verify), 3. software last.
Short hotspot windows still land the important changes. Sync never clears an override.

## Update mode
Keypad `.` / local UI button / portal command: drop AP, join hotspot N min (default 5), return to AP.

## Software update (signed A/B)
Releases built + signed offline (`tools/release.py genkey|sign`). Pi verifies against
baked-in public key, installs to inactive slot, symlink switch, restarts, health-checks
(agent, core, player, display, local UI). Rolls back + reports on failure.
Never switches mid-service unless forced; waits for idle/restart/Apply-now.

## Backups
Nightly: `python3 tools/backup.py` (pg_dump or sqlite copy + storage copy).
Restore drill: restore DB, restore storage dir, restart portal, verify manifest + bundle sha.
Retention: keep active + previous two program versions per program on the Pi;
delete older files, then GC unreferenced audio.

## Diagnostics (local UI)
Connection, last sync, pairing code, test pattern/message/announcement,
live keypad/GPIO state, logs, check-for-updates, restart/shutdown.
`vcgencmd get_throttled` must read `0x0` under full-white load; drop source-to-panel ≤100 mV.
