# CourierLedger — scheduled jobs (Phase 3, Stream A)

## Why systemd timers, not cron

`cron` / `crond` is **not installed on this VM** (verified
2026-11-01: `which cron crond` returns nothing) and there is no
cron daemon running. `systemd` **is** the init system
(`systemctl is-system-running` → `running`). So the mechanism
that genuinely works here is a systemd timer, not a crontab
entry that would never fire. The choice is therefore:

- **Primary (this VM and the expected production host):**
  systemd timer units in this directory.
- **Fallback (a host with cron but no systemd):** the equivalent
  crontab lines are at the bottom of this file. They are
  documented, not installed, because cron does not exist here.

Timers also beat cron on the properties this workload needs:
`Persistent=true` replays a missed run after downtime (cron
silently skips it), failures surface in the journal with the
job's own structured log lines (`journalctl -u <service>`),
and `RandomizedDelaySec` spreads load. Nothing about the jobs
themselves is systemd-specific — both entry points are plain
scripts/commands, so the cron fallback runs the identical code.

## Units

| Unit | Schedule | Runs |
|---|---|---|
| `courierledger-backup.service` / `.timer` | Daily 02:30 UTC (08:30 Dhaka) | `backup_encrypted.sh courierledger_app` — encrypted `pg_dump -Fc` + globals, JSON sidecar, verify, retention |
| `courierledger-maintenance.service` / `.timer` | Hourly | `maintenance.py --db courierledger_app --db courierledger_dev` — expired session + reset-token purge (B-F19) |

All paths in the units are absolute for this VM. On another host,
adjust the repo path (`/home/hatch/workspace/goals/
courierledger-planning-and-market-validation`) and the venv path
to match that host — the scripts themselves take no host-specific
arguments beyond the database name.

## Install (this VM — already done 2026-11-01)

```bash
# 1. Backup passphrase (generated once with `openssl rand -base64 32`;
#    the value is NOT in this repo and NOT in this file). It lives
#    under /home/hatch/.config — NOT /etc — because this VM's
#    container is replaced repeatedly and /etc does not survive a
#    replacement, while the home directory does. A passphrase kept
#    only in /etc was lost exactly this way on 2026-11-01, orphaning
#    an encrypted backup (see DATABASE_BACKUP_RECOVERY.md).
mkdir -p /home/hatch/.config/courierledger
openssl rand -base64 32 > /home/hatch/.config/courierledger/backup.passphrase
chmod 600 /home/hatch/.config/courierledger/backup.passphrase

# 2. Environment file the backup service reads (one line, 0600):
printf 'CL_BACKUP_PASSPHRASE_FILE=/home/hatch/.config/courierledger/backup.passphrase\n' \
  | sudo tee /etc/courierledger/backup.env >/dev/null
sudo chmod 600 /etc/courierledger/backup.env

# 3. Install + enable the timers:
sudo cp m1/scripts/systemd/courierledger-*.service m1/scripts/systemd/courierledger-*.timer \
  /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now courierledger-backup.timer courierledger-maintenance.timer

# 4. Verify:
systemctl list-timers courierledger-backup.timer courierledger-maintenance.timer
systemctl status courierledger-backup.timer courierledger-maintenance.timer
```

To prove the timer path (not just the scripts) works, trigger a
run on demand without waiting for the schedule:

```bash
sudo systemctl start courierledger-backup.service
sudo systemctl start courierledger-maintenance.service
journalctl -u courierledger-backup.service -n 20
journalctl -u courierledger-maintenance.service -n 20
```

## Failure visibility

- Both jobs log **one JSON line per run** (backup) / **per
  database** (maintenance) and exit non-zero on failure.
  A failed oneshot service shows as `failed` in
  `systemctl status` and in the journal — that is the alert
  channel on this VM. There is **no email/webhook alert yet**:
  adding one is a founder decision (needs a mailbox or webhook
  URL) — see DATABASE_BACKUP_RECOVERY.md § "Still missing".
- A backup that fails verification deletes its own artifacts
  and reports `backup_failed`; it never leaves an unverified
  backup in place pretending to be good.
- A maintenance run that finds the advisory lock held reports
  `skipped` and exits 0 — overlap is not a failure.

## Cron fallback (documented only — cron is not installed here)

If a future host has cron but no systemd, these lines run the
identical jobs (passphrase from the same `/etc/courierledger/`
files; maintenance needs the backend venv on PATH):

```cron
# CourierLedger backup — daily 02:30 UTC
30 2 * * * root CL_BACKUP_PASSPHRASE_FILE=/home/hatch/.config/courierledger/backup.passphrase /home/hatch/workspace/goals/courierledger-planning-and-market-validation/m1/scripts/backup_encrypted.sh courierledger_app >> /var/log/courierledger-backup.log 2>&1
# CourierLedger maintenance — hourly
0 * * * * root /home/hatch/workspace/goals/courierledger-planning-and-market-validation/m1/backend/.venv/bin/python /home/hatch/workspace/goals/courierledger-planning-and-market-validation/m1/scripts/maintenance.py --db courierledger_app --db courierledger_dev >> /var/log/courierledger-maintenance.log 2>&1
```

Cron has no `Persistent` equivalent: a missed run after
downtime is silently skipped, and failure alerting depends on
cron mail, which is why the timer is primary wherever systemd
exists.
