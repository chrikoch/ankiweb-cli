# Unattended backup acceptance: partial results

Date: 2026-11-05. This acceptance check is **open**.

On 2026-11-06, the user chose to defer the remaining Raspberry Pi, cron and restart checks until a later time if problems arise. Their behavior remains unverified.

## Headless run

On Debian 13.4 x86-64 under WSL2, with Python 3.13.5 and Anki 26.9.3, an editable installation of the current working tree was run against the dedicated synthetic AnkiWeb account. The process received a minimal environment with `PATH` and `LANG`, but without `HOME`, `DISPLAY`, `WAYLAND_DISPLAY`, or a D-Bus session address:

```sh
env -i PATH=/usr/bin:/bin LANG=C.UTF-8 \
  /path/to/venv/bin/ankiweb-cli ankiweb-backup \
  --output-dir /path/to/output --credentials-file /path/to/credentials
```

The command exited successfully and produced one collection package. Its media manifest contained both expected synthetic files, including the unreferenced file. Neither credential value appeared in the captured output. Separate invocations with a missing credentials file and with a synthetic credentials file readable by other users failed with nonzero exit status; the synthetic values did not appear in their output.

## Outstanding checks

- **Actual cron execution:** not verified. An isolated Debian cron daemon was attempted without modifying the system crontab; its test job did not run. In the isolated user namespace, `initgroups` failed with `EPERM`. A temporary PAM configuration was used only inside that namespace. This result does not establish whether a normally configured system cron can run the backup.
- **After restart:** not verified. The successful headless run was not performed after a system restart.
- **Raspberry Pi:** not verified. No accessible target was available for the check.

The headless run does not substitute for any of these checks.
