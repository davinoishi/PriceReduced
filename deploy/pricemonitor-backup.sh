#!/bin/bash
# Nightly backup of the PriceMonitor SQLite DB to the USB drive.
# - Uses sqlite3's online .backup (safe while the app is writing).
# - Verifies the copy with PRAGMA integrity_check before keeping it.
# - Rotates: keeps the newest 30 daily backups.
# - Also snapshots .env + docker-compose.yml (needed for a full restore).
# Restore: gunzip a backup, stop the container, copy it over
#   /var/lib/docker/volumes/pricemonitorapp_app_data/_data/prices.db, start.
set -u

DB=/var/lib/docker/volumes/pricemonitorapp_app_data/_data/prices.db
APP_DIR=/root/PriceMonitorApp
DEST=/mnt/backups/pricemonitor
KEEP=30
STAMP=$(date +%Y%m%d-%H%M%S)

fail() {
    echo "FAILED $STAMP: $1" > "$DEST/last-status.txt" 2>/dev/null
    logger -t pricemonitor-backup "FAILED: $1"
    exit 1
}

# The USB drive is mounted nofail — refuse to write into the bare mountpoint
# directory on the SD card if the drive is missing.
mountpoint -q /mnt/backups || fail "/mnt/backups is not mounted (USB drive missing?)"
mkdir -p "$DEST" || fail "cannot create $DEST"
[ -f "$DB" ] || fail "database not found at $DB"

TMP="$DEST/.inflight-$STAMP.db"
sqlite3 "$DB" ".backup '$TMP'" || { rm -f "$TMP"; fail "sqlite .backup failed"; }

CHECK=$(sqlite3 "$TMP" "PRAGMA integrity_check;") || { rm -f "$TMP"; fail "integrity check errored"; }
[ "$CHECK" = "ok" ] || { rm -f "$TMP"; fail "integrity check: $CHECK"; }

ITEMS=$(sqlite3 "$TMP" "SELECT count(*) FROM item;")
POINTS=$(sqlite3 "$TMP" "SELECT count(*) FROM pricepoint;")

gzip -9 "$TMP" || { rm -f "$TMP" "$TMP.gz"; fail "gzip failed"; }
mv "$TMP.gz" "$DEST/prices-$STAMP.db.gz" || fail "rename failed"

# Config snapshot (contains secrets — the drive stays inside the Pi).
cp "$APP_DIR/.env" "$DEST/env-snapshot" 2>/dev/null
cp "$APP_DIR/docker-compose.yml" "$DEST/docker-compose-snapshot.yml" 2>/dev/null

# Rotate only after a verified backup landed.
ls -1t "$DEST"/prices-*.db.gz 2>/dev/null | tail -n +$((KEEP + 1)) | xargs -r rm -f

SIZE=$(du -h "$DEST/prices-$STAMP.db.gz" | cut -f1)
echo "OK $STAMP: $ITEMS items, $POINTS price points, $SIZE" > "$DEST/last-status.txt"
logger -t pricemonitor-backup "OK: prices-$STAMP.db.gz ($ITEMS items, $POINTS points, $SIZE)"
