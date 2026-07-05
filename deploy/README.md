# Deployment & operations (pi5-ai2)

The app runs via docker compose in `/root/PriceMonitorApp` on pi5-ai2.

## Keeping it up

- `restart: unless-stopped` restarts the container after crashes and reboots
  (Docker itself is `systemctl enable`d).
- The compose healthcheck polls `/health`; the `autoheal` sidecar restarts the
  container if it goes unhealthy (hung process), which the restart policy
  alone would never catch.
- Container logs are capped (json-file rotation) so they can't fill the SD card.

## Backups

Nightly at 03:30 (America/Vancouver) a systemd timer runs
[`pricemonitor-backup.sh`](pricemonitor-backup.sh): an online `sqlite3
.backup` of `prices.db`, integrity-checked, gzipped to
`/mnt/backups/pricemonitor/` (ext4 USB drive, fstab `nofail` mount), rotated
to the newest 30. `.env` and `docker-compose.yml` are snapshotted alongside.
Status: `/mnt/backups/pricemonitor/last-status.txt` and
`journalctl -t pricemonitor-backup`.

Install on a fresh host:

```bash
cp deploy/pricemonitor-backup.sh /usr/local/bin/
cp deploy/pricemonitor-backup.{service,timer} /etc/systemd/system/
systemctl daemon-reload && systemctl enable --now pricemonitor-backup.timer
```

## Restore

```bash
cd /root/PriceMonitorApp && docker compose stop app
gunzip -c /mnt/backups/pricemonitor/prices-<STAMP>.db.gz \
  > /var/lib/docker/volumes/pricemonitorapp_app_data/_data/prices.db
docker compose start app
```
