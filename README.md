# HostPulse

[English](README.md) | [فارسی](README.fa.md)

Detects high-resource-usage users on DirectAdmin + CloudLinux servers by
running a set of collectors (LVE fault counts, live CPU/memory/process
snapshot) and writing a merged JSON report, with optional Prometheus
textfile output for node_exporter.

Python stdlib only. No pip dependencies. Intended to run as root via cron
or a systemd timer.

Everything HostPulse needs lives under its install directory
(`/opt/hostpulse`): the code, the venv, `config/htpasswd`,
`output/users.json`, logs and state. Only the systemd unit files go to
`/etc/systemd/system/`.

## Install

```bash
sudo mkdir -p /opt/hostpulse
sudo cp -r hostpulse/* /opt/hostpulse/
cd /opt/hostpulse

# The env file name is hostpulse.env, not .env
sudo cp hostpulse.env.sample hostpulse.env
sudo nano hostpulse.env
```

Before running on a real server, check/edit in `hostpulse.env`:

- `HOSTPULSE_LVEINFO_COMMAND` — confirm `lveinfo` syntax matches this
  server's CloudLinux version (`lveinfo --help`)
- `HOSTPULSE_PS_COMMAND` — if you change the column order here, you must
  also update the positional parsing in `collectors/live_stats.py`
- All `HOSTPULSE_*_WARNING` / `HOSTPULSE_*_CRITICAL` thresholds — the
  defaults are starting guesses, not values tuned to your servers
- `HOSTPULSE_IGNORED_USERS` — add any server-specific service accounts on
  top of the built-in default list

## Run manually (test before relying on cron/timer)

```bash
cd /opt/hostpulse
python3 hostpulse.py
cat output/users.json
```

Run each collector standalone to sanity-check its output against real
commands on this server before trusting the merged report:

```bash
python3 -m collectors.lve_faults
python3 -m collectors.live_stats
```

## Run automatically

Cron example (every 15 minutes):

```
*/15 * * * * root /usr/bin/python3 /opt/hostpulse/hostpulse.py
```

Or a systemd service + timer pointed at the same command works the same
way — a oneshot service triggered by a timer, no special setup needed
beyond `WorkingDirectory=/opt/hostpulse`.

## Web service (FastAPI, port 35707)

Instead of reading `users.json` off disk, the report can be served over
HTTP with `hostpulse_web.py` — a small FastAPI app protected by HTTP Basic
Auth backed by an htpasswd file (this is the only part of HostPulse with
pip dependencies; the collector itself stays stdlib-only).

One-command install (creates a venv, generates the htpasswd, installs the
systemd units and enables both the web service and the hourly timer):

```bash
sudo bash deploy/install-web.sh
```

The script detects when it is being run from the install directory itself
(e.g. `cd /opt/hostpulse && bash deploy/install-web.sh`, after copying the
repo there) and safely skips the file copy in that case.

Manual setup:

```bash
cd /opt/hostpulse
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# Create the htpasswd file (apr1 hashes, same format as `htpasswd -m`).
# Default location is /opt/hostpulse/config/htpasswd:
.venv/bin/python hostpulse_web.py --add-user admin \
  --htpasswd /opt/hostpulse/config/htpasswd
```

Endpoints — everything except `/health` requires Basic Auth:

| Endpoint         | What it serves                                             |
|------------------|------------------------------------------------------------|
| `GET /`          | The JSON report (same payload as `/users.json`)            |
| `GET /users.json`| The JSON report produced by hostpulse.py                   |
| `GET /metrics`   | Prometheus output (404 if `HOSTPULSE_PROM_OUTPUT` is empty)|
| `GET /health`    | Unauthenticated liveness probe                             |

```bash
curl -u admin http://SERVER:35707/users.json
curl -u admin http://SERVER:35707/metrics
```

The htpasswd file is reloaded automatically on change — adding/removing
users never needs a restart. Web-only settings (OS environment or systemd
drop-in on the service): `HOSTPULSE_WEB_HOST` (default `0.0.0.0`),
`HOSTPULSE_WEB_PORT` (default `35707`), `HOSTPULSE_HTPASSWD_FILE`
(default `/opt/hostpulse/config/htpasswd`). The JSON/Prometheus paths are read
from the same `HOSTPULSE_JSON_OUTPUT` / `HOSTPULSE_PROM_OUTPUT` variables
the collector uses, so the API always matches what the collectors wrote.

### systemd units (web service + hourly timer)

`deploy/` contains three units — enable them directly or via the install
script above:

