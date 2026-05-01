# Deployment

em-phi is designed to run on a schedule — a cron job or systemd timer is all you need. It processes whatever unread emails are waiting, logs every decision, and exits. No long-running process required.

---

## Prerequisites

Complete [gmail-setup.md](gmail-setup.md) and verify em-phi works locally:

```bash
em-phi --config /path/to/config.yaml run --dry-run
```

---

## cron

Add a cron entry to run em-phi every 30 minutes:

```bash
crontab -e
```

```cron
# Run em-phi every 30 minutes
*/30 * * * * /path/to/.venv/bin/em-phi --config /path/to/config.yaml run >> /var/log/em-phi.log 2>&1
```

Replace `/path/to/` with your actual paths. Use `which em-phi` (inside the activated venv) to find the binary path.

To find the correct path:

```bash
cd /path/to/em-phi
source .venv/bin/activate
which em-phi
```

**Environment variables in cron:** cron runs with a minimal environment. Set `ANTHROPIC_API_KEY` explicitly:

```cron
*/30 * * * * ANTHROPIC_API_KEY=sk-ant-... /path/to/.venv/bin/em-phi --config /path/to/config.yaml run >> /var/log/em-phi.log 2>&1
```

Or store it in a file and source it:

```cron
*/30 * * * * . /path/to/em-phi.env && /path/to/.venv/bin/em-phi --config /path/to/config.yaml run >> /var/log/em-phi.log 2>&1
```

Where `/path/to/em-phi.env` contains:

```bash
export ANTHROPIC_API_KEY=sk-ant-...
export EM_PHI_CONFIG=/path/to/config.yaml
```

---

## systemd timer

systemd timers are more reliable than cron — they handle missed runs, log to journald, and integrate with `systemctl`.

Create the service unit at `/etc/systemd/system/em-phi.service`:

```ini
[Unit]
Description=em-phi email filter
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
User=youruser
WorkingDirectory=/path/to/em-phi
Environment=ANTHROPIC_API_KEY=sk-ant-...
Environment=EM_PHI_CONFIG=/path/to/config.yaml
ExecStart=/path/to/.venv/bin/em-phi run
StandardOutput=journal
StandardError=journal
```

Create the timer unit at `/etc/systemd/system/em-phi.timer`:

```ini
[Unit]
Description=Run em-phi every 30 minutes
Requires=em-phi.service

[Timer]
OnBootSec=2min
OnUnitActiveSec=30min
Persistent=true

[Install]
WantedBy=timers.target
```

Enable and start the timer:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now em-phi.timer
```

Check status and logs:

```bash
systemctl status em-phi.timer
journalctl -u em-phi.service -f
```

---

## Raspberry Pi notes

em-phi runs fine on a Raspberry Pi (any model with 512 MB+ RAM).

- Use a user-level cron (`crontab -e` as your user, not root)
- Keep `decisions.db` on the SD card — it's small and write patterns are light (a few inserts per run)
- If running headless, complete `em-phi setup` on a machine with a browser first, copy `token.json` to the Pi

---

## Log rotation

em-phi writes one line per email to stdout. If redirecting to a file, add logrotate:

`/etc/logrotate.d/em-phi`:

```
/var/log/em-phi.log {
    weekly
    rotate 8
    compress
    missingok
    notifempty
}
```
