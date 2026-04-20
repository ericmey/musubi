# Maintenance Scheduled Tasks

These systemd units run the `slice-ops-hardening-suite` offline maintenance workers (retention enforcement and hard-delete cleanup).

## Installation

1. Copy the `.service` and `.timer` files to `/etc/systemd/system/`.
2. Reload the daemon: `sudo systemctl daemon-reload`
3. Enable and start the timers:
   ```bash
   sudo systemctl enable --now musubi-cleanup.timer
   sudo systemctl enable --now musubi-retention.timer
   ```

## Logs
Logs can be viewed via journalctl:
`sudo journalctl -u musubi-cleanup.service -f`