```bash
sudo cp deploy/hostpulse-web.service deploy/hostpulse-collect.service \
     deploy/hostpulse.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now hostpulse-web.service  # always-on API on :35707
sudo systemctl enable --now hostpulse.timer        # refresh report every hour
systemctl list-timers hostpulse.timer
```

- `hostpulse-web.service` — long-running uvicorn process; the API must be
  reachable at all times, so this unit stays up and restarts on failure.
- `hostpulse-collect.service` + `hostpulse.timer` — the timer fires every
  hour (`OnBootSec=5min`, `OnUnitActiveSec=1h`) and runs the collector as
  root, which rewrites `users.json`; the web service then serves the
  fresh data. Check with `journalctl -u hostpulse-collect.service`.

## Output

Written to `HOSTPULSE_JSON_OUTPUT` (default `/opt/hostpulse/output/users.json`):

```json
{
  "generated_at": "2026-08-16T10:00:00+00:00",
  "server": "lh675.irandns.com",
  "users": [
    {
      "username": "someuser",
      "metrics": {"pmemf": 62, "nprocf": 0, "cpuf": 0, "cpu_percent": 12.3, "rss_mb": 340.1, "nproc": 4},
      "causes": [
        {"source": "lveinfo", "metric": "pmemf", "value": 62, "status": "warning"}
      ],
      "score": 5,
      "status": "warning"
    }
  ],
  "collector_stats": {
    "lveinfo": {"status": "ok", "users_found": 3, "error": null},
    "live_stats": {"status": "ok", "users_found": 40, "error": null}
  }
}
```

Only users that crossed at least one warning/critical threshold are
included — this is a flagged-users report, not a full inventory.

Scoring is per cause (since v2.1.0): every individual metric that crossed
a warning/critical threshold adds its category's weight once, so a user
flagged for two LVE metrics scores twice the `lve` weight.

## Feeding this into Grafana

Set `HOSTPULSE_PROM_OUTPUT` to a path inside node_exporter's textfile
collector directory (e.g.
`/var/lib/node_exporter/textfile_collector/hostpulse.prom`) to get
`hostpulse_user_score`, `hostpulse_user_status`, and per-metric gauges
directly in Prometheus/Grafana, alongside the JSON report for anything
that needs the full detail (causes, per-collector status).

## Design notes (why it's structured this way)

- **Threshold names live in exactly one place**: `THRESHOLD_SPEC` in
  `hostpulse.py`. Both `build_config()` and `evaluate_user()` iterate the
  same table instead of each hardcoding the metric/env-var list separately
  — this is what caused the env/code name mismatch in an earlier version,
  so adding a new metric now means adding one row to the table, not
  editing three places that have to stay in sync by hand.
- **`HOSTPULSE_IGNORED_USERS` in the env file is additive**, not a
  replacement for the built-in `DEFAULT_IGNORED_USERS` list in
  `hostpulse.py`. Only list server-specific extra accounts there.
- **`$HOSTNAME` and other `$VAR` references in `hostpulse.env` are
  expanded** against the real OS environment (not a shell — no command
  execution). If the variable isn't actually set in the environment this
  script runs under (cron/systemd jobs often don't export `HOSTNAME`),
  HostPulse detects the unresolved `$VAR` and falls back to
  `os.uname().nodename` with a logged warning, rather than silently using
  the literal string `"$HOSTNAME"` as the server name.
- **`lve_faults.py` tries `lveinfo --json` first**, falling back to
  text-table parsing automatically if the output isn't valid JSON (older
  lveinfo builds without `--json` support, or any unexpected output).
  Confirmed against real CloudLinux `--json` output: fields map as
  `ID`→username, `PMemF`→pmemf, `NprocF`→nprocf. Note that this lveinfo
  build has **no CPU fault field at all** (no `CPUf` key) — the `cpuf`
  metric stays 0 on servers like this; it's kept for lveinfo builds that
  do report it, not removed, since assuming it'll never exist anywhere
  would be its own kind of unverified guess.

## Known things to verify before trusting this in production

- `collectors/lve_faults.py`: the table parser handles several delimiter
  styles and finds the header by column name rather than position, but
  hasn't been validated against every CloudLinux version's actual output
  — run it standalone (`python3 -m collectors.lve_faults`) on each server
  type before relying on it.
- `collectors/live_stats.py`: parses `ps` output positionally
  (`user, pcpu, pmem, rss, pid`) — if you change `HOSTPULSE_PS_COMMAND`'s
  column order, update the parsing to match.
- Thresholds and scoring weights in `hostpulse.env.sample` are starting
  points, not numbers derived from real server data — tune them after
  watching real output for a few days.
